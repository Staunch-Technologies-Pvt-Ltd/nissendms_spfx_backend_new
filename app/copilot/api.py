"""REST API for the Documents Copilot.

Mounted from main.py via build_router(require_session) — same pattern as
folder_structure_api.py and tag_config_api.py — so it reuses the existing
session dependency without an import cycle. Both endpoints only read
(existing search + tag vocabulary); neither writes to the database or to
SharePoint, so no admin check is needed beyond the normal session.

  GET  /api/copilot/status   -> {"configured": bool}   (no secrets returned)
  POST /api/copilot/query    -> {"answer", "mode", "filters", "results", "total",
                                  "breakdown", "top_vessels", "suggestions"}
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
    # SharePoint site to search ("all"/None = every site in Site Management).
    site_key: str | None = None
    # Filters from the previous answer, so "only drawings" refines it.
    context: dict | None = None
    # Earlier turns [{question, answer}], so the AI can follow the conversation.
    history: list[dict] | None = None
    vessel_id: str | None = None  # older clients; ignored


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/copilot", tags=["copilot"])

    @router.get("/status")
    async def status(_session=Depends(require_session)):
        return {"configured": cfg.is_configured(), "provider": cfg.provider(), "m365_url": cfg.M365_COPILOT_URL}

    @router.post("/query")
    async def query(body: QueryIn, _session=Depends(require_session)):
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "Ask a question first.")
        try:
            return await service.ask(question, body.site_key, body.context, (body.history or [])[-6:])
        except Exception as exc:  # noqa: BLE001
            log.exception("[copilot] query failed")
            raise HTTPException(500, f"Copilot could not answer that: {exc}")

    return router
