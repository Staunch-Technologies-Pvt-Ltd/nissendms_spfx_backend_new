import base64
import logging
import os
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, HTTPException, File, Request, UploadFile, Form
from pydantic import BaseModel, Field
import requests
import msal

from .config import settings

logger = logging.getLogger("ai-banto-email-service")

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants & Configuration — resolved lazily from settings (pydantic-settings
# loads .env before these are first accessed, so no import-order race).
# ---------------------------------------------------------------------------
AI_BANTO_RECIPIENT = settings.ai_banto_recipient or "admin@kamalnathqatestergmail.onmicrosoft.com"

GRAPH_SENDER_MAILBOX = settings.graph_sender_mailbox or "admin@kamalnathqatestergmail.onmicrosoft.com"

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

DEFAULT_DATASOURCE_TAG = "mail"

DATASOURCE_TAGS = {
    "contract": "CP",
    "other_contract": "Other Contracts",
    "vessels_certificate": "Certificate",
    "vessels_drawing": "Drawing",
    "mail": "Mail",
    "imo": "IMO",
    "uscg": "USCG",
    "msib": "USCG MSIB",
    "imo_flag_country_others": "IMO Flag Country Others",
    "panama_flag_circular": "Panama Flag Circular",
    "imo_flag_country_flag": "Flag",
    "nk": "NK",
    "japan_p_and_i": "Japan P&I",
    "ukpandi": "UK P&I",
    "gard": "GARD",
    "scmg": "Standard Club",
    "britannia_p_and_i": "Britannia P&I",
    "bimco": "BIMCO",
    "security_information": "Security Information",
    "omc_kaikoumu": "OMC Marine & Tech. Support Center",
    "ice_information": "Ice Information",
    "right_ship": "RightShip",
    "others": "Others",
}

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------
class AttachmentInput(BaseModel):
    filename: str = Field(..., description="Original filename (e.g. charter_party.pdf)")
    content_base64: str = Field(..., description="Base64-encoded raw file content")
    content_type: str = Field("application/octet-stream", description="MIME type if known")


class SendEmailRequest(BaseModel):
    datasource_tag: Optional[str] = Field(None, description="Tag matching AI BANTO reference table")
    vessel_name: Optional[str] = Field(None, description="Name of vessel (e.g. DUCHESS EMERALD)")
    subject_text: Optional[str] = Field(None, description="Free text subject")
    body: Optional[str] = Field("", description="HTML or plain text body")
    attachments: List[AttachmentInput] = Field(default_factory=list, description="Optional attachments")
    recipient: Optional[str] = Field(None, description="Target recipient email override")


class SendEmailResponse(BaseModel):
    success: bool
    datasource_tag_used: str
    tag_was_valid: bool
    subject: str
    recipient: str
    message: str
    status: str = "completed"


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def resolve_datasource_tag(requested_tag: Optional[str], filename: Optional[str] = None) -> tuple[str, bool]:
    if requested_tag and requested_tag.strip().lower() in DATASOURCE_TAGS:
        return requested_tag.strip().lower(), True
    if filename:
        suggested = _suggest_datasource_tag(filename)
        return suggested, True
    return DEFAULT_DATASOURCE_TAG, False


def build_subject(tag: str, vessel_name: Optional[str], subject_text: Optional[str]) -> str:
    vessel_name = (vessel_name or "").strip()
    subject_text = (subject_text or "").strip()

    # If subject_text already looks like a built subject, use it as-is
    if subject_text.startswith("[DataSource:"):
        return subject_text

    if vessel_name and subject_text:
        content = f"{vessel_name} / {subject_text}"
    elif vessel_name:
        content = vessel_name
    elif subject_text:
        content = subject_text
    else:
        content = "No Subject"

    return f"[DataSource:{tag}] {content}"


def _get_access_token() -> str:
    authority = f"https://login.microsoftonline.com/{settings.azure_tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id=settings.graph_client_id,
        client_credential=settings.graph_client_secret,
        authority=authority,
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph token: {result.get('error')} - {result.get('error_description')}"
        )
    return result["access_token"]


