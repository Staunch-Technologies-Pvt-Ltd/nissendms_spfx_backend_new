"""Shared helpers: resolve the source SharePoint Online site's drive id, and
resolve/browse folders under it by path — used to let a reviewer browse the
Documents library and pick a source folder + subfolders.

The classify pipeline's *destination* (vessel folders, category hierarchies,
and where Confirm Move actually writes) is a different site —
`get_destination_drive_id` below resolves that one, from the same
`ALLOWED_SITES` config Site-to-Site migration uses.
"""
from ..config import settings
from ..graph import drive as gd
from ..graph.site import get_site_drive_id

from . import site_to_site_common
from .errors import BadRequest, NotFound


async def get_migration_drive_id() -> str:
    if not settings.graph_configured:
        raise BadRequest(
            "Not configured — set AZURE_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, "
            "SITE_HOSTNAME and SITE_PATH (see README.md)."
        )
    return await get_site_drive_id(settings.site_hostname, settings.site_path)


def get_destination_site() -> dict:
    """The classify pipeline's destination site config ({"key", "label",
    "hostname", "site_path"}) — `settings.destination_site_key` looked up in
    `ALLOWED_SITES`. Raises BadRequest if that key isn't configured there."""
    if not settings.destination_site_key:
        raise BadRequest("DESTINATION_SITE_KEY is not configured")
    return site_to_site_common.find_site(settings.destination_site_key)


async def get_destination_drive_id() -> str:
    if not settings.graph_configured:
        raise BadRequest(
            "Not configured — set AZURE_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, "
            "SITE_HOSTNAME and SITE_PATH (see README.md)."
        )
    site = get_destination_site()
    return await get_site_drive_id(site["hostname"], site["site_path"])


async def resolve_folder_path(drive_id: str, path: str) -> dict:
    """Walk a "/"-separated path (relative to the drive root) segment by
    segment and return the final folder's driveItem. Raises NotFound if any
    segment is missing or isn't a folder. An empty path returns the root."""
    item_id = await gd.get_root_item_id(drive_id)
    item: dict = {"id": item_id, "name": "", "folder": {}}
    for segment in [p for p in path.split("/") if p]:
        child = await gd.find_child(drive_id, item["id"], segment)
        if child is None or "folder" not in child:
            raise NotFound(f"Folder '{path}' was not found (missing segment '{segment}')")
        item = child
    return item


async def list_child_folders(drive_id: str, path: str | None) -> dict:
    """List the immediate children under `path` (relative to the drive root),
    or the Documents library's top level if `path` is empty/None — for the
    source-folder/subfolder browser. Returns both folders (navigable/
    selectable) and files (shown read-only, for visibility into exactly
    what's there before anything is scanned): {"folders": [...], "files": [...]}."""
    base_path = (path or "").strip("/")
    parent = await resolve_folder_path(drive_id, base_path)
    children = await gd.list_children(drive_id, parent["id"])
    folders = [
        {"id": c["id"], "name": c["name"], "path": f"{base_path}/{c['name']}" if base_path else c["name"]}
        for c in children
        if "folder" in c
    ]
    files = [
        {
            "id": c["id"],
            "name": c["name"],
            "size": c.get("size") or 0,
            "content_type": (c.get("file") or {}).get("mimeType") or "application/octet-stream",
        }
        for c in children
        if "file" in c
    ]
    return {"folders": folders, "files": files}


async def list_vessel_folders(drive_id: str) -> list[dict]:
    """List the vessel folders under `settings.destination_root` (e.g.
    "Technical and Crewing") — powers the destination vessel picker."""
    root = await resolve_folder_path(drive_id, settings.destination_root)
    children = await gd.list_children(drive_id, root["id"])
    return [
        {"id": c["id"], "name": c["name"], "path": f"{settings.destination_root}/{c['name']}"}
        for c in children
        if "folder" in c
    ]
