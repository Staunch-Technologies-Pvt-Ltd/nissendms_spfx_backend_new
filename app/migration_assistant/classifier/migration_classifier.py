"""Classifies one `MigrationItem`: download its bytes from SharePoint, extract
its full text, and match it against the *selected vessel's* live folder
hierarchy using deterministic keyword matching (see keyword_classifier.py —
no local LLM involved, so there's nothing to time out or answer
inconsistently). Never guesses below the confidence threshold — the item is
left as `needs_review` with no destination pre-filled instead (it still gets
a shot at "To Be Classified" later, at Confirm Move time — see
services/migration_mover.py).
"""
from __future__ import annotations

import json
import logging
import re

from ..db import SessionLocal
from ..config import settings
from ..document_parser.extract import extract_text
from ..graph import drive as gd
from ..graph import fields as graph_fields
from ..models import db_models as models
from ..services.migration_common import (
    get_destination_drive_id,
    get_migration_drive_id,
    resolve_folder_path,
)
from ..services.migration_hierarchy import discover_vessel_hierarchy, discover_vessel_list

from .taxonomy_classifier import classify_document, load_destination_taxonomy, resolve_category_path
from .keyword_classifier import classify_by_keywords

logger = logging.getLogger("migration_assistant")

# Same pattern as migration_mover.py's own _SHIP_CODE_RE (kept as a separate
# copy here rather than imported, to avoid a circular import: migration_mover
# already imports from this module). A leading ship-code token (e.g. "SS378")
# in a checked-subfolder name is boilerplate, not part of the vessel's name.
_SHIP_CODE_RE = re.compile(r"^[A-Z]{1,4}\d{2,6}$", re.IGNORECASE)
# Generic words that show up in these archive folder names alongside the
# actual vessel name (e.g. "SS378-PEISSY-Drawings, Plans, Manuals") and carry
# no naming signal of their own.
_GENERIC_FOLDER_WORDS = {
    "and", "drawing", "drawings", "plan", "plans", "manual", "manuals",
}

# How many already-known vessel names must appear side by side for a folder
# level to count as the archive's vessel level (see _vessel_level_ancestor).
# Two is enough to rule out coincidence while still recognising a mostly-new
# batch folder.
_VESSEL_LEVEL_MIN_MATCHES = 2
# folder path -> its subfolder names, for _vessel_level_ancestor's walk.
_folder_children_cache: dict[str, list[str]] = {}

EXCERPT_CHARS = 2000
# Belt-and-suspenders guard against an oversized non-document item slipping
# through — a real case saw a 2.8GB .zip get stuck for hours trying to
# download and decode as text. Real ship documents in this dataset are
# single-digit-to-low-double-digit MB; nothing legitimate needs more than
# this.
MAX_EXTRACTION_SIZE_BYTES = 200 * 1024 * 1024

# Archives — never a document to read text out of, so extraction is skipped
# unconditionally (not just above the size cap): they're moved as-is,
# mirroring their original relative folder position rather than being
# classified (see migration_mover.py's confirm_job, which does that mirroring
# at move time by checking this same extension list).
ARCHIVE_EXTENSIONS = (".zip", ".rar", ".7z", ".tar", ".gz", ".dmg", ".iso", ".exe", ".msi")


def is_archive(filename: str) -> bool:
    return (filename or "").lower().endswith(ARCHIVE_EXTENSIONS)


def _tagged_category_path(tag_value: object, paths: list[str]) -> str | None:
    """Resolve a source Category tag to one existing destination path.

    Tags may contain a full relative path (``Manuals/Engine``) or only the
    destination folder name (``Engine``). A leaf tag is accepted only when it
    identifies one path, so an ambiguous tag never sends a file somewhere
    arbitrary.
    """
    if isinstance(tag_value, list):
        tag_value = tag_value[0] if len(tag_value) == 1 else None
    if isinstance(tag_value, dict):
        tag_value = tag_value.get("Label") or tag_value.get("label") or tag_value.get("Value")
    if not isinstance(tag_value, str):
        return None
    tag = tag_value.strip().strip("/").casefold()
    if not tag:
        return None

    exact = [path for path in paths if path.casefold() == tag]
    if len(exact) == 1:
        return exact[0]
    leaf_matches = [path for path in paths if path.rsplit("/", 1)[-1].casefold() == tag]
    return leaf_matches[0] if len(leaf_matches) == 1 else None