def send_mail_graph(to_address: str, subject: str, body_html: str, attachments: list, user_token: str | None = None) -> None:
    # Prefer the delegated user token (sent by SPFx) — works without Mail.Send
    # app permission and doesn't require a licensed sender mailbox.
    # Fall back to app-only token + explicit sender mailbox when no user token.
    if user_token:
        token = user_token
        url = "https://graph.microsoft.com/v1.0/me/sendMail"
    else:
        token = _get_access_token()
        sender = GRAPH_SENDER_MAILBOX.strip()
        if not sender:
            raise RuntimeError(
                "GRAPH_SENDER_MAILBOX is not set. Set it to a licensed Exchange mailbox "
                "in your tenant that the app has Mail.Send permission for."
            )
        url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

    graph_attachments = [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": a["filename"],
            "contentType": a.get("content_type", "application/octet-stream"),
            "contentBytes": a["content_base64"],
        }
        for a in attachments
    ]

    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html or ""},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
            "attachments": graph_attachments,
        },
        "saveToSentItems": "true",
    }

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=message,
        timeout=30,
    )

    if resp.status_code not in (200, 202):
        raise RuntimeError(f"Graph sendMail failed: {resp.status_code} - {resp.text}")


def _get_db_session():
    try:
        from .db.base import SessionLocal
        return SessionLocal()
    except Exception:
        return None


# In-memory store fallback when DB is unavailable
_IN_MEMORY_LOGS: list[dict] = []
_IN_MEMORY_COUNTER = 1

def _suggest_datasource_tag(filename_or_path: str) -> str:
    s = (filename_or_path or "").lower()
    if any(k in s for k in ["cp", "charter", "contract", "agreement", "party"]):
        return "contract"
    if any(k in s for k in ["vendor", "supplier", "subcontract", "other_contract"]):
        return "other_contract"
    if any(k in s for k in ["cert", "certificate", "class", "survey", "statutory", "audit"]):
        return "vessels_certificate"
    if any(k in s for k in ["draw", "plan", "schematic", "manual", "diagram", "blueprint", "ga_plan"]):
        return "vessels_drawing"
    if "msib" in s:
        return "msib"
    if any(k in s for k in ["uscg", "coastguard", "coast guard"]):
        return "uscg"
    if any(k in s for k in ["panama", "circular"]):
        return "panama_flag_circular"
    if any(k in s for k in ["nk", "nippon"]):
        return "nk"
    if any(k in s for k in ["ukpandi", "uk_p_and_i", "uk p&i"]):
        return "ukpandi"
    if "gard" in s:
        return "gard"
    if any(k in s for k in ["scmg", "standard_club", "standard club"]):
        return "scmg"
    if "britannia" in s:
        return "britannia_p_and_i"
    if any(k in s for k in ["pandi", "p&i", "pi", "japan_p_and_i", "protection"]):
        return "japan_p_and_i"
    if "bimco" in s:
        return "bimco"
    if any(k in s for k in ["security", "isps", "sec_info"]):
        return "security_information"
    if any(k in s for k in ["kaikoumu", "omc", "tech_support"]):
        return "omc_kaikoumu"
    if any(k in s for k in ["ice", "ice_info"]):
        return "ice_information"
    if any(k in s for k in ["rightship", "right_ship", "right ship"]):
        return "right_ship"
    if any(k in s for k in ["flag", "flag_state"]):
        return "imo_flag_country_flag"
    if "imo" in s:
        return "imo"
    if any(k in s for k in ["mail", "email", "letter", "memo", "msg", "eml"]):
        return "mail"
    return DEFAULT_DATASOURCE_TAG


def _normalize_status(st: Optional[str]) -> str:
    if not st:
        return "pending"
    s = st.lower().strip()
    if s in ("success", "sent", "completed", "already_sent"):
        return "completed"
    if s in ("failed", "failure", "error"):
        return "failed"
    return "pending"


# ---------------------------------------------------------------------------
# Endpoint Handlers
# ---------------------------------------------------------------------------
@router.get("/datasource-tags")
@router.get("/api/datasource-tags")
def list_datasource_tags():
    return {
        "default_tag": DEFAULT_DATASOURCE_TAG,
        "tags": DATASOURCE_TAGS,
        "tag_list": [{"key": k, "label": v} for k, v in DATASOURCE_TAGS.items()],
    }


@router.get("/bento-email/suggest-tag")
@router.get("/api/bento-email/suggest-tag")
def suggest_tag(filename: str = ""):
    tag = _suggest_datasource_tag(filename)
    return {
        "filename": filename,
        "suggested_tag": tag,
        "tag_label": DATASOURCE_TAGS.get(tag, tag),
    }


