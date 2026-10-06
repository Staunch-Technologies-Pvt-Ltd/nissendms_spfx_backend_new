"""Facade for Site-to-Site migration — the single entry point
`backend/app/main.py`'s new route group calls into, mirroring
`migration_service.py`'s shape for the existing same-site flow. Entirely
separate data path from that module: nothing here reads or writes
`MigrationScanJob`/`MigrationItem`.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from sqlalchemy import func

from ..graph import site as gsite
from ..graph.client import GraphError
from ..models import db_models as models

from ..db import SessionLocal
from ..config import settings

from . import site_to_site_common as s2s_common
from . import site_to_site_mover, site_to_site_progress, site_to_site_scanner, site_to_site_verify
from .errors import BadRequest, NotFound


def _require_configured() -> None:
    if not settings.graph_configured:
        raise BadRequest(
            "Not configured — set AZURE_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET (see README.md)."
        )


async def list_sites() -> list[dict]:
    """Sites for the pickers: every site in the DMS's Site Management (so a
    site added there appears here automatically), then any `ALLOWED_SITES`
    entry not already listed. Read fresh on every call. Any other site can
    be found with `search_sites` or `resolve_site_url`."""
    _require_configured()
    return [
        {
            "key": s["key"],
            "label": s.get("label", s["key"]),
            "url": s.get("url") or f"https://{s.get('hostname', '')}/{s.get('site_path', '').strip('/')}".rstrip("/"),
            "origin": s.get("origin", "allowed_sites"),
        }
        for s in s2s_common.picker_sites()
    ]


async def search_sites(query: str) -> list[dict]:
    _require_configured()
    query = query.strip()
    if len(query) < 2:
        raise BadRequest("Type at least 2 characters to search")
    try:
        found = await gsite.search_sites(query)
    except GraphError as e:
        if e.status in (401, 403):
            raise BadRequest(
                "This app isn't allowed to search the tenant's sites (it needs Sites.Read.All). "
                "Paste the site's URL instead."
            ) from e
        raise
    return [{"key": s2s_common.site_key_for_url(s["url"]), "label": s["label"], "url": s["url"]} for s in found]


async def resolve_site_url(url: str) -> dict:
    """Look up one site by URL and confirm the app can actually read it."""
    _require_configured()
    hostname, site_path = s2s_common.parse_site_url(url)
    try:
        site = await gsite.get_site_by_url(hostname, site_path)
    except GraphError as e:
        if e.status in (403, 404):
            raise NotFound(
                "Site not found, or this app hasn't been granted access to it "
                "(Sites.Selected needs a per-site grant — read to copy from it, write to copy into it)."
            ) from e
        raise
    return {"key": s2s_common.site_key_for_url(site["url"] or url), "label": site["label"], "url": site["url"]}


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
        "permissions_report": json.loads(row.permissions_report) if row.permissions_report else None,
        "verify_status": row.verify_status,
        "verify_detail": row.verify_detail,
        "error": row.error,
    }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _site_label(key: str) -> str:
    try:
        return s2s_common.find_site(key).get("label", key)
    except Exception:
        return key


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
        "source_site_label": _site_label(job.source_site_key),
        "dest_site_label": _site_label(job.dest_site_key),
        # Jobs confirmed before copy runs were tracked have no copy_status.
        "copy_status": job.copy_status or ("completed" if job.confirmed_at else None),
        "conflict_policy": job.conflict_policy,
        "copy_permissions": bool(job.copy_permissions),
        "copy_versions": bool(job.copy_versions),
        "copy_started_at": _iso(job.copy_started_at),
        "copy_finished_at": _iso(job.copy_finished_at),
        "copy_summary": json.loads(job.copy_summary) if job.copy_summary else None,
        "verify_status": job.verify_status,
        "verified_at": _iso(job.verified_at),
        "verify_summary": json.loads(job.verify_summary) if job.verify_summary else None,
        "live": site_to_site_progress.is_active(job.id),
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


class CopyOptions:
    def __init__(self, conflict_policy: str = "skip", copy_permissions: bool = False, copy_versions: bool = False):
        self.conflict_policy = conflict_policy
        self.copy_permissions = copy_permissions
        self.copy_versions = copy_versions


async def confirm_job(job_id: str, decided_by_email: str, options: CopyOptions) -> dict:
    """Start the copy in the background and return straight away — follow
    it with `stream_progress` (or `get_scan_job`)."""
    _require_configured()
    await site_to_site_mover.start_copy(
        job_id, decided_by_email,
        conflict_policy=options.conflict_policy,
        copy_permissions=options.copy_permissions,
        copy_versions=options.copy_versions,
    )
    return _job_public(_get_job_row(job_id))


async def pause_job(job_id: str) -> dict:
    _require_configured()
    site_to_site_mover.pause(job_id)
    return _job_public(_get_job_row(job_id))


async def resume_job(job_id: str, decided_by_email: str) -> dict:
    _require_configured()
    await site_to_site_mover.resume(job_id, decided_by_email)
    return _job_public(_get_job_row(job_id))


async def cancel_job(job_id: str) -> dict:
    _require_configured()
    await site_to_site_mover.cancel(job_id)
    return _job_public(_get_job_row(job_id))


async def verify_job(job_id: str) -> dict:
    """Re-run verification on demand (it also runs automatically at the end
    of every copy)."""
    _require_configured()
    job = _get_job_row(job_id)
    if site_to_site_progress.is_active(job.id):
        raise BadRequest("Wait for the copy to finish before verifying")
    await site_to_site_verify.verify_job(job.id)
    return _job_public(_get_job_row(job_id))


def build_report(job_id: str) -> tuple[bytes, str]:
    job = _get_job_row(job_id)
    return site_to_site_verify.build_report(job.id)


def _db_progress(job: models.SiteToSiteJob) -> dict:
    """Progress rebuilt from the item table — used when no live run exists in
    this process (finished/interrupted jobs, or another worker owns it)."""
    item = models.SiteToSiteItem
    with SessionLocal() as db:
        rows = (
            db.query(item.kind, item.status, func.count(), func.coalesce(func.sum(item.size), 0))
            .filter(item.job_id == job.id)
            .group_by(item.kind, item.status)
            .all()
        )
    done = ("copied", "metadata_done", "metadata_attention")
    c = {"files_total": 0, "files_done": 0, "files_failed": 0, "files_skipped": 0, "bytes_total": 0, "bytes_done": 0,
         "folders_total": 0, "folders_done": 0, "folders_failed": 0, "metadata_attention": 0}
    for kind, status, count, size in rows:
        if kind == "file":
            c["files_total"] += count
            c["bytes_total"] += int(size)
            if status in done:
                c["files_done"] += count
                c["bytes_done"] += int(size)
            if status == "failed":
                c["files_failed"] += count
            if status == "skipped":
                c["files_skipped"] += count
            if status == "metadata_attention":
                c["metadata_attention"] += count
        else:
            c["folders_total"] += count
            if status == "copied":
                c["folders_done"] += count
            if status == "failed":
                c["folders_failed"] += count
    summary = json.loads(job.copy_summary) if job.copy_summary else {}
    terminal = job.copy_status in site_to_site_progress.TERMINAL_STATES
    return {
        "type": "progress", "job_id": str(job.id), "state": job.copy_status or "not_started",
        "phase": "verifying" if job.verify_status == "running" else ("done" if terminal else "files"),
        **c,
        "permissions_attention": summary.get("permissions_attention", 0),
        "verify_total": 0, "verify_done": 0, "speed_bps": 0, "eta_seconds": None,
        "elapsed_seconds": summary.get("elapsed_seconds", 0), "in_flight": [], "events": [], "seq": 0,
    }


async def stream_progress(job_id: str) -> AsyncIterator[str]:
    """NDJSON progress stream for one job: a snapshot line whenever
    something changes (at most ~4 per second, at least every 15 s as a
    heartbeat), and a final {"type": "done", "job": ...} line once the run
    ends. Each line carries only the item events the client hasn't seen."""
    _require_configured()
    job = _get_job_row(job_id)
    seq = 0
    while True:
        run = site_to_site_progress.get(job.id)
        if run is not None and site_to_site_progress.is_active(job.id):
            snap = run.snapshot(since_seq=seq)
            seq = snap["seq"]
            yield json.dumps(snap) + "\n"
            await run.wait_for_change(timeout=15)
            await asyncio.sleep(0.25)  # coalesce bursts of tiny changes
            continue
        fresh = _get_job_row(job_id)
        if run is not None and run.task is not None and run.task.done():
            # Our own run just ended: send its final live counters once.
            snap = run.snapshot(since_seq=seq)
            snap["state"] = fresh.copy_status or snap["state"]
            yield json.dumps(snap) + "\n"
        elif fresh.copy_status in ("running", "paused"):
            # Owned by another worker process: fall back to DB polling.
            yield json.dumps(_db_progress(fresh)) + "\n"
            await asyncio.sleep(2)
            continue
        else:
            yield json.dumps(_db_progress(fresh)) + "\n"
        yield json.dumps({"type": "done", "job": _job_public(fresh)}) + "\n"
        return
