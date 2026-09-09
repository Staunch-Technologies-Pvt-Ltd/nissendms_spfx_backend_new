"""Drive-level helpers for a SharePoint Embedded container.

A container exposes a `drive`; folders and files are `driveItem`s addressed by id.
All folder creation is idempotent (`ensure_folder`) so provisioning and the
month-folder scheduler can run repeatedly without creating duplicates.
"""
import asyncio
import logging
from urllib.parse import quote

_logger = logging.getLogger(__name__)

import httpx

from ..config import settings
from .client import GraphError, graph
from .http import verify

# Graph: files <= 4 MiB can use a simple PUT; larger needs an upload session.
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
CHUNK = 10 * 320 * 1024  # 3.2 MiB, multiple of 320 KiB as Graph requires


async def get_container_drive_id(container_id: str) -> str:
    data = await graph().get(f"/storage/fileStorage/containers/{container_id}/drive")
    return data["id"]


async def get_root_item_id(drive_id: str) -> str:
    data = await graph().get(f"/drives/{drive_id}/root")
    return data["id"]


async def get_item(
    drive_id: str,
    item_id: str,
    access_token: str | None = None,
) -> dict:
    """Fetch a single driveItem by id with the standard field selection."""
    return await graph().get(
        f"/drives/{drive_id}/items/{item_id}"
        "?$select=id,name,folder,file,size,lastModifiedDateTime,parentReference,webUrl",
        access_token=access_token,
    )


async def list_children(drive_id: str, item_id: str, access_token: str | None = None) -> list[dict]:
    items, url = [], (
        f"/drives/{drive_id}/items/{item_id}/children"
        "?$top=200&$select=id,name,folder,file,size,lastModifiedDateTime,parentReference,webUrl"
    )
    while url:
        data = await graph().get(url, access_token=access_token)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


async def find_child(drive_id: str, parent_id: str, name: str) -> dict | None:
    for child in await list_children(drive_id, parent_id):
        if child.get("name", "").lower() == name.lower():
            return child
    return None


_BATCH_SIZE = 20  # Graph JSON $batch limit per request
log = logging.getLogger(__name__)