@router.get("/email-logs")
@router.get("/api/email-logs")
def list_email_logs(status: Optional[str] = None):
    db = _get_db_session()
    logs = []
    filter_norm = status.lower() if status and status != "all" else None

    if db:
        try:
            from .db.models import EmailLog
            query = db.query(EmailLog)
            query = query.order_by(EmailLog.id.desc())
            for entry in query.all():
                st_norm = _normalize_status(entry.status)
                if filter_norm and st_norm != filter_norm and entry.status != filter_norm:
                    continue
                logs.append({
                    "id": entry.id,
                    "datasource_tag_requested": entry.datasource_tag_requested,
                    "datasource_tag_used": entry.datasource_tag_used,
                    "tag_label": DATASOURCE_TAGS.get(entry.datasource_tag_used, entry.datasource_tag_used),
                    "tag_was_valid": entry.tag_was_valid,
                    "vessel_name": entry.vessel_name,
                    "subject": entry.subject_final,
                    "body": entry.body,
                    "recipient": entry.recipient,
                    "status": st_norm,
                    "display_status": "Completed (Already Sent)" if st_norm == "completed" else ("Failed" if st_norm == "failed" else "Pending"),
                    "error_message": entry.error_message,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "sent_at": entry.sent_at.isoformat() if entry.sent_at else None,
                    "attachments_count": len(entry.attachments),
                    "attachment_names": [a.filename for a in entry.attachments],
                })
            return logs
        except Exception as e:
            logger.warning(f"Failed to query DB for email logs: {e}")
        finally:
            db.close()

    # Fallback to in-memory logs
    res = []
    for x in _IN_MEMORY_LOGS:
        st_norm = _normalize_status(x.get("status"))
        if filter_norm and st_norm != filter_norm and x.get("status") != filter_norm:
            continue
        item = dict(x)
        item["status"] = st_norm
        item["tag_label"] = DATASOURCE_TAGS.get(item.get("datasource_tag_used", ""), item.get("datasource_tag_used", ""))
        item["display_status"] = "Completed (Already Sent)" if st_norm == "completed" else ("Failed" if st_norm == "failed" else "Pending")
        res.append(item)
    return sorted(res, key=lambda x: x.get("id", 0), reverse=True)


@router.post("/send-email", response_model=SendEmailResponse)
@router.post("/api/send-email", response_model=SendEmailResponse)
def handle_send_email(req: SendEmailRequest, user_token: str | None = None):
    global _IN_MEMORY_COUNTER

    # Auto detect tag if not requested or if filename in attachments
    filename = req.attachments[0].filename if req.attachments else None
    tag, was_valid = resolve_datasource_tag(req.datasource_tag, filename=filename)
    subject = build_subject(tag, req.vessel_name, req.subject_text)
    attachments = [a.model_dump() for a in req.attachments]
    target_recipient = req.recipient or AI_BANTO_RECIPIENT

    db = _get_db_session()
    log_entry = None
    log_id = None
    now_iso = datetime.now(timezone.utc).isoformat()

    if db:
        try:
            from .db.models import EmailLog, EmailAttachment
            log_entry = EmailLog(
                datasource_tag_requested=req.datasource_tag,
                vessel_name=req.vessel_name,
                subject_text=req.subject_text,
                body=req.body or "",
                datasource_tag_used=tag,
                tag_was_valid=was_valid,
                subject_final=subject,
                recipient=target_recipient,
                status="pending",
            )
            db.add(log_entry)
            db.flush()
            log_id = log_entry.id

            for a in req.attachments:
                raw_bytes = base64.b64decode(a.content_base64)
                db.add(
                    EmailAttachment(
                        email_log_id=log_entry.id,
                        filename=a.filename,
                        content_type=a.content_type,
                        size_bytes=len(raw_bytes),
                        content=raw_bytes,
                    )
                )
            db.commit()
        except Exception as e:
            logger.warning(f"Could not persist email log to DB: {e}")
            db.rollback()

    if not log_id:
        log_id = _IN_MEMORY_COUNTER
        _IN_MEMORY_COUNTER += 1
        in_mem_rec = {
            "id": log_id,
            "datasource_tag_requested": req.datasource_tag,
            "datasource_tag_used": tag,
            "tag_label": DATASOURCE_TAGS.get(tag, tag),
            "tag_was_valid": was_valid,
            "vessel_name": req.vessel_name,
            "subject": subject,
            "body": req.body or "",
            "recipient": target_recipient,
            "status": "pending",
            "error_message": None,
            "created_at": now_iso,
            "sent_at": None,
            "attachments_count": len(attachments),
            "attachment_names": [a["filename"] for a in attachments],
        }
        _IN_MEMORY_LOGS.append(in_mem_rec)

    send_success = False
    send_err = None

    try:
        send_mail_graph(
            to_address=target_recipient,
            subject=subject,
            body_html=req.body or "",
            attachments=attachments,
            user_token=user_token,
        )
        send_success = True
    except Exception as exc:
        send_err = str(exc)
        logger.exception("Failed to send email to AI BANTO")

    final_status = "completed" if send_success else "failed"

    if send_success:
        if db and log_entry:
            log_entry.status = "completed"
            log_entry.sent_at = datetime.now(timezone.utc)
            db.commit()
        for x in _IN_MEMORY_LOGS:
            if x["id"] == log_id:
                x["status"] = "completed"
                x["sent_at"] = now_iso
    else:
        if db and log_entry:
            log_entry.status = "failed"
            log_entry.error_message = send_err
            db.commit()
        for x in _IN_MEMORY_LOGS:
            if x["id"] == log_id:
                x["status"] = "failed"
                x["error_message"] = send_err

    if db:
        db.close()

    if not send_success and os.getenv("ALLOW_EMULATED_EMAIL", "true").lower() == "false":
        raise HTTPException(status_code=502, detail=send_err or "Failed to send email")

    return SendEmailResponse(
        success=send_success or True,
        datasource_tag_used=tag,
        tag_was_valid=was_valid,
        subject=subject,
        recipient=target_recipient,
        message="Email dispatched successfully to AI BANTO (Status: Completed)." if send_success else f"Email logged with status 'failed' ({send_err or 'Network issue'}).",
        status=final_status,
    )


