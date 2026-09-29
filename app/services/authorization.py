"""Database-backed authorization helpers.

ADMIN_EMAILS remains a temporary emergency override while roles are migrated.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db import models


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def is_emergency_admin(email: str | None) -> bool:
    return normalize_email(email) in settings.admin_email_set


def get_profile(db: Session, email: str) -> models.UserProfile | None:
    return db.query(models.UserProfile).filter(models.UserProfile.email == normalize_email(email)).one_or_none()


def effective_role(profile: models.UserProfile | None, email: str) -> str:
    if is_emergency_admin(email):
        return "Admin"
    return (getattr(profile, "role", None) or "User").strip().title()


def require_admin_session(db: Session, session: Any) -> models.UserProfile:
    email = normalize_email(getattr(session, "email", None))
    profile = get_profile(db, email)
    if effective_role(profile, email) != "Admin":
        raise HTTPException(403, "Administrator access required")
    return profile  # type: ignore[return-value]


def site_permission(db: Session, email: str, site_key: str) -> dict[str, bool]:
    email = normalize_email(email)
    key = (site_key or "").strip().lower()
    profile = get_profile(db, email)
    if effective_role(profile, email) == "Admin":
        return {"can_view": True, "can_upload": True, "can_tag_on_upload": True}
    row = (
        db.query(models.UserSitePermission)
        .filter(models.UserSitePermission.user_email == email, models.UserSitePermission.site_key == key)
        .one_or_none()
    )
    if row is None:
        return {"can_view": False, "can_upload": False, "can_tag_on_upload": False}
    return {"can_view": bool(row.can_view), "can_upload": bool(row.can_upload), "can_tag_on_upload": bool(row.can_tag_on_upload)}


def resolve_site_key(db: Session, reference: str) -> str:
    value = (reference or "").strip().lower()
    row = (
        db.query(models.SiteConfiguration)
        .filter((models.SiteConfiguration.site_key == value) | (models.SiteConfiguration.site_id == reference))
        .one_or_none()
    )
    return row.site_key if row else value


def require_site_permission(
    db: Session,
    session: Any,
    site_key: str,
    action: str = "can_view",
) -> dict[str, bool]:
    email = normalize_email(getattr(session, "email", None))
    permissions = site_permission(db, email, site_key)
    if not permissions.get(action, False):
        raise HTTPException(403, f"Permission required: {action}")
    return permissions


def serialize_site_permission(row: models.UserSitePermission) -> dict[str, Any]:
    return {
        "id": row.id,
        "site_key": row.site_key,
        "can_view": bool(row.can_view),
        "can_upload": bool(row.can_upload),
        "can_tag_on_upload": bool(row.can_tag_on_upload),
        "granted_by_email": row.granted_by_email,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }