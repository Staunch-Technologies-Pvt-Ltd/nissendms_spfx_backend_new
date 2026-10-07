"""Two things happen here, and only here — nothing before Confirm Move ever
touches Graph's write path:

- `override_item`: a reviewer changing a row's category in the preview table
  before confirming — just edits the draft suggestion.
- `confirm_job`: the bulk "Confirm Move" action — copies every fully
    classified item into its suggested folder. Items that fail classification,
    tagging, or verification remain for review.

The destination is a *different* SharePoint site from the source (see
`config.settings.destination_site_key` / `services.migration_common
.get_destination_drive_id`), so "Confirm Move" is, mechanically, a
`gd.copy_item` cross-site copy rather than an in-place `gd.move_item` — Graph
can only move a driveItem within one drive. The source item is deliberately
left untouched: nothing here ever deletes from the source site.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from ..config import settings
from ..graph import drive as gd
from ..graph import fields as gf
from ..graph.client import GraphError
from ..models import db_models as models

from ..db import SessionLocal
from ..classifier.keyword_classifier import classify_by_keywords
from ..classifier.migration_classifier import is_archive
from . import migration_common, migration_tagging, site_to_site_common
from .errors import BadRequest, Conflict, NotFound
from .migration_common import get_migration_drive_id
from .migration_hierarchy import discover_vessel_hierarchy

_FINISHED_PLAN_RE = re.compile(r"finished[-_ ]plan", re.IGNORECASE)
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')

# Lines that are boilerplate in these documents (list/drawing numbers,
# shipyard letterhead, dates, the vessel's own name) rather than the actual
# document subject — skipped when looking for the real heading.
_BOILERPLATE_LINE_RE = re.compile(
    r"^(list\s*no|drawing\s*no|s\.?\s*no|ship\s*no|spec\.?\s*no|type\s*[:：]|date\s*[:：])",
    re.IGNORECASE,
)
_DATE_CODE_RE = re.compile(r"^\d{6,}[\-\d]*$")
_SHIPBUILDER_RE = re.compile(r"shipbuilding|co\.,?\s*ltd|technical\s*(service|department)", re.IGNORECASE)
_PARENTHETICAL_RE = re.compile(r"^[\(（].*[\)）]$")
_SHIP_CODE_RE = re.compile(r"^[A-Z]{1,4}\d{2,6}$", re.IGNORECASE)
# The shipyard's standard confidentiality/copyright notice — appears as a
# multi-line, all-caps paragraph on nearly every drawing right before the
# real heading, and (being all-caps) otherwise passes every other filter
# below and gets mistaken for the document's stated subject.
_CONFIDENTIALITY_NOTICE_RE = re.compile(
    r"exclusive\s*property|strict\s*confidence|copied\s*or\s*reproduced|"
    r"express\s*permission|must\s*not\s*be\s*used|handle\s*it\s*in",
    re.IGNORECASE,
)


def _extract_heading(text: str, vessel_name: str = "") -> str | None:
    """The first real, subject-bearing line near the top of the document
    (e.g. "SATELLITE COMPASS", "SHAFT REVOLUTION INDICATOR") — these
    documents consistently state their actual subject as a short all-caps
    line after a few lines of list/drawing-number letterhead. Returns None
    if nothing that looks like a real heading is found, rather than
    guessing."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    vessel_upper = vessel_name.strip().upper()
    for line in lines:
        if not line:
            continue
        if _DATE_CODE_RE.match(line) or _BOILERPLATE_LINE_RE.match(line):
            continue
        if _SHIPBUILDER_RE.search(line) or _PARENTHETICAL_RE.match(line):
            continue
        if _CONFIDENTIALITY_NOTICE_RE.search(line):
            continue
        if vessel_upper and line.upper() == vessel_upper:
            continue
        if _SHIP_CODE_RE.match(line):
            continue
        letters = [c for c in line if c.isalpha()]
        if len(letters) < 4:
            continue
        # Mostly-uppercase is the signal that this is a stated heading/title
        # rather than incidental prose.
        if sum(1 for c in letters if c.isupper()) / len(letters) < 0.8:
            continue
        return line
    return None