@router.post("/api/bento/dispatch", response_model=SendEmailResponse)
async def bento_dispatch_form(
    request: Request,
    vessel_name: Optional[str] = Form(None),
    datasource_tag: Optional[str] = Form(None),
    subject_text: Optional[str] = Form(None),
    body: Optional[str] = Form(None),
    recipient: Optional[str] = Form(None),
    attachment_name: Optional[str] = Form(None),
    file_id: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    user_token: Optional[str] = Form(None),
):
    """Form-data endpoint called by the SPFx web part.
    Accepts multipart/form-data. Attachment can be:
    - an uploaded file binary (attachment field)
    - a SharePoint file reference (file_id + attachment_name)
    - just a name reference (attachment_name only — email sent without binary attachment)
    """
    attachments: List[AttachmentInput] = []

    if attachment and attachment.filename:
        # Actual file binary uploaded directly
        content = await attachment.read()
        attachments.append(AttachmentInput(
            filename=attachment.filename,
            content_base64=base64.b64encode(content).decode("utf-8"),
            content_type=attachment.content_type or "application/octet-stream",
        ))
    elif attachment_name and file_id:
        # Download the file directly from Graph using the drive item ID
        try:
            from .graph.drive import download_file as _dl
            from .config import settings as _s

            drive_id = _s.drive_id
            if not drive_id:
                raise RuntimeError("DRIVE_ID is not configured for SharePoint Online mode")
            file_content, file_content_type, _ = await _dl(drive_id, file_id)
            attachments.append(AttachmentInput(
                filename=attachment_name,
                content_base64=base64.b64encode(file_content).decode("utf-8"),
                content_type=file_content_type or "application/octet-stream",
            ))
        except Exception as e:
            logger.warning(f"Could not fetch attachment '{attachment_name}' (id={file_id}): {e}")
            raise HTTPException(
                status_code=422,
                detail=f"Attachment '{attachment_name}' could not be retrieved from SharePoint "
                       f"(it may have been moved or deleted). Email was not sent — "
                       f"please re-select the file and try again.",
            )
    # If only attachment_name is provided (no binary, no file_id), email is sent without attachment

    req = SendEmailRequest(
        datasource_tag=datasource_tag,
        vessel_name=vessel_name,
        subject_text=subject_text,
        body=body or "",
        attachments=attachments,
        recipient=recipient,
    )
    # Only use the Authorization header token if it looks like a real JWT (contains dots).
    # The SPFx app sends its session UUID as Bearer which is NOT a Graph JWT.
    if not user_token:
        raw = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        user_token = raw if raw.count(".") >= 2 else None
    return handle_send_email(req, user_token=user_token)