def _candidate_vessel_name(source_path: str, source_folder: str) -> str | None:
    """Best-effort vessel name parsed out of the checked-subfolder name this
    item was found under (e.g. "SS378-PEISSY-Drawings, Plans, Manuals" ->
    "Peissy") — used only for auto-detect-vessel scans (see classify_item).
    Strips a leading ship-code token and generic archive words, title-cases
    what's left. Returns None if nothing distinctive remains, rather than
    guessing — the item is then left needs_review instead of being routed to
    a made-up vessel name."""
    rel = source_path[len(source_folder):].strip("/") if source_path.startswith(source_folder) else source_path
    # Only the first segment of `rel` can name a vessel, and only when `rel`
    # actually has a folder component. A file sitting directly in the selected
    # source folder has none - `rel` is then just the filename, which must
    # never be treated as a folder name (doing so yielded candidates like
    # "Ee 1 Gmdss Radio Station (Incl. Inst. & Test Report).Pdf", matching no
    # vessel and leaving every such item unclassified). When the reviewer
    # selects the vessel-level folder itself, that folder is the candidate.
    folder_name = rel.split("/", 1)[0] if "/" in rel else source_folder.rsplit("/", 1)[-1]
    return folder_name.strip() or None


def _clean_folder_name(folder_name: str) -> str | None:
    """Strip a leading ship-code token and generic archive words off one
    folder name and title-case what's left ("SS378-PEISSY-Drawings, Plans,
    Manuals" -> "Peissy"). None if nothing distinctive remains."""
    tokens = [t for t in re.split(r"[-_,\s]+", folder_name) if t]
    kept = [
        t for t in tokens
        if not _SHIP_CODE_RE.match(t) and t.lower() not in _GENERIC_FOLDER_WORDS
    ]
    if not kept:
        return None
    return " ".join(kept).title()


async def _list_subfolder_names(drive_id: str, path: str) -> list[str]:
    """Subfolder names directly under `path`, cached in-process for the life
    of the scan — the ancestor walk below re-asks for the same few parent
    folders once per item otherwise."""
    cached = _folder_children_cache.get(path)
    if cached is not None:
        return cached
    item = await resolve_folder_path(drive_id, path)
    children = await gd.list_children(drive_id, item["id"])
    names = [c["name"] for c in children if "folder" in c]
    _folder_children_cache[path] = names
    return names


async def _vessel_level_ancestor(
    source_drive_id: str, source_path: str, vessel_names: list[str]
) -> str | None:
    """The name of the ancestor folder that sits at the archive's *vessel
    level*, found by evidence rather than by assuming a fixed depth.

    These archives nest a vessel's documents arbitrarily deep under it
    ("<archive>/Elephanta/GENERAL & HULL PART/HF OUT FITTING"), so the folder
    a reviewer selects is usually not the vessel folder, and the vessel is not
    at a predictable level either (vessel folders sit both directly under
    "type of vessel" and under batch folders like "Drawings and Manuals
    August"). What *is* reliable: the level holding vessel folders holds
    *several* of them, and an archive of any age has some already filed in the
    destination. So walk this item's ancestors deepest-first and take the
    first one whose siblings include at least `_VESSEL_LEVEL_MIN_MATCHES`
    known vessel names — the ancestor is then a vessel folder itself, even
    when that particular vessel is brand new to the destination.

    Returns None when no level shows that evidence; the caller then falls back
    to _candidate_vessel_name's simpler assumption."""
    segments = source_path.split("/")[:-1]  # drop the filename
    known = {n.casefold() for n in vessel_names}
    # Deepest-first: with nested batch folders more than one level can look
    # vessel-ish, and the deepest such level is the specific one.
    for i in range(len(segments) - 1, 0, -1):
        try:
            siblings = await _list_subfolder_names(source_drive_id, "/".join(segments[:i]))
        except Exception:
            logger.debug("Could not list %s while locating the vessel level", "/".join(segments[:i]))
            continue
        matches = 0
        for name in siblings:
            cleaned = _clean_folder_name(name)
            if cleaned and cleaned.casefold() in known:
                matches += 1
        if matches >= _VESSEL_LEVEL_MIN_MATCHES:
            return segments[i].strip() or None
    return None