_DRAWING_NO_RE = re.compile(r"drawing\s*no|dwg\s*no", re.IGNORECASE)
_MANUAL_WORD_RE = re.compile(r"\bmanual\b", re.IGNORECASE)


_HEADER_CHARS = 500


def _detect_branch(heading: str | None, text: str) -> str | None:
    """Best-effort guess of whether an unclassified document is a drawing or
    a manual, even though its specific subfolder couldn't be confidently
    determined — used to file it into that branch's own catch-all folder
    ("Other Drawings"/"Other Manuals") instead of the generic "To Be
    Classified", which is reserved for documents that fit neither branch.

    Searching the *whole* document for "manual" or "drawing no" both produced
    real false positives in testing: a manual can incidentally be stamped
    with a "DRAWING No." field on its own boilerplate cover page (this
    shipyard puts that field on every document type, not just drawings), and
    conversely a genuine drawings-index document can mention "manual"
    somewhere later in its body without being one. The document's own
    self-declared heading (already extracted for renaming — see
    `_extract_heading`) is the reliable signal instead: a manual announces
    itself as one right there (e.g. "OPERATION & MAINTENANCE MANUAL FOR
    PURIFIER"). Only if the heading doesn't settle it do we fall back to the
    boilerplate "DRAWING No." field, and only as it appears near the top of
    the document (where that cover-page field actually lives), not anywhere
    in the body."""
    if heading and _MANUAL_WORD_RE.search(heading):
        return "manuals"
    if _DRAWING_NO_RE.search((text or "")[:_HEADER_CHARS]):
        return "drawings"
    return None


def _find_branch_paths(hierarchy_paths: list[str]) -> dict[str, str]:
    """Locate the vessel's own top-level "Drawings" and "Manuals" category
    folders (if present) from its live-discovered hierarchy, by name — not a
    hardcoded template, since this project never assumes a fixed taxonomy."""
    result: dict[str, str] = {}
    for path in hierarchy_paths:
        segments = path.split("/")
        if len(segments) != 2:
            continue
        name = segments[-1].lower()
        if name.startswith("drawing") and "drawings" not in result:
            result["drawings"] = path
        elif name.startswith("manual") and "manuals" not in result:
            result["manuals"] = path
    return result


def _find_branch_catch_all(hierarchy_paths: list[str], branch_path: str) -> str | None:
    """The branch's own catch-all subfolder ("Drawings/Other Drawings",
    "Manuals/Other Manuals"), located by name in the vessel's live hierarchy
    the same way _find_branch_paths locates the branches themselves — never
    assumed, never created. A document that is identifiably a drawing but
    whose specific sub-category couldn't be determined belongs here: filing
    within a branch is mandatory, and "Other Drawings" is the vessel's own
    existing answer for "a drawing, type unknown".

    Returns None if this vessel has no such folder, leaving the caller to fall
    back to the branch folder itself rather than inventing a taxonomy folder.
    """
    depth = branch_path.count("/") + 1
    for path in hierarchy_paths:
        if not path.startswith(branch_path + "/") or path.count("/") != depth:
            continue
        if path.rsplit("/", 1)[-1].lower().startswith("other"):
            return path
    return None


def _resolve_best_branch_target(
    row: dict,
    hierarchy_paths: list[str],
    branch_path: str,
) -> str | None:
    """Legacy helper retained for review tooling; confirmation uses the
    persisted Group/Category result directly.
    """
    branch_prefix = f"{branch_path}/"
    candidates = [p for p in hierarchy_paths if p.startswith(branch_prefix)]
    if not candidates:
        return _find_branch_catch_all(hierarchy_paths, branch_path) or branch_path

    text = f"{row.get('heading') or ''}\n{row.get('filename') or ''}\n{row.get('extracted_text_excerpt') or ''}"
    result = classify_by_keywords(text, row.get("filename") or "", candidates)
    target = result.get("path")
    if target and target in hierarchy_paths:
        return target
    return _find_branch_catch_all(hierarchy_paths, branch_path) or branch_path


