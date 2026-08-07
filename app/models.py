from typing import List, Optional
from pydantic import BaseModel, Field


class Attachment(BaseModel):
    filename: str
    content_base64: str = Field(..., description="Base64-encoded file content")
    content_type: str = Field(
        default="application/octet-stream",
        description="MIME type, e.g. application/pdf",
    )


class SendEmailRequest(BaseModel):
    datasource_tag: Optional[str] = Field(
        None,
        description="DataSource tag, e.g. 'contract', 'right_ship'. "
        "Falls back to 'mail' if missing/invalid.",
    )
    vessel_name: Optional[str] = Field(
        None, description="Registered vessel name, recommended for linking in AI BANTO."
    )
    subject_text: Optional[str] = Field(
        None, description="Free-text subject detail (optional, combined with vessel name)."
    )
    body: Optional[str] = Field("", description="Email body (HTML or plain text).")
    attachments: List[Attachment] = Field(default_factory=list)


class SendEmailResponse(BaseModel):
    success: bool
    datasource_tag_used: str
    tag_was_valid: bool
    subject: str
    recipient: str
    message: str