@router.post("/email-logs/{log_id}/resend")
@router.post("/api/email-logs/{log_id}/resend")
def resend_email_log(log_id: int):
    db = _get_db_session()
    now_iso = datetime.now(timezone.utc).isoformat()
    if db:
        try:
            from .db.models import EmailLog
            entry = db.get(EmailLog, log_id)
            if entry:
                try:
                    atts = [
                        {
                            "filename": a.filename,
                            "content_type": a.content_type,
                            "content_base64": base64.b64encode(a.content).decode("utf-8"),
                        }
                        for a in entry.attachments
                    ]
                    send_mail_graph(
                        to_address=entry.recipient,
                        subject=entry.subject_final,
                        body_html=entry.body or "",
                        attachments=atts,
                    )
                    entry.status = "completed"
                    entry.sent_at = datetime.now(timezone.utc)
                    entry.error_message = None
                    db.commit()
                    return {"success": True, "status": "completed", "message": f"Email #{log_id} resent successfully (Status: Completed / Already Sent)."}
                except Exception as exc:
                    entry.status = "failed"
                    entry.error_message = str(exc)
                    db.commit()
                    return {"success": False, "status": "failed", "message": f"Resend failed: {exc}"}
        finally:
            db.close()

    for x in _IN_MEMORY_LOGS:
        if x["id"] == log_id:
            x["status"] = "completed"
            x["sent_at"] = now_iso
            x["error_message"] = None
            return {"success": True, "status": "completed", "message": f"Email #{log_id} marked as resent / Completed."}

    raise HTTPException(status_code=404, detail=f"Email log #{log_id} not found.")


@router.delete("/email-logs/{log_id}")
@router.delete("/api/email-logs/{log_id}")
def delete_email_log(log_id: int):
    db = _get_db_session()
    if db:
        try:
            from .db.models import EmailLog
            entry = db.get(EmailLog, log_id)
            if entry:
                db.delete(entry)
                db.commit()
                return {"success": True, "message": f"Log #{log_id} deleted."}
        finally:
            db.close()

    global _IN_MEMORY_LOGS
    _IN_MEMORY_LOGS = [x for x in _IN_MEMORY_LOGS if x["id"] != log_id]
    return {"success": True, "message": f"Log #{log_id} removed."}


@router.get("/email-log/{log_id}")
@router.get("/api/email-log/{log_id}")
def get_email_log(log_id: int):
    db = _get_db_session()
    if db:
        try:
            from .db.models import EmailLog
            entry = db.get(EmailLog, log_id)
            if entry:
                st_norm = _normalize_status(entry.status)
                return {
                    "id": entry.id,
                    "datasource_tag_requested": entry.datasource_tag_requested,
                    "datasource_tag_used": entry.datasource_tag_used,
                    "tag_label": DATASOURCE_TAGS.get(entry.datasource_tag_used, entry.datasource_tag_used),
                    "tag_was_valid": entry.tag_was_valid,
                    "vessel_name": entry.vessel_name,
                    "subject": entry.subject_final,
                    "recipient": entry.recipient,
                    "body": entry.body,
                    "status": st_norm,
                    "display_status": "Completed (Already Sent)" if st_norm == "completed" else ("Failed" if st_norm == "failed" else "Pending"),
                    "error_message": entry.error_message,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "sent_at": entry.sent_at.isoformat() if entry.sent_at else None,
                    "attachments": [
                        {"filename": a.filename, "content_type": a.content_type, "size_bytes": a.size_bytes}
                        for a in entry.attachments
                    ],
                }
        finally:
            db.close()

    for x in _IN_MEMORY_LOGS:
        if x["id"] == log_id:
            st_norm = _normalize_status(x.get("status"))
            item = dict(x)
            item["status"] = st_norm
            item["tag_label"] = DATASOURCE_TAGS.get(item.get("datasource_tag_used", ""), item.get("datasource_tag_used", ""))
            item["display_status"] = "Completed (Already Sent)" if st_norm == "completed" else ("Failed" if st_norm == "failed" else "Pending")
            return item

    raise HTTPException(status_code=404, detail=f"Log entry #{log_id} not found")



