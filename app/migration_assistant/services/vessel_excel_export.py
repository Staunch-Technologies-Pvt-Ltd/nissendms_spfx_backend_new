"""Per-vessel Excel export of every document under a vessel's Drawings and
Manuals folders — entirely separate from the migration scan/classify/move
pipeline (`migration_scanner.py` / `migration_classifier.py` / `migration_mover.py`);
this module never reads or writes anything those touch.

One workbook per vessel, downloaded straight to the reviewer's computer (no
Graph write). The workbook has exactly two sheets — "Drawings" and "Manuals"
(only the ones that actually exist under this vessel) — each listing every
file found anywhere under that branch, one row per file: Document Name
(a clickable link straight to the file in SharePoint), Vessel Name, Category
(the name of the folder the file directly sits in, e.g. "Basic",
"Electrical"), and Group ("Drawings"/"Manuals").
"""
from __future__ import annotations

import asyncio
import io
import re

from openpyxl import Workbook

from ..graph import drive as gd

from .errors import NotFound
from .migration_common import get_migration_drive_id, resolve_folder_path

# Matches migration_hierarchy.py's own walk depth limit — a sane ceiling
# against a pathologically deep/cyclical folder structure.
_MAX_DEPTH = 10

# How far under the vessel folder to search for the real "Drawings"/"Manuals"
# folders — real vessels here have one wrapper folder in between (e.g.
# vessel/"Drawings and Manuals"/Drawings), so this can't assume they sit
# directly under the vessel.
_MAX_ROOT_SEARCH_DEPTH = 4
_DRAWINGS_NAMES = {"drawings", "drawing"}
_CATEGORY_NAMES = _DRAWINGS_NAMES | {"manuals", "manual"}

_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')

_SHEET_ORDER = ["Drawings", "Manuals"]
_HEADER = ["Document Name", "Vessel Name", "Category", "Group"]


async def _find_category_roots(
    drive_id: str, item_id: str, depth: int, sem: asyncio.Semaphore
) -> list[dict]:
    """Search down from `item_id` for folders whose own name is exactly
    "Drawings"/"Manuals" (singular or plural, case-insensitive) — these can
    sit directly under the vessel, or nested one or more wrapper folders deep
    (e.g. vessel/"Drawings and Manuals"/Drawings), so this searches rather
    than assuming a fixed depth or matching by prefix (a prefix match would
    also catch a wrapper folder literally named "Drawings and Manuals" and
    treat its whole subtree as one mislabeled branch instead of two real
    ones). Stops descending a branch once it finds a match in it."""
    if depth >= _MAX_ROOT_SEARCH_DEPTH:
        return []
    async with sem:
        children = await gd.list_children(drive_id, item_id)
    roots: list[dict] = []
    to_search: list[dict] = []
    for c in children:
        if "folder" not in c:
            continue
        if c["name"].strip().lower() in _CATEGORY_NAMES:
            roots.append(c)
        else:
            to_search.append(c)
    nested = await asyncio.gather(
        *(_find_category_roots(drive_id, c["id"], depth + 1, sem) for c in to_search)
    )
    for group in nested:
        roots.extend(group)
    return roots


async def _collect_rows(
    drive_id: str,
    item_id: str,
    folder_name: str,
    group: str,
    depth: int,
    out: dict[str, list[tuple[str, str, str]]],
    sem: asyncio.Semaphore,
) -> None:
    """Walk `item_id` fully recursively, appending (filename, webUrl,
    category) to `out[group]` for every file found — `category` is always
    the name of the folder the file directly sits in, however deep."""
    if depth >= _MAX_DEPTH:
        return
    async with sem:
        children = await gd.list_children(drive_id, item_id)
    files = sorted((c for c in children if "file" in c), key=lambda c: c["name"])
    for f in files:
        out[group].append((f["name"], f.get("webUrl", ""), folder_name))
    subfolders = [c for c in children if "folder" in c]
    await asyncio.gather(
        *(
            _collect_rows(drive_id, c["id"], c["name"], group, depth + 1, out, sem)
            for c in subfolders
        )
    )


async def build_vessel_workbook(vessel_path: str) -> tuple[bytes, str]:
    """Return (xlsx bytes, suggested filename) for `vessel_path` (e.g.
    "Technical and Crewing/Peissy").

    Finds that vessel's real "Drawings"/"Manuals" folders by exact name,
    wherever they sit under the vessel (see `_find_category_roots`), walks
    each fully recursively, and lists every file it finds on the matching
    "Drawings"/"Manuals" sheet. Raises NotFound if the vessel path doesn't
    exist or has neither folder anywhere underneath it.
    """
    drive_id = await get_migration_drive_id()
    vessel = await resolve_folder_path(drive_id, vessel_path)
    vessel_name = vessel_path.rsplit("/", 1)[-1]

    sem = asyncio.Semaphore(8)
    branch_folders = await _find_category_roots(drive_id, vessel["id"], 0, sem)
    if not branch_folders:
        raise NotFound(f"'{vessel_path}' has no Drawings or Manuals folder")

    rows: dict[str, list[tuple[str, str, str]]] = {"Drawings": [], "Manuals": []}
    await asyncio.gather(
        *(
            _collect_rows(
                drive_id,
                root["id"],
                root["name"],
                "Drawings" if root["name"].strip().lower() in _DRAWINGS_NAMES else "Manuals",
                0,
                rows,
                sem,
            )
            for root in branch_folders
        )
    )

    wb = Workbook()
    wb.remove(wb.active)
    for group in _SHEET_ORDER:
        if not rows[group]:
            continue
        ws = wb.create_sheet(group)
        ws.append(_HEADER)
        for filename, weburl, category in rows[group]:
            ws.append([filename, vessel_name, category, group])
            if weburl:
                cell = ws.cell(row=ws.max_row, column=1)
                cell.hyperlink = weburl
                cell.style = "Hyperlink"
        ws.column_dimensions["A"].width = 60
        ws.column_dimensions["B"].width = 20
        ws.column_dimensions["C"].width = 24
        ws.column_dimensions["D"].width = 12
    if not wb.sheetnames:
        wb.create_sheet("No files found")

    buf = io.BytesIO()
    wb.save(buf)
    safe_vessel = _INVALID_FILENAME_CHARS_RE.sub("-", vessel_name).strip() or "vessel"
    return buf.getvalue(), f"{safe_vessel} - Drawings and Manuals.xlsx"
