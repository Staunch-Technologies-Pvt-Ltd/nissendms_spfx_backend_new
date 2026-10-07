"""Vessel folder template — the sub-folders created inside every new vessel
folder, editable by admins from Settings → Vessel Settings → Vessel Folder
Template.

Stored in the `app_settings` table (key "vessel_folder_template") as
{"version", "updated_by", "updated_at", "folders": [node, ...]} where a node
is {"name": str, "children": [node, ...]}. Until an admin saves one, the
built-in DEFAULT_FOLDERS are used.

Used by every vessel-creation path (custom site + parent folder, site
provisioning, pool claim) through `ensure_tree`, which only ever creates
missing folders: existing folders (matched case-insensitively, with runs of
spaces collapsed) are reused, nothing is renamed or deleted. The same call
adds newly-added template folders to existing vessels ("Apply to existing
vessels").
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

SETTING_KEY = "vessel_folder_template"
INVALID_CHARS = '~"#%&*:<>?/\\{|}'
MAX_DEPTH = 6
MAX_NODES = 400
MAX_NAME = 120


def _n(name: str, *children: Any, aliases: list[str] | None = None) -> dict:
    node = {"name": name, "children": [c if isinstance(c, dict) else {"name": c, "children": []} for c in children]}
    if aliases:
        node["aliases"] = aliases
    return node


DEFAULT_FOLDERS: list[dict] = [
    _n(
        "Drawings and Manuals",
        _n("Drawings", "Basic", "Hull", "Electrical", "Machinery", "Safety", "Other Drawings", "Archive"),
        _n(
            "Manuals",
            "Automation",
            # Existing vessel folders use the "Auxilliary" spelling — match
            # them instead of creating a near-duplicate next to them.
            _n("Auxiliary Engine", aliases=["Auxilliary Engine"]),
            "Boiler", "Cargo", "Deck Machinery", "Electrical", "Main Engine",
            "Other Manuals", "Pollution", "Propulsion", "Refrigeration", "Safety", "Shafting", "Steering Gear",
            "Thrusters",
        ),
        "To Be Classified",
    ),
]


def clean_name(name: str) -> str:
    return " ".join((name or "").split())


def match_key(name: str) -> str:
    return clean_name(name).casefold()


def validate(folders: Any) -> list[dict]:
    """Return a cleaned copy of the tree or raise ValueError with a message
    an admin can act on (which folder, what's wrong)."""
    if not isinstance(folders, list):
        raise ValueError("The template must be a list of folders")
    count = 0

    def walk(nodes: Any, depth: int, where: str) -> list[dict]:
        nonlocal count
        if not isinstance(nodes, list):
            raise ValueError(f"Invalid folder list under {where}")
        if depth > MAX_DEPTH:
            raise ValueError(f"Folders can be nested at most {MAX_DEPTH} levels deep (under {where})")
        seen: set[str] = set()
        out = []
        for node in nodes:
            raw = node.get("name") if isinstance(node, dict) else node
            name = clean_name(raw if isinstance(raw, str) else "")
            label = f'"{name}"' if name else "a folder"
            if not name:
                raise ValueError(f"A folder under {where} has no name")
            if len(name) > MAX_NAME:
                raise ValueError(f"{label} under {where} is longer than {MAX_NAME} characters")
            bad = sorted({ch for ch in name if ch in INVALID_CHARS})
            if bad:
                raise ValueError(f"{label} under {where} contains characters SharePoint doesn't allow: {' '.join(bad)}")
            if name.startswith(".") or name.endswith("."):
                raise ValueError(f"{label} under {where} can't start or end with a dot")
            raw_aliases = node.get("aliases", []) if isinstance(node, dict) else []
            if not isinstance(raw_aliases, list):
                raise ValueError(f"'Also matches' for {label} must be a list of names")
            aliases = []
            for alias in raw_aliases:
                alias = clean_name(alias if isinstance(alias, str) else "")
                if alias and match_key(alias) != match_key(name) and match_key(alias) not in {match_key(a) for a in aliases}:
                    aliases.append(alias)
            for key in [match_key(name), *(match_key(a) for a in aliases)]:
                if key in seen:
                    raise ValueError(f"{label} (or one of its 'also matches' names) appears twice under {where}")
                seen.add(key)
            count += 1
            if count > MAX_NODES:
                raise ValueError(f"The template can have at most {MAX_NODES} folders")
            children = node.get("children", []) if isinstance(node, dict) else []
            entry = {"name": name, "children": walk(children or [], depth + 1, f'"{name}"')}
            if aliases:
                entry["aliases"] = aliases
            out.append(entry)
        return out

    return walk(folders, 1, "the vessel folder")


def count_folders(folders: list[dict]) -> int:
    return sum(1 + count_folders(n.get("children") or []) for n in folders)


def flatten(folders: list[dict], prefix: str = "") -> list[str]:
    """Every folder as a "/"-joined path, parents before children."""
    out: list[str] = []
    for node in folders:
        path = f"{prefix}/{node['name']}" if prefix else node["name"]
        out.append(path)
        out.extend(flatten(node.get("children") or [], path))
    return out


# --- storage -------------------------------------------------------------

def get_template() -> dict:
    """{"version", "updated_by", "updated_at", "is_default", "folders"}."""
    try:
        from ..db import models
        from ..db.base import SessionLocal

        if SessionLocal is not None:
            with SessionLocal() as db:
                row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                return {
                    "version": int(data.get("version") or 1),
                    "updated_by": data.get("updated_by"),
                    "updated_at": data.get("updated_at"),
                    "is_default": False,
                    "folders": validate(data.get("folders") or []),
                }
    except Exception:
        log.exception("[vessel_folder_template] could not read the saved template — using the default")
    return {"version": 0, "updated_by": None, "updated_at": None, "is_default": True, "folders": validate(DEFAULT_FOLDERS)}


def save_template(folders: Any, email: str) -> dict:
    from ..db import models
    from ..db.base import SessionLocal

    cleaned = validate(folders)
    current = get_template()
    now = datetime.utcnow()
    payload = {
        "version": current["version"] + 1,
        "updated_by": email,
        "updated_at": now.isoformat(timespec="seconds"),
        "folders": cleaned,
    }
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = json.dumps(payload), email, now
        db.commit()
    log.info("[vessel_folder_template] v%s saved by %s (%d folders)", payload["version"], email, count_folders(cleaned))
    return {**payload, "is_default": False}


def reset_template(email: str) -> dict:
    return save_template(DEFAULT_FOLDERS, email)


# --- creating folders in SharePoint ----------------------------------------

async def ensure_tree(client, drive_id: str, parent_id: str, folders: list[dict], *,
                      parent_is_new: bool = False, dry_run: bool = False) -> dict:
    """Create every template folder missing under `parent_id`.

    One listing per existing parent; siblings are created concurrently and
    children of a just-created folder skip the listing (it's empty). Returns
    {"created": [paths], "existing": int, "failed": [{"path", "error"}]}.
    With dry_run nothing is written; "created" lists what would be.
    """
    result: dict = {"created": [], "existing": 0, "failed": []}
    sem = asyncio.Semaphore(6)

    async def level(pid: str | None, nodes: list[dict], prefix: str, is_new: bool) -> None:
        existing: dict[str, dict] = {}
        if pid and not is_new:
            url = f"/drives/{drive_id}/items/{pid}/children?$top=999&$select=id,name,folder"
            while url:
                data = await client.get(url)
                for item in data.get("value", []):
                    if item.get("folder") is not None:
                        existing[match_key(item.get("name", ""))] = item
                url = data.get("@odata.nextLink")

        async def one(node: dict) -> None:
            path = f"{prefix}/{node['name']}" if prefix else node["name"]
            found = next(
                (existing[k] for k in [match_key(node["name"]), *(match_key(a) for a in node.get("aliases") or [])] if k in existing),
                None,
            )
            child_id, child_new = (found or {}).get("id"), False
            if found:
                result["existing"] += 1
            elif dry_run or pid is None:
                result["created"].append(path)
                child_new = True
            else:
                try:
                    async with sem:
                        made = await client.post(
                            f"/drives/{drive_id}/items/{pid}/children",
                            json={"name": node["name"], "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
                        )
                    child_id, child_new = made["id"], True
                    result["created"].append(path)
                except Exception as exc:  # 409 = created concurrently; anything else is a real failure
                    if "409" in str(exc):
                        result["existing"] += 1
                        child_new = False
                        child_id = None
                        # Look it up so its children can still be ensured.
                        try:
                            listing = await client.get(f"/drives/{drive_id}/items/{pid}/children?$top=999&$select=id,name,folder")
                            match = next((i for i in listing.get("value", []) if match_key(i.get("name", "")) == match_key(node["name"])), None)
                            child_id = (match or {}).get("id")
                        except Exception:
                            pass
                    else:
                        result["failed"].append({"path": path, "error": str(exc)[:300]})
                        return
            if node.get("children"):
                await level(child_id, node["children"], path, child_new)

        await asyncio.gather(*(one(n) for n in nodes))

    await level(parent_id, folders, "", parent_is_new)
    return result


async def ensure_for_vessel(vessel_id: int, *, dry_run: bool = False) -> dict:
    """Ensure the current template under one existing vessel's folder(s).

    Finds the folder by the vessel's site + `vessel_folder_path` (vessels
    created at a chosen location), else by its "ship" Folder rows (vessels
    provisioned at a drive root / from the pool)."""
    from ..config import Settings
    from ..db import models
    from ..db.base import SessionLocal
    from ..graph.client import graph

    folders = get_template()["folders"]
    with SessionLocal() as db:
        vessel = db.get(models.Vessel, vessel_id)
        if vessel is None:
            return {"vessel_id": vessel_id, "ok": False, "error": "Vessel not found"}
        name, site_key, folder_path = vessel.name, vessel.provisioned_site_key, vessel.vessel_folder_path
        vessel_sites = [k for k in [site_key, *(vessel.provisioned_site_ids or [])] if k]
        ship_rows = [
            (r.site_id, r.drive_item_id)
            for r in db.query(models.Folder).execution_options(all_sites=True)
            .filter_by(vessel_id=vessel_id, kind="ship").all()
            if r.drive_item_id
        ]

    targets: list[tuple[Any, str, str, str]] = []  # (client, drive_id, folder_id, where)
    if site_key and folder_path:
        try:
            config = Settings.load_site_config(site_key)
            client = graph(site_name=site_key, site_config=config)
            drive_id = config.drive_id
            from urllib.parse import quote

            item = await client.get(f"/drives/{drive_id}/root:/{quote(folder_path.strip('/'), safe='/')}?$select=id,name,folder")
            targets.append((client, drive_id, item["id"], f"{site_key}: {folder_path}"))
        except Exception as exc:
            return {"vessel_id": vessel_id, "name": name, "ok": False, "error": f"Vessel folder not found ({folder_path}): {exc}"[:300]}
    else:
        for drive_id, item_id in dict.fromkeys(ship_rows):
            targets.append((graph(), drive_id, item_id, name))
    if not targets:
        found = await _find_vessel_folder_by_name(name, vessel_sites)
        if found:
            targets.append(found[:4])
            _record_location(vessel_id, found[4], found[5])
    if not targets:
        return {"vessel_id": vessel_id, "name": name, "ok": False,
                "error": "No SharePoint folder with this vessel's name was found in any site"}

    created: list[str] = []
    failed: list[dict] = []
    existing = 0
    locations = [t[3] for t in targets]
    for client, drive_id, folder_id, _where in targets:
        try:
            r = await ensure_tree(client, drive_id, folder_id, folders, dry_run=dry_run)
        except Exception as exc:
            failed.append({"path": "", "error": str(exc)[:300]})
            continue
        created += r["created"]
        existing += r["existing"]
        failed += r["failed"]
    return {"vessel_id": vessel_id, "name": name, "ok": not failed, "locations": locations,
            "created": created, "existing": existing, "failed": failed}


async def _find_vessel_folder_by_name(name: str, preferred_sites: list[str]):
    """Fallback for vessels whose folder location isn't recorded (e.g. ones
    discovered from SharePoint): search the vessel's own site(s) for a
    folder with exactly this name, at most
    two levels below the library root (e.g. "Technical and Crewing New/<Vessel>").
    The shallowest match wins. Returns (client, drive_id, folder_id, where)."""
    from ..config import Settings
    from ..graph.client import graph

    from ..config import settings as dms_settings

    # Only the vessel's own site(s), else the active site — never every site,
    # so a same-named folder in another site (e.g. a migration source) is
    # never picked up.
    keys = list(dict.fromkeys(preferred_sites or [dms_settings.active_site]))
    seen_drives: set[str] = set()
    wanted = match_key(name)
    escaped = name.replace("'", "''")
    for key in keys:
        try:
            config = Settings.load_site_config(key)
        except Exception:
            continue
        drive_id = config.drive_id
        if not drive_id or drive_id in seen_drives:
            continue
        seen_drives.add(drive_id)
        client = graph(site_name=key, site_config=config)
        # Deterministic first: the library's folders at the root and one
        # level down (e.g. "Technical and Crewing New/<Vessel>"), listed once
        # per drive and cached briefly. Graph search is only the fallback —
        # its index can lag and return different results run to run.
        try:
            index = await _top_level_index(client, drive_id)
        except Exception:
            index = {}
        hit = index.get(wanted)
        if hit:
            item, parent_path = hit
            rel = f"{parent_path}/{item['name']}" if parent_path else item["name"]
            return (client, drive_id, item["id"], f"{key}: {rel}", key, rel)
        try:
            data = await client.get(
                f"/drives/{drive_id}/root/search(q='{escaped}')?$select=id,name,folder,parentReference&$top=100"
            )
        except Exception:
            log.debug("[vessel_folder_template] search for '%s' on site %s failed", name, key, exc_info=True)
            continue
        best = None
        for item in data.get("value", []):
            if item.get("folder") is None or match_key(item.get("name", "")) != wanted:
                continue
            parent_path = ((item.get("parentReference") or {}).get("path") or "").split("root:", 1)[-1].strip("/")
            depth = len([p for p in parent_path.split("/") if p])
            if depth <= 2 and (best is None or depth < best[0]):
                best = (depth, item, parent_path)
        if best:
            _, item, parent_path = best
            rel = f"{parent_path}/{item['name']}" if parent_path else item["name"]
            return (client, drive_id, item["id"], f"{key}: {rel}", key, rel)
    return None


_INDEX_TTL = 120.0
_index_cache: dict[str, tuple[float, dict]] = {}
_index_locks: dict[str, asyncio.Lock] = {}


async def _list_folders(client, drive_id: str, item_path: str) -> list[dict]:
    url = f"/drives/{drive_id}/{item_path}/children?$top=999&$select=id,name,folder"
    out: list[dict] = []
    while url:
        data = await client.get(url)
        out += [i for i in data.get("value", []) if i.get("folder") is not None]
        url = data.get("@odata.nextLink")
    return out


async def _top_level_index(client, drive_id: str) -> dict[str, tuple[dict, str]]:
    """{match_key(name): (folder item, parent path)} for folders at the
    library root and one level below it; the shallower one wins on a clash."""
    import time

    lock = _index_locks.setdefault(drive_id, asyncio.Lock())
    async with lock:
        cached = _index_cache.get(drive_id)
        if cached and time.monotonic() - cached[0] < _INDEX_TTL:
            return cached[1]
        index: dict[str, tuple[dict, str]] = {}
        roots = await _list_folders(client, drive_id, "root")
        for item in roots:
            index.setdefault(match_key(item["name"]), (item, ""))
        sem = asyncio.Semaphore(6)

        async def children(top: dict) -> tuple[dict, list[dict]]:
            async with sem:
                return top, await _list_folders(client, drive_id, f"items/{top['id']}")

        for top, kids in await asyncio.gather(*(children(t) for t in roots)):
            for item in kids:
                index.setdefault(match_key(item["name"]), (item, top["name"]))
        _index_cache[drive_id] = (time.monotonic(), index)
        return index


# --- recording where a vessel's folder is ----------------------------------

def _record_location(vessel_id: int, site_key: str, folder_path: str) -> None:
    """Save a vessel's folder path (and its site, if it had none) once it has
    been found, so "View Documents" and the cards can use it directly."""
    from ..db import models
    from ..db.base import SessionLocal

    with SessionLocal() as db:
        v = db.get(models.Vessel, vessel_id)
        if v is None or v.vessel_folder_path:
            return
        v.vessel_folder_path = folder_path
        if not v.provisioned_site_key:
            v.provisioned_site_key = site_key
        db.commit()
    log.info("[vessel location] vessel %s folder recorded: %s: %s", vessel_id, site_key, folder_path)


async def locate_vessel_folder(vessel_id: int) -> dict:
    """Return {"site_key", "folder_path"} for a vessel, finding (and saving)
    it by name in the vessel's own site when it isn't recorded yet."""
    from ..db import models
    from ..db.base import SessionLocal

    with SessionLocal() as db:
        v = db.get(models.Vessel, vessel_id)
        if v is None:
            return {"found": False, "error": "Vessel not found"}
        if v.vessel_folder_path:
            return {"found": True, "site_key": v.provisioned_site_key, "folder_path": v.vessel_folder_path}
        name = v.name
        sites = [k for k in [v.provisioned_site_key, *(v.provisioned_site_ids or [])] if k]
    found = await _find_vessel_folder_by_name(name, sites)
    if not found:
        return {"found": False, "error": "No folder with this vessel's name was found in its site"}
    _record_location(vessel_id, found[4], found[5])
    return {"found": True, "site_key": found[4], "folder_path": found[5]}


async def backfill_vessel_locations() -> int:
    """Find and save the folder path of every vessel that has none (run in
    the background at startup). Returns how many were recorded."""
    from ..db import models
    from ..db.base import SessionLocal

    if SessionLocal is None:
        return 0
    with SessionLocal() as db:
        ids = [v.id for v in db.query(models.Vessel).filter(models.Vessel.vessel_folder_path.is_(None)).all()]
    recorded = 0
    for vid in ids:
        try:
            if (await locate_vessel_folder(vid)).get("found"):
                recorded += 1
        except Exception:
            log.debug("[vessel location] lookup failed for vessel %s", vid, exc_info=True)
    if ids:
        log.info("[vessel location] backfill: %d of %d vessel(s) without a folder path found", recorded, len(ids))
    return recorded