async def _resolve_mirrored_folder(
    drive_id: str, source_path: str, source_folder: str, vessel_folder_id: str, vessel_path: str
) -> tuple[str, str]:
    """For an archive/binary file that was never classified: recreate its
    relative folder position (relative to the folder the user actually
    scanned, not the whole legacy source tree) under the destination vessel,
    creating any missing folders as needed — e.g. a file sitting directly in
    the scanned folder mirrors to the vessel's own root; one two levels
    deeper mirrors two folders deep under the vessel root. Returns
    (target_folder_id, target_folder_path)."""
    rel = source_path[len(source_folder):].strip("/") if source_path.startswith(source_folder) else source_path
    rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
    parent_id = vessel_folder_id
    parent_path = vessel_path
    if rel_dir:
        for segment in rel_dir.split("/"):
            folder = await gd.ensure_folder(drive_id, parent_id, segment)
            parent_id = folder["id"]
            parent_path = f"{parent_path}/{segment}"
    return parent_id, parent_path


def _renamed_with_heading(filename: str, heading: str) -> str:
    """Replace the "finished-plan" token in a filename with the document's
    own stated heading (e.g. "SS378_ED-25_finished-plan.pdf" ->
    "SS378_ED-25_Shaft Revolution Indicator.pdf") — the ship code / list
    number prefix is left untouched. If that token isn't present, the
    filename is left unchanged rather than guessing where to insert it."""
    stem, sep, ext = filename.rpartition(".")
    if not sep:
        stem, ext = filename, ""
    safe_heading = _INVALID_FILENAME_CHARS_RE.sub("-", heading).strip().title()
    new_stem, n = _FINISHED_PLAN_RE.subn(safe_heading, stem)
    if n == 0:
        return filename
    return f"{new_stem}.{ext}" if ext else new_stem


async def _ensure_vessel_structure(
    drive_id: str, destination_root_id: str, vessel_name: str, template_paths: list[str]
) -> dict:
    """Create a brand-new vessel folder under the destination root and clone
    the template vessel's category-folder structure into it (see
    config.settings.template_vessel_name). `gd.ensure_folder` is create-or-get,
    so this is safe to re-run (e.g. a retried Confirm Move). Returns
    {"vessel_folder_id", "path_to_id", "paths"} — the same shape a real
    vessel's `discover_vessel_hierarchy` would, so the rest of confirm_job
    doesn't need to distinguish a freshly-created vessel from an existing one."""
    vessel_folder = await gd.ensure_folder(drive_id, destination_root_id, vessel_name)
    path_to_id: dict[str, str] = {}
    for path in sorted(template_paths, key=lambda p: p.count("/")):
        segments = path.split("/")
        parent_id = path_to_id["/".join(segments[:-1])] if len(segments) > 1 else vessel_folder["id"]
        folder = await gd.ensure_folder(drive_id, parent_id, segments[-1])
        path_to_id[path] = folder["id"]
    return {"vessel_folder_id": vessel_folder["id"], "path_to_id": path_to_id, "paths": list(path_to_id.keys())}


