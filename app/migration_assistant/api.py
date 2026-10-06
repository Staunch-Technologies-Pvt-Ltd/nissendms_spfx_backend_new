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
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import settings
from .db import SessionLocal, init_db
from .graph.client import GraphError
from .models import db_models as models
from .services import existing_file_tagger, migration_service, site_to_site_service, vessel_excel_export
from .services.errors import BadRequest, Conflict, NotFound

logger = logging.getLogger("migration_assistant")

PREFIX = "/api/migration-assistant"


class _JsonErrorRoute(APIRoute):
    """Turn an unexpected exception in a Migration Assistant route into a
    JSON 500 with the reason. Without this the error escapes to Starlette's
    outermost error handler, whose plain 500 carries no CORS headers — the
    browser then reports a network failure and the UI can only show a
    generic "Could not load …" with no clue what went wrong."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except (StarletteHTTPException, RequestValidationError, GraphError):
                raise  # already turned into proper JSON responses
            except Exception as exc:
                logger.exception("Migration Assistant %s %s failed", request.method, request.url.path)
                return JSONResponse(
                    status_code=500,
                    content={"detail": f"Migration Assistant server error ({type(exc).__name__}): {exc}"},
                )

        return handler


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
    # Picked source site (a Site Management / site-picker key) and library;
    # omitted = the configured default source.
    source_site_key: str | None = None
    source_drive_id: str | None = None


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


class SiteToSiteConfirmIn(BaseModel):
    # skip | replace | rename (keep both) | fail — for files that already
    # exist at the destination.
    conflict_policy: str = "skip"
    copy_permissions: bool = False
    copy_versions: bool = False


class SiteUrlIn(BaseModel):
    url: str


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
        # A copy that was running or paused when the server stopped can be
        # resumed — every finished item is already recorded per item.
        interrupted = (
            db.query(models.SiteToSiteJob)
            .filter(models.SiteToSiteJob.copy_status.in_(("running", "paused")))
            .all()
        )
        for job in interrupted:
            job.copy_status = "interrupted"
        stuck_verify = db.query(models.SiteToSiteJob).filter_by(verify_status="running").all()
        for job in stuck_verify:
            job.verify_status = "failed"
        if orphaned or orphaned_s2s or interrupted or stuck_verify:
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
    router = APIRouter(
        prefix=PREFIX, tags=["migration-assistant"], dependencies=[Depends(require_session)],
        route_class=_JsonErrorRoute,
    )

    def acting_user(
        session=Depends(require_session),
        x_user_email: str = Header(default="unknown", alias="X-User-Email"),
    ) -> str:
        """Who to record as having confirmed a copy: the signed-in session's
        own email when there is one, the client-sent header only as a
        fallback (stub/dev mode has no session)."""
        return getattr(session, "email", None) or x_user_email

    @router.get("/health")
    def health():
        return {"status": "ok", "configured": settings.graph_configured}

    # ── Same-site migration ────────────────────────────────────────────────
    @router.get("/migration/source-folders")
    async def list_source_folders(path: str | None = None, site_key: str | None = None, drive_id: str | None = None):
        try:
            return await migration_service.list_source_folders(path, site_key, drive_id)
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
                payload.source_folder, payload.subfolders, payload.vessel_path, payload.files,
                payload.source_site_key, payload.source_drive_id,
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

    @router.get("/site-to-site/sites/search")
    async def search_s2s_sites(q: str):
        try:
            return await site_to_site_service.search_sites(q)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.post("/site-to-site/sites/resolve")
    async def resolve_s2s_site(payload: SiteUrlIn):
        try:
            return await site_to_site_service.resolve_site_url(payload.url)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/site-to-site/drives")
    async def list_s2s_site_drives_by_key(site_key: str):
        """Same as /sites/{site_key}/drives, but with the key as a query
        parameter — URL-based site keys contain slashes."""
        try:
            return await site_to_site_service.list_site_drives(site_key)
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

    @router.post("/site-to-site/jobs/{job_id}/confirm", status_code=202)
    async def confirm_s2s_job(job_id: str, payload: SiteToSiteConfirmIn | None = None, user: str = Depends(acting_user)):
        """Starts copying files (and metadata) into the destination site in
        the background and returns the job immediately — follow it with
        /stream. The source site is only ever read. Also retries a finished
        job's failed/remaining items."""
        options = payload or SiteToSiteConfirmIn()
        try:
            return await site_to_site_service.confirm_job(
                job_id, user,
                site_to_site_service.CopyOptions(options.conflict_policy, options.copy_permissions, options.copy_versions),
            )
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.post("/site-to-site/jobs/{job_id}/pause")
    async def pause_s2s_job(job_id: str):
        try:
            return await site_to_site_service.pause_job(job_id)
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.post("/site-to-site/jobs/{job_id}/resume")
    async def resume_s2s_job(job_id: str, user: str = Depends(acting_user)):
        try:
            return await site_to_site_service.resume_job(job_id, user)
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.post("/site-to-site/jobs/{job_id}/cancel")
    async def cancel_s2s_job(job_id: str):
        try:
            return await site_to_site_service.cancel_job(job_id)
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)

    @router.get("/site-to-site/jobs/{job_id}/stream")
    async def stream_s2s_job(job_id: str):
        """Live copy progress as NDJSON (one JSON object per line)."""
        try:
            await site_to_site_service.get_scan_job(job_id)  # 404 before the stream opens
        except (NotFound, BadRequest) as e:
            _raise(e)
        return StreamingResponse(
            site_to_site_service.stream_progress(job_id),
            media_type="application/x-ndjson",
            # Stop proxies (nginx etc.) from buffering the stream.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/site-to-site/jobs/{job_id}/verify")
    async def verify_s2s_job(job_id: str):
        try:
            return await site_to_site_service.verify_job(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)

    @router.get("/site-to-site/jobs/{job_id}/report")
    async def s2s_job_report(job_id: str):
        try:
            content, filename = site_to_site_service.build_report(job_id)
        except (NotFound, BadRequest) as e:
            _raise(e)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router


def setup(app: FastAPI, require_session: Callable) -> None:
    """One-call integration used by app/main.py: routes + startup + Graph error handler."""

    @app.exception_handler(GraphError)
    async def _migration_graph_error_handler(request: Request, exc: GraphError):
        return JSONResponse(status_code=502, content={"detail": f"Microsoft Graph error: {exc}"})

    app.include_router(build_router(require_session))
    app.router.on_startup.append(startup)
