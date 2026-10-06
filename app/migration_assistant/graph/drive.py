"""Drive-level helpers for the target SharePoint Online site.

Folders/files are `driveItem`s addressed by id. Only the operations the
migration scanner/hierarchy-discovery/mover need are implemented — no upload
support, since this project only ever *reads* and *moves* existing documents.
"""
from urllib.parse import quote

import httpx

from .client import GraphError, graph
from .http import verify


async def get_root_item_id(drive_id: str) -> str:
    data = await graph().get(f"/drives/{drive_id}/root")
    return data["id"]


async def get_drive_list_title(drive_id: str) -> str:
    """The SharePoint list *display title* backing a drive (document
    library) — needed only for SharePoint REST calls (`sharepoint_rest.py`),
    which addresses lists by title/id rather than Graph's drive id."""
    data = await graph().get(f"/drives/{drive_id}/list?$select=displayName")
    return data["displayName"]


async def list_children(drive_id: str, item_id: str) -> list[dict]:
    items, url = [], f"/drives/{drive_id}/items/{item_id}/children?$top=200"
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


async def get_item(drive_id: str, item_id: str) -> dict:
    return await graph().get(f"/drives/{drive_id}/items/{item_id}")


async def move_item(
    drive_id: str, item_id: str, new_parent_id: str, new_name: str | None = None
) -> dict:
    """Move (and optionally rename) a driveItem in place — same item id, new parent."""
    body: dict = {"parentReference": {"id": new_parent_id}}
    if new_name:
        body["name"] = new_name
    return await graph().patch(f"/drives/{drive_id}/items/{item_id}", json=body)


async def batch_move_items(drive_id: str, moves: list[dict]) -> list[dict]:
    """Move many items using as few HTTP round-trips as possible, via Graph's
    `$batch` endpoint (chunked to its 20-requests-per-call limit).

    Each entry in `moves` is {"item_id", "new_parent_id", "new_name"?}.
    Returns one result per move, in the same order:
    {"item_id", "ok": bool, "status": int, "error": str | None} — a failed
    move never raises, so the caller can continue processing the rest of the
    batch and report per-item errors."""
    results: list[dict] = []
    for start in range(0, len(moves), 20):
        chunk = moves[start : start + 20]
        requests = []
        for i, m in enumerate(chunk):
            body: dict = {"parentReference": {"id": m["new_parent_id"]}}
            if m.get("new_name"):
                body["name"] = m["new_name"]
            requests.append(
                {
                    "id": str(i),
                    "method": "PATCH",
                    "url": f"/drives/{drive_id}/items/{m['item_id']}",
                    "body": body,
                    "headers": {"Content-Type": "application/json"},
                }
            )
        responses = await graph().batch(requests)
        by_id = {r.get("id"): r for r in responses}
        for i, m in enumerate(chunk):
            r = by_id.get(str(i))
            if r is None:
                results.append(
                    {"item_id": m["item_id"], "ok": False, "status": 0, "error": "No response from Graph batch"}
                )
                continue
            status = r.get("status", 0)
            if 200 <= status < 300:
                results.append({"item_id": m["item_id"], "ok": True, "status": status, "error": None})
            else:
                body = r.get("body") or {}
                message = (body.get("error") or {}).get("message") or str(body)
                results.append({"item_id": m["item_id"], "ok": False, "status": status, "error": message})
    return results


