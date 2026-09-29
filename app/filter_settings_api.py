"""REST API for Settings -> Filter Search Management.

Mounted from main.py via build_router(require_session), same pattern as
module_settings_api / folder_structure_api. Lets an admin switch the
Documents page's filter UI app-wide between the existing inline dropdown
row ("dropdown") and a slide-out Filters panel ("panel") that presents the
same filters as grouped radio lists (Keyword, Main folder, Vessel,
Category, Sub-folder, Attachment status, Sort — see FilterPanel.tsx).
Reads are available to any signed-in user — every Documents page needs the
current mode to render. Writes are admin-only (existing model —
ADMIN_EMAILS or a user_profiles role of Admin, see
folder_structure_api._is_admin).

Storage reuses the existing `app_settings` key/value table (models.AppSetting)
under key "filter_ui_mode" — the same table folder_structure_default_mode /
hidden_modules use — so this needs no schema change and no migration.

  GET /api/filter-settings/config   {mode, is_admin}
  PUT /api/filter-settings/mode     {mode: "dropdown" | "panel"}  (admin only)
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .folder_structure_api import _email, _is_admin

log = logging.getLogger(__name__)

SETTING_KEY = "filter_ui_mode"
VALID_MODES = {"dropdown", "panel"}
DEFAULT_MODE = "dropdown"


def _get_mode() -> str:
    from .db import models
    from .db.base import SessionLocal

    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
    value = (row.value or "").strip() if row else ""
    return value if value in VALID_MODES else DEFAULT_MODE


def _set_mode(mode: str, email: str) -> str:
    from .db import models
    from .db.base import SessionLocal

    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = mode, email, datetime.utcnow()
        db.commit()
    return mode


class FilterModeIn(BaseModel):
    mode: str = Field(...)


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/filter-settings", tags=["filter-settings"])

    @router.get("/config")
    async def get_config(session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        try:
            return {"mode": _get_mode(), "is_admin": _is_admin(session, x_user_email)}
        except Exception as exc:  # noqa: BLE001
            log.exception("[filter-settings] config failed")
            raise HTTPException(500, f"Could not load filter settings: {exc}")

    @router.put("/mode")
    async def put_mode(body: FilterModeIn, session=Depends(require_session),
                        x_user_email: str | None = Header(default=None)):
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Administrator access required")
        if body.mode not in VALID_MODES:
            raise HTTPException(400, f"mode must be one of {sorted(VALID_MODES)}")
        try:
            mode = _set_mode(body.mode, _email(session, x_user_email))
            return {"mode": mode}
        except Exception as exc:  # noqa: BLE001
            log.exception("[filter-settings] update failed")
            raise HTTPException(500, f"Could not save filter settings: {exc}")

    return router
