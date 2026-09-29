"""REST API for Settings -> Settings Management.

Mounted from main.py via build_router(require_session), same pattern as
module_settings_api. Lets an admin show/hide the Settings page's own left-nav
tabs (Site Management, Vessel Settings, Tag Configuration, Module Management,
Filter Search Management, Color Management, Audit Logs) app-wide. Reads are
available to any signed-in user — the Settings left nav needs the current
hidden set to render for everyone, not just admins. Writes are admin-only
(existing model — ADMIN_EMAILS or a user_profiles role of Admin, see
folder_structure_api._is_admin).

Storage reuses the existing `app_settings` key/value table (models.AppSetting)
under key "hidden_settings_tabs" — the same table hidden_modules and
folder_structure_default_mode use — so this needs no schema change and no
migration.

  GET /api/settings-tab-settings/config   catalog + currently hidden ids + is_admin
  PUT /api/settings-tab-settings/hidden   {hidden: string[]}  (admin only)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .folder_structure_api import _email, _is_admin

log = logging.getLogger(__name__)

# Every tab in the Settings page's own left nav that can be hidden.
# "Settings Management" itself (where this toggle lives) is deliberately
# excluded — hiding it would lock every admin out of the control that
# un-hides everything else. Ids match SettingsPageView's tab list in
# frontend/.../pages/SettingsPage.tsx (host.state.settingsTab values).
SETTINGS_TAB_CATALOG: list[dict[str, str]] = [
    {"id": "Site Management", "label": "Site Management"},
    {"id": "Vessel Settings", "label": "Vessel Settings"},
    {"id": "Tag Configuration", "label": "Tag Configuration"},
    {"id": "Module Management", "label": "Module Management"},
    {"id": "Filter Search Management", "label": "Filter Search Management"},
    {"id": "Color Management", "label": "Color Management"},
    {"id": "Audit Logs", "label": "Audit Logs"},
]
SETTINGS_TAB_IDS = {t["id"] for t in SETTINGS_TAB_CATALOG}
SETTING_KEY = "hidden_settings_tabs"


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
    return [h for h in hidden if h in SETTINGS_TAB_IDS]


def _set_hidden(hidden: list[str], email: str) -> list[str]:
    from .db import models
    from .db.base import SessionLocal

    cleaned = sorted({h for h in hidden if h in SETTINGS_TAB_IDS})
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = json.dumps(cleaned), email, datetime.utcnow()
        db.commit()
    return cleaned


class HiddenSettingsTabsIn(BaseModel):
    hidden: list[str] = Field(default_factory=list)


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/settings-tab-settings", tags=["settings-tab-settings"])

    @router.get("/config")
    async def get_config(session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        try:
            return {
                "tabs": SETTINGS_TAB_CATALOG,
                "hidden": _get_hidden(),
                "is_admin": _is_admin(session, x_user_email),
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("[settings-tab-settings] config failed")
            raise HTTPException(500, f"Could not load settings tab settings: {exc}")

    @router.put("/hidden")
    async def put_hidden(body: HiddenSettingsTabsIn, session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Administrator access required")
        try:
            hidden = _set_hidden(body.hidden, _email(session, x_user_email))
            return {"hidden": hidden}
        except Exception as exc:  # noqa: BLE001
            log.exception("[settings-tab-settings] update failed")
            raise HTTPException(500, f"Could not save settings tab settings: {exc}")

    return router