async def ensure_folder(drive_id: str, parent_id: str, name: str) -> dict:
    """Return the child folder named `name` under `parent_id`, creating it if
    absent. Create-first: one API call when the folder is new; only falls
    back to a lookup if it already exists (409). Used only for the app's own
    "To Be Classified" safety-net folder — never for AI-picked categories."""
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
            existing = await find_child(drive_id, parent_id, name)
            if existing and "folder" in existing:
                return existing
        raise


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
    from ..config import settings

    token = graph()._token()
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(
            f"{settings.graph_base_url}/drives/{drive_id}/items/{item_id}/content",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.content, ctype, name


async def get_preview_url(drive_id: str, item_id: str) -> str:
    """Short-lived web URL to view the document."""
    data = await graph().post(f"/drives/{drive_id}/items/{item_id}/preview", json={})
    return data.get("getUrl") or data.get("postUrl", "")


async def copy_item(
    drive_id: str,
    item_id: str,
    dest_drive_id: str,
    dest_parent_id: str,
    new_name: str,
    *,
    conflict_behavior: str = "fail",
    include_versions: bool = False,
) -> str:
    """Start a server-side copy of a file/folder into a *different* drive
    (used for Site-to-Site migration — same-site moves use `move_item`
    instead, since those never need to leave the source untouched). Graph's
    copy action is always asynchronous: this returns the monitor URL (from
    the `Location` response header) to poll with `poll_copy_status`, not the
    copied item itself.

    `conflict_behavior` is Graph's own name-collision handling: "fail",
    "replace" (overwrite the existing file) or "rename" (keep both — Graph
    appends " 1", " 2", ... to the new copy). `include_versions` asks Graph
    to bring the source file's whole version history along
    (`includeAllVersionHistory`); without it only the current version is
    copied."""
    body: dict = {
        "parentReference": {"driveId": dest_drive_id, "id": dest_parent_id},
        "name": new_name,
    }
    if include_versions:
        body["includeAllVersionHistory"] = True
    resp = await graph().request(
        "POST",
        f"/drives/{drive_id}/items/{item_id}/copy",
        json=body,
        params={"@microsoft.graph.conflictBehavior": conflict_behavior},
    )
    monitor_url = resp.headers.get("Location")
    if not monitor_url:
        raise GraphError(resp.status_code, "Graph did not return a copy monitor URL")
    return monitor_url


async def poll_copy_status(monitor_url: str, *, timeout_seconds: float = 120) -> dict:
    """Poll a copy operation's monitor URL until it completes. Returns the
    monitor endpoint's final JSON body (`status` "completed" with a
    `resourceId`, or "failed"/other with an error) — raises only if the
    operation doesn't finish within `timeout_seconds`, never for the copy
    itself failing (the caller inspects `status`). Polls quickly at first
    (most single-file copies finish in a second or two) then backs off."""
    import asyncio
    import time

    start = time.monotonic()
    delay = 0.5
    while True:
        resp = await graph().request("GET", monitor_url)
        data = resp.json()
        status = data.get("status")
        if status in ("completed", "failed"):
            return data
        if time.monotonic() - start > timeout_seconds:
            return {"status": "failed", "error": f"Copy did not complete within {int(timeout_seconds)}s"}
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 5)


async def batch_get_items(drive_id: str, item_ids: list[str], select: str = "id,name,size,file,folder") -> dict[str, dict | None]:
    """Fetch many driveItems in as few round-trips as possible via `$batch`.
    Returns {item_id: item | None} — None means Graph answered 404 for it
    (the item doesn't exist). Any other per-item failure is returned as
    {"_error": status, "_message": ...} so the caller can tell "missing"
    apart from "couldn't check"."""
    out: dict[str, dict | None] = {}
    for start in range(0, len(item_ids), 20):
        chunk = item_ids[start : start + 20]
        requests = [
            {"id": str(i), "method": "GET", "url": f"/drives/{drive_id}/items/{iid}?$select={select}"}
            for i, iid in enumerate(chunk)
        ]
        responses = await graph().batch(requests)
        by_id = {r.get("id"): r for r in responses}
        for i, iid in enumerate(chunk):
            r = by_id.get(str(i)) or {}
            status = r.get("status", 0)
            if 200 <= status < 300:
                out[iid] = r.get("body") or {}
            elif status == 404:
                out[iid] = None
            else:
                body = r.get("body") or {}
                out[iid] = {"_error": status, "_message": (body.get("error") or {}).get("message") or str(body)}
    return out


async def list_permissions(drive_id: str, item_id: str) -> list[dict]:
    data = await graph().get(f"/drives/{drive_id}/items/{item_id}/permissions")
    return data.get("value", [])


async def invite(drive_id: str, item_id: str, emails: list[str], roles: list[str]) -> dict:
    """Grant `roles` ("read"/"write") on an item to the given users/groups
    without emailing them (`sendInvitation: false`) — this is a silent
    permission copy, not a share notification."""
    return await graph().post(
        f"/drives/{drive_id}/items/{item_id}/invite",
        json={
            "recipients": [{"email": e} for e in emails],
            "roles": roles,
            "requireSignIn": True,
            "sendInvitation": False,
        },
    )
