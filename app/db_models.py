"""
ORM models for persisting every email automation request:
- EmailLog: the full request details, resolved tag, built subject, and outcome
- EmailAttachment: each attached file's bytes, stored directly in Postgres
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
)
from sqlalchemy.orm import relationship

from app.database import Base


class EmailLog(Base):
    __tablename__ = "email_log"

    id = Column(Integer, primary_key=True, index=True)

    # What was requested
    datasource_tag_requested = Column(String(100), nullable=True)
    vessel_name = Column(String(255), nullable=True)
    subject_text = Column(String(500), nullable=True)
    body = Column(Text, nullable=True)

    # What was resolved / actually sent
    datasource_tag_used = Column(String(100), nullable=False)
    tag_was_valid = Column(Boolean, nullable=False)
    subject_final = Column(String(600), nullable=False)
    recipient = Column(String(320), nullable=False)

    # Outcome
    status = Column(String(20), nullable=False, default="pending")  # pending|success|failed
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    sent_at = Column(DateTime, nullable=True)

    attachments = relationship(
        "EmailAttachment", back_populates="email_log", cascade="all, delete-orphan"
    )


class EmailAttachment(Base):
    __tablename__ = "email_attachment"

    id = Column(Integer, primary_key=True, index=True)
    email_log_id = Column(Integer, ForeignKey("email_log.id"), nullable=False)

    filename = Column(String(500), nullable=False)
    content_type = Column(String(200), nullable=False, default="application/octet-stream")
    size_bytes = Column(Integer, nullable=False)
    content = Column(LargeBinary, nullable=False)  # raw file bytes (Postgres bytea)

    email_log = relationship("EmailLog", back_populates="attachments")
