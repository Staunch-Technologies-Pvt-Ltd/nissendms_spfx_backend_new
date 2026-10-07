"""Compose and send an email with SharePoint documents.

Sending identity:
  * the signed-in user's own mailbox (`/me`, delegated token from the SPFx
    web part — needs the Microsoft Graph *Mail.Send* delegated permission
    approved in SharePoint admin > API access). The mail lands in the
    user's Outlook Sent Items, exactly as if sent from Outlook;
  * otherwise the configured sender mailbox (GRAPH_SENDER_MAILBOX) with the
    app's own permission, and the user set as Reply-To — so replies still
    reach them.

Documents:
  * files picked from any SharePoint site go as real attachments (copied
    from SharePoint with the app's permission); files over 3 MB use an
    upload session (Graph's limit for a plain attachment), up to 150 MB;
  * folders, and files the user marks "link", go as company-only view
    links in the message body — nothing is copied;
  * files from the user's computer are attached as they are.
"""
from __future__ import annotations

import base64
import html
import logging
import re
import time
from datetime import datetime

import httpx

from ..config import settings
from ..graph import drive as gd
from ..graph.client import GraphError, graph

log = logging.getLogger(__name__)

INLINE_LIMIT = 3 * 1024 * 1024          # Graph: plain fileAttachment up to 3 MB
MAX_FILE_BYTES = 150 * 1024 * 1024      # Graph upload session limit per attachment
MAX_TOTAL_BYTES = 150 * 1024 * 1024     # keep the whole message deliverable
_CHUNK = 4 * 1024 * 1024 - (4 * 1024 * 1024) % (320 * 1024)
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class MailError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------ people search
_PEOPLE_CACHE: dict = {"at": 0.0, "people": []}
_PEOPLE_TTL = 600.0


async def _directory() -> list[dict]:
    if time.monotonic() - _PEOPLE_CACHE["at"] < _PEOPLE_TTL and _PEOPLE_CACHE["people"]:
        return _PEOPLE_CACHE["people"]
    people: list[dict] = []
    if settings.graph_configured:
        url = "/users?$select=displayName,mail,userPrincipalName,jobTitle,department,accountEnabled&$top=999"
        while url:
            page = await graph().get(url)
            for u in page.get("value") or []:
                email = (u.get("mail") or "").strip()
                if not email or u.get("accountEnabled") is False or "#ext#" in email.lower():
                    continue
                people.append({
                    "name": (u.get("displayName") or email).strip(),
                    "email": email,
                    "job_title": u.get("jobTitle"),
                    "department": u.get("department"),
                })
            url = page.get("@odata.nextLink")
    people.sort(key=lambda p: p["name"].casefold())
    _PEOPLE_CACHE.update(at=time.monotonic(), people=people)
    return people


async def search_people(q: str, limit: int = 15) -> list[dict]:
    q = (q or "").strip().casefold()
    people = await _directory()
    if not q:
        return people[:limit]
    starts, contains = [], []
    for p in people:
        name, email = p["name"].casefold(), p["email"].casefold()
        if name.startswith(q) or email.startswith(q) or any(w.startswith(q) for w in name.split()):
            starts.append(p)
        elif q in name or q in email:
            contains.append(p)
    return (starts + contains)[:limit]


# ----------------------------------------------------------------- helpers
def _recipients(addresses: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in addresses]


def clean_addresses(values: list[str] | None, field: str) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in re.split(r"[;,]", raw or ""):
            addr = part.strip().strip("<>").strip()
            if not addr:
                continue
            if not _EMAIL_RE.match(addr):
                raise MailError(400, f"“{addr}” in {field} is not a valid email address.")
            if addr.lower() not in {a.lower() for a in out}:
                out.append(addr)
    return out


async def _share_link(drive_id: str, item_id: str) -> tuple[str, str]:
    """(name, url) — a company-only view link, or the item's own web URL."""
    meta = await graph().get(f"/drives/{drive_id}/items/{item_id}?$select=id,name,webUrl,folder")
    try:
        res = await graph().post(f"/drives/{drive_id}/items/{item_id}/createLink",
                                 json={"type": "view", "scope": "organization"})
        url = ((res or {}).get("link") or {}).get("webUrl")
        if url:
            return meta.get("name") or "Shared item", url
    except GraphError as exc:
        log.info("[mail] createLink failed for %s (%s) — using the item URL", item_id, exc.status)
    return meta.get("name") or "Shared item", meta.get("webUrl") or ""


