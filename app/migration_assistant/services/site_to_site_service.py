"""Facade for Site-to-Site migration — the single entry point
`backend/app/main.py`'s new route group calls into, mirroring
`migration_service.py`'s shape for the existing same-site flow. Entirely
separate data path from that module: nothing here reads or writes
`MigrationScanJob`/`MigrationItem`.
"""
from __future__ import annotations

import asyncio
import json

from ..graph import site as gsite
from ..models import db_models as models

from ..db import SessionLocal
from ..config import settings

from . import site_to_site_common as s2s_common
from . import site_to_site_mover, site_to_site_scanner
from .errors import BadRequest, NotFound


def _require_configured() -> None:
    if not settings.graph_configured:
        raise BadRequest(
            "Not configured — set AZURE_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET (see README.md)."
        )
    if not s2s_common.allowed_sites():
        raise BadRequest("No sites configured for Site-to-Site migration — set ALLOWED_SITES (see README.md).")


async def list_sites() -> list[dict]:
    _require_configured()
    return [{"key": s["key"], "label": s.get("label", s["key"])} for s in s2s_common.allowed_sites()]


async def list_site_drives(site_key: str) -> list[dict]:
    """Auto-provisions a "Documents" library if the site has none yet (a
    genuinely blank destination) — see `graph.site.ensure_site_drives`."""
    _require_configured()
    site = await s2s_common.resolve_site(site_key)
    return await gsite.ensure_site_drives(site["site_id"])


async def browse_folder(site_key: str, drive_id: str, path: str | None) -> dict:
    _require_configured()
    await s2s_common.resolve_site(site_key)  # validates the site key
    return await s2s_common.list_child_folders(drive_id, path)


def _item_public(row: models.SiteToSiteItem) -> dict:
    return {
        "id": str(row.id),
        "job_id": str(row.job_id),
        "kind": row.kind,
        "relative_path": row.relative_path,
        "name": row.name,
        "size": row.size,
        "status": row.status,
        "dest_item_id": row.dest_item_id,
        "metadata_report": json.loads(row.metadata_report) if row.metadata_report else None,
        "error": row.error,
    }


def _job_public(job: models.SiteToSiteJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "source_site_key": job.source_site_key,
        "source_folder_path": job.source_folder_path,
        "selected_folders": json.loads(job.selected_folders) if job.selected_folders else [],
        "selected_files": json.loads(job.selected_files) if job.selected_files else [],
        "dest_site_key": job.dest_site_key,
        "dest_folder_path": job.dest_folder_path,
        "total_found": job.total_found,
        "processed": job.processed,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "confirmed_at": job.confirmed_at.isoformat() if job.confirmed_at else None,
        "confirmed_by_email": job.confirmed_by_email,
        "error": job.error,
    }


def _get_job_row(job_id: str) -> models.SiteToSiteJob:
    if not job_id.isdigit():
        raise NotFound("Copy job not found")
    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, int(job_id))
        if job is None:
            raise NotFound("Copy job not found")
        db.expunge(job)
        return job


async def start_scan(
    source_site_key: str, source_drive_id: str, source_folder_path: str,
    selected_folders: list[str], selected_files: list[str],
    dest_site_key: str, dest_drive_id: str, dest_folder_path: str,
) -> dict:
    _require_configured()
    source_folder_path = source_folder_path.strip("/")
    dest_folder_path = dest_folder_path.strip("/")
    selected_folders = [f.strip("/") for f in selected_folders if f.strip("/")]
    selected_files = [f.strip("/") for f in selected_files if f.strip("/")]
    if not selected_folders and not selected_files:
        raise BadRequest("Select at least one folder or file to copy")
    # dest_folder_path may be "" — that means the destination library's root,
    # which is a valid target (e.g. copying into a brand-new, empty library).

    await s2s_common.resolve_site(source_site_key)
    await s2s_common.resolve_site(dest_site_key)
    # 404s early if any selection is invalid, rather than failing deep inside
    # the background scan task.
    await s2s_common.resolve_folder_path(source_drive_id, source_folder_path)
    for rel in selected_folders:
        await s2s_common.resolve_folder_path(source_drive_id, f"{source_folder_path}/{rel}" if source_folder_path else rel)
    for rel in selected_files:
        await s2s_common.resolve_item_path(source_drive_id, f"{source_folder_path}/{rel}" if source_folder_path else rel)
    dest_folder = await s2s_common.resolve_folder_path(dest_drive_id, dest_folder_path)

    job_id = site_to_site_scanner.create_job(
        source_site_key=source_site_key,
        source_drive_id=source_drive_id,
        source_folder_path=source_folder_path,
        selected_folders=selected_folders,
        selected_files=selected_files,
        dest_site_key=dest_site_key,
        dest_drive_id=dest_drive_id,
        dest_folder_path=dest_folder_path,
        dest_folder_id=dest_folder["id"],
    )
    asyncio.create_task(
        site_to_site_scanner.run_scan(job_id, source_drive_id, source_folder_path, selected_folders, selected_files)
    )
    return _job_public(_get_job_row(str(job_id)))


async def get_scan_job(job_id: str) -> dict:
    _require_configured()
    return _job_public(_get_job_row(job_id))


async def list_jobs() -> list[dict]:
    _require_configured()
    with SessionLocal() as db:
        jobs = db.query(models.SiteToSiteJob).order_by(models.SiteToSiteJob.id.desc()).all()
        return [_job_public(j) for j in jobs]


async def get_job_items(job_id: str) -> dict:
    _require_configured()
    job = _get_job_row(job_id)
    with SessionLocal() as db:
        rows = (
            db.query(models.SiteToSiteItem)
            .filter_by(job_id=job.id)
            .order_by(models.SiteToSiteItem.relative_path.asc())
            .all()
        )
        items = [_item_public(r) for r in rows]
    return {"job": _job_public(job), "items": items}


async def confirm_job(job_id: str, decided_by_email: str) -> dict:
    _require_configured()
    _get_job_row(job_id)  # 404s early if invalid
    return await site_to_site_mover.confirm_job(job_id, decided_by_email)
