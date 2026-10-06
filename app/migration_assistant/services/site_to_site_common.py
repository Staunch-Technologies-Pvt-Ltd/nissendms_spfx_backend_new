"""Shared helpers for Site-to-Site migration: resolving a configured site key
to its Graph ids, and browsing folders on an arbitrary drive (the existing
`migration_common.py` only ever browses the one drive fixed by
`SITE_HOSTNAME`/`SITE_PATH`/`DESTINATION_ROOT` — this module is the
multi-site, multi-drive equivalent used only by Site-to-Site migration).
"""
from __future__ import annotations

import json
from urllib.parse import unquote, urlparse

from ..config import settings
from ..graph import drive as gd
from ..graph import site as gsite

from . import dms_sites
from .errors import BadRequest, NotFound


URL_KEY_PREFIX = "url:"


def allowed_sites() -> list[dict]:
    """`ALLOWED_SITES` from `.env.migration` (the original fixed list)."""
    try:
        sites = json.loads(settings.allowed_sites or "[]")
    except json.JSONDecodeError:
        return []
    return sites if isinstance(sites, list) else []


def _norm_url(site: dict) -> str:
    url = site.get("url") or f"https://{site.get('hostname', '')}/{(site.get('site_path') or '').strip('/')}"
    return url.rstrip("/").lower()


def known_sites() -> list[dict]:
    """Every site a key can refer to: the DMS Site Management sites plus
    `ALLOWED_SITES` (kept so existing keys such as DESTINATION_SITE_KEY and
    old jobs keep resolving)."""
    return [*dms_sites.site_management_sites(), *allowed_sites()]


def picker_sites() -> list[dict]:
    """Sites offered in the Site-to-Site pickers: everything in the DMS's
    Site Management first (so a site added there shows up here with the
    same name), then any `ALLOWED_SITES` entry that isn't already among
    them. Other sites can still be found by search or pasted URL."""
    out: list[dict] = []
    seen: set[str] = set()
    for site in known_sites():
        url = _norm_url(site)
        if url in seen:
            continue
        seen.add(url)
        out.append(site)
    return out


def parse_site_url(url: str) -> tuple[str, str]:
    """("contoso.sharepoint.com", "sites/Docs") from any URL inside a site —
    a library or folder URL pasted from the browser works too, since only
    the /sites/<name> or /teams/<name> part is kept. A URL with neither is
    the tenant root site (site_path "")."""
    parsed = urlparse(url.strip() if "://" in url else f"https://{url.strip()}")
    if not parsed.hostname or not parsed.hostname.endswith(".sharepoint.com"):
        raise BadRequest("Enter a SharePoint site URL, e.g. https://contoso.sharepoint.com/sites/Docs")
    segments = [unquote(p) for p in parsed.path.split("/") if p]
    if len(segments) >= 2 and segments[0].lower() in ("sites", "teams"):
        return parsed.hostname.lower(), f"{segments[0].lower()}/{segments[1]}"
    return parsed.hostname.lower(), ""


def site_key_for_url(url: str) -> str:
    hostname, site_path = parse_site_url(url)
    return f"{URL_KEY_PREFIX}https://{hostname}/{site_path}".rstrip("/")


def find_site(site_key: str) -> dict:
    """{"key", "label", "hostname", "site_path"} for a site key — a Site
    Management site ("dms:<key>"), an `ALLOWED_SITES` key, or a
    "url:https://host/sites/x" key minted
    by site search / URL lookup. Used directly (no Graph call) by anything
    that only needs the hostname/site_path. `resolve_site` below
    additionally resolves the Graph site id."""
    if site_key.startswith(URL_KEY_PREFIX):
        url = site_key[len(URL_KEY_PREFIX):]
        hostname, site_path = parse_site_url(url)
        configured = next(
            (s for s in known_sites() if s.get("hostname", "").lower() == hostname and s.get("site_path", "").strip("/") == site_path),
            None,
        )
        label = configured.get("label", site_path or hostname) if configured else (site_path.split("/")[-1] if site_path else hostname)
        return {"key": site_key, "label": label, "hostname": hostname, "site_path": site_path}
    site = next((s for s in known_sites() if s.get("key") == site_key), None)
    if site is None:
        if site_key.startswith(dms_sites.KEY_PREFIX):
            raise BadRequest(f"'{site_key[len(dms_sites.KEY_PREFIX):]}' is no longer in Site Management")
        raise BadRequest(f"'{site_key}' is not a known site — pick it from the list or search for it")
    return site


async def resolve_site(site_key: str) -> dict:
    """{"key", "label", "hostname", "site_path", "site_id"} for a configured
    site — resolves the Graph site id (needed for Term Store / site-column
    lookups) alongside the config entry."""
    site = find_site(site_key)
    if site_key.startswith(dms_sites.KEY_PREFIX):
        return await dms_sites.resolve(site)
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