async def batch_create_folders(
    drive_id: str,
    items: list[tuple[str, str]],  # [(parent_id, folder_name), ...]
) -> dict[tuple[str, str], dict]:
    """Create many folders in parallel using Graph JSON $batch.

    Sends up to _BATCH_SIZE create-folder requests per HTTP call instead of
    one HTTP round-trip per folder.  Handles 409 (already exists) by falling
    back to individual fetches for those items.

    Returns {(parent_id, folder_name): driveItem_dict}.
    """
    result: dict[tuple[str, str], dict] = {}
    if not items:
        return result

    def _is_invalid_parent_for_drive(err: GraphError) -> bool:
        msg = str(err).lower()
        return (
            err.status == 400
            and "invalidrequest" in msg
            and "not valid for the requested drive" in msg
        )

    for offset in range(0, len(items), _BATCH_SIZE):
        chunk = items[offset : offset + _BATCH_SIZE]

        batch_requests = [
            {
                "id": str(i),
                "method": "POST",
                "url": f"/drives/{drive_id}/items/{pid}/children",
                "headers": {"Content-Type": "application/json"},
                "body": {
                    "name": name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            }
            for i, (pid, name) in enumerate(chunk)
        ]

        resp = await graph().post("/$batch", json={"requests": batch_requests})
        by_id = {r["id"]: r for r in resp.get("responses", [])}

        conflict_items: list[tuple[str, str]] = []
        throttled_items: list[tuple[str, str]] = []
        for i, (pid, name) in enumerate(chunk):
            r = by_id.get(str(i), {})
            status = r.get("status", 0)
            body = r.get("body", {})
            if status in (200, 201):
                result[(pid, name)] = body
            elif status == 409:
                conflict_items.append((pid, name))
            elif status == 429:
                throttled_items.append((pid, name))
            elif status == 404:
                log.warning(
                    "batch_create_folders: parent item not found (stale DB cache?) "
                    "pid=%s name=%s — skipping", pid, name
                )
            elif (
                status == 400
                and isinstance(body, dict)
                and (body.get("error") or {}).get("code") == "invalidRequest"
                and "not valid for the requested drive"
                in ((body.get("error") or {}).get("message") or "").lower()
            ):
                # Stale DB cache can hold driveItem IDs from a previous drive/container.
                # Skip this parent for now so one bad row does not fail the whole batch.
                log.warning(
                    "batch_create_folders: parent item belongs to a different drive "
                    "(stale cache) pid=%s name=%s — skipping",
                    pid,
                    name,
                )
            else:
                raise GraphError(status, str(body))

        # Retry throttled items individually with back-off
        if throttled_items:
            for pid, name in throttled_items:
                for attempt in range(6):
                    try:
                        item = await ensure_folder(drive_id, pid, name)
                        result[(pid, name)] = item
                        break
                    except GraphError as e:
                        if _is_invalid_parent_for_drive(e) or e.status == 404:
                            log.warning(
                                "batch_create_folders: stale parent during retry "
                                "pid=%s name=%s (%s) — skipping",
                                pid,
                                name,
                                e,
                            )
                            break
                        if e.status == 429 and attempt < 5:
                            await asyncio.sleep(min(2 ** (attempt + 1), 60) + __import__('random').random())
                        else:
                            raise

       # Resolve already-existing folders individually. Bounded concurrency
        # (not asyncio.gather-all-at-once) — a burst of many simultaneous
        # lookups here is exactly what tripped Graph's activityLimitReached
        # 429 when most/all items in a chunk were conflicts (e.g. re-running
        # precreate for a month that's already mostly provisioned).
        if conflict_items:
            sem = asyncio.Semaphore(3)

            async def _resolve_one(pid, name):
                async with sem:
                    for attempt in range(6):
                        try:
                            return (pid, name), await ensure_folder(drive_id, pid, name)
                        except GraphError as e:
                            if _is_invalid_parent_for_drive(e) or e.status == 404:
                                log.warning(
                                    "batch_create_folders: stale parent while resolving conflict "
                                    "pid=%s name=%s (%s) — skipping",
                                    pid,
                                    name,
                                    e,
                                )
                                return None
                            if e.status == 429 and attempt < 5:
                                await asyncio.sleep(
                                    min(2 ** (attempt + 1), 60) + __import__('random').random()
                                )
                            else:
                                raise

            resolved = await asyncio.gather(
                *(_resolve_one(pid, name) for pid, name in conflict_items)
            )
            for entry in resolved:
                if not entry:
                    continue
                (pid, name), item = entry
                result[(pid, name)] = item

    return result


async def ensure_folder(drive_id: str, parent_id: str, name: str) -> dict:
    """Return the child folder named `name` under `parent_id`, creating it if absent.

    Create-first: one API call when the folder is new (the common case during
    provisioning); only falls back to a direct item lookup if it already exists (409)."""
    try:
        return await graph().post(
            f"/drives/{drive_id}/items/{parent_id}/children",
            json={
                "name": name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            },
        )
    except GraphError as e:
        if e.status == 409:
            # Folder already exists — fetch it directly by path instead of
            # listing all children (much faster for large directories).
            encoded = quote(name, safe="")
            try:
                return await graph().get(
                    f"/drives/{drive_id}/items/{parent_id}:/{encoded}"
                )
            except GraphError:
                pass
            # Last resort: scan children
            existing = await find_child(drive_id, parent_id, name)
            if existing and "folder" in existing:
                return existing
        raise


async def upload_file(
    drive_id: str, parent_id: str, name: str, content: bytes, content_type: str = "", access_token: str | None = None
) -> dict:
    if len(content) <= SIMPLE_UPLOAD_LIMIT:
        return await _upload_small(drive_id, parent_id, name, content, content_type, access_token=access_token)
    return await _upload_large(drive_id, parent_id, name, content, access_token=access_token)


async def _upload_small(drive_id, parent_id, name, content, content_type, access_token=None) -> dict:
    path = f"/drives/{drive_id}/items/{parent_id}:/{quote(name)}:/content"
    headers = {"Content-Type": content_type or "application/octet-stream"}
    return (await graph().request("PUT", path, content=content, headers=headers, access_token=access_token)).json()


async def _upload_large(drive_id, parent_id, name, content, access_token=None) -> dict:
    path = f"/drives/{drive_id}/items/{parent_id}:/{quote(name)}:/createUploadSession"
    session = await graph().post(
        path, json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}, access_token=access_token
    )
    upload_url = session["uploadUrl"]
    size = len(content)
    result: dict = {}
    _NETWORK_ERRORS = (
        httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError,
        httpx.WriteError, httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout,
    )
    # uploadUrl is pre-authenticated — must NOT carry the bearer header.
    async with httpx.AsyncClient(timeout=180, verify=verify()) as client:
        for start in range(0, size, CHUNK):
            end = min(start + CHUNK, size)
            chunk = content[start:end]
            for attempt in range(5):
                try:
                    resp = await client.put(
                        upload_url,
                        content=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end - 1}/{size}",
                        },
                    )
                    if resp.status_code in (429, 503) and attempt < 4:
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            delay = min(float(retry_after), 30) if retry_after else min(2 ** attempt, 16)
                        except (TypeError, ValueError):
                            delay = min(2 ** attempt, 16)
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code >= 400:
                        raise GraphError(resp.status_code, resp.text)
                    if resp.content:
                        result = resp.json()
                    break
                except _NETWORK_ERRORS:
                    if attempt >= 4:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 16))
    return result


async def get_item(
    drive_id: str,
    item_id: str,
    select: str | None = None,
    access_token: str | None = None,
) -> dict:
    url = f"/drives/{drive_id}/items/{item_id}"
    if select:
        url += f"?$select={select}"
    return await graph().get(url, access_token=access_token)