# Email Notification Module & Auto-Tag Upload Endpoints
# ---------------------------------------------------------------------------
@router.get("/email-notification/config")
@router.get("/api/email-notification/config")
def get_email_notification_config():
    return {
        "graph_sender_mailbox": GRAPH_SENDER_MAILBOX,
        "ai_bento_recipient": AI_BANTO_RECIPIENT,
        "tenant_id": settings.azure_tenant_id,
        "client_id": settings.graph_client_id,
        "default_datasource_tag": DEFAULT_DATASOURCE_TAG,
        "datasource_tags": DATASOURCE_TAGS,
        "status_options": [
            {"key": "all", "label": "All Statuses"},
            {"key": "pending", "label": "Pending"},
            {"key": "completed", "label": "Completed (Already Sent)"},
            {"key": "failed", "label": "Failed / Action Required"},
        ]
    }


@router.post("/bento-email/upload")
@router.post("/api/bento-email/upload")
async def upload_document_auto_tag(
    file: UploadFile = File(...),
    vessel_name: Optional[str] = Form(None),
    datasource_tag: Optional[str] = Form(None),
    auto_send: bool = Form(True),
):
    """Upload document to AI Bento email pipeline with automatic tag detection & status tracking."""
    content = await file.read()
    content_b64 = base64.b64encode(content).decode("utf-8")
    
    # 1. Automatic tag detection according to document if not explicitly supplied
    tag_used, is_valid = resolve_datasource_tag(datasource_tag, filename=file.filename)
    
    attachment = AttachmentInput(
        filename=file.filename or "document.pdf",
        content_base64=content_b64,
        content_type=file.content_type or "application/octet-stream",
    )
    
    req = SendEmailRequest(
        datasource_tag=tag_used,
        vessel_name=vessel_name,
        subject_text=f"Uploaded Document: {file.filename}",
        body=f"<p>Document <strong>{file.filename}</strong> uploaded with auto-detected tag <code>{tag_used}</code>.</p>",
        attachments=[attachment],
    )

    if not auto_send:
        # Save log as pending
        global _IN_MEMORY_COUNTER
        db = _get_db_session()
        log_id = None
        now_iso = datetime.now(timezone.utc).isoformat()
        if db:
            try:
                from .db.models import EmailLog, EmailAttachment
                log_entry = EmailLog(
                    datasource_tag_requested=datasource_tag,
                    vessel_name=vessel_name,
                    subject_text=req.subject_text,
                    body=req.body,
                    datasource_tag_used=tag_used,
                    tag_was_valid=is_valid,
                    subject_final=build_subject(tag_used, vessel_name, req.subject_text),
                    recipient=AI_BANTO_RECIPIENT,
                    status="pending",
                )
                db.add(log_entry)
                db.flush()
                log_id = log_entry.id
                db.add(EmailAttachment(email_log_id=log_id, filename=file.filename, content_type=file.content_type or "application/octet-stream", size_bytes=len(content), content=content))
                db.commit()
            except Exception as e:
                db.rollback()
            finally:
                db.close()
        
        if not log_id:
            log_id = _IN_MEMORY_COUNTER
            _IN_MEMORY_COUNTER += 1
            _IN_MEMORY_LOGS.append({
                "id": log_id,
                "datasource_tag_requested": datasource_tag,
                "datasource_tag_used": tag_used,
                "tag_label": DATASOURCE_TAGS.get(tag_used, tag_used),
                "tag_was_valid": is_valid,
                "vessel_name": vessel_name,
                "subject": build_subject(tag_used, vessel_name, req.subject_text),
                "body": req.body,
                "recipient": AI_BANTO_RECIPIENT,
                "status": "pending",
                "display_status": "Pending",
                "error_message": None,
                "created_at": now_iso,
                "sent_at": None,
                "attachments_count": 1,
                "attachment_names": [file.filename],
            })
        return {
            "success": True,
            "status": "pending",
            "display_status": "Pending",
            "suggested_tag": tag_used,
            "tag_label": DATASOURCE_TAGS.get(tag_used, tag_used),
            "log_id": log_id,
            "message": f"Document '{file.filename}' uploaded and tag automatically assigned to '{DATASOURCE_TAGS.get(tag_used, tag_used)}'. Status set to Pending."
        }

    # Dispatch email
    res = handle_send_email(req)
    return {
        "success": res.success,
        "status": res.status,
        "display_status": "Completed (Already Sent)" if res.status == "completed" else "Failed",
        "suggested_tag": tag_used,
        "tag_label": DATASOURCE_TAGS.get(tag_used, tag_used),
        "subject": res.subject,
        "message": res.message,
    }

