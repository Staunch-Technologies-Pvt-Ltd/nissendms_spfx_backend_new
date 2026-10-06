"""Facade for the Migration Assistant — the single entry point `backend/app/main.py`
calls into. Composes the browse/scan/classify/move modules and handles
serialization. Every action (except browsing and Confirm Move) is scoped to
one `MigrationScanJob` — one source-folder + subfolders + destination-vessel
choice the reviewer made.
"""
from __future__ import annotations

import asyncio
import json

from ..graph import drive as gd
from ..models import db_models as models

from ..db import SessionLocal
from ..config import settings

from . import migration_common, migration_mover, migration_scanner
from .errors import BadRequest, NotFound
from .migration_common import get_migration_drive_id


def _require_configured() -> None:
    if not settings.graph_configured:
        raise BadRequest(
            "Not configured — set MIGRATION_SITE_HOSTNAME and MIGRATION_SITE_PATH in backend/.env (the Graph credentials come from the DMS settings)."
        )


def _item_public(row: models.MigrationItem) -> dict:
    category, _, subcategory = (row.suggested_path or "").partition("/")
    return {
        "id": str(row.id),
        "job_id": str(row.job_id),
        "source_path": row.source_path,
        "filename": row.filename,
        "content_type": row.content_type,
        "size": row.size,
        "status": row.status,
        "extracted_text_excerpt": row.extracted_text_excerpt,
        "suggested_path": row.suggested_path,
        "category": category or None,
        "subcategory": subcategory or None,
        "confidence": row.confidence,
        "reason": row.reason,
        "keywords": json.loads(row.keywords) if row.keywords else [],
        "classification_result": json.loads(row.classification_result) if row.classification_result else None,
        "detected_vessel_name": row.detected_vessel_name,
        "vessel_exists": row.vessel_exists,
        "vessel_confidence": row.vessel_confidence,
        "term_tag_status": row.term_tag_status,
        "term_tag_report": json.loads(row.term_tag_report) if row.term_tag_report else [],
        "overridden": row.overridden,
        "final_path": row.final_path,
        "decided_by_email": row.decided_by_email,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _job_public(job: models.MigrationScanJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "source_folder": job.source_folder,
        "subfolders": json.loads(job.subfolders) if job.subfolders else [],
        "files": json.loads(job.files) if job.files else [],
        "vessel_name": job.vessel_name,
        "vessel_path": job.vessel_path,
        "auto_detect_vessel": job.auto_detect_vessel,
        "total_found": job.total_found,
        "processed": job.processed,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "confirmed_at": job.confirmed_at.isoformat() if job.confirmed_at else None,
        "confirmed_by_email": job.confirmed_by_email,
        "error": job.error,
    }


def _get_job_row(job_id: str) -> models.MigrationScanJob:
    if not job_id.isdigit():
        raise NotFound("Scan job not found")
    with SessionLocal() as db:
        job = db.get(models.MigrationScanJob, int(job_id))
        if job is None:
            raise NotFound("Scan job not found")
        db.expunge(job)
        return job


def _get_item_in_job(job_id: str, item_id: str) -> models.MigrationItem:
    if not job_id.isdigit() or not item_id.isdigit():
        raise NotFound("Migration item not found")
    with SessionLocal() as db:
        row = db.get(models.MigrationItem, int(item_id))
        if row is None or row.job_id != int(job_id):
            raise NotFound("Migration item not found")
        db.expunge(row)
        return row


async def list_source_folders(path: str | None = None) -> dict:
    """Immediate children under `path`, or the Documents library's top level
    if empty — powers the source-folder/subfolder browser. Includes files
    (read-only, for visibility into exactly what's there) alongside the
    navigable/selectable folders."""
    _require_configured()
    drive_id = await get_migration_drive_id()
    children = await migration_common.list_child_folders(drive_id, path)
    return {"path": (path or "").strip("/"), "folders": children["folders"], "files": children["files"]}


async def list_vessels() -> dict:
    """Vessel folders under the configured destination root, on the
    destination site — powers the destination vessel picker."""
    _require_configured()
    drive_id = await migration_common.get_destination_drive_id()
    folders = await migration_common.list_vessel_folders(drive_id)
    return {"destination_root": settings.destination_root, "vessels": folders}


async def start_scan(
    source_folder: str, subfolders: list[str], vessel_path: str, files: list[str] | None = None
) -> dict:
    """Kick off a scan of exactly the checked subfolders under `source_folder`
    (each fully, including everything nested beneath it), plus any
    individually-checked loose files sitting directly in `source_folder`
    alongside those subfolders. If neither a subfolder nor a file was
    checked (this folder holds files directly and nothing was singled out),
    the source folder itself is scanned as a whole. Every discovered
    document is classified against `vessel_path`'s existing folder
    structure."""
    _require_configured()
    source_folder = source_folder.strip("/")
    if not source_folder:
        raise BadRequest("A source folder must be selected")
    vessel_path = vessel_path.strip("/")
    if not vessel_path:
        raise BadRequest("A destination vessel must be selected")
    files = [f.strip("/") for f in (files or []) if f.strip("/")]

    source_drive_id = await get_migration_drive_id()
    dest_drive_id = await migration_common.get_destination_drive_id()
    # 404s early if any of these are invalid, rather than failing deep inside
    # the background scan task.
    await migration_common.resolve_folder_path(source_drive_id, source_folder)
    if subfolders:
        subfolder_paths = [f"{source_folder}/{s.strip('/')}" for s in subfolders]
    elif files:
        # Individual files were singled out instead — don't fall back to
        # scanning the whole source folder, that would sweep in everything
        # the user deliberately left unchecked.
        subfolder_paths = []
    else:
        subfolder_paths = [source_folder]
    for p in subfolder_paths:
        await migration_common.resolve_folder_path(source_drive_id, p)
    # The vessel folder itself lives on the destination site.
    vessel = await migration_common.resolve_folder_path(dest_drive_id, vessel_path)
    # Picking the destination root itself (rather than one of its vessel
    # subfolders) means "figure out each file's vessel automatically" — see
    # classifier/migration_classifier.py and services/migration_mover.py.
    auto_detect_vessel = vessel_path == settings.destination_root.strip("/")

    job_id = migration_scanner.create_job(
        source_folder=source_folder,
        subfolders=subfolders,
        files=files,
        vessel_name=vessel_path.rsplit("/", 1)[-1],
        vessel_path=vessel_path,
        vessel_folder_id=vessel["id"],
        auto_detect_vessel=auto_detect_vessel,
    )
    asyncio.create_task(migration_scanner.run_scan(job_id, subfolder_paths, source_folder, files))
    return _job_public(_get_job_row(str(job_id)))


async def get_scan_job(job_id: str) -> dict:
    _require_configured()
    return _job_public(_get_job_row(job_id))


async def list_jobs() -> list[dict]:
    """Every scan job run so far, most recent first — lets a reviewer get
    back to a previous scan's preview table instead of re-scanning (which
    would find 0 new files, since files are deduplicated by SharePoint id)."""
    _require_configured()
    with SessionLocal() as db:
        jobs = db.query(models.MigrationScanJob).order_by(models.MigrationScanJob.id.desc()).all()
        return [_job_public(j) for j in jobs]


async def get_job_items(job_id: str) -> dict:
    """The preview-table data for one job — every item discovered by its
    scan, with its current (possibly overridden) suggestion."""
    _require_configured()
    job = _get_job_row(job_id)
    with SessionLocal() as db:
        rows = (
            db.query(models.MigrationItem)
            .filter_by(job_id=job.id)
            .order_by(models.MigrationItem.created_at.asc())
            .all()
        )
        items = [_item_public(r) for r in rows]
    return {"job": _job_public(job), "items": items}


async def get_job_hierarchy(job_id: str) -> dict:
    """The destination vessel's existing folder tree, relative paths — used
    to populate the preview table's per-row override dropdown."""
    _require_configured()
    from .migration_hierarchy import discover_vessel_hierarchy

    job = _get_job_row(job_id)
    drive_id = await migration_common.get_destination_drive_id()
    # An auto-detect job's own vessel_path is just the destination root, not
    # any one vessel — the template vessel's structure is the best available
    # stand-in for the override dropdown (every item is classified against
    # either a real vessel's hierarchy or this same template — see
    # classifier/migration_classifier.py). Per-item, per-detected-vessel
    # overrides aren't supported yet (see migration_mover.override_item's
    # own per-item resolution, used once an override is actually submitted).
    vessel_path = (
        f"{settings.destination_root}/{settings.template_vessel_name}"
        if job.auto_detect_vessel and settings.template_vessel_name
        else job.vessel_path
    )
    hierarchy = await discover_vessel_hierarchy(drive_id, vessel_path)
    return {"tree": hierarchy["tree"], "paths": hierarchy["paths"]}


async def override_item(job_id: str, item_id: str, target_path: str) -> dict:
    _require_configured()
    _get_item_in_job(job_id, item_id)  # validates it belongs to this job
    await migration_mover.override_item(item_id, target_path)
    return _item_public(_get_item_in_job(job_id, item_id))


async def reclassify_item(job_id: str, item_id: str) -> dict:
    _require_configured()
    from ..classifier import migration_classifier

    _get_item_in_job(job_id, item_id)
    await migration_classifier.classify_item(int(item_id))
    return _item_public(_get_item_in_job(job_id, item_id))


async def get_item_preview(job_id: str, item_id: str):
    _require_configured()
    row = _get_item_in_job(job_id, item_id)
    drive_id = await get_migration_drive_id()
    content, content_type, name = await gd.download_file(drive_id, row.source_drive_item_id)
    return content, content_type, name


async def confirm_job(job_id: str, decided_by_email: str) -> dict:
    _require_configured()
    _get_job_row(job_id)  # 404s early if invalid
    return await migration_mover.confirm_job(job_id, decided_by_email)