async def get_item_by_path(
    drive_id: str,
    path: str,
    select: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Resolve a driveItem by its root-relative path.

    Example path: "Technical & Crewing/MV Horizon".
    """
    encoded_path = quote(path.strip("/"), safe="/")
    url = f"/drives/{drive_id}/root:/{encoded_path}"
    if select:
        url += f"?$select={select}"
    return await graph().get(url, access_token=access_token)


async def move_item(
    drive_id: str,
    item_id: str,
    new_parent_id: str,
    new_name: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Move (and optionally rename) a driveItem in place — same item id, new parent.

    Used by the approval workflow to relocate a staged upload to its final
    destination (or to a fallback folder — "To be Classified", "Other
    Drawings", or "Other Manuals") without re-uploading bytes.
    """
    body: dict = {"parentReference": {"id": new_parent_id}}
    if new_name:
        body["name"] = new_name
    return await graph().patch(
        f"/drives/{drive_id}/items/{item_id}",
        json=body,
        access_token=access_token,
    )


async def download_file(drive_id: str, item_id: str, access_token: str | None = None) -> tuple[bytes, str, str]:
    """Return (content, content_type, name) for a file driveItem.

    Fetch full item metadata (no $select — otherwise the pre-authed
    @microsoft.graph.downloadUrl is omitted) and download the bytes from it.
    Falls back to the /content endpoint *following redirects*."""
    _NETWORK_ERRORS = (
        httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError,
        httpx.WriteError, httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout,
    )
    meta = await graph().get(f"/drives/{drive_id}/items/{item_id}", access_token=access_token)
    name = meta.get("name", "download")
    ctype = (meta.get("file") or {}).get("mimeType", "application/octet-stream")
    url = meta.get("@microsoft.graph.downloadUrl")
    if url:
        async with httpx.AsyncClient(timeout=180, follow_redirects=True, verify=verify()) as client:
            for attempt in range(4):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.content, ctype, name
                except _NETWORK_ERRORS:
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 8))
    # Fallback: authenticated content endpoint (302 -> storage host; httpx
    # strips the auth header on the cross-host redirect, which is correct).
    from .client import graph as _graph

    token = _graph()._token()
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        for attempt in range(4):
            try:
                resp = await client.get(
                    f"{settings.graph_base_url}/drives/{drive_id}/items/{item_id}/content",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                return resp.content, ctype, name
            except _NETWORK_ERRORS:
                if attempt >= 3:
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))


async def search_items(drive_id: str, query: str) -> list[dict]:
    q = query.replace("'", "''")
    data = await graph().get(
        f"/drives/{drive_id}/root/search(q='{q}')"
        "?$select=id,name,file,folder,parentReference&$top=50"
    )
    return data.get("value", [])


async def search_items_in(drive_id: str, folder_item_id: str, query: str) -> list[dict]:
    """Same as search_items, but scoped to one folder's subtree (recursive).

    Used to restrict search to a single vessel's ship folder instead of the
    whole container — Graph does the recursive scoping server-side, so this
    is cheaper than fetching a container-wide search and filtering locally.
    """
    q = query.replace("'", "''")
    data = await graph().get(
        f"/drives/{drive_id}/items/{folder_item_id}/search(q='{q}')"
        "?$select=id,name,file,folder,parentReference&$top=50"
    )
    return data.get("value", [])


async def delete_item(drive_id: str, item_id: str) -> None:
    await graph().delete(f"/drives/{drive_id}/items/{item_id}")


async def get_preview_url(drive_id: str, item_id: str) -> str:
    """Short-lived web URL to view the document."""
    data = await graph().post(f"/drives/{drive_id}/items/{item_id}/preview", json={})
    return data.get("getUrl") or data.get("postUrl", "")


_TERM_STORE_CACHE: dict[str, dict] = {}
_TERM_STORE_LOCK = asyncio.Lock()


def _decode_sp_internal_name(name: str) -> str:
    """Decode SharePoint internal name hex encoding, e.g. _x0020_ -> space, _x002d_ -> hyphen."""
    import re
    return re.sub(r"_x([0-9a-fA-F]{4})_", lambda m: chr(int(m.group(1), 16)), name or "")


async def _get_term_store_info(site_id: str, access_token: str | None = None) -> dict:
    """Load and cache term store sets and terms for a site."""
    async with _TERM_STORE_LOCK:
        if site_id in _TERM_STORE_CACHE:
            return _TERM_STORE_CACHE[site_id]

        info = {
            "vessel_set_id": None,
            "dms_set_id": None,
            "terms": {},  # norm_name -> (official_label, guid)
            "vessel_terms": [],  # list of (label, guid) for vessel set only
        }
        try:
            groups = await graph().get(f"/sites/{site_id}/termStore/groups", access_token=access_token)
            for g in groups.get("value", []):
                gid = g.get("id")
                gname = (g.get("displayName") or "").lower()
                try:
                    sets = await graph().get(f"/sites/{site_id}/termStore/groups/{gid}/sets", access_token=access_token)
                    for s in sets.get("value", []):
                        sid = s.get("id")
                        sname = (s.get("displayName") or "").lower()
                        is_vessel_set = (
                            "vessel name" in gname or "vessel name" in sname
                            or sid == "d8dd5606-f5b5-4d9c-ac36-505f717099c5"
                        )
                        if is_vessel_set:
                            info["vessel_set_id"] = sid
                        elif "vessel dms" in gname or "dms" in sname or sid == "552ae441-6494-4c7e-97c7-825933bb1a80":
                            info["dms_set_id"] = sid

                        # Load terms in set
                        try:
                            terms = await graph().get(f"/sites/{site_id}/termStore/groups/{gid}/sets/{sid}/terms", access_token=access_token)
                            for t in terms.get("value", []):
                                tid = t.get("id")
                                for l in t.get("labels", []):
                                    lname = l.get("name")
                                    if lname:
                                        import re
                                        norm_l = re.sub(r"[^a-z0-9]", "", lname.lower())
                                        info["terms"][norm_l] = (lname, tid)
                                        if is_vessel_set:
                                            info["vessel_terms"].append((lname, tid))
                        except Exception as e:
                            logging.getLogger(__name__).debug("Failed to fetch terms for set %s: %s", sid, e)
                except Exception as e:
                    logging.getLogger(__name__).debug("Failed to fetch sets for group %s: %s", gid, e)
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to inspect termStore for site %s: %s", site_id, e)

        _TERM_STORE_CACHE[site_id] = info
        return info