def _links_block(links: list[tuple[str, str, bool]]) -> str:
    rows = "".join(
        f'<li style="margin:4px 0">{"📁" if is_folder else "📄"} '
        f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a></li>'
        for name, url, is_folder in links if url
    )
    if not rows:
        return ""
    return ('<div style="margin-top:16px;padding:12px 14px;border:1px solid #d6e2ea;border-radius:8px;'
            'font-family:Segoe UI,Arial,sans-serif;font-size:13px">'
            '<div style="font-weight:600;margin-bottom:6px">Shared from Vessel DMS</div>'
            f'<ul style="margin:0;padding-left:18px">{rows}</ul></div>')


async def _add_attachment(base: str, message_id: str, name: str, ctype: str, data: bytes,
                          token: str | None) -> None:
    if len(data) <= INLINE_LIMIT:
        await graph().post(f"{base}/messages/{message_id}/attachments", access_token=token, json={
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name, "contentType": ctype or "application/octet-stream",
            "contentBytes": base64.b64encode(data).decode("ascii"),
        })
        return
    session = await graph().post(
        f"{base}/messages/{message_id}/attachments/createUploadSession", access_token=token,
        json={"AttachmentItem": {"attachmentType": "file", "name": name, "size": len(data)}},
    )
    upload_url = session.get("uploadUrl")
    if not upload_url:
        raise MailError(502, f"Outlook did not accept the large attachment “{name}”.")
    total = len(data)
    async with httpx.AsyncClient(timeout=120) as client:
        for start in range(0, total, _CHUNK):
            chunk = data[start:start + _CHUNK]
            end = start + len(chunk) - 1
            resp = await client.put(upload_url, content=chunk, headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            })
            if resp.status_code >= 400:
                raise MailError(502, f"Uploading “{name}” failed ({resp.status_code}).")


# --------------------------------------------------------------------- send
def allowed_senders() -> list[str]:
    """Shared mailboxes an admin allows as "From" (MAIL_FROM_ADDRESSES in
    .env, comma-separated), plus the system sender mailbox. Sending from them
    uses the app's Mail.Send application permission."""
    import os
    from pathlib import Path

    raw = os.environ.get("MAIL_FROM_ADDRESSES")
    if raw is None:
        env = Path(__file__).resolve().parents[2] / ".env"
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("MAIL_FROM_ADDRESSES="):
                    raw = line.split("=", 1)[1]
        except OSError:
            raw = ""
    out: list[str] = []
    for a in [*(raw or "").split(","), settings.graph_sender_mailbox or ""]:
        a = a.strip()
        if a and _EMAIL_RE.match(a) and a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out


