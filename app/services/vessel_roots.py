"""Per-site "vessel folders": where in a site's library the vessel folders
live, chosen by an admin in Sites → Site Management.

Vessel discovery (the Vessels page's "Found in SharePoint" suggestions, the
Dashboard's vessel count and the SharePoint vessel sync) only looks inside
these folders: every sub-folder of a chosen folder is a vessel folder.
Sites such as a migration source hold many unrelated folders, so guessing
from the whole library produced false vessels.

Stored in app_settings (key "vessel_roots") keyed by drive id — discovery
runs per document library, and two site keys can point at the same library:
  {drive_id: {"site_key", "mode": "folders"|"none", "paths": [...],
              "updated_by", "updated_at"}}
A library with no entry keeps the previous automatic behaviour.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import quote

log = logging.getLogger(__name__)

SETTING_KEY = "vessel_roots"
MODES = ("folders", "none")


def _load() -> dict:
    from ..db import models
    from ..db.base import SessionLocal

    if SessionLocal is None:
        return {}
    try:
        with SessionLocal() as db:
            row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        data = json.loads(row.value) if row and row.value else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        log.exception("[vessel_roots] could not read the setting")
        return {}


def get_all() -> dict:
    return _load()


def get_for_drive(drive_id: str | None) -> dict | None:
    """The configuration for one library, or None when it was never set
    (then discovery keeps its automatic behaviour)."""
    if not drive_id:
        return None
    cfg = _load().get(drive_id)
    if not isinstance(cfg, dict) or cfg.get("mode") not in MODES:
        return None
    return cfg


def save(drive_id: str, site_key: str, mode: str | None, paths: list[str], email: str) -> dict:
    """mode None = back to automatic (removes the entry)."""
    from ..db import models
    from ..db.base import SessionLocal

    data = _load()
    if mode is None:
        data.pop(drive_id, None)
        entry = {}
    else:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        clean = []
        for p in paths:
            p = "/".join(part.strip() for part in (p or "").replace("\\", "/").split("/") if part.strip())
            if p and p not in clean:
                clean.append(p)
        if mode == "folders" and not clean:
            raise ValueError("Choose at least one folder, or pick 'This site has no vessel folders'")
        entry = {
            "site_key": site_key, "mode": mode, "paths": clean if mode == "folders" else [],
            "updated_by": email, "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        data[drive_id] = entry
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = json.dumps(data), email, now
        db.commit()
    log.info("[vessel_roots] drive %s (site %s) set to %s by %s", drive_id, site_key, entry or "automatic", email)
    return entry


# Last discovered location of each vessel folder, per library:
# {drive_id: {normalized name: "root/child" path}} — lets the Vessels page
# open a discovered (not registered) vessel's real folder.
_discovered_paths: dict[str, dict[str, str]] = {}


def remember_paths(drive_id: str, paths: dict[str, str]) -> None:
    _discovered_paths[drive_id] = paths


def discovered_path(drive_id: str | None, norm_name: str) -> str | None:
    return _discovered_paths.get(drive_id or "", {}).get(norm_name)


async def child_folders_of_roots(client, drive_id: str, paths: list[str]) -> list[tuple[dict, str]]:
    """[(folder item, its parent path)] for every sub-folder of the chosen
    folders. A chosen folder that no longer exists is skipped (and logged)."""
    out: list[tuple[dict, str]] = []
    for root in paths:
        try:
            item = await client.get(f"/drives/{drive_id}/root:/{quote(root, safe='/')}?$select=id,name,folder")
        except Exception as exc:
            log.warning("[vessel_roots] vessel folder '%s' not found on drive %s: %s", root, drive_id, exc)
            continue
        url = f"/drives/{drive_id}/items/{item['id']}/children?$select=id,name,folder&$top=999"
        while url:
            page = await client.get(url)
            for child in page.get("value") or []:
                if child.get("folder") is not None and child.get("name"):
                    out.append((child, root))
            url = page.get("@odata.nextLink")
    return out