async def get_vessel_terms(site_id: str) -> list[str]:
    """Return sorted list of vessel name labels from the Term Store for the given site."""
    try:
        info = await _get_term_store_info(site_id)
        labels = sorted({label for label, _guid in info.get("vessel_terms", [])}, key=str.casefold)
        return labels
    except Exception:
        return []


async def _resolve_term_guid(
    site_id: str,
    semantic_key: str,
    raw_value: str,
    access_token: str | None = None,
) -> tuple[str, str] | None:
    """Resolve a term's official label and GUID from the Term Store.
    If it's a vessel and not present in the term store, dynamically create it."""
    import re
    if not raw_value or not raw_value.strip():
        return None
    val = raw_value.strip()
    norm_val = re.sub(r"[^a-z0-9]", "", val.lower())

    info = await _get_term_store_info(site_id, access_token=access_token)
    terms = info["terms"]

    # 1. Exact or normalized match in term store
    if norm_val in terms:
        return terms[norm_val]

    # 2. Common aliases / pluralization
    aliases = []
    if norm_val in ("drawing", "drawings"):
        aliases.extend(["drawings", "drawing"])
    elif norm_val in ("manual", "manuals"):
        aliases.extend(["manuals", "manual"])
    elif norm_val in ("electrical", "electric", "electricpart"):
        aliases.extend(["electrical", "electricaldrawings"])

    for a in aliases:
        if a in terms:
            return terms[a]

    # 3. If vessel and not found in term store, create it dynamically!
    if semantic_key == "vessel" and info.get("vessel_set_id"):
        vessel_set_id = info["vessel_set_id"]
        try:
            res = await graph().post(
                f"/sites/{site_id}/termStore/sets/{vessel_set_id}/children",
                json={"labels": [{"name": val, "languageTag": "en-US", "isDefault": True}]},
                access_token=access_token,
            )
            new_id = res.get("id")
            if new_id:
                logging.getLogger(__name__).info("Created term '%s' in vessel set %s -> %s", val, vessel_set_id, new_id)
                terms[norm_val] = (val, new_id)
                return (val, new_id)
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to create vessel term '%s' in set %s: %s", val, vessel_set_id, e)

    # 4. If category or group and not found in term store, try creating in dms_set_id if available
    if semantic_key in ("category", "group") and info.get("dms_set_id"):
        dms_set_id = info["dms_set_id"]
        try:
            res = await graph().post(
                f"/sites/{site_id}/termStore/sets/{dms_set_id}/children",
                json={"labels": [{"name": val, "languageTag": "en-US", "isDefault": True}]},
                access_token=access_token,
            )
            new_id = res.get("id")
            if new_id:
                logging.getLogger(__name__).info("Created term '%s' in DMS set %s -> %s", val, dms_set_id, new_id)
                terms[norm_val] = (val, new_id)
                return (val, new_id)
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to create DMS term '%s' in set %s: %s", val, dms_set_id, e)


