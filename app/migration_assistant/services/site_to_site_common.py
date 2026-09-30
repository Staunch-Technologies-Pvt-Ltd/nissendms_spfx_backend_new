"""Shared helpers for Site-to-Site migration: resolving a configured site key
to its Graph ids, and browsing folders on an arbitrary drive (the existing
`migration_common.py` only ever browses the one drive fixed by
`SITE_HOSTNAME`/`SITE_PATH`/`DESTINATION_ROOT` — this module is the
multi-site, multi-drive equivalent used only by Site-to-Site migration).
"""
from __future__ import annotations

import json

from ..config import settings
from ..graph import drive as gd
from ..graph import site as gsite

from .errors import BadRequest, NotFound


def allowed_sites() -> list[dict]:
    """The configured site allow-list — see `config/settings.py`'s
    `allowed_sites` docstring for why this isn't a free-text field or a
    tenant-wide site search."""
    try:
        sites = json.loads(settings.allowed_sites or "[]")
    except json.JSONDecodeError:
        return []
    return sites if isinstance(sites, list) else []


def find_site(site_key: str) -> dict:
    """{"key", "label", "hostname", "site_path"} for a configured site, from
    `ALLOWED_SITES` — used directly (no Graph call) by anything that only
    needs the hostname/site_path, e.g. resolving a drive id via
    `graph.site.get_site_drive_id`. `resolve_site` below additionally
    resolves the Graph site id, for callers that also need Term Store /
    site-column lookups."""
    site = next((s for s in allowed_sites() if s.get("key") == site_key), None)
    if site is None:
        raise BadRequest(f"'{site_key}' is not a configured site — add it to ALLOWED_SITES first")
    return site


async def resolve_site(site_key: str) -> dict:
    """{"key", "label", "hostname", "site_path", "site_id"} for a configured
    site — resolves the Graph site id (needed for Term Store / site-column
    lookups) alongside the config entry."""
    site = find_site(site_key)
    site_id = await gsite.get_site_id(site["hostname"], site["site_path"])
    return {**site, "site_id": site_id}


async def resolve_folder_path(drive_id: str, path: str) -> dict:
    """Same walk as `migration_common.resolve_folder_path`, just not tied to
    the one fixed migration drive — Site-to-Site picks its drive per-call."""
    item_id = await gd.get_root_item_id(drive_id)
    item: dict = {"id": item_id, "name": "", "folder": {}}
    for segment in [p for p in path.split("/") if p]:
        child = await gd.find_child(drive_id, item["id"], segment)
        if child is None or "folder" not in child:
            raise NotFound(f"Folder '{path}' was not found (missing segment '{segment}')")
        item = child
    return item


async def resolve_item_path(drive_id: str, path: str) -> dict:
    """Like `resolve_folder_path`, but the final segment may be a *file*
    (used to resolve an individually-selected file, as opposed to a
    selected folder, which is always walked with `resolve_folder_path`)."""
    parent_path, _, name = path.rpartition("/")
    parent = await resolve_folder_path(drive_id, parent_path)
    child = await gd.find_child(drive_id, parent["id"], name)
    if child is None:
        raise NotFound(f"'{path}' was not found")
    return child


async def list_child_folders(drive_id: str, path: str | None) -> dict:
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
    return {"path": base_path, "folders": folders, "files": files}


def site_url(site: dict) -> str:
    return f"https://{site['hostname']}/{site['site_path'].strip('/')}"
