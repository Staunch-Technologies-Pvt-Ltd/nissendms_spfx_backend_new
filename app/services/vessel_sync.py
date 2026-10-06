"""Vessel auto-discovery: cross-match SharePoint root folders against the
DB vessel registry and the SharePoint Term Store vessel term set.

This module is intentionally read-only and DB-session-free. It reuses the
same root + one-level-in Graph walk real_backend.py's
_term_store_vessel_folder_count() already does for the dashboard's "Total
Vessels" tile, but returns every folder name it finds instead of only the
ones that already match the Term Store — that's the difference between
"count existing vessel folders" and "find brand-new ones nobody has
registered yet".

The caller (RealBackend.sync_vessels_from_sharepoint in real_backend.py)
owns the DB session and decides what to do with each candidate:
matched / needs a Term Store entry / needs a FolderAnomaly row for admin
review. Nothing here writes to Postgres or SharePoint.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .. import template
from ..graph.client import graph

log = logging.getLogger(__name__)

# Folder names at the drive root that are never vessels themselves — same
# exclusion list anomaly_detector.py already applies branch-by-branch, plus
# the legacy "Vessels/Specific Vessels/..." wrapper names (CLAUDE-CONTEXT
# §2: "Vessels/Specific Vessels/..." or a flat root, depending on site).
_NON_VESSEL_ROOT_NAMES = {
    "kaizen - knowledge bank",
    "vessels",
    "specific vessels",
    "common for all ships",
    "common for all vessels",
    "common",
} | {m.lower() for m in template.MAIN_FOLDERS}


def normalize_label(value: str) -> str:
    """Case- and punctuation-insensitive key — the same normalization rule
    already used for Term Store label matching (graph/drive.py) and the
    dashboard's vessel cross-match (real_backend.py _normalize_term_label)."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


@dataclass
class RootCandidate:
    name: str
    drive_item_id: str
    # Folder-relative path from the drive root, e.g. "MV Horizon" (flat
    # empty_pool layout) or "Technical & Crewing/MV Horizon" (templated /
    # legacy layout).
    path: str


async def list_root_vessel_candidates(drive_id: str, pool_slugs: set[str]) -> list[RootCandidate]:
    """Every folder at the drive root, plus one level inside every
    non-excluded root folder — minus known non-vessel names and pool
    slots (Pool-xxxxx, never a real vessel; see PoolSlot in db/models.py).

    Mirrors real_backend.py's _term_store_vessel_folder_count exactly for
    where it looks, but keeps every folder name it finds there instead of
    discarding the ones that aren't already a Term Store match.
    """

    async def _list_folders(item_id: str) -> list[dict]:
        g = graph()
        found: list[dict] = []
        url = f"/drives/{drive_id}/items/{item_id}/children?$select=id,name,folder&$top=999"
        while url:
            page = await g.get(url)
            for item in (page.get("value") or []) if isinstance(page, dict) else []:
                if isinstance(item, dict) and "folder" in item and item.get("name"):
                    found.append(item)
            url = page.get("@odata.nextLink") if isinstance(page, dict) else None
        return found

    # Admin-chosen vessel folders (Site Management) replace the guesswork
    # below: only their sub-folders are vessel candidates.
    from . import vessel_roots
    roots_cfg = vessel_roots.get_for_drive(drive_id)
    if roots_cfg is not None:
        if roots_cfg["mode"] == "none":
            return []
        out: list[RootCandidate] = []
        for item, parent in await vessel_roots.child_folders_of_roots(graph(), drive_id, roots_cfg["paths"]):
            low = item["name"].strip().lower()
            if low in pool_slugs or low.startswith("pool-") or low in _NON_VESSEL_ROOT_NAMES:
                continue
            out.append(RootCandidate(name=item["name"], drive_item_id=item["id"], path=f"{parent}/{item['name']}"))
        return out

    try:
        root_folders = await _list_folders("root")
    except Exception as e:
        log.warning("vessel_sync: could not list root folders for drive=%s: %s", drive_id, e)
        return []

    candidates: list[RootCandidate] = []
    nested_parents: list[dict] = []
    for item in root_folders:
        low = item["name"].strip().lower()
        if low in pool_slugs or low.startswith("pool-"):
            continue  # pool slots never contain real vessels, at root or nested
        if low in _NON_VESSEL_ROOT_NAMES:
            # Not a vessel itself, but may wrap vessels one level down
            # (e.g. "Vessels/Specific Vessels/<ship>", or a main department
            # folder in the templated layout).
            nested_parents.append(item)
            continue
        candidates.append(RootCandidate(name=item["name"], drive_item_id=item["id"], path=item["name"]))

    for parent in nested_parents:
        try:
            children = await _list_folders(parent["id"])
        except Exception as e:
            log.debug("vessel_sync: could not list children of '%s': %s", parent.get("name"), e)
            continue
        for child in children:
            clow = child["name"].strip().lower()
            if clow in _NON_VESSEL_ROOT_NAMES or clow in pool_slugs or clow.startswith("pool-"):
                continue
            candidates.append(RootCandidate(
                name=child["name"],
                drive_item_id=child["id"],
                path=f"{parent['name']}/{child['name']}",
            ))

    return candidates
