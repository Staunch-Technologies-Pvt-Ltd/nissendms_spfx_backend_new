"""REST API for Sites → Site Management → Vessel folders.

  GET /api/vessel-roots/{site_key}   {site_key, drive_id, mode, paths, ...}
       mode: "auto" (never set — automatic discovery), "folders", "none"
  PUT /api/vessel-roots/{site_key}   {mode, paths}   (admin)

See services/vessel_roots.py for what the setting controls.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .folder_structure_api import _email, _is_admin
from .services import vessel_roots

log = logging.getLogger(__name__)


class VesselRootsIn(BaseModel):
    mode: str  # auto | folders | none
    paths: list[str] = Field(default_factory=list)


async def _drive_for_site(site_key: str) -> str:
    from .services.site_provisioning import list_site_folders

    try:
        return (await list_site_folders(site_key, ""))["drive_id"]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not open this site's document library: {exc}")


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/vessel-roots", tags=["vessel-roots"])

    @router.get("/{site_key}")
    async def get_roots(site_key: str, session=Depends(require_session), x_user_email: str | None = Header(default=None)):
        drive_id = await _drive_for_site(site_key)
        cfg = vessel_roots.get_for_drive(drive_id)
        return {
            "site_key": site_key, "drive_id": drive_id,
            "mode": cfg["mode"] if cfg else "auto",
            "paths": (cfg or {}).get("paths", []),
            "updated_by": (cfg or {}).get("updated_by"), "updated_at": (cfg or {}).get("updated_at"),
            "is_admin": _is_admin(session, x_user_email),
        }

    @router.put("/{site_key}")
    async def put_roots(site_key: str, body: VesselRootsIn, session=Depends(require_session),
                        x_user_email: str | None = Header(default=None)):
        if not _is_admin(session, x_user_email):
            raise HTTPException(403, "Only administrators can choose a site's vessel folders")
        if body.mode not in ("auto", "folders", "none"):
            raise HTTPException(400, "mode must be auto, folders or none")
        drive_id = await _drive_for_site(site_key)
        if body.mode == "folders":
            from .services.site_provisioning import list_site_folders

            for path in body.paths:
                try:
                    await list_site_folders(site_key, path)
                except Exception:
                    raise HTTPException(400, f"Folder '{path}' was not found in this site")
        try:
            entry = vessel_roots.save(drive_id, site_key, None if body.mode == "auto" else body.mode,
                                      body.paths, _email(session, x_user_email))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        # Re-scan this site in the background so the Vessels page and the
        # Dashboard reflect the new vessel folders without waiting.
        async def _refresh() -> None:
            try:
                from .services import get_backend

                await get_backend().get_dashboard_stats(force_refresh=True, site_key=site_key)
            except Exception:
                log.debug("[vessel-roots] background refresh failed for %s", site_key, exc_info=True)

        asyncio.create_task(_refresh())
        return {"site_key": site_key, "drive_id": drive_id, "mode": body.mode,
                "paths": entry.get("paths", []), "updated_by": entry.get("updated_by"), "updated_at": entry.get("updated_at")}

    return router
