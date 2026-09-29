"""REST API for the Documents Copilot.

Mounted from main.py via build_router(require_session) — same pattern as
folder_structure_api.py and tag_config_api.py — so it reuses the existing
session dependency without an import cycle. Both endpoints only read
(existing search + tag vocabulary); neither writes to the database or to
SharePoint, so no admin check is needed beyond the normal session.

  GET  /api/copilot/status   -> {"configured": bool}   (no secrets returned)
  POST /api/copilot/query    -> {"answer", "mode", "filters", "results"}
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import config as cfg
from . import service

log = logging.getLogger(__name__)


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    vessel_id: str | None = None


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/copilot", tags=["copilot"])

    @router.get("/status")
    async def status(_session=Depends(require_session)):
        return {"configured": cfg.is_configured()}

    @router.post("/query")
    async def query(body: QueryIn, _session=Depends(require_session)):
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "Ask a question first.")
        try:
            return await service.ask(question, body.vessel_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("[copilot] query failed")
            raise HTTPException(500, f"Copilot could not answer that: {exc}")

    return router