async def _update_taxonomy_with_sharepoint_rest(
    drive_id: str,
    item_id: str,
    field_name: str,
    label: str,
    term_guid: str,
    access_token: str | None = None,
    sp_access_token: str | None = None,
) -> dict:
    """Update a managed-metadata field through SharePoint's form validator.

    Graph list-item fields accepts ordinary columns but does not persist this
    tenant's taxonomy field. ValidateUpdateListItem is the supported write API.

    Token priority: delegated sp_access_token first (from frontend), then
    automatic fallback to an app-only MSAL token if the delegated one returns
    401 (e.g. insufficient permissions for taxonomy writes).
    """
    item_meta = await graph().get(
        f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds",
        access_token=access_token,
    )
    sp_ids = item_meta.get("sharepointIds") or {}
    list_id = sp_ids.get("listId")
    list_item_id = sp_ids.get("listItemId")
    site_url = sp_ids.get("siteUrl")
    if not list_id or not list_item_id or not site_url:
        return {"ok": False, "error": "SharePoint list identity is unavailable for taxonomy update"}

    from urllib.parse import urlparse
    hostname = urlparse(site_url).netloc
    if not hostname:
        return {"ok": False, "error": "SharePoint site URL is invalid for taxonomy update"}

    def _acquire_app_token() -> str | None:
        """Acquire an app-only SharePoint token via MSAL client-credentials."""
        try:
            import msal
            result = msal.ConfidentialClientApplication(
                client_id=settings.graph_client_id,
                authority=settings.authority_url,
                client_credential=settings.graph_client_secret,
            ).acquire_token_for_client(scopes=[f"https://{hostname}/.default"])
            tok = result.get("access_token")
            if not tok:
                _logger.warning(
                    "_update_taxonomy_with_sharepoint_rest: app-only token acquisition failed: %s",
                    result.get("error_description", result.get("error")),
                )
            return tok
        except Exception as exc:
            _logger.warning(
                "_update_taxonomy_with_sharepoint_rest: MSAL exception: %s", exc
            )
            return None

    url = f"{site_url.rstrip('/')}/_api/web/lists(guid'{list_id}')/items({list_item_id})/ValidateUpdateListItem()"

    # SharePoint ValidateUpdateListItem requires the Field InternalName (or StaticName).
    # Display names with spaces are never valid column names in SharePoint REST.
    # In this tenant's NKSDocMan library, "Vessel Name" has internal name "Vessel_x0020_Name_x0020_".
    field_candidates: list[str] = []
    if " " in field_name:
        encoded = field_name.replace(" ", "_x0020_")
        field_candidates.append(encoded + "_x0020_")
        field_candidates.append(encoded)
        field_candidates.append(field_name.replace(" ", ""))
    field_candidates.append(field_name)

    # SharePoint taxonomy field value format expects -1;#Label|TermGuid (or Label|TermGuid)
    value_candidates: list[str] = []
    if term_guid and label:
        value_candidates.append(f"-1;#{label}|{term_guid}")
        value_candidates.append(f"{label}|{term_guid}")
    if label:
        value_candidates.append(label)

    # Build ordered token list: delegated first (if provided), then app-only.
    # When no delegated token is present the app-only token is acquired eagerly
    # so the retry loop still runs rather than exiting immediately.
    tokens_to_try: list[tuple[str, str]] = []
    if sp_access_token:
        tokens_to_try.append(("delegated", sp_access_token))
    else:
        eager_app_tok = _acquire_app_token()
        if eager_app_tok:
            tokens_to_try.append(("app-only", eager_app_tok))
        else:
            return {
                "ok": False,
                "status": 401,
                "error": (
                    "No SharePoint token available and app-only token acquisition failed. "
                    "Ensure the app has Sites.FullControl.All (application) permission."
                ),
            }

    last_error: str | None = None

    async with httpx.AsyncClient(timeout=30, verify=verify()) as client:
        tried_app = False
        token_idx = 0
        while token_idx < len(tokens_to_try):
            token_label, spo_token = tokens_to_try[token_idx]
            token_idx += 1

            req_headers = {
                "Authorization": f"Bearer {spo_token}",
                "Accept": "application/json;odata=verbose",
                "Content-Type": "application/json;odata=verbose",
            }

            got_401 = False
            success = False
            for candidate in field_candidates:
                if success or got_401:
                    break
                for val_cand in value_candidates:
                    payload = {
                        "formValues": [{"FieldName": candidate, "FieldValue": val_cand}],
                        "bNewDocumentUpdate": False,
                    }
                    response = await client.post(url, json=payload, headers=req_headers)

                    if response.status_code == 401:
                        _logger.warning(
                            "_update_taxonomy_with_sharepoint_rest: 401 with %s token for item_id=%s; "
                            "will try app-only fallback if available.",
                            token_label, item_id,
                        )
                        got_401 = True
                        break

                    if response.status_code >= 400:
                        last_error = response.text[:1000]
                        _logger.debug(
                            "_update_taxonomy_with_sharepoint_rest: candidate '%s' returned HTTP %s: %s",
                            candidate, response.status_code, last_error,
                        )
                        break  # field name invalid — try next field candidate

                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    val_results = (
                        body.get("d", {}).get("ValidateUpdateListItem", [])
                        if isinstance(body, dict) else []
                    )
                    if isinstance(val_results, dict) and "results" in val_results:
                        results = val_results["results"]
                    elif isinstance(val_results, list):
                        results = val_results
                    else:
                        results = []
                    errors = [r for r in results if isinstance(r, dict) and r.get("HasException")]
                    if not errors and results:
                        _logger.info(
                            "_update_taxonomy_with_sharepoint_rest: updated field '%s' value '%s' "
                            "for item_id=%s (token=%s)",
                            candidate, val_cand, item_id, token_label,
                        )
                        return {"ok": True, "field": candidate}
                    last_error = str(errors)

            if got_401 and not tried_app:
                tried_app = True
                app_tok = _acquire_app_token()
                if app_tok:
                    tokens_to_try.append(("app-only", app_tok))
                else:
                    return {
                        "ok": False,
                        "status": 401,
                        "error": (
                            "SharePoint REST rejected the token (401) and the app-only token "
                            "fallback also failed. Ensure the app registration has "
                            "Sites.FullControl.All (application) permission or the user has "
                            "site-member/owner rights."
                        ),
                    }

    if last_error is None:
        return {
            "ok": False,
            "status": 401,
            "error": (
                "SharePoint REST rejected all available tokens (401). Ensure delegated "
                "SharePoint token or app permissions (Sites.FullControl.All) are granted."
            ),
        }
    return {"ok": False, "status": 400, "error": last_error or "ValidateUpdateListItem failed"}


