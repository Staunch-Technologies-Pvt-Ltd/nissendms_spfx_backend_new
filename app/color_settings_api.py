"""REST API for Settings -> Color Management.

Mounted from main.py via build_router(require_session), same pattern as
module_settings_api / filter_settings_api. Lets an admin recolor the app's
"Ocean Clay" theme app-wide: background, text, design/accent color, and
hover color, set independently for light mode and night mode. Reads are
available to any signed-in user — every page needs the current colors to
render (VesselEmail._loadColorSettings, called once in componentDidMount).
Writes are admin-only (existing model — ADMIN_EMAILS or a user_profiles
role of Admin, see folder_structure_api._is_admin).

Storage reuses the existing `app_settings` key/value table (models.AppSetting)
under key "color_theme" — the same table hidden_modules / filter_ui_mode /
folder_structure_default_mode use — so this needs no schema change and no
migration. The value is a small JSON blob:

  {"light": {"bg": "#f8f1ea", "text": "#342417", "accent": "#DD9159", "hover": "#C77A3E"},
   "night": {"bg": "#211812", "text": "#f8eee6", "accent": "#DD9159", "hover": "#C77A3E"}}

Everything else the theme needs (surfaces, soft accent tints, shadows,
Fluent UI's palette) is derived client-side from these four values per mode
— see frontend/.../clayTheme.ts applyColorTheme() / buildDeepHarborTheme().

  GET /api/color-settings/config   {colors, defaults, presets, is_admin}
  PUT /api/color-settings/colors   {light: ColorSet, night: ColorSet}  (admin only)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from .folder_structure_api import _email, _is_admin

log = logging.getLogger(__name__)

SETTING_KEY = "color_theme"
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Ocean Clay defaults — must stay in sync with DEFAULT_CLAY_COLORS in
# frontend/.../clayTheme.ts. Used both as the initial value (no row saved
# yet) and as the "Reset to default" target the frontend offers.
DEFAULT_COLORS: dict = {
    "light": {"bg": "#f8f1ea", "text": "#342417", "accent": "#DD9159", "hover": "#C77A3E"},
    "night": {"bg": "#211812", "text": "#f8eee6", "accent": "#DD9159", "hover": "#C77A3E"},
}

# A handful of ready-made palettes offered alongside custom pickers.
# "Ocean Clay" is the shipped default; the rest are new options.
PRESETS: list[dict] = [
    {"id": "ocean-clay", "label": "Ocean Clay (Default)", "colors": DEFAULT_COLORS},
    {"id": "slate-blue", "label": "Slate Blue", "colors": {
        "light": {"bg": "#eef2f8", "text": "#1e293b", "accent": "#3b6fd6", "hover": "#2f59b0"},
        "night": {"bg": "#141b29", "text": "#e7edf7", "accent": "#4f83e6", "hover": "#3b6fd6"},
    }},
    {"id": "forest-sage", "label": "Forest Sage", "colors": {
        "light": {"bg": "#f0f4ec", "text": "#26331f", "accent": "#5c8a4a", "hover": "#4a7239"},
        "night": {"bg": "#182016", "text": "#e7f0e0", "accent": "#6ea057", "hover": "#5c8a4a"},
    }},
    {"id": "sunset-coral", "label": "Sunset Coral", "colors": {
        "light": {"bg": "#fcf1ec", "text": "#3a2118", "accent": "#e2634a", "hover": "#c94f38"},
        "night": {"bg": "#241713", "text": "#fbe9e2", "accent": "#e97a63", "hover": "#e2634a"},
    }},
    {"id": "midnight-indigo", "label": "Midnight Indigo", "colors": {
        "light": {"bg": "#eeeefb", "text": "#221f3d", "accent": "#5b4bd6", "hover": "#4636b5"},
        "night": {"bg": "#161327", "text": "#e8e6fb", "accent": "#7566e6", "hover": "#5b4bd6"},
    }},
]


def _valid_set(d: dict) -> bool:
    return isinstance(d, dict) and all(isinstance(d.get(k), str) and HEX_RE.match(d[k]) for k in ("bg", "text", "accent", "hover"))


def _get_colors() -> dict:
    from .db import models
    from .db.base import SessionLocal

    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
    if not row or not row.value:
        return DEFAULT_COLORS
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return DEFAULT_COLORS
    if not isinstance(data, dict) or not _valid_set(data.get("light", {})) or not _valid_set(data.get("night", {})):
        return DEFAULT_COLORS
    return {"light": data["light"], "night": data["night"]}


def _set_colors(colors: dict, email: str) -> dict:
    from .db import models
    from .db.base import SessionLocal

    if not _valid_set(colors.get("light", {})) or not _valid_set(colors.get("night", {})):
        raise ValueError("Each color set needs bg, text, accent and hover as #RRGGBB hex values.")
    cleaned = {"light": colors["light"], "night": colors["night"]}
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = json.dumps(cleaned), email, datetime.utcnow()
        db.commit()
    return cleaned


class ColorSetIn(BaseModel):
    bg: str = Field(...)
    text: str = Field(...)
    accent: str = Field(...)
    hover: str = Field(...)

    @field_validator("bg", "text", "accent", "hover")
    @classmethod
    def _is_hex(cls, v: str) -> str:
        if not HEX_RE.match(v or ""):
            raise ValueError("must be a #RRGGBB hex color")
        return v


class ColorThemeIn(BaseModel):
    light: ColorSetIn
    night: ColorSetIn


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/color-settings", tags=["color-settings"])

    @router.get("/config")
    async def get_config(session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        try:
            return {
                "colors": _get_colors(),
                "defaults": DEFAULT_COLORS,
                "presets": PRESETS,
                "is_admin": _is_admin(session, x_user_email),
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("[color-settings] config failed")
            raise HTTPException(500, f"Could not load color settings: {exc}")

    @router.put("/colors")
    async def put_colors(body: ColorThemeIn, session=Depends(require_session),
                          x_user_email: str | None = Header(default=None)):
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Administrator access required")
        try:
            colors = _set_colors({"light": body.light.model_dump(), "night": body.night.model_dump()}, _email(session, x_user_email))
            return {"colors": colors}
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("[color-settings] update failed")
            raise HTTPException(500, f"Could not save color settings: {exc}")

    return router
