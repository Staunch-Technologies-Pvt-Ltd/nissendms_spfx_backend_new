"""Sites from the DMS's own Site Management, offered in the Site-to-Site
pickers — so a site added there (Sites → Site Management) is available for
migration straight away, with the same name, and nothing has to be repeated
in `.env.migration`.

Uses the DMS's own site discovery (`app.config.Settings.discover_available_sites`:
the `.env` site blocks plus `site_configurations` rows) and honours the same
visibility rules as Site Management: the auto-generated default site, and
rows marked hidden or removed, are skipped. The DMS and the Migration
Assistant use the same Entra app, so any site the DMS can open is reachable
here too.

Keys are "dms:<site_key>". A site's real URL is resolved from Graph (by its
site id, or through its document library) the first time it's needed.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..graph.client import graph

logger = logging.getLogger("migration_assistant")

KEY_PREFIX = "dms:"

# site key -> {"hostname", "site_path", "site_id", "url"} once resolved via Graph
_resolved: dict[str, dict] = {}


def _split_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    segments = [p for p in parsed.path.split("/") if p]
    site_path = "/".join(segments[:2]) if len(segments) >= 2 and segments[0].lower() in ("sites", "teams") else ""
    return (parsed.hostname or "").lower(), site_path


def site_management_sites() -> list[dict]:
    """Visible Site Management sites as picker entries. Never raises — if the
    DMS config or database isn't available this simply returns []."""
    try:
        from ...config import Settings
        from ...db.base import engine

        sites = Settings.discover_available_sites()
        hidden_keys = Settings.hidden_default_site_keys(sites)
        flags: dict[str, tuple[bool, bool]] = {}
        if engine is not None:
            from sqlalchemy import text

            with engine.connect() as conn:
                for key, is_hidden, is_removed in conn.execute(
                    text("SELECT site_key, is_hidden, is_removed FROM site_configurations")
                ):
                    flags[(key or "").lower()] = (bool(is_hidden), bool(is_removed))
    except Exception as exc:  # pragma: no cover - DMS not configured / DB down
        logger.warning("Could not read Site Management sites: %s", exc)
        return []

    out: list[dict] = []
    for key, info in sites.items():
        if not info.get("configured") or key in hidden_keys or any(flags.get(key, (False, False))):
            continue
        cached = _resolved.get(key, {})
        url = cached.get("url") or info.get("web_url") or ""
        hostname, site_path = (cached["hostname"], cached["site_path"]) if cached else _split_url(url)
        out.append({
            "key": f"{KEY_PREFIX}{key}",
            "label": info.get("sp_site_name") or key,
            "hostname": hostname,
            "site_path": site_path,
            "url": url,
            "site_id": cached.get("site_id") or info.get("site_id") or "",
            "drive_id": info.get("drive_id") or "",
            "origin": "site_management",
        })
    return out


async def resolve(site: dict) -> dict:
    """Fill in the authoritative site id, hostname and path from Graph — the
    URL Site Management shows can be computed rather than stored."""
    key = site["key"][len(KEY_PREFIX):]
    if key not in _resolved:
        data = None
        if site.get("site_id"):
            data = await graph().get(f"/sites/{site['site_id']}?$select=id,webUrl")
            site_id, web_url = data["id"], data["webUrl"]
        else:
            root = await graph().get(f"/drives/{site['drive_id']}/root?$select=sharepointIds")
            ids = root.get("sharepointIds") or {}
            web_url = ids.get("siteUrl") or site.get("url", "")
            hostname, site_path = _split_url(web_url)
            data = await graph().get(f"/sites/{hostname}:/{site_path}" if site_path else f"/sites/{hostname}")
            site_id = data["id"]
        hostname, site_path = _split_url(web_url)
        _resolved[key] = {"hostname": hostname, "site_path": site_path, "site_id": site_id, "url": web_url.rstrip("/")}
    return {**site, **_resolved[key]}
