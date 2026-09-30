"""Walks exactly the subfolders a reviewer checked (never the whole source
tree, never sibling subfolders that weren't selected) and records every
not-yet-seen document as a `MigrationItem` tied to the job, then kicks off
classification for the newly discovered ones. Runs as a background asyncio
task.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from ..db import SessionLocal
from ..graph import drive as gd
from ..models import db_models as models

from . import migration_common

logger = logging.getLogger("migration_assistant")

SCAN_CONCURRENCY = 5
# Classification is now local keyword matching (no GPU/network call to wait
# on), so there's no contention concern that requires serializing — this just
# bounds how many documents extract+match at once.
CLASSIFY_CONCURRENCY = 5

# Only "moved" means a file actually landed in a real, confidently-matched
# category folder — that's genuinely done, permanently skip it. "to_be_classified"
# means it was NEVER confidently classified in the first place; leaving those
# permanently un-revisitable would mean a file that missed classification once
# (e.g. before a subfolder existed, or before a classifier fix) can never be
# retried. So a fresh scan of wherever it currently sits (including its own
# "To Be Classified" folder) re-adopts it and gives it another classification
# attempt — anything still unclassified lands back in a "To Be Classified"
# bucket as before, this time possibly a more specific one (see
# migration_mover.py's branch routing).
_ALREADY_MOVED_STATUSES = ("moved",)

# OS/filesystem clutter that sometimes ends up in these folders — never a
# real document, never worth a review-queue slot, never moved.
_EXCLUDED_FILENAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def _is_scannable(name: str) -> bool:
    return name.lower() not in _EXCLUDED_FILENAMES


def create_job(
    source_folder: str,
    subfolders: list[str],
    vessel_name: str,
    vessel_path: str,
    vessel_folder_id: str,
    files: list[str] | None = None,
    auto_detect_vessel: bool = False,
) -> int:
    with SessionLocal() as db:
        job = models.MigrationScanJob(
            status="running",
            source_folder=source_folder,
            subfolders=json.dumps(subfolders),
            files=json.dumps(files or []),
            vessel_name=vessel_name,
            vessel_path=vessel_path,
            vessel_folder_id=vessel_folder_id,
            auto_detect_vessel=auto_detect_vessel,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def _discover_or_readopt_file(db, f: dict, source_path: str, job_id: int) -> int | None:
    """Record a newly-seen file as a `MigrationItem`, or re-adopt one already
    known (by its stable Graph item id) if it isn't permanently done yet.
    Returns the item's id, or None if it was already permanently moved and
    should be skipped."""
    existing = (
        db.query(models.MigrationItem).filter_by(source_drive_item_id=f["id"]).one_or_none()
    )
    if existing:
        if existing.status in _ALREADY_MOVED_STATUSES:
            return None
        # Still sitting untouched at its source path (never confirmed, or a
        # previous move attempt failed) — re-adopt it into this job and
        # reclassify fresh, since the destination vessel may even be
        # different this time.
        existing.job_id = job_id
        # SharePoint may have renamed the file since the previous scan.
        # Keep original_filename immutable, but classify using the current
        # name because it may contain the category signal.
        existing.filename = f["name"]
        existing.source_path = f"{source_path}/{f['name']}"
        existing.status = "discovered"
        existing.suggested_path = None
        existing.suggested_folder_drive_item_id = None
        existing.confidence = None
        existing.reason = None
        existing.keywords = None
        existing.classification_result = None
        existing.extracted_text_excerpt = None
        existing.overridden = False
        existing.final_path = None
        existing.decided_by_email = None
        existing.decided_at = None
        existing.error = None
        db.flush()
        return existing.id
    item = models.MigrationItem(
        job_id=job_id,
        source_drive_item_id=f["id"],
        source_path=f"{source_path}/{f['name']}",
        filename=f["name"],
        original_filename=f["name"],
        content_type=(f.get("file") or {}).get("mimeType") or "application/octet-stream",
        size=f.get("size") or 0,
        status="discovered",
    )
    db.add(item)
    db.flush()
    return item.id


async def _process_folder(
    drive_id: str, folder_id: str, source_path: str, job_id: int, sem: asyncio.Semaphore
) -> list[int]:
    async with sem:
        children = await gd.list_children(drive_id, folder_id)
    files = [c for c in children if "file" in c and _is_scannable(c["name"])]
    folders = [c for c in children if "folder" in c]

    new_ids: list[int] = []
    with SessionLocal() as db:
        for f in files:
            item_id = _discover_or_readopt_file(db, f, source_path, job_id)
            if item_id is not None:
                new_ids.append(item_id)
        db.commit()

    sub_results = await asyncio.gather(
        *(
            _process_folder(drive_id, c["id"], f"{source_path}/{c['name']}", job_id, sem)
            for c in folders
        )
    )
    for r in sub_results:
        new_ids.extend(r)
    return new_ids


async def _process_specific_files(
    drive_id: str,
    folder_id: str,
    source_path: str,
    filenames: set[str],
    job_id: int,
    sem: asyncio.Semaphore,
) -> list[int]:
    """Process only the given loose files sitting directly in `folder_id` —
    no recursion, since these were checked individually rather than as a
    subfolder to walk fully."""
    async with sem:
        children = await gd.list_children(drive_id, folder_id)
    files = [c for c in children if "file" in c and c["name"] in filenames and _is_scannable(c["name"])]

    new_ids: list[int] = []
    with SessionLocal() as db:
        for f in files:
            item_id = _discover_or_readopt_file(db, f, source_path, job_id)
            if item_id is not None:
                new_ids.append(item_id)
        db.commit()
    return new_ids


def _fail_job(job_id: int, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(models.MigrationScanJob, job_id)
        if job:
            job.status = "failed"
            job.error = message
            job.finished_at = datetime.utcnow()
            db.commit()


async def run_scan(
    job_id: int,
    subfolder_paths: list[str],
    source_folder: str | None = None,
    files: list[str] | None = None,
) -> None:
    """Scan exactly the given list of absolute subfolder paths (each fully,
    i.e. including every nested folder beneath it), plus any individually
    checked loose files sitting directly in `source_folder` — sibling
    subfolders/files that weren't in either list are never touched.
    """
    # Imported lazily to avoid a hard import-time dependency loop between
    # migration_scanner <-> migration_classifier.
    from ..classifier import migration_classifier

    try:
        drive_id = await migration_common.get_migration_drive_id()
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)
        per_folder_results = await asyncio.gather(
            *(_scan_one(drive_id, path, job_id, sem) for path in subfolder_paths)
        )
        new_ids = [i for ids in per_folder_results for i in ids]
        if files:
            folder = await migration_common.resolve_folder_path(drive_id, source_folder)
            new_ids.extend(
                await _process_specific_files(drive_id, folder["id"], source_folder, set(files), job_id, sem)
            )
        with SessionLocal() as db:
            job = db.get(models.MigrationScanJob, job_id)
            job.total_found = len(new_ids)
            job.processed = 0
            db.commit()
        # Deliberately still "running" here — the job isn't done until every
        # discovered document has also been classified (or failed trying).
        # Flipping to "done" at this point (before classification even
        # starts) would let the frontend load the preview table with most
        # items still un-classified, and it never re-polls afterward.
    except Exception as e:
        logger.exception("Migration scan %s failed", job_id)
        _fail_job(job_id, str(e))
        return

    classify_sem = asyncio.Semaphore(CLASSIFY_CONCURRENCY)

    async def _classify_bounded(item_id: int) -> None:
        async with classify_sem:
            try:
                await migration_classifier.classify_item(item_id)
            except Exception:
                logger.exception("Classification failed for migration item %s", item_id)
        with SessionLocal() as db:
            job = db.get(models.MigrationScanJob, job_id)
            job.processed += 1
            db.commit()

    await asyncio.gather(*(_classify_bounded(i) for i in new_ids))

    with SessionLocal() as db:
        job = db.get(models.MigrationScanJob, job_id)
        job.status = "done"
        job.finished_at = datetime.utcnow()
        db.commit()


async def _scan_one(drive_id: str, path: str, job_id: int, sem: asyncio.Semaphore) -> list[int]:
    folder = await migration_common.resolve_folder_path(drive_id, path)
    return await _process_folder(drive_id, folder["id"], path, job_id, sem)
