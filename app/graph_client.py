"""
Microsoft Graph integration: acquires an app-only token via client
credentials (msal) and sends mail through /users/{mailbox}/sendMail.
"""

import requests
import msal

from app.config import (
    AZURE_CLIENT_ID,
    AZURE_CLIENT_SECRET,
    GRAPH_AUTHORITY,
    GRAPH_SCOPE,
    GRAPH_SENDER_MAILBOX,
)

GRAPH_SEND_MAIL_URL = (
    f"https://graph.microsoft.com/v1.0/users/{GRAPH_SENDER_MAILBOX}/sendMail"
)


def _get_access_token() -> str:
    app = msal.ConfidentialClientApplication(
        client_id=AZURE_CLIENT_ID,
        client_credential=AZURE_CLIENT_SECRET,
        authority=GRAPH_AUTHORITY,
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph token: "
            f"{result.get('error')} - {result.get('error_description')}"
        )
    return result["access_token"]


def send_mail(
    to_address: str,
    subject: str,
    body_html: str,
    attachments: list,
) -> None:
    """
    Sends an email via Microsoft Graph as GRAPH_SENDER_MAILBOX.
    attachments: list of dicts with filename, content_base64, content_type
    """
    token = _get_access_token()

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
        GRAPH_SEND_MAIL_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=message,
        timeout=30,
    )

    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"Graph sendMail failed: {resp.status_code} - {resp.text}"
        )
