"""REST API for Settings → Vessel Settings → Folder Structure Mode.

Mounted from main.py via build_router(require_session) so it reuses the
existing session dependency without an import cycle. Admin-only writes,
using the existing authorization model (session role / ADMIN_EMAILS).

  GET  /api/folder-structure/config        modes, default, vessels, site, guard
  GET  /api/folder-structure/template      the editable template JSON
  PUT  /api/folder-structure/default-mode  {mode}
  POST /api/folder-structure/preview       {mode, vessel_ids[], all_vessels}  (dry-run)
  POST /api/folder-structure/apply         {mode, vessel_ids[], all_vessels, confirm: true}
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .config import settings
from .graph.guard import ProtectedTargetError, deployment_env, is_production
from .services import folder_structure as fs
from .services import get_backend

log = logging.getLogger(__name__)

MODE_HELP = {
    "empty_pool": "Keep today's behaviour: only the pooled slot folder is created for a vessel. No named structure.",
    "full_template": "Slot folder plus the complete standard structure, in the DMS and SharePoint. Ship folders per vessel, common folders once.",
    "adopt_existing": "Scan and adopt what already exists (names kept, custom folders kept, SharePoint folders linked). Creates nothing.",
    "adopt_create": "Adopt like the previous option, then create missing main folders and missing standard sub-folders.",
}


class ModeIn(BaseModel):
    mode: str


class RunIn(BaseModel):
    mode: str
    vessel_ids: list[str] = Field(default_factory=list)
    all_vessels: bool = False
    confirm: bool = False


def _email(session, header_email: str | None) -> str:
    return (getattr(session, "email", None) or header_email or "").strip().lower()


def _is_admin(session, header_email: str | None) -> bool:
    email = _email(session, header_email)
    if not email:
        return False
    if email in settings.admin_email_set:
        return True
    if session is None or not settings.db_configured:
        return False
    from .db.base import SessionLocal
    from .services.authorization import effective_role, get_profile

    with SessionLocal() as db:
        return effective_role(get_profile(db, email), email) == "Admin"


def _real_backend():
    backend = get_backend()
    if backend.__class__.__name__ != "RealBackend":
        raise HTTPException(400, "Folder Structure Mode needs SharePoint and the database configured.")
    return backend


def _guard_status() -> dict:
    site = fs.current_site_info()
    try:
        fs.assert_site_allowed(site)
        blocked, reason = False, ""
    except ProtectedTargetError as exc:
        blocked, reason = True, str(exc)
    return {"production": is_production(), "environment": deployment_env(),
            "blocked": blocked, "reason": reason}


def _map_errors(exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ProtectedTargetError):
        raise HTTPException(423, str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc))
    if isinstance(exc, RuntimeError) and "Another folder-structure apply" in str(exc):
        raise HTTPException(409, str(exc))
    log.exception("[folder-structure] request failed")
    raise HTTPException(500, f"Folder structure operation failed: {exc}")


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/folder-structure", tags=["folder-structure"])

    def require_admin(session=Depends(require_session),
                      x_user_email: str | None = Header(default=None)) -> str:
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Administrator access required")
        return _email(session, x_user_email)

    @router.get("/config")
    async def get_config(session=Depends(require_session),
                         x_user_email: str | None = Header(default=None)):
        try:
            _real_backend()
            tpl = fs.load_template()
            return {
                "modes": [{"id": m, "label": fs.MODE_LABELS[m], "description": MODE_HELP[m]}
                          for m in fs.MODES],
                "default_mode": fs.get_default_mode(),
                "is_admin": _is_admin(session, x_user_email),
                "site": fs.current_site_info(),
                "guard": _guard_status(),
                "template": {
                    "id": tpl.template_id, "version": tpl.version, "root": tpl.root,
                    "mains": [{"name": m.name, "common_folder": m.common_folder,
                               "per_ship": len(m.per_ship), "common": len(m.common)}
                              for m in tpl.mains],
                },
                "vessels": fs.list_site_vessels(),
            }
        except Exception as exc:  # noqa: BLE001
            _map_errors(exc)

    @router.get("/template")
    async def get_template(_session=Depends(require_session)):
        try:
            with open(fs.template_path(), encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            _map_errors(exc)

    @router.put("/default-mode")
    async def put_default_mode(body: ModeIn, email: str = Depends(require_admin)):
        try:
            _real_backend()
            return {"default_mode": fs.set_default_mode(body.mode, email)}
        except Exception as exc:  # noqa: BLE001
            _map_errors(exc)

    @router.post("/preview")
    async def preview(body: RunIn, email: str = Depends(require_admin)):
        try:
            return await fs.run(_real_backend(), mode=body.mode, vessel_ids=body.vessel_ids,
                                all_vessels=body.all_vessels, apply=False,
                                requesting_email=email)
        except Exception as exc:  # noqa: BLE001
            _map_errors(exc)

    @router.post("/apply")
    async def apply(body: RunIn, email: str = Depends(require_admin)):
        if not body.confirm:
            raise HTTPException(400, "Run a preview first and confirm to apply.")
        try:
            return await fs.run(_real_backend(), mode=body.mode, vessel_ids=body.vessel_ids,
                                all_vessels=body.all_vessels, apply=True,
                                requesting_email=email, requesting_name=email.split("@")[0])
        except Exception as exc:  # noqa: BLE001
            _map_errors(exc)

    return router
