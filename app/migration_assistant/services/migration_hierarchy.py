"""Live discovery of one vessel's destination folder hierarchy from the
SharePoint Online site — the *only* source of truth for valid classification
targets for that vessel. Scoped to a single vessel folder (e.g. "Technical
and Crewing/Peissy") rather than the whole tree, since each scan job targets
exactly one selected destination vessel.

Cached in-process per vessel path with a short TTL: re-walking a vessel's
whole tree on every single document classification would be slow, but the
cache is just a performance optimisation — a manual refresh (or TTL expiry)
always re-reads Graph.
"""
from __future__ import annotations

import asyncio
import time

from ..graph import drive as gd

from . import migration_common

CACHE_TTL_SECONDS = 600
_MAX_DEPTH = 10

# vessel_path -> {"tree": ..., "paths": ..., "path_to_id": ..., "_at": float}
_cache: dict[str, dict] = {}
_sem: asyncio.Semaphore | None = None

# Separate cache for the flat list of vessel folders under destination_root —
# used by auto-vessel-detect scans to match a candidate name against every
# existing vessel (see classifier/migration_classifier.py). A different cache
# from `_cache` above since it's keyed by drive_id, not one vessel's path.
_vessel_list_cache: dict[str, dict] = {}


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(5)
    return _sem


async def _walk(drive_id: str, item_id: str, name: str, parent_path: str, depth: int) -> dict:
    path = f"{parent_path}/{name}" if parent_path else name
    node = {"id": item_id, "name": name, "path": path, "children": []}
    if depth >= _MAX_DEPTH:
        return node
    async with _semaphore():
        children = await gd.list_children(drive_id, item_id)
    subfolders = [c for c in children if "folder" in c]
    node["children"] = await asyncio.gather(
        *(_walk(drive_id, c["id"], c["name"], path, depth + 1) for c in subfolders)
    )
    return node


def _flatten(node: dict, out_paths: list[str], out_ids: dict[str, str]) -> None:
    out_paths.append(node["path"])
    out_ids[node["path"]] = node["id"]
    for child in node["children"]:
        _flatten(child, out_paths, out_ids)


async def discover_vessel_hierarchy(
    drive_id: str, vessel_path: str, *, force_refresh: bool = False
) -> dict:
    """Return {"tree": [...], "paths": [...], "path_to_id": {...}} for the
    subtree under `vessel_path` (e.g. "Technical and Crewing/Peissy").

    `tree` is the list of top-level category folders directly under the
    vessel; `paths` is every folder path *relative to the vessel folder*
    (e.g. "Drawings", "Drawings/Hull") flattened for the LLM prompt;
    `path_to_id` maps such a relative path back to its driveItem id.
    """
    cached = _cache.get(vessel_path)
    if not force_refresh and cached is not None and (time.monotonic() - cached["_at"]) < CACHE_TTL_SECONDS:
        return cached

    vessel = await migration_common.resolve_folder_path(drive_id, vessel_path)
    children = await gd.list_children(drive_id, vessel["id"])
    top_folders = [c for c in children if "folder" in c]
    tree = await asyncio.gather(
        *(_walk(drive_id, c["id"], c["name"], "", 0) for c in top_folders)
    )

    paths: list[str] = []
    path_to_id: dict[str, str] = {}
    for node in tree:
        _flatten(node, paths, path_to_id)

    result = {"tree": tree, "paths": paths, "path_to_id": path_to_id, "_at": time.monotonic()}
    _cache[vessel_path] = result
    return result


async def discover_vessel_list(drive_id: str, *, force_refresh: bool = False) -> list[dict]:
    """The vessel folders under `settings.destination_root` (each
    {"id", "name", "path"}), cached the same way `discover_vessel_hierarchy`
    caches one vessel's subtree — avoids re-listing this on every single
    item classified in an auto-detect-vessel scan."""
    cached = _vessel_list_cache.get(drive_id)
    if not force_refresh and cached is not None and (time.monotonic() - cached["_at"]) < CACHE_TTL_SECONDS:
        return cached["vessels"]
    vessels = await migration_common.list_vessel_folders(drive_id)
    _vessel_list_cache[drive_id] = {"vessels": vessels, "_at": time.monotonic()}
    return vessels
