"""REST API for Settings -> Module Management.

Mounted from main.py via build_router(require_session), same pattern as
folder_structure_api / tag_config_api. Lets an admin show/hide top-level
application modules (the sidebar entries) app-wide. Reads are available to
any signed-in user — the sidebar needs the current hidden set to render for
everyone, not just admins. Writes are admin-only (existing model —
ADMIN_EMAILS or a user_profiles role of Admin, see
folder_structure_api._is_admin).

Storage reuses the existing `app_settings` key/value table (models.AppSetting)
under key "hidden_modules" — the same table folder_structure_default_mode
uses — so this needs no schema change and no migration.

  GET /api/module-settings/config   catalog + currently hidden ids + is_admin
  PUT /api/module-settings/hidden   {hidden: string[]}  (admin only)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .folder_structure_api import _email, _is_admin

log = logging.getLogger(__name__)

# Every module the sidebar can navigate to, except Settings (where this
# toggle lives — hiding it would lock the app out of this page) and Profile
# (the signed-in user's own account page). Ids match Sidebar.tsx navItems /
# auxLinks `id` values (frontend/.../types/view.ts AppView).
MODULE_CATALOG: list[dict[str, str]] = [
    {"id": "dashboard", "label": "Home"},
    {"id": "list", "label": "Documents"},
    {"id": "sites", "label": "Sites"},
    {"id": "vessels", "label": "Vessels"},
    {"id": "migration", "label": "Migration Assistant"},
    {"id": "users", "label": "User Management"},
    {"id": "bento_email", "label": "AI Bento Email"},
    {"id": "recycle", "label": "Recycle Bin"},
    {"id": "archive", "label": "Archive"},
]
MODULE_IDS = {m["id"] for m in MODULE_CATALOG}
SETTING_KEY = "hidden_modules"


def _get_hidden() -> list[str]:
    from .db import models
    from .db.base import SessionLocal

    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
    if not row or not row.value:
        return []
    try:
        hidden = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(hidden, list):
        return []
    return [h for h in hidden if h in MODULE_IDS]


def _set_hidden(hidden: list[str], email: str) -> list[str]:
    from .db import models
    from .db.base import SessionLocal

    # "Home" always stays reachable — without it a mistake here could lock
    # every user out of the app with no way back in.
    cleaned = sorted({h for h in hidden if h in MODULE_IDS and h != "dashboard"})
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = json.dumps(cleaned), email, datetime.utcnow()
        db.commit()
    return cleaned


class HiddenModulesIn(BaseModel):
    hidden: list[str] = Field(default_factory=list)


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/module-settings", tags=["module-settings"])

    @router.get("/config")
    async def get_config(session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        try:
            return {
                "modules": MODULE_CATALOG,
                "hidden": _get_hidden(),
                "is_admin": _is_admin(session, x_user_email),
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("[module-settings] config failed")
            raise HTTPException(500, f"Could not load module settings: {exc}")

    @router.put("/hidden")
    async def put_hidden(body: HiddenModulesIn, session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Administrator access required")
        try:
            hidden = _set_hidden(body.hidden, _email(session, x_user_email))
            return {"hidden": hidden}
        except Exception as exc:  # noqa: BLE001
            log.exception("[module-settings] update failed")
            raise HTTPException(500, f"Could not save module settings: {exc}")

    return router