async def send(*, sender_email: str, sender_name: str | None, to: list[str], cc: list[str], bcc: list[str],
               subject: str, body_html: str, importance: str, items: list[dict], local_files: list[dict],
               user_token: str | None, from_address: str | None = None) -> dict:
    if not settings.graph_configured:
        raise MailError(503, "Email isn't available: Microsoft 365 is not connected on the server.")
    if not (to or cc or bcc):
        raise MailError(400, "Add at least one recipient.")

    # 1) Work out what each picked item becomes: an attachment or a link.
    links: list[tuple[str, str, bool]] = []
    attachments: list[tuple[str, str, bytes]] = []
    total = 0
    for it in items:
        drive_id, item_id = it.get("drive_id"), it.get("item_id")
        if not drive_id or not item_id:
            continue
        if it.get("is_folder") or it.get("mode") == "link":
            links.append((*await _share_link(drive_id, item_id), bool(it.get("is_folder"))))
            continue
        try:
            data, ctype, name = await gd.download_file(drive_id, item_id)
        except GraphError as exc:
            raise MailError(502, f"Couldn't read “{it.get('name') or item_id}” from SharePoint ({exc.status}).")
        if len(data) > MAX_FILE_BYTES:
            raise MailError(413, f"“{name}” is larger than 150 MB — send it as a link instead.")
        total += len(data)
        attachments.append((it.get("name") or name, ctype, data))
    for f in local_files:
        try:
            data = base64.b64decode(f.get("content_b64") or "", validate=False)
        except Exception:
            raise MailError(400, f"Couldn't read the file “{f.get('name')}”.")
        if len(data) > MAX_FILE_BYTES:
            raise MailError(413, f"“{f.get('name')}” is larger than 150 MB.")
        total += len(data)
        attachments.append((f.get("name") or "attachment", f.get("content_type") or "application/octet-stream", data))
    if total > MAX_TOTAL_BYTES:
        raise MailError(413, "The attachments add up to more than 150 MB — send some of them as links.")

    content = (body_html or "").strip() + _links_block(links)
    message = {
        "subject": subject or "(no subject)",
        "body": {"contentType": "HTML", "content": content or "&nbsp;"},
        "toRecipients": _recipients(to),
        "ccRecipients": _recipients(cc),
        "bccRecipients": _recipients(bcc),
        "importance": importance if importance in ("low", "normal", "high") else "normal",
    }

    # 2) Send as the user (or the approved shared mailbox they chose);
    #    fall back to the system mailbox with Reply-To.
    delegated = isinstance(user_token, str) and user_token.count(".") == 2
    attempts: list[tuple[str, str | None, str]] = []
    chosen = (from_address or "").strip()
    if chosen and chosen.lower() != (sender_email or "").lower():
        if chosen.lower() not in {a.lower() for a in allowed_senders()}:
            raise MailError(403, f"You can't send from {chosen} — it isn't an approved sender mailbox.")
        attempts.append((f"/users/{chosen}", None, "shared"))
    else:
        if delegated:
            attempts.append(("/me", user_token, "user"))
        if settings.graph_sender_mailbox:
            attempts.append((f"/users/{settings.graph_sender_mailbox}", None, "system"))
    last_error = "No mailbox is available to send from."
    for base, token, sent_as in attempts:
        msg = dict(message)
        if sent_as in ("system", "shared") and sender_email:
            msg["replyTo"] = [{"emailAddress": {"address": sender_email, "name": sender_name or sender_email}}]
        try:
            draft = await graph().post(f"{base}/messages", access_token=token, json=msg)
        except GraphError as exc:
            last_error = _explain(exc, sent_as)
            log.warning("[mail] creating the message as %s failed (%s): %s", sent_as, exc.status, last_error)
            continue
        draft_id = draft.get("id")
        try:
            for name, ctype, data in attachments:
                await _add_attachment(base, draft_id, name, ctype, data, token)
            await graph().post(f"{base}/messages/{draft_id}/send", access_token=token)
        except (GraphError, MailError) as exc:
            try:
                await graph().delete(f"{base}/messages/{draft_id}", access_token=token)
            except Exception:  # noqa: BLE001 — best effort cleanup of the draft
                pass
            if isinstance(exc, MailError):
                raise
            raise MailError(502, _explain(exc, sent_as))
        return {
            "sent_as": sent_as,
            "from": sender_email if sent_as == "user" else (chosen or settings.graph_sender_mailbox),
            "attachments": len(attachments),
            "links": len(links),
            "size_bytes": total,
            "sent_at": datetime.utcnow().isoformat() + "Z",
        }
    raise MailError(403, last_error)


def _explain(exc: GraphError, sent_as: str) -> str:
    if exc.status in (401, 403):
        if sent_as == "user":
            return ("Sending from your mailbox isn't allowed yet — an admin needs to approve the "
                    "Microsoft Graph 'Mail.Send' permission in SharePoint admin > API access.")
        return ("This mailbox isn't allowed to send email yet — IT needs to grant the app the Microsoft Graph "
                "'Mail.Send' application permission.")
    if exc.status == 404:
        who = "Your account" if sent_as == "user" else "That mailbox"
        return f"{who} has no Outlook mailbox (no Exchange Online licence) — choose another From address."
    if exc.status == 429:
        return "Microsoft 365 is busy — try again in a minute."
    return f"Microsoft 365 rejected the email ({exc.status})."