async def _resolve_item_vessel(
    source_drive_id: str, dest_drive_id: str, source_path: str, source_folder: str
) -> dict | None:
    """For an auto-detect-vessel job, decide which vessel this item belongs
    to and which folder hierarchy to classify its category against. Never
    writes to Graph (folder creation for a brand-new vessel happens later, at
    Confirm Move — see services/migration_mover.py). Vessel folders and
    category hierarchies live on the destination site (`dest_drive_id`); only
    the ancestor walk below reads the source archive's own folder names
    (`source_drive_id`), since the two sites' trees are unrelated. Returns
    {"vessel_name", "vessel_path", "vessel_exists", "vessel_confidence", "hierarchy"}
    or None if no vessel could even be guessed at (item stays needs_review)."""
    vessel_folders = await discover_vessel_list(dest_drive_id)
    # Matched against bare vessel names, not full paths — every vessel sits
    # under the same "{destination_root}/" prefix, and classify_by_keywords'
    # "2+ keywords need 2+ matches" rule would otherwise reject a genuine
    # single-word vessel-name match (the candidate never supplies more than
    # one word) once that shared prefix inflates each candidate's keyword
    # count to 2+.
    name_to_path = {v["path"].rsplit("/", 1)[-1]: v["path"] for v in vessel_folders}

    # Prefer the real vessel-level ancestor over whichever folder the reviewer
    # happened to select — selecting a folder deep inside a vessel otherwise
    # names the vessel after that subfolder ("HF OUT FITTING").
    candidate = await _vessel_level_ancestor(source_drive_id, source_path, list(name_to_path.keys()))
    if candidate is None:
        candidate = _candidate_vessel_name(source_path, source_folder)
    if candidate is None:
        return None

    match = classify_by_keywords(candidate, "", list(name_to_path.keys()))
    if not match.get("path"):
        cleaned_candidate = _clean_folder_name(candidate)
        if cleaned_candidate and cleaned_candidate != candidate:
            match = classify_by_keywords(cleaned_candidate, "", list(name_to_path.keys()))
    confidence = float(match.get("confidence") or 0.0)

    if match.get("path") and confidence >= settings.vessel_match_confidence_threshold:
        vessel_path = name_to_path[match["path"]]
        hierarchy = await discover_vessel_hierarchy(dest_drive_id, vessel_path)
        return {
            "vessel_name": vessel_path.rsplit("/", 1)[-1],
            "vessel_path": vessel_path,
            "vessel_exists": True,
            "vessel_confidence": confidence,
            "hierarchy": hierarchy,
        }

    if not settings.template_vessel_name:
        # No template configured — a brand-new vessel folder structure could
        # never be created for this item at Confirm Move, so there's nothing
        # useful to classify it against yet.
        return None

    template_path = f"{settings.destination_root}/{settings.template_vessel_name}"
    hierarchy = await discover_vessel_hierarchy(dest_drive_id, template_path)
    return {
        "vessel_name": candidate,
        "vessel_path": f"{settings.destination_root}/{candidate}",
        "vessel_exists": False,
        "vessel_confidence": confidence,
        "hierarchy": hierarchy,
    }


def _set_status(item_id: int, status: str, **fields) -> None:
    with SessionLocal() as db:
        item = db.get(models.MigrationItem, item_id)
        if item is None:
            return
        item.status = status
        for k, v in fields.items():
            setattr(item, k, v)
        db.commit()


