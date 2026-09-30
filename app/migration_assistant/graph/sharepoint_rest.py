"""SharePoint REST (`_api/web/...`), used *only* for the two things Microsoft
Graph v1.0 doesn't reliably do for Managed Metadata (taxonomy) columns:

- Reading a taxonomy field's real `TermGuid` (Graph's `listItem/fields`
  facet — see `graph/fields.py` — only ever returns the display label as
  plain text for a taxonomy column, never the term's stable id).
- Writing a taxonomy field value onto a list item (there is no supported way
  to PATCH a taxonomy column through Graph's `fields` endpoint in v1.0; SPFx
  and CSOM tooling both do this through SharePoint REST's
  `ValidateUpdateListItem` instead, which is what this module wraps).

Everything else in this app (file copy/move, folder listing, plain-column
metadata) uses Graph, via `graph/client.py`. This module also handles
non-taxonomy field writes through the same `ValidateUpdateListItem` call,
purely so a single write to a list item only needs one API round-trip
instead of splitting plain columns (Graph) from taxonomy columns (REST).

This needs a *second* app-only token: SharePoint REST's resource/audience is
`https://{tenant}.sharepoint.com`, not `https://graph.microsoft.com` — a
different Entra API permission (`Sites.Selected` under "SharePoint", not
"Microsoft Graph") must be granted and admin-consented for this to work, in
addition to the existing Graph permission. See README "Graph API
configuration" for the Graph side; the SharePoint-REST side is the same
per-site `Sites.Selected` grant, just under the other API resource.
"""
from __future__ import annotations

import msal

from ..config import settings

from .client import GraphError

_app: msal.ConfidentialClientApplication | None = None
_digest_cache: dict[str, tuple[str, float]] = {}  # site_url -> (digest, expires_at monotonic)


def _msal_app() -> msal.ConfidentialClientApplication:
    global _app
    if _app is None:
        _app = msal.ConfidentialClientApplication(
            client_id=settings.graph_client_id,
            authority=settings.authority_url,
            client_credential=settings.graph_client_secret,
            verify=settings.graph_verify_ssl,
        )
    return _app


def _sp_scope() -> str:
    if not settings.sharepoint_tenant_url:
        raise GraphError(0, "SHAREPOINT_TENANT_URL is not configured (needed for Managed Metadata writes)")
    return f"{settings.sharepoint_tenant_url.rstrip('/')}/.default"


def _token() -> str:
    app = _msal_app()
    scope = _sp_scope()
    result = app.acquire_token_silent([scope], account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=[scope])
    if "access_token" not in result:
        raise GraphError(401, result.get("error_description", result.get("error", "SharePoint REST token failure")))
    return result["access_token"]


async def _client():
    from .client import graph

    return graph()._client()  # reuse the same pooled httpx.AsyncClient/TLS setup


async def _get_digest(site_url: str) -> str:
    import time

    cached = _digest_cache.get(site_url)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    client = await _client()
    resp = await client.post(
        f"{site_url.rstrip('/')}/_api/contextinfo",
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json;odata=nometadata"},
    )
    if resp.status_code >= 400:
        raise GraphError(resp.status_code, f"Could not get SharePoint request digest: {resp.text}")
    data = resp.json()
    digest = data["FormDigestValue"]
    timeout = float(data.get("FormDigestTimeoutSeconds", 1800))
    _digest_cache[site_url] = (digest, time.monotonic() + timeout - 60)
    return digest


async def get_taxonomy_field_value(
    site_url: str, list_title: str, item_id: int, field_internal_name: str
) -> dict | None:
    """Read one taxonomy column's real value for one list item:
    {"term_id": <guid>, "label": <text>} — or None if the field is empty.
    `list_title` is the SharePoint list/library display title (not the
    Graph drive id)."""
    client = await _client()
    select = f"{field_internal_name},{field_internal_name}/TermGuid,{field_internal_name}/Label"
    resp = await client.get(
        f"{site_url.rstrip('/')}/_api/web/lists/getbytitle('{list_title}')/items({item_id})",
        params={"$select": select, "$expand": field_internal_name},
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json;odata=nometadata"},
    )
    if resp.status_code >= 400:
        raise GraphError(resp.status_code, f"Could not read taxonomy field: {resp.text}")
    data = resp.json()
    field = data.get(field_internal_name)
    if not field or not field.get("TermGuid"):
        return None
    return {"term_id": field["TermGuid"], "label": field.get("Label", "")}


async def validate_update_list_item(
    site_url: str, list_title: str, item_id: int, field_values: list[dict]
) -> list[dict]:
    """Write one or more field values onto a list item in a single call.

    `field_values` is [{"FieldName": <internal name>, "FieldValue": <str>}]
    — for an ordinary column, `FieldValue` is the plain text/number/date
    string; for a taxonomy column, it must be the SharePoint-specific
    `"<Label>|<TermGUID>"` format (see `services/term_mapping.py`, which
    builds this). Returns SharePoint's per-field result list — a field
    failing to validate does not raise; the caller inspects each result's
    `ErrorMessage`, same "continue past individual failures" convention used
    elsewhere in this app (e.g. `services/migration_mover.py`)."""
    digest = await _get_digest(site_url)
    client = await _client()
    resp = await client.post(
        f"{site_url.rstrip('/')}/_api/web/lists/getbytitle('{list_title}')/items({item_id})/ValidateUpdateListItem",
        json={"formValues": field_values, "bNewDocumentUpdate": False},
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
            "X-RequestDigest": digest,
        },
    )
    if resp.status_code >= 400:
        raise GraphError(resp.status_code, f"ValidateUpdateListItem failed: {resp.text}")
    return resp.json().get("value", [])
