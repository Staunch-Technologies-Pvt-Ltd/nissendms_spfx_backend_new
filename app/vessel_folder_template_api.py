"""REST API for Settings -> Vessel Settings -> Vessel Folder Template.

The sub-folders created automatically inside every new vessel folder
(services/vessel_folder_template.py). Reads are open to any signed-in user
(the Create Vessel form previews the structure); changes are admin-only,
same admin model as folder_structure_api.

  GET  /api/vessel-folder-template            current template + default + is_admin
  PUT  /api/vessel-folder-template            {folders}            (admin)
  POST /api/vessel-folder-template/reset      back to the default  (admin)
  GET  /api/vessel-folder-template/vessels    vessels it can be applied to
  POST /api/vessel-folder-template/apply      {vessel_ids|null, dry_run} (admin)
       — adds missing template folders to existing vessels; never deletes
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .folder_structure_api import _email, _is_admin
from .services import vessel_folder_template as vft

log = logging.getLogger(__name__)


class TemplateIn(BaseModel):
    folders: list[Any]


class ApplyIn(BaseModel):
    vessel_ids: list[int] | None = None  # None = every vessel with a folder
    dry_run: bool = True


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/vessel-folder-template", tags=["vessel-folder-template"])

    def admin_email(session, x_user_email: str | None) -> str:
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Only administrators can change the vessel folder template")
        return _email(session, x_user_email)

    @router.get("")
    async def get_template(session=Depends(require_session), x_user_email: str | None = Header(default=None)):
        tpl = vft.get_template()
        return {
            **tpl,
            "folder_count": vft.count_folders(tpl["folders"]),
            "default_folders": vft.validate(vft.DEFAULT_FOLDERS),
            "is_admin": _is_admin(session, x_user_email),
            "rules": {"invalid_chars": vft.INVALID_CHARS, "max_depth": vft.MAX_DEPTH, "max_name": vft.MAX_NAME},
        }

    @router.put("")
    async def put_template(body: TemplateIn, session=Depends(require_session), x_user_email: str | None = Header(default=None)):
        email = admin_email(session, x_user_email)
        try:
            saved = vft.save_template(body.folders, email)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {**saved, "folder_count": vft.count_folders(saved["folders"])}

    @router.post("/reset")
    async def reset_template(session=Depends(require_session), x_user_email: str | None = Header(default=None)):
        saved = vft.reset_template(admin_email(session, x_user_email))
        return {**saved, "folder_count": vft.count_folders(saved["folders"])}

    @router.get("/locate/{vessel_id}")
    async def locate(vessel_id: int, session=Depends(require_session)):
        """Where a vessel's folder is; finds and saves it by name if unknown."""
        return await vft.locate_vessel_folder(vessel_id)

    @router.get("/vessels")
    async def list_vessels(session=Depends(require_session)):
        from .db import models
        from .db.base import SessionLocal

        with SessionLocal() as db:
            ship_ids = {
                vid for (vid,) in db.query(models.Folder.vessel_id).execution_options(all_sites=True)
                .filter(models.Folder.kind == "ship", models.Folder.vessel_id.isnot(None)).distinct()
            }
            rows = db.query(models.Vessel).order_by(models.Vessel.name).all()
            return [
                {
                    "id": v.id, "name": v.name, "site_key": v.provisioned_site_key,
                    "folder_path": v.vessel_folder_path,
                    # "recorded" = location known; "by_name" = will be looked up by name
                    "folder": "recorded" if ((v.provisioned_site_key and v.vessel_folder_path) or v.id in ship_ids) else "by_name",
                }
                for v in rows
            ]

    @router.post("/apply")
    async def apply_template(body: ApplyIn, session=Depends(require_session), x_user_email: str | None = Header(default=None)):
        email = admin_email(session, x_user_email)
        ids = body.vessel_ids
        if ids is None:
            ids = [v["id"] for v in await list_vessels(session)]
        sem = asyncio.Semaphore(4)

        async def one(vid: int) -> dict:
            async with sem:
                try:
                    return await vft.ensure_for_vessel(vid, dry_run=body.dry_run)
                except Exception as exc:  # noqa: BLE001
                    log.exception("[vessel-folder-template] apply failed for vessel %s", vid)
                    return {"vessel_id": vid, "ok": False, "error": str(exc)[:300]}

        results = await asyncio.gather(*(one(v) for v in ids))
        if not body.dry_run:
            log.info("[vessel-folder-template] applied to %d vessel(s) by %s: %d folders created",
                     len(results), email, sum(len(r.get("created") or []) for r in results))
        return {
            "dry_run": body.dry_run,
            "vessels": len(results),
            "folders_to_create" if body.dry_run else "folders_created": sum(len(r.get("created") or []) for r in results),
            "results": results,
        }

    return router