async def classify_item(item_id: int) -> None:
    with SessionLocal() as db:
        item = db.get(models.MigrationItem, item_id)
        if item is None:
            return
        source_id, filename, vessel_path = item.source_drive_item_id, item.filename, item.job.vessel_path
        auto_detect_vessel = item.job.auto_detect_vessel
        source_path, source_folder = item.source_path, item.job.source_folder
        content_type, size = item.content_type, item.size
        item.status = "extracting"
        db.commit()

    try:
        source_drive_id = await get_migration_drive_id()
        dest_drive_id = await get_destination_drive_id()

        vessel_fields: dict = {}
        if auto_detect_vessel:
            resolved = await _resolve_item_vessel(source_drive_id, dest_drive_id, source_path, source_folder)
            if resolved is None:
                _set_status(item_id, "needs_review", confidence=0.0, reason="Could not determine a vessel for this file.")
                return
            hierarchy = resolved["hierarchy"]
            vessel_fields = {
                "detected_vessel_name": resolved["vessel_name"],
                "detected_vessel_path": resolved["vessel_path"],
                "vessel_exists": resolved["vessel_exists"],
                "vessel_confidence": resolved["vessel_confidence"],
            }
        else:
            hierarchy = await discover_vessel_hierarchy(dest_drive_id, vessel_path)

        if not hierarchy["paths"]:
            _set_status(item_id, "needs_review", confidence=0.0, reason="The selected vessel has no category folders.")
            return

        # A vessel-level wrapper folder that only ever contains further
        # folders (e.g. this destination's shared "Drawings and Manuals"
        # top level) is never itself a real filing destination once
        # anything deeper exists — exclude it from what a file can be
        # classified *into*, without assuming any fixed depth or taxonomy.
        classify_paths = hierarchy["paths"]
        if any("/" in p for p in classify_paths):
            classify_paths = [p for p in classify_paths if "/" in p]

        source_tag = ""
        if settings.source_category_field_name:
            try:
                source_fields = await graph_fields.get_item_fields(source_drive_id, source_id)
                source_tag = str(source_fields.get(settings.source_category_field_name) or "")
            except Exception:
                logger.exception("Could not read source category tag for item %s", item_id)

        # Read the document's own content — the primary classification
        # signal (see module docstring: these ship documents reliably state
        # their subject in a short heading near the top). Archives are never
        # read here (moved as-is, mirroring their source folder position —
        # see migration_mover.py); an oversized non-document slipping through
        # is also skipped rather than risking a multi-hour OCR run on it.
        excerpt = ""
        if not is_archive(filename) and (size or 0) <= MAX_EXTRACTION_SIZE_BYTES:
            try:
                file_bytes, downloaded_content_type, _ = await gd.download_file(source_drive_id, source_id)
                text = extract_text(file_bytes, filename, content_type or downloaded_content_type)
                excerpt = (text or "")[:EXCERPT_CHARS]
            except Exception:
                logger.exception("Could not extract text for migration item %s", item_id)

        vessel_names = [vessel_fields["detected_vessel_name"]] if auto_detect_vessel else [vessel_path.rsplit("/", 1)[-1]]
        document_evidence = "\n".join(part for part in (excerpt, source_tag) if part)
        taxonomy = load_destination_taxonomy(classify_paths)
        selected_vessel = None if auto_detect_vessel else vessel_names[0]
        classification = classify_document(
            filename=filename,
            document_text=document_evidence,
            source_folder=source_folder,
            destination_vessels=vessel_names,
            taxonomy=taxonomy,
            selected_vessel=selected_vessel,
        )
        classification["destination_path"] = (
            resolve_category_path(classification["group"], classification["category"], classify_paths)
            if classification.get("group") and classification.get("category") else None
        )
        classification["taxonomy_paths"] = classify_paths
        classification_json = json.dumps(classification)
        path = classification.get("destination_path")
        confidence = min(
            float(classification.get("group_confidence") or 0.0),
            float(classification.get("category_confidence") or 0.0),
        )
        if classification.get("status") == "classified" and path:
            _set_status(
                item_id, "suggested", extracted_text_excerpt=excerpt, confidence=confidence,
                reason=classification.get("reason"), keywords="[]",
                suggested_path=path,
                suggested_folder_drive_item_id=hierarchy["path_to_id"].get(path)
                if (not auto_detect_vessel or vessel_fields.get("vessel_exists")) else None,
                classification_result=classification_json,
                **vessel_fields,
            )
        else:
            _set_status(
                item_id, "needs_review", extracted_text_excerpt=excerpt, confidence=confidence,
                reason=classification.get("reason") or "Mandatory Group/Category classification failed.",
                keywords="[]", suggested_path=None, suggested_folder_drive_item_id=None,
                classification_result=classification_json,
                **vessel_fields,
            )
        return

    except Exception as e:
        # Never leave an item silently stuck in "extracting" — an unexpected
        # error (missing dependency, corrupt file, ...) must always surface
        # as a visible, actionable failure.
        logger.exception("Unexpected error classifying migration item %s", item_id)
        _set_status(item_id, "failed", error=f"Classification error: {e}")
