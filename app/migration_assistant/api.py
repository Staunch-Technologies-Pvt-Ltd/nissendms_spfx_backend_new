"""HTTP routes for the Migration Assistant, mounted under /api/migration-assistant.

Ported from the standalone project's backend/app/main.py. All routes require
a valid DMS session (`require_session`, same dependency every other DMS
router uses). Preview / Excel-export downloads may pass ``?session_id=`` since
`require_session` accepts it as a fallback to the X-Session-ID header.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import settings
from .db import SessionLocal, init_db
from .graph.client import GraphError
from .models import db_models as models
from .services import existing_file_tagger, migration_service, site_to_site_service, vessel_excel_export
from .services.errors import BadRequest, Conflict, NotFound

logger = logging.getLogger("migration_assistant")

PREFIX = "/api/migration-assistant"


def _raise(e: Exception):
    """Map domain exceptions to HTTP errors."""
    status = getattr(e, "status", None)
    if status:
        raise HTTPException(status, str(e))
    raise e


class ScanIn(BaseModel):
    source_folder: str
    subfolders: list[str]
    vessel_path: str
    files: list[str] = []


class OverrideIn(BaseModel):
    target_path: str


class SiteToSiteScanIn(BaseModel):
    source_site_key: str
    source_drive_id: str
    source_folder_path: str
    selected_folders: list[str] = []
    selected_files: list[str] = []
    dest_site_key: str
    dest_drive_id: str
    dest_folder_path: str


class ExistingFilesScanIn(BaseModel):
    root_path: str


class ExistingFilesApplyIn(BaseModel):
    root_path: str
    file_ids: list[str]


def _fail_orphaned_jobs() -> None:
    """A scan runs as an in-process background task — if the server restarts
    mid-scan the job row stays "running" forever. A fresh process can't have a
    legitimately running job yet, so mark leftovers failed at startup."""
    with SessionLocal() as db:
        now = datetime.utcnow()
        orphaned = db.query(models.MigrationScanJob).filter_by(status="running").all()
        orphaned_s2s = db.query(models.SiteToSiteJob).filter_by(status="running").all()
        for job in [*orphaned, *orphaned_s2s]:
            job.status = "failed"
            job.error = "Interrupted by a server restart — never completed. Re-scan this folder to retry."
            job.finished_at = now
        if orphaned or orphaned_s2s:
            db.commit()


def startup() -> None:
    """Create the module's own tables and clean up orphaned jobs. Never raises:
    a Migration Assistant problem must not stop the main DMS from booting."""
    try:
        init_db()
        _fail_orphaned_jobs()
        logger.info("Migration Assistant ready (graph_configured=%s)", settings.graph_configured)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Migration Assistant startup failed: %s", exc)


def build_router(require_session: Callable) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["migration-assistant"], dependencies=[Depends(require_session)])

    @router.get("/health")
    def health():
        return {"status": "ok", "configured": settings.graph_configured}

    # ── Same-site migration ────────────────────────────────────────────────
    @router.get("/migration/source-folders")
    async def list_source_folders(path: str | None = None):
        try:
            return await migration_service.list_source_folders(path)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/migration/vessels")
    async def list_vessels():
        try:
            return await migration_service.list_vessels()
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/vessel-export/excel")
    async def export_vessel_excel(vessel_path: str):
        """One downloadable .xlsx per vessel. Read-only."""
        try:
            content, filename = await vessel_excel_export.build_vessel_workbook(vessel_path)
        except (NotFound, BadRequest) as e:
            _raise(e)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/tag-existing/scan")
    async def scan_existing_files(payload: ExistingFilesScanIn):
        try:
            return await existing_file_tagger.scan(payload.root_path)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.post("/tag-existing/apply")
    async def apply_existing_file_tags(payload: ExistingFilesApplyIn):
        try:
            return await existing_file_tagger.apply(payload.root_path, payload.file_ids)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/tag-existing/jobs")
    async def list_tagged_scan_jobs():
        try:
            return await existing_file_tagger.list_recent_scans()
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/tag-existing/jobs/{job_id}")
    async def get_tagged_scan_job(job_id: str):
        try:
            return await existing_file_tagger.get_recent_scan(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/migration/jobs")
    async def list_migration_jobs():
        try:
            return await migration_service.list_jobs()
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.post("/migration/scan")
    async def start_migration_scan(payload: ScanIn):
        try:
            return await migration_service.start_scan(
                payload.source_folder, payload.subfolders, payload.vessel_path, payload.files
            )
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.get("/migration/scan/{job_id}")
    async def get_migration_scan(job_id: str):
        try:
            return await migration_service.get_scan_job(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/migration/jobs/{job_id}/items")
    async def list_job_items(job_id: str):
        try:
            return await migration_service.get_job_items(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/migration/jobs/{job_id}/hierarchy")
    async def get_job_hierarchy(job_id: str):
        try:
            return await migration_service.get_job_hierarchy(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.post("/migration/jobs/{job_id}/items/{item_id}/override")
    async def override_job_item(job_id: str, item_id: str, payload: OverrideIn):
        try:
            return await migration_service.override_item(job_id, item_id, payload.target_path)
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.post("/migration/jobs/{job_id}/items/{item_id}/classify")
    async def classify_job_item(job_id: str, item_id: str):
        try:
            return await migration_service.reclassify_item(job_id, item_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/migration/jobs/{job_id}/items/{item_id}/preview")
    async def preview_job_item(job_id: str, item_id: str):
        try:
            result = await migration_service.get_item_preview(job_id, item_id)
        except (NotFound, BadRequest) as e:
            _raise(e)
        if result is None:
            raise HTTPException(404, "Source file not found")
        content, content_type, name = result
        return Response(
            content=content,
            media_type=content_type,
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )

    @router.post("/migration/jobs/{job_id}/confirm")
    async def confirm_migration_job(job_id: str, user: str = Header(default="unknown", alias="X-User-Email")):
        """The one action that actually moves files in SharePoint."""
        try:
            return await migration_service.confirm_job(job_id, user)
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    # ── Site-to-Site migration ─────────────────────────────────────────────
    @router.get("/site-to-site/sites")
    async def list_s2s_sites():
        try:
            return await site_to_site_service.list_sites()
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/site-to-site/sites/{site_key}/drives")
    async def list_s2s_site_drives(site_key: str):
        try:
            return await site_to_site_service.list_site_drives(site_key)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/site-to-site/browse")
    async def browse_s2s_folder(site_key: str, drive_id: str, path: str | None = None):
        try:
            return await site_to_site_service.browse_folder(site_key, drive_id, path)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/site-to-site/jobs")
    async def list_s2s_jobs():
        try:
            return await site_to_site_service.list_jobs()
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.post("/site-to-site/scan")
    async def start_s2s_scan(payload: SiteToSiteScanIn):
        try:
            return await site_to_site_service.start_scan(
                payload.source_site_key, payload.source_drive_id, payload.source_folder_path,
                payload.selected_folders, payload.selected_files,
                payload.dest_site_key, payload.dest_drive_id, payload.dest_folder_path,
            )
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.get("/site-to-site/jobs/{job_id}")
    async def get_s2s_job(job_id: str):
        try:
            return await site_to_site_service.get_scan_job(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/site-to-site/jobs/{job_id}/items")
    async def list_s2s_job_items(job_id: str):
        try:
            return await site_to_site_service.get_job_items(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.post("/site-to-site/jobs/{job_id}/confirm")
    async def confirm_s2s_job(job_id: str, user: str = Header(default="unknown", alias="X-User-Email")):
        """Copies files (and migrates metadata) into the destination site.
        The source site is only ever read."""
        try:
            return await site_to_site_service.confirm_job(job_id, user)
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    return router


def setup(app: FastAPI, require_session: Callable) -> None:
    """One-call integration used by app/main.py: routes + startup + Graph error handler."""

    @app.exception_handler(GraphError)
    async def _migration_graph_error_handler(request: Request, exc: GraphError):
        return JSONResponse(status_code=502, content={"detail": f"Microsoft Graph error: {exc}"})

    app.include_router(build_router(require_session))
    app.router.on_startup.append(startup)