async def _prepare_group_moves(
    drive_id: str,
    vessel_path: str,
    vessel_folder_id: str,
    source_folder: str,
    hierarchy: dict,
    rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Prepare only rows with a resolved Group/Category destination, rename every row using its already-extracted
    heading, and precheck for filename collisions — the logic confirm_job has
    always run for its one destination vessel, now scoped to one "vessel
    group" so it can run once per detected vessel in an auto-detect-vessel
    job (see confirm_job). Rows without a valid destination are rejected.
    Returns (to_move, precheck_failed)."""

    # Resolve each item's real target + detect filename collisions concurrently
    # before moving anything (Graph's $batch can't do a conditional "only if
    # no collision" move, so this has to happen as a separate pass).
    sem = asyncio.Semaphore(8)

    async def _prepare(row: dict) -> dict:
        target_id = row["suggested_folder_drive_item_id"]
        relative_path = row["suggested_path"]
        target_path = f"{vessel_path}/{relative_path}" if relative_path else None
        if not target_id:
            return {**row, "precheck_error": "Mandatory classification has no resolved destination category"}
        row = {**row, "suggested_path": relative_path}
        # Renamed using the document's own stated heading — only items where no
        # heading could be confidently found keep their original name.
        # Always computed from the file's untouched original name so a
        # re-scan can still fix a wrong or missing rename from a previous
        # attempt (the current `filename` may already have been renamed,
        # possibly wrongly, and would have nothing left to substitute).
        heading = row["heading"]
        new_filename = _renamed_with_heading(row["original_filename"], heading) if heading else row["filename"]
        async with sem:
            try:
                existing = await gd.find_child(drive_id, target_id, new_filename)
            except GraphError as e:
                return {**row, "target_id": target_id, "target_path": target_path, "new_filename": new_filename, "precheck_error": str(e)}
        # A found child with the SAME drive item id as this row's own source
        # isn't a real collision.
        if existing and "file" in existing and existing.get("id") != row["source_drive_item_id"]:
            error = f"'{new_filename}' already exists in the destination folder"
            return {**row, "target_id": target_id, "target_path": target_path, "new_filename": new_filename, "precheck_error": error}
        return {**row, "target_id": target_id, "target_path": target_path, "new_filename": new_filename, "precheck_error": None}

    prepared = await asyncio.gather(*(_prepare(r) for r in rows))
    to_move = [p for p in prepared if p["precheck_error"] is None]
    precheck_failed = [p for p in prepared if p["precheck_error"] is not None]
    return to_move, precheck_failed


async def override_item(item_id: str, target_path: str) -> None:
    """Pre-confirm edit from the preview table's category dropdown — updates
    the draft suggestion only, no Graph call. Records the AI's original
    suggestion (if any) as feedback."""
    if not item_id.isdigit():
        raise NotFound("Migration item not found")
    with SessionLocal() as db:
        item = db.get(models.MigrationItem, int(item_id))
        if item is None:
            raise NotFound("Migration item not found")
        if item.status not in ("suggested", "needs_review", "failed"):
            raise Conflict(f"This item cannot be edited from its current status ({item.status})")
        auto_detect_vessel = item.job.auto_detect_vessel
        vessel_exists = item.vessel_exists
        if auto_detect_vessel:
            # An already-matched vessel's own real hierarchy; a not-yet-
            # created one's template (same blueprint classify_item used) —
            # never the job's own vessel_path, which for an auto-detect job
            # is just the destination root, not any one vessel.
            vessel_path = (
                item.detected_vessel_path if vessel_exists
                else (f"{settings.destination_root}/{settings.template_vessel_name}"
                      if settings.template_vessel_name else None)
            )
        else:
            vessel_path = item.job.vessel_path
        prior_path = item.suggested_path

    if vessel_path is None:
        raise BadRequest("No destination vessel is known yet for this item — refresh and try again")

    drive_id = await migration_common.get_destination_drive_id()
    hierarchy = await discover_vessel_hierarchy(drive_id, vessel_path)
    if target_path not in hierarchy["path_to_id"]:
        raise BadRequest(
            "Selected destination is not a currently known folder for this vessel — "
            "refresh and try again"
        )
    # For an auto-detect item whose vessel doesn't exist yet, the real folder
    # id can't be resolved until Confirm Move creates that vessel's structure
    # (see confirm_job) — leave it null, same as classify_item does.
    folder_id = hierarchy["path_to_id"][target_path] if (not auto_detect_vessel or vessel_exists) else None

    with SessionLocal() as db:
        item = db.get(models.MigrationItem, int(item_id))
        item.suggested_path = target_path
        item.suggested_folder_drive_item_id = folder_id
        item.overridden = True
        item.status = "suggested"
        item.confidence = None
        item.reason = "Manually selected by reviewer"
        db.add(
            models.MigrationFeedback(
                migration_item_id=item.id,
                ai_suggested_path=prior_path,
                user_final_path=target_path,
                corrected=(prior_path != target_path),
            )
        )
        db.commit()


async def confirm_job(job_id: str, decided_by_email: str) -> dict:
    """Copy every not-yet-moved item in this job into its suggested folder on
    the destination site (see the module docstring — the source item is left
    untouched). An item with no confident suggestion is placed by whatever
    *is* known about
    it: a drawing that couldn't be pinned to a specific subfolder goes into
    an existing valid Category. Nothing is ever left loose at a branch's root
    and no arbitrary category is invented.

    For an auto-detect-vessel job (see MigrationScanJob.auto_detect_vessel),
    items can resolve to different vessels — grouped below by vessel, with a
    brand-new vessel's folder structure created here (cloned from the
    configured template vessel) the first time any of its items is confirmed,
    since nothing before Confirm Move is allowed to write to Graph.

    After each successful move, Managed Metadata tagging (Category/Sub-
    category/Vessel Name) is attempted via services/migration_tagging.py —
    also never raising for one item's tagging failure.

    Never raises for individual item failures; returns a summary."""
    if not job_id.isdigit():
        raise NotFound("Scan job not found")
    with SessionLocal() as db:
        job = db.get(models.MigrationScanJob, int(job_id))
        if job is None:
            raise NotFound("Scan job not found")
        if job.status != "done":
            raise BadRequest("This job's scan hasn't finished yet")
        if job.confirmed_at is not None:
            raise Conflict("This job has already been confirmed")

        pending = (
            db.query(models.MigrationItem)
            .filter_by(job_id=job.id)
            .filter(models.MigrationItem.status == "suggested")
            .all()
        )
        pending_rows = [
            {
                "id": i.id,
                "filename": i.filename,
                "original_filename": i.original_filename or i.filename,
                "source_drive_item_id": i.source_drive_item_id,
                "source_path": i.source_path,
                "suggested_path": i.suggested_path,
                "suggested_folder_drive_item_id": i.suggested_folder_drive_item_id,
                "extracted_text_excerpt": i.extracted_text_excerpt,
                "detected_vessel_name": i.detected_vessel_name,
                "detected_vessel_path": i.detected_vessel_path,
                "vessel_exists": i.vessel_exists,
                "classification_result": json.loads(i.classification_result)
                if i.classification_result else None,
            }
            for i in pending
        ]
        auto_detect_vessel = job.auto_detect_vessel
        vessel_folder_id = job.vessel_folder_id
        vessel_path = job.vessel_path
        vessel_name = job.vessel_name
        source_folder = job.source_folder
        job_source_drive_id = job.source_drive_id
        total_items = db.query(models.MigrationItem).filter_by(job_id=job.id).count()
        categorized_count = sum(
            1 for r in pending_rows
            if r["suggested_folder_drive_item_id"] or (r["vessel_exists"] is False and r["suggested_path"])
        )

    # Extracted once per item and reused for both the branch guess below and
    # the on-move rename — same heading, same reasoning, no need to redo it.
    for row in pending_rows:
        row["heading"] = _extract_heading(
            row["extracted_text_excerpt"],
            row["detected_vessel_name"] or "" if auto_detect_vessel else vessel_name,
        )

    source_drive_id = await get_migration_drive_id(job_source_drive_id)
    drive_id = await migration_common.get_destination_drive_id()
    dest_site = migration_common.get_destination_site()
    dest_site_url = site_to_site_common.site_url(dest_site)

    # An auto-detect job may have items no vessel could even be guessed for
    # (classify_item left them needs_review with no candidate name at all) —
    # nothing to bucket or move them into; fail them outright rather than
    # silently dropping them.
    no_vessel_rows: list[dict] = []
    if auto_detect_vessel:
        no_vessel_rows = [r for r in pending_rows if not r["detected_vessel_name"]]
        pending_rows = [r for r in pending_rows if r["detected_vessel_name"]]

    # One "vessel group" per distinct destination vessel this job's items
    # resolve to — for a normal (pick-one-vessel) job this is always exactly
    # one group, identical to this function's original single-vessel behaviour.
    groups: dict[str, dict] = {}
    vessels_created = 0
    if not auto_detect_vessel:
        groups[vessel_path] = {
            "vessel_path": vessel_path, "vessel_folder_id": vessel_folder_id, "rows": pending_rows,
        }
    else:
        for row in pending_rows:
            key = row["detected_vessel_path"]
            group = groups.setdefault(key, {
                "vessel_path": key, "vessel_folder_id": None, "rows": [],
                "vessel_exists": row["vessel_exists"], "vessel_name": row["detected_vessel_name"],
            })
            group["rows"].append(row)

        destination_root_folder = await migration_common.resolve_folder_path(drive_id, settings.destination_root)
        for group in groups.values():
            if group["vessel_exists"]:
                vessel_item = await migration_common.resolve_folder_path(drive_id, group["vessel_path"])
                group["vessel_folder_id"] = vessel_item["id"]
                continue
            if not settings.template_vessel_name:
                # Shouldn't happen — classify_item only routes an item to a
                # not-yet-existing vessel when a template is configured — but
                # never silently drop items over a config gap that changed
                # since scan time; fail them individually below instead.
                group["creation_error"] = "No template vessel is configured to create a new vessel's structure from"
                continue
            template_hierarchy = await discover_vessel_hierarchy(
                drive_id, f"{settings.destination_root}/{settings.template_vessel_name}"
            )
            created = await _ensure_vessel_structure(
                drive_id, destination_root_folder["id"], group["vessel_name"], template_hierarchy["paths"]
            )
            group["vessel_folder_id"] = created["vessel_folder_id"]
            group["path_to_id"] = created["path_to_id"]
            vessels_created += 1
            # Resolve each row's real folder id now that the structure exists
            # (classify time only knew the relative suggested_path, matched
            # against the template — see classifier/migration_classifier.py).
            for row in group["rows"]:
                if row["suggested_path"]:
                    row["suggested_folder_drive_item_id"] = created["path_to_id"].get(row["suggested_path"])

    to_move: list[dict] = []
    precheck_failed: list[dict] = [
        {**r, "precheck_error": "No vessel could be determined for this file"} for r in no_vessel_rows
    ]
    for group in groups.values():
        if group.get("creation_error"):
            precheck_failed.extend({**r, "precheck_error": group["creation_error"]} for r in group["rows"])
            continue
        if not auto_detect_vessel or group.get("vessel_exists"):
            hierarchy = await discover_vessel_hierarchy(drive_id, group["vessel_path"])
        else:
            hierarchy = {"paths": list(group["path_to_id"].keys()), "path_to_id": group["path_to_id"]}
        group_to_move, group_precheck_failed = await _prepare_group_moves(
            drive_id, group["vessel_path"], group["vessel_folder_id"], source_folder, hierarchy, group["rows"]
        )
        to_move.extend(group_to_move)
        precheck_failed.extend(group_precheck_failed)

    moved_count = failed_count = 0
    tags_applied = tags_unmapped = tags_failed = 0
    failed_items: list[dict] = []
    now = datetime.utcnow()

    # The destination is a different site than the source (see the module
    # docstring), so "moving" an item is a server-side copy into the
    # destination drive — the source item is never touched. Copy first, then
    # tag the newly created destination item (unlike the old same-site
    # behaviour, which tagged the source item before moving it).
    copy_sem = asyncio.Semaphore(8)

    async def _copy_one(row: dict) -> dict:
        async with copy_sem:
            try:
                monitor_url = await gd.copy_item(
                    source_drive_id, row["source_drive_item_id"], drive_id, row["target_id"], row["new_filename"]
                )
                result = await gd.poll_copy_status(monitor_url)
                if result.get("status") != "completed":
                    return {**row, "copy_error": result.get("error") or "Copy did not complete"}
                dest_item_id = result.get("resourceId")
                if not dest_item_id:
                    # Some tenants omit resourceId on the monitor payload —
                    # look the file up by name instead.
                    found = await gd.find_child(drive_id, row["target_id"], row["new_filename"])
                    dest_item_id = found["id"] if found else None
                if dest_item_id:
                    # Graph's async-copy monitor has been observed to report
                    # "completed" with a resourceId that then 404s — never
                    # trust "completed" alone before recording this as done.
                    try:
                        await gd.get_item(drive_id, dest_item_id)
                    except GraphError:
                        dest_item_id = None
                if not dest_item_id:
                    return {**row, "copy_error": "Copy reported success but the destination file could not be verified"}
                return {**row, "dest_item_id": dest_item_id, "copy_error": None}
            except GraphError as e:
                return {**row, "copy_error": str(e)}

    copied_rows = await asyncio.gather(*(_copy_one(r) for r in to_move)) if to_move else []
    to_move = [r for r in copied_rows if r["copy_error"] is None]
    precheck_failed.extend(
        {**r, "precheck_error": r["copy_error"]} for r in copied_rows if r["copy_error"] is not None
    )

    # Category is mandatory on the copy just created at the destination —
    # applied here rather than blocking the copy above, since (unlike the old
    # same-site PATCH move) the copy can't be conditioned on the tag write
    # succeeding first. Sub-category and Vessel are best effort. A category
    # tag failure marks the item failed, but the already-created destination
    # copy is left in place rather than deleted — matching this function's
    # existing never-roll-back-on-partial-failure contract elsewhere.
    tag_reports: dict[int, list[dict]] = {}
    if to_move:
        list_title = await gd.get_drive_list_title(drive_id)
        sem = asyncio.Semaphore(8)

        async def _tag_after_copy(row: dict) -> tuple[dict, list[dict]]:
            classification = row.get("classification_result") or {}
            tags = {
                "group": classification.get("group"),
                "category": classification.get("category"),
                "vessel": classification.get("vessel")
                or (row["detected_vessel_name"] if auto_detect_vessel else vessel_name),
            }
            async with sem:
                try:
                    list_item_id = await gf.get_list_item_id(drive_id, row["dest_item_id"])
                    write_report = await migration_tagging.apply_term_tags(
                        dest_site_url, list_title, list_item_id, tags
                    )
                    verify_report = await migration_tagging.verify_term_tags(
                        dest_site_url, list_title, list_item_id, tags
                    )
                    report = write_report + verify_report
                except Exception as e:
                    report = [{"field": "*", "status": "error", "detail": f"Tagging failed: {e}"}]
            return row, report

        tagged_rows = await asyncio.gather(*(_tag_after_copy(row) for row in to_move))
        eligible_rows = []
        for row, report in tagged_rows:
            tag_reports[row["id"]] = report
            required_entries = {
                entry["field"]: entry for entry in report
                if entry["field"] in ("group", "category", "vessel")
            }
            required_ok = all(
                required_entries.get(field, {}).get("status") == "applied"
                for field in ("group", "category", "vessel")
            ) and all(
                any(entry["field"] == field and entry["status"] == "verified" for entry in report)
                for field in ("group", "category", "vessel")
            )
            statuses = {entry["status"] for entry in report}
            if "error" in statuses:
                tags_failed += 1
            elif "unmapped" in statuses:
                tags_unmapped += 1
            elif "applied" in statuses:
                tags_applied += 1
            if required_ok:
                eligible_rows.append(row)
            else:
                detail = next(
                    (entry["detail"] for entry in report if entry["status"] in ("error", "unmapped", "skipped")),
                    "Required Group/Category/Vessel tagging or verification failed",
                )
                precheck_failed.append({**row, "precheck_error": detail})
        to_move = eligible_rows

    with SessionLocal() as db:
        for row in precheck_failed:
            item = db.get(models.MigrationItem, row["id"])
            item.status = "needs_review"
            item.error = row["precheck_error"]
            report = tag_reports.get(row["id"])
            if report is not None:
                item.term_tag_status = "failed"
                item.term_tag_report = json.dumps(report)
            item.decided_by_email = decided_by_email
            item.decided_at = now
            failed_count += 1
            failed_items.append({"filename": row["filename"], "error": row["precheck_error"]})

        for row in to_move:
            item = db.get(models.MigrationItem, row["id"])
            item.status = "moved"
            item.final_path = f"{row['target_path']}/{row['new_filename']}"
            item.filename = row["new_filename"]
            item.decided_by_email = decided_by_email
            item.decided_at = now
            moved_count += 1
            report = tag_reports.get(row["id"], [])
            item.term_tag_status = "verified"
            item.term_tag_report = json.dumps(report)
            classification = json.loads(item.classification_result) if item.classification_result else {}
            classification["termStoreTagsResolved"] = True
            classification["taggingSucceeded"] = True
            item.classification_result = json.dumps(classification)

        job = db.get(models.MigrationScanJob, job.id)
        job.confirmed_at = now
        job.confirmed_by_email = decided_by_email
        db.commit()

    return {
        "total": total_items,
        "categorized": categorized_count,
        "moved": moved_count,
        "failed": failed_count,
        "failed_items": failed_items,
        "vessels_created": vessels_created,
        "tags_applied": tags_applied,
        "tags_unmapped": tags_unmapped,
        "tags_failed": tags_failed,
    }
