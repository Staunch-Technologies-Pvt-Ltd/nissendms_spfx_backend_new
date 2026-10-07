"""REST API for composing email from Vessel DMS.

  GET  /api/mail/people?q=   -> [{name, email, job_title, department}]
  POST /api/mail/send        -> {sent_as, from, attachments, links, size_bytes, sent_at}

Mounted from main.py via build_router(require_session). Each send is also
recorded in the existing email_log table, so it shows on the Send Email page.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import service

log = logging.getLogger(__name__)


class MailItem(BaseModel):
    drive_id: str
    item_id: str
    name: str | None = None
    is_folder: bool = False
    mode: str = "attach"  # attach | link


class LocalFile(BaseModel):
    name: str
    content_type: str | None = None
    content_b64: str


class SendIn(BaseModel):
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str = Field(default="", max_length=900)
    body_html: str = ""
    importance: str = "normal"
    items: list[MailItem] = Field(default_factory=list, max_length=100)
    local_files: list[LocalFile] = Field(default_factory=list, max_length=20)
    from_address: str | None = None


def _session_email(session, header_email: str | None) -> str:
    return (getattr(session, "email", None) or header_email or "").strip()


def _log(body: SendIn, sender: str, status: str, error: str | None) -> None:
    try:
        from ..config import settings
        if not settings.db_configured:
            return
        from ..db.base import SessionLocal
        from ..db.models import EmailLog
        import json
        recipients = ", ".join(body.to + body.cc + body.bcc)
        docs = [{"name": i.name or i.item_id, "kind": "folder" if i.is_folder else ("link" if i.mode == "link" else "file")}
                for i in body.items] + [{"name": f.name, "kind": "file"} for f in body.local_files]
        with SessionLocal() as db:
            db.add(EmailLog(
                datasource_tag_requested="compose", datasource_tag_used="compose", tag_was_valid=True,
                vessel_name=None, subject_text=body.subject[:500], subject_final=(body.subject or "(no subject)")[:600],
                body=body.body_html, recipient=recipients[:320], status=status,
                error_message=error, sent_at=datetime.utcnow() if status == "completed" else None,
                sender=(body.from_address or sender or None), documents=json.dumps(docs) if docs else None,
            ))
            db.commit()
    except Exception as exc:  # noqa: BLE001 — logging must never fail the send
        log.warning("[mail] could not record the email in email_log (sender=%s): %s", sender, exc)


def build_router(require_session) -> APIRouter:
    router = APIRouter(prefix="/api/mail", tags=["mail"])

    @router.get("/people")
    async def people(q: str = Query(default="", max_length=100), limit: int = Query(default=15, ge=1, le=50),
                     _session=Depends(require_session)):
        try:
            return {"people": await service.search_people(q, limit)}
        except Exception as exc:  # noqa: BLE001
            log.warning("[mail] people search failed: %s", exc)
            return {"people": [], "error": "The company directory isn't available right now."}

    @router.get("/senders")
    async def senders(session=Depends(require_session), x_user_email: str | None = Header(default=None)):
        me = _session_email(session, x_user_email)
        out = [{"email": me, "label": "Me", "kind": "me"}] if me else []
        out += [{"email": a, "label": a, "kind": "shared"} for a in service.allowed_senders()
                if a.lower() != me.lower()]
        return {"senders": out}

    @router.post("/send")
    async def send(body: SendIn, session=Depends(require_session),
                   x_user_email: str | None = Header(default=None),
                   x_graph_access_token: str | None = Header(default=None)):
        sender = _session_email(session, x_user_email)
        try:
            to = service.clean_addresses(body.to, "To")
            cc = service.clean_addresses(body.cc, "Cc")
            bcc = service.clean_addresses(body.bcc, "Bcc")
            result = await service.send(
                sender_email=sender, sender_name=None, to=to, cc=cc, bcc=bcc,
                subject=body.subject.strip(), body_html=body.body_html, importance=body.importance,
                items=[i.model_dump() for i in body.items], local_files=[f.model_dump() for f in body.local_files],
                user_token=x_graph_access_token, from_address=body.from_address,
            )
        except service.MailError as exc:
            _log(body, sender, "failed", exc.message)
            raise HTTPException(exc.status, exc.message)
        except Exception as exc:  # noqa: BLE001
            log.exception("[mail] send failed")
            _log(body, sender, "failed", str(exc)[:500])
            raise HTTPException(500, "The email could not be sent. Please try again.")
        _log(body, sender, "completed", None)
        log.info("[mail] %s sent an email as %s to %d recipient(s), %d attachment(s), %d link(s)",
                 sender, result["sent_as"], len(to) + len(cc) + len(bcc), result["attachments"], result["links"])
        return result

    return router