async def update_file_columns(
    drive_id: str,
    item_id: str,
    fields: dict,
    access_token: str | None = None,
    sp_access_token: str | None = None,
) -> dict:
    """Patch the SharePoint list-item column values for a file in a drive.

    Uses the Graph drives/{drive_id}/items/{item_id}/listItem/fields endpoint
    which works for both SharePoint Embedded containers and classic communication
    site document libraries.

    For Managed Metadata (Taxonomy) columns (such as Vessel Name, Category, Group),
    automatically discovers their companion hidden note field (e.g. 'Vessel Name_0')
    and resolves the term GUID in the Term Store, writing '{label}|{guid}'.

    Excludes SubCategory per tenant requirements.
    """
    import logging as _log
    import re as _re
    _logger = _log.getLogger(__name__)
    if not fields:
        return {}

    # Semantic aliases accepted from callers (SubCategory intentionally excluded).
    candidates_map = {
        "group": ["group", "Group", "dms_group", "dmsgroup", "DMS_Group", "vessel_group", "vesselgroup"],
        "category": ["category", "Category", "dms_category", "dmscategory", "DMS_Category", "document_category", "doc_category"],
        "vessel": ["vesselname", "VesselName", "vessel", "vessel_name", "vessel_x0020_name",
                   "Vessel Name", "ship", "shipname", "ship_name", "Vessel_x0020_Name_x0020_"],
        "department": ["department", "Department", "main_folder", "mainfolder", "MainFolder", "main_x0020_folder", "Main Folder", "dms_department", "DMS_Department", "domain", "Domain"],
    }

    def _norm_key(s: str) -> str:
        s_dec = _decode_sp_internal_name(s or "")
        return _re.sub(r"[^a-z0-9]", "", s_dec.strip().lower())

    # Normalize incoming fields key names to lowercase alphanumeric for matching
    normalized_input = {
        _norm_key(k): v
        for k, v in fields.items()
        if v is not None and str(v).strip()
    }

    # Canonical list internal names confirmed from list column schema.
    canonical_internal = {
        "vessel": "VesselName",
        "group": "Group",
        "category": "Category",
    }

    patch_payload: dict[str, str] = {}
    matched_by: dict[str, str] = {}
    semantic_values: dict[str, str] = {}

    def _input_value_for(candidates: list[str], semantic_key: str) -> str | None:
        norm_key = _norm_key(semantic_key)
        val = normalized_input.get(norm_key) or fields.get(semantic_key)
        if not val:
            candidate_norms = {_norm_key(c) for c in candidates}
            for k, v in fields.items():
                if k.lower() in candidates or _norm_key(k) in candidate_norms:
                    val = v
                    break
        if val is None or not str(val).strip():
            return None
        return str(val).strip()

    for semantic_key, candidates in candidates_map.items():
        resolved = _input_value_for(candidates, semantic_key)
        if resolved:
            semantic_values[semantic_key] = resolved

    # First pass: attempt canonical internal names when values exist (for simple libraries).
    for semantic_key, internal_name in canonical_internal.items():
        val = semantic_values.get(semantic_key)
        if val:
            patch_payload[internal_name] = str(val)
            matched_by[semantic_key] = "canonical"

    # Second pass: resolve by list column schema (display/internal names & taxonomy note fields).
    try:
        item_meta = await graph().get(f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds", access_token=access_token)
        sp_ids = item_meta.get("sharepointIds") or {}
        site_id = sp_ids.get("siteId")
        list_id = sp_ids.get("listId")
        if site_id and list_id:
            cols = await graph().get(
                f"/sites/{site_id}/lists/{list_id}/columns?expand=hidden",
                access_token=access_token,
            )
            values = cols.get("value", []) if isinstance(cols, dict) else []
            note_cols_by_disp_0: dict[str, str] = {}
            by_norm: dict[str, str] = {}

            for c in values:
                if not isinstance(c, dict):
                    continue
                internal = str(c.get("name") or "").strip()
                display = str(c.get("displayName") or "").strip()
                if display.endswith("_0") and internal:
                    base_disp = display[:-2]
                    norm_base = _norm_key(base_disp)
                    if norm_base:
                        note_cols_by_disp_0[norm_base] = internal
                if internal:
                    by_norm.setdefault(_norm_key(internal), internal)
                if display:
                    by_norm.setdefault(_norm_key(display), internal)

            # If real list schema columns were discovered, remove any canonical
            # names that are not actually defined in this library to prevent Graph 400
            if by_norm:
                valid_cols = set(by_norm.values())
                for sem_key, can_col in canonical_internal.items():
                    if matched_by.get(sem_key) == "canonical" and can_col not in valid_cols:
                        patch_payload.pop(can_col, None)
                        matched_by.pop(sem_key, None)

            for semantic_key, candidates in candidates_map.items():
                val = semantic_values.get(semantic_key)
                if not val:
                    continue

                # Check if this semantic field is a Managed Metadata column with companion hidden note field
                note_col_name = None
                for cand in [semantic_key] + candidates:
                    cand_norm = _norm_key(cand)
                    if cand_norm in note_cols_by_disp_0:
                        note_col_name = note_cols_by_disp_0[cand_norm]
                        break

                if note_col_name:
                    term_info = await _resolve_term_guid(site_id, semantic_key, val, access_token=access_token)
                    if term_info:
                        term_label, term_guid = term_info
                        # This tenant's Graph taxonomy note fields reject the
                        # REST-style -1;# prefix and accept Label|TermGuid.
                        patch_payload[note_col_name] = f"{term_label}|{term_guid}"
                        matched_by[semantic_key] = "taxonomy_note_field"
                        _logger.info(
                            "update_file_columns: mapped taxonomy %s=%s -> note column %s (%s|%s)",
                            semantic_key, val, note_col_name, term_label, term_guid,
                        )
                        continue
                    else:
                        # Term GUID resolution failed (term not in Term Store or creation failed).
                        # Best-effort fallback when the term cannot be resolved.
                        patch_payload[note_col_name] = f"{val}|"
                        matched_by[semantic_key] = "taxonomy_note_field"
                        _logger.warning(
                            "update_file_columns: %s term '%s' not resolved in Term Store; "
                            "writing note field %s with plain-text format '%s|'",
                            semantic_key, val, note_col_name, val,
                        )
                        continue

                # Standard column match fallback
                for cand in candidates:
                    resolved = by_norm.get(_norm_key(cand))
                    if resolved:
                        patch_payload[resolved] = str(val)
                        matched_by[semantic_key] = "list_columns"
                        break

            # Remove base taxonomy columns ONLY for semantic keys that were actually
            # resolved via a taxonomy note field. This prevents VesselName from being
            # stripped when Category/Group resolved to _0 note fields but vessel fell
            # back to a plain-text column match.
            _SEM_TO_BASE = {
                "vessel": ["VesselName", "Vessel_x0020_Name", "Vessel_x0020_Name_x0020_"],
                "group": ["Group"],
                "category": ["Category"],
            }
            for _sem_key, _base_cols in _SEM_TO_BASE.items():
                if matched_by.get(_sem_key) == "taxonomy_note_field":
                    for _bc in _base_cols:
                        patch_payload.pop(_bc, None)
            # Always strip SubCategory (excluded per requirements)
            patch_payload.pop("Sub_x002d_Category", None)
            patch_payload.pop("SubCategory", None)
    except Exception as e:
        _logger.debug("update_file_columns: list column inspection unavailable for item_id=%s (%s)", item_id, e)

    # Exclude any subcategory keys from final patch payload per user requirement
    for k in list(patch_payload.keys()):
        if "subcat" in k.lower() or "sub_x002d_cat" in k.lower():
            patch_payload.pop(k, None)

    # Third pass: listItem field discovery (legacy fallback for existing behavior).
    try:
        current_item = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=access_token)
        available_fields = {k.lower(): k for k in current_item.keys() if not k.startswith("@")}
        available_by_norm: dict[str, str] = {}
        for real_col in available_fields.values():
            n = _norm_key(real_col)
            if n and n not in available_by_norm:
                available_by_norm[n] = real_col

        for semantic_key, candidates in candidates_map.items():
            if semantic_key in matched_by:
                continue
            val = semantic_values.get(semantic_key)
            if not val:
                continue
            for cand in candidates:
                real_col = available_fields.get(cand.lower()) or available_by_norm.get(_norm_key(cand))
                if real_col:
                    patch_payload[real_col] = str(val)
                    matched_by[semantic_key] = "list_item_fields"
                    break
    except Exception as e:
        _logger.debug("update_file_columns: listItem field discovery unavailable for item_id=%s (%s)", item_id, e)

    # Some SharePoint document-library responses omit the list schema metadata,
    # but still expose the managed field under its encoded internal name. Use
    # the tenant's stable hidden note column in that case.
    if semantic_values.get("vessel") and "vessel" not in matched_by:
        vessel_field = "Vessel_x0020_Name_x0020_"
        vessel_note_field = "i62be25c1f7249f48f51efaf91f1f739"
        if vessel_field in available_fields.values() if "available_fields" in locals() else False:
            term_info = None
            try:
                item_meta = await graph().get(
                    f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds",
                    access_token=access_token,
                )
                site_id = (item_meta.get("sharepointIds") or {}).get("siteId")
                if site_id:
                    term_info = await _resolve_term_guid(
                        site_id, "vessel", semantic_values["vessel"], access_token=access_token
                    )
            except Exception as exc:
                _logger.warning("Could not resolve vessel term for fallback field mapping: %s", exc)
            if term_info:
                patch_payload[vessel_note_field] = f"{term_info[0]}|{term_info[1]}"
            else:
                patch_payload[vessel_note_field] = f"{semantic_values['vessel']}|"
            patch_payload.pop(vessel_field, None)
            matched_by["vessel"] = "known_taxonomy_note_field"

    # Do not send display names or guessed internal names. Graph rejects them
    # when the custom columns are not provisioned in the document library.
    if not patch_payload:
        _logger.info(
            "update_file_columns: no matching SharePoint columns for item_id=%s; requested=%s",
            item_id, list(fields.keys()),
        )
        return {"ok": False, "attempted": False, "reason": "no_matching_columns"}
    # Final cleanup: only remove base taxonomy columns for semantic keys that were
    # successfully resolved to taxonomy note fields. Columns resolved via plain-text
    # list_columns or list_item_fields fallback must remain in the payload.
    _SEM_TO_BASE_FINAL = {
        "vessel": ["VesselName", "Vessel_x0020_Name", "Vessel_x0020_Name_x0020_"],
        "group": ["Group"],
        "category": ["Category"],
    }
    for _sem_key, _base_cols in _SEM_TO_BASE_FINAL.items():
        if matched_by.get(_sem_key) == "taxonomy_note_field":
            for _bc in _base_cols:
                patch_payload.pop(_bc, None)
    # Always strip SubCategory keys (excluded per requirements)
    for k in list(patch_payload.keys()):
        if "subcat" in k.lower() or "sub_x002d_cat" in k.lower():
            patch_payload.pop(k, None)

    # Managed metadata must be written through SharePoint REST. A Graph PATCH
    # to the hidden note field can return 200 while leaving the visible field
    # empty, so do not treat that hidden-field write as a successful vessel tag.
    if semantic_values.get("vessel"):
        try:
            item_meta = await graph().get(
                f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds",
                access_token=access_token,
            )
            site_id = (item_meta.get("sharepointIds") or {}).get("siteId")
            term_info = await _resolve_term_guid(
                site_id, "vessel", semantic_values["vessel"], access_token=access_token
            ) if site_id else None
            if not term_info:
                return {"ok": False, "attempted": True, "reason": "vessel_term_not_found"}
            rest_result = await _update_taxonomy_with_sharepoint_rest(
                drive_id,
                item_id,
                "Vessel_x0020_Name_x0020_",
                term_info[0],
                term_info[1],
                access_token=access_token,
                sp_access_token=sp_access_token,
            )
            if not rest_result.get("ok"):
                _logger.warning(
                    "SharePoint REST vessel update failed for item_id=%s: %s",
                    item_id,
                    rest_result,
                )
                return {
                    "ok": False,
                    "attempted": True,
                    "status": rest_result.get("status"),
                    "error": rest_result.get("error") or "SharePoint taxonomy update failed",
                }
            patch_payload.pop("VesselName", None)
            patch_payload.pop("Vessel_x0020_Name_x0020_", None)
            patch_payload.pop("i62be25c1f7249f48f51efaf91f1f739", None)
            matched_by["vessel"] = "sharepoint_validate_update"
        except Exception as exc:
            return {"ok": False, "attempted": True, "error": str(exc)}

    final_payload = patch_payload

    try:
        result = await graph().patch(
            f"/drives/{drive_id}/items/{item_id}/listItem/fields",
            json=final_payload,
            access_token=access_token,
        )
        _logger.info(
            "update_file_columns: successfully tagged item_id=%s with fields=%s",
            item_id, list(final_payload.keys()),
        )
        return {
            "ok": True,
            "attempted": True,
            "patched_fields": list(final_payload.keys()),
            "match_mode": matched_by,
            "graph_result": result or {},
        }
    except GraphError as exc:
        # If delegated user token was rejected (e.g. 401/403), retry using app-only credentials token
        if exc.status in (401, 403) and access_token:
            try:
                _logger.info("update_file_columns: retrying with app-only credentials for item_id=%s", item_id)
                result = await graph().patch(
                    f"/drives/{drive_id}/items/{item_id}/listItem/fields",
                    json=final_payload,
                    access_token=None,
                )
                _logger.info(
                    "update_file_columns: successfully tagged item_id=%s via app-credentials with fields=%s",
                    item_id, list(final_payload.keys()),
                )
                return {
                    "ok": True,
                    "attempted": True,
                    "patched_fields": list(final_payload.keys()),
                    "match_mode": matched_by,
                    "graph_result": result or {},
                }
            except Exception as retry_exc:
                _logger.warning("update_file_columns: app-only retry failed for item_id=%s: %s", item_id, retry_exc)
        if exc.status == 400 and len(final_payload) > 1:
            # Managed metadata columns cause Graph 400 on the entire batch.
            # Retry individual fields so standard columns (e.g. Domain) succeed.
            successes = []
            for col_k, col_v in final_payload.items():
                try:
                    await graph().patch(
                        f"/drives/{drive_id}/items/{item_id}/listItem/fields",
                        json={col_k: col_v},
                        access_token=access_token,
                    )
                    successes.append(col_k)
                except Exception:
                    pass
            if successes:
                _logger.info(
                    "update_file_columns: individual patch succeeded for item_id=%s fields=%s",
                    item_id, successes,
                )
                return {
                    "ok": True,
                    "attempted": True,
                    "patched_fields": successes,
                    "match_mode": matched_by,
                }
        if exc.status == 403:
            _logger.warning(
                "update_file_columns: 403 forbidden patching listItem fields for item_id=%s. "
                "Required: Graph application permission Sites.ReadWrite.All with admin consent, "
                "and site/list app grant for this SharePoint site.",
                item_id,
            )
        _logger.warning(
            "update_file_columns: non-fatal warning patching columns for item_id=%s fields=%s: %s",
            item_id, list(final_payload.keys()), exc,
        )
        return {
            "ok": False,
            "attempted": True,
            "patched_fields": list(final_payload.keys()),
            "status": exc.status,
            "error": str(exc),
        }

