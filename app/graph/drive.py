"""Drive-level helpers for a SharePoint Embedded container.

A container exposes a `drive`; folders and files are `driveItem`s addressed by id.
All folder creation is idempotent (`ensure_folder`) so provisioning and the
month-folder scheduler can run repeatedly without creating duplicates.
"""
import asyncio
from urllib.parse import quote

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


async def list_children(drive_id: str, item_id: str) -> list[dict]:
    items, url = [], (
        f"/drives/{drive_id}/items/{item_id}/children"
        "?$top=200&$select=id,name,folder,file,size,lastModifiedDateTime,parentReference"
    )
    while url:
        data = await graph().get(url)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


async def find_child(drive_id: str, parent_id: str, name: str) -> dict | None:
    for child in await list_children(drive_id, parent_id):
        if child.get("name", "").lower() == name.lower():
            return child
    return None


_BATCH_SIZE = 20  # Graph JSON $batch limit per request


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
                import logging as _log
                _log.getLogger(__name__).warning(
                    "batch_create_folders: parent item not found (stale DB cache?) "
                    "pid=%s name=%s — skipping", pid, name
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
                            if e.status == 429 and attempt < 5:
                                await asyncio.sleep(
                                    min(2 ** (attempt + 1), 60) + __import__('random').random()
                                )
                            else:
                                raise

            resolved = await asyncio.gather(
                *(_resolve_one(pid, name) for pid, name in conflict_items)
            )
            for (pid, name), item in resolved:
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
    drive_id: str, parent_id: str, name: str, content: bytes, content_type: str = ""
) -> dict:
    if len(content) <= SIMPLE_UPLOAD_LIMIT:
        return await _upload_small(drive_id, parent_id, name, content, content_type)
    return await _upload_large(drive_id, parent_id, name, content)


async def _upload_small(drive_id, parent_id, name, content, content_type) -> dict:
    path = f"/drives/{drive_id}/items/{parent_id}:/{quote(name)}:/content"
    headers = {"Content-Type": content_type or "application/octet-stream"}
    return (await graph().request("PUT", path, content=content, headers=headers)).json()


async def _upload_large(drive_id, parent_id, name, content) -> dict:
    path = f"/drives/{drive_id}/items/{parent_id}:/{quote(name)}:/createUploadSession"
    session = await graph().post(
        path, json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}
    )
    upload_url = session["uploadUrl"]
    size = len(content)
    result: dict = {}
    # uploadUrl is pre-authenticated — must NOT carry the bearer header.
    async with httpx.AsyncClient(timeout=120, verify=verify()) as client:
        for start in range(0, size, CHUNK):
            end = min(start + CHUNK, size)
            chunk = content[start:end]
            resp = await client.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end - 1}/{size}",
                },
            )
            if resp.status_code >= 400:
                raise GraphError(resp.status_code, resp.text)
            if resp.content:
                result = resp.json()
    return result


async def get_item(drive_id: str, item_id: str, select: str | None = None) -> dict:
    url = f"/drives/{drive_id}/items/{item_id}"
    if select:
        url += f"?$select={select}"
    return await graph().get(url)


async def move_item(
    drive_id: str, item_id: str, new_parent_id: str, new_name: str | None = None
) -> dict:
    """Move (and optionally rename) a driveItem in place — same item id, new parent.

    Used by the approval workflow to relocate a staged upload to its final
    destination (or to a fallback folder — "To be Classified", "Other
    Drawings", or "Other Manuals") without re-uploading bytes.
    """
    body: dict = {"parentReference": {"id": new_parent_id}}
    if new_name:
        body["name"] = new_name
    return await graph().patch(f"/drives/{drive_id}/items/{item_id}", json=body)


async def download_file(drive_id: str, item_id: str) -> tuple[bytes, str, str]:
    """Return (content, content_type, name) for a file driveItem.

    Fetch full item metadata (no $select — otherwise the pre-authed
    @microsoft.graph.downloadUrl is omitted) and download the bytes from it.
    Falls back to the /content endpoint *following redirects*."""
    meta = await graph().get(f"/drives/{drive_id}/items/{item_id}")
    name = meta.get("name", "download")
    ctype = (meta.get("file") or {}).get("mimeType", "application/octet-stream")
    url = meta.get("@microsoft.graph.downloadUrl")
    if url:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, verify=verify()) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content, ctype, name
    # Fallback: authenticated content endpoint (302 -> storage host; httpx
    # strips the auth header on the cross-host redirect, which is correct).
    from .client import graph as _graph

    token = _graph()._token()
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(
            f"{settings.graph_base_url}/drives/{drive_id}/items/{item_id}/content",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.content, ctype, name


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
