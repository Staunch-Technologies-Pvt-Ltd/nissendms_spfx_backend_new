"""Session service — pure business logic for server-side session tracking.

This module has no FastAPI dependencies; it only requires a SQLAlchemy
Session (or None for stub mode) to be passed in from the caller.

Public API
──────────
  create_session(...)         → UserSession
  validate_session(...)       → (UserSession, None) | (None, reason_str)
  logout_session(...)         → bool
  revoke_session(...)         → bool
  expire_old_sessions(...)    → int  (number of sessions expired)
  revalidate_active_accounts(...)  → int  (number of sessions revoked)
  list_user_sessions(...)     → list[UserSession]
  write_audit(...)            → None
  parse_user_agent(...)       → dict[str, str | None]

Session status values: Active | Expired | Logged Out | Revoked
Audit event values: session_created | session_logged_out | session_expired |
                    session_revoked | invalid_session_attempt | auth_failure
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DbSession

from ..config import settings

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """UTC-naive datetime, consistent with the rest of the codebase."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _format_duration(seconds: int | None) -> str | None:
    """Return a human-readable duration string from a total number of seconds.

    Examples: 45 -> '45s', 125 -> '2m 5s', 3661 -> '1h 1m 1s'
    Returns None when seconds is None or negative.
    """
    if seconds is None or seconds < 0:
        return None
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# ── UA Parsing (light-weight, no third-party dep) ──────────────────────────────

_MOBILE_RE = re.compile(
    r"(mobi|android|iphone|ipad|ipod|blackberry|windows phone|opera mini)",
    re.IGNORECASE,
)
_TABLET_RE = re.compile(r"(ipad|tablet|kindle|silk|playbook)", re.IGNORECASE)

_BROWSER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Edge", re.compile(r"Edg(?:e|\/)", re.IGNORECASE)),
    ("Chrome", re.compile(r"Chrome\/[\d.]+", re.IGNORECASE)),
    ("Safari", re.compile(r"Version\/[\d.]+ Safari", re.IGNORECASE)),
    ("Firefox", re.compile(r"Firefox\/[\d.]+", re.IGNORECASE)),
    ("Internet Explorer", re.compile(r"(MSIE |Trident\/)", re.IGNORECASE)),
    ("Opera", re.compile(r"(OPR\/|Opera)", re.IGNORECASE)),
]

_OS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Windows 11", re.compile(r"Windows NT 10\.0", re.IGNORECASE)),  # Win 11 reports NT 10.0 too
    ("Windows 10", re.compile(r"Windows NT 10\.0", re.IGNORECASE)),
    ("Windows", re.compile(r"Windows", re.IGNORECASE)),
    ("macOS", re.compile(r"Mac OS X", re.IGNORECASE)),
    ("iOS", re.compile(r"(iPhone|iPad|iPod)", re.IGNORECASE)),
    ("Android", re.compile(r"Android", re.IGNORECASE)),
    ("Linux", re.compile(r"Linux", re.IGNORECASE)),
]


def parse_user_agent(ua: str | None) -> dict[str, str | None]:
    """Return {browser, operating_system, device_type} from a UA string."""
    if not ua:
        return {"browser": None, "operating_system": None, "device_type": None}

    # Device type
    if _TABLET_RE.search(ua):
        device_type = "Tablet"
    elif _MOBILE_RE.search(ua):
        device_type = "Mobile"
    else:
        device_type = "Desktop"

    # Browser (first match wins; Edge must come before Chrome)
    browser: str | None = None
    for name, pattern in _BROWSER_PATTERNS:
        if pattern.search(ua):
            browser = name
            break

    # OS
    os_name: str | None = None
    for name, pattern in _OS_PATTERNS:
        if pattern.search(ua):
            os_name = name
            break

    return {"browser": browser, "operating_system": os_name, "device_type": device_type}


# ── Core session operations ────────────────────────────────────────────────────

def create_session(
    db: "DbSession",
    email: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
    idle_timeout_minutes: int | None = None,
    max_lifetime_hours: int | None = None,
) -> "object":
    """Insert a new Active session row and write a session_created audit entry.

    Returns the newly created UserSession ORM object (committed).
    """
    from ..db import models as m

    idle_min = idle_timeout_minutes or settings.session_idle_timeout_minutes
    max_h = max_lifetime_hours or settings.session_max_lifetime_hours

    now = _utcnow()
    expiry = now + timedelta(hours=max_h)
    ua_info = parse_user_agent(user_agent)

    # Resolve user_profiles.id (best-effort — may be None for new users)
    profile_id: int | None = None
    try:
        profile = db.query(m.UserProfile).filter_by(email=email).one_or_none()
        if profile:
            profile_id = profile.id
    except Exception:
        pass

    session = m.UserSession(
        id=_new_uuid(),
        session_id=_new_uuid(),
        user_id=profile_id,
        email=email,
        login_time=now,
        last_activity=now,
        expiry_time=expiry,
        user_agent=user_agent,
        browser=ua_info["browser"],
        operating_system=ua_info["operating_system"],
        device_type=ua_info["device_type"],
        ip_address=ip_address,
        authentication_method="AzureAD",
        status="Active",
    )
    db.add(session)

    # Audit entry
    write_audit(
        db,
        email=email,
        event="session_created",
        session_id=session.session_id,
        ip_address=ip_address,
        browser=ua_info["browser"],
        user_agent=user_agent,
        detail=f"Login from {ip_address or 'unknown IP'} using {ua_info['browser'] or 'unknown browser'}",
        flush=False,
    )

    db.commit()
    log.info("Session created: %s for %s from %s", session.session_id[:8], email, ip_address)
    return session


def validate_session(
    db: "DbSession",
    session_id: str,
) -> tuple["object", None] | tuple[None, str]:
    """Validate a session ID.

    Returns (UserSession, None) on success, or (None, reason) on failure.
    Reason values: 'not_found' | 'logged_out' | 'revoked' | 'expired'

    Does NOT update last_activity — callers must do that after the dependency
    resolves, since we don't want a failed validation to side-effect anything.
    """
    from ..db import models as m

    if not session_id:
        return None, "not_found"

    try:
        row = db.query(m.UserSession).filter_by(session_id=session_id).one_or_none()
    except Exception as exc:
        log.warning("DB error during session lookup: %s", exc)
        return None, "not_found"

    if row is None:
        return None, "not_found"

    status = row.status
    if status == "Logged Out":
        return None, "logged_out"
    if status == "Revoked":
        return None, "revoked"
    if status == "Expired":
        return None, "expired"

    # Double-check expiry from DB timestamps (in case sweep hasn't run yet)
    now = _utcnow()
    if now >= row.expiry_time:
        _mark_expired(db, row, commit=True)
        return None, "expired"

    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    if row.last_activity < idle_cutoff:
        _mark_expired(db, row, commit=True)
        return None, "expired"

    return row, None


def _mark_expired(db: "DbSession", row: "object", *, commit: bool = False) -> None:
    """Transition a session to Expired and write an audit row."""
    from ..db import models as m
    now = _utcnow()
    row.status = "Expired"  # type: ignore[attr-defined]
    row.logout_time = now  # type: ignore[attr-defined]
    write_audit(
        db,
        email=row.email,  # type: ignore[attr-defined]
        event="session_expired",
        session_id=row.session_id,  # type: ignore[attr-defined]
        ip_address=row.ip_address,  # type: ignore[attr-defined]
        browser=row.browser,  # type: ignore[attr-defined]
        user_agent=row.user_agent,  # type: ignore[attr-defined]
        login_time=row.login_time,  # type: ignore[attr-defined]
        logout_time=now,
        session_status="Expired",
        flush=False,
    )
    if commit:
        db.commit()


def logout_session(db: "DbSession", session_id: str, ip_address: str | None = None) -> bool:
    """Mark a specific session as Logged Out.  Returns True if the row was found."""
    from ..db import models as m

    if not session_id:
        return False
    try:
        row = db.query(m.UserSession).filter_by(session_id=session_id).one_or_none()
    except Exception:
        return False

    if row is None:
        return False

    now = _utcnow()
    row.status = "Logged Out"
    row.logout_time = now
    write_audit(
        db,
        email=row.email,
        event="session_logged_out",
        session_id=session_id,
        ip_address=ip_address or row.ip_address,
        browser=row.browser,
        user_agent=row.user_agent,
        login_time=row.login_time,
        logout_time=now,
        session_status="Logged Out",
        flush=False,
    )
    db.commit()
    log.info("Session logged out: %s for %s", session_id[:8], row.email)
    return True


def revoke_session(
    db: "DbSession", session_id: str, reason: str = "admin_revoked", ip_address: str | None = None
) -> bool:
    """Mark a session as Revoked (e.g. admin action or account disabled).

    Returns True if the row was found and updated.
    """
    from ..db import models as m

    if not session_id:
        return False
    try:
        row = db.query(m.UserSession).filter_by(session_id=session_id).one_or_none()
    except Exception:
        return False

    if row is None:
        return False

    now = _utcnow()
    row.status = "Revoked"
    row.logout_time = now
    write_audit(
        db,
        email=row.email,
        event="session_revoked",
        session_id=session_id,
        ip_address=ip_address or row.ip_address,
        browser=row.browser,
        user_agent=row.user_agent,
        login_time=row.login_time,
        logout_time=now,
        session_status="Revoked",
        detail=reason,
        flush=False,
    )
    db.commit()
    log.info("Session revoked: %s for %s — reason: %s", session_id[:8], row.email, reason)
    return True


def expire_old_sessions(db: "DbSession") -> int:
    """Sweep Active sessions that have exceeded their idle or absolute lifetime.

    Returns the count of sessions transitioned to Expired.
    """
    from ..db import models as m

    now = _utcnow()
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)

    # Find sessions that are past hard expiry OR past idle timeout
    try:
        candidates = (
            db.query(m.UserSession)
            .filter(
                m.UserSession.status == "Active",
            )
            .all()
        )
    except Exception as exc:
        log.warning("expire_old_sessions: DB error fetching candidates: %s", exc)
        return 0

    expired_count = 0
    for row in candidates:
        if now >= row.expiry_time or row.last_activity < idle_cutoff:
            _mark_expired(db, row, commit=False)
            expired_count += 1

    if expired_count:
        try:
            db.commit()
        except Exception as exc:
            log.warning("expire_old_sessions: commit failed: %s", exc)
            return 0
        log.info("Expired %d stale sessions", expired_count)

    return expired_count


def revalidate_active_accounts(db: "DbSession", app_token: str | None = None) -> int:
    """Spot-check active sessions older than the configured revalidation interval.

    Uses an app-only Graph token (client credentials) to verify that the
    Azure AD account is still enabled.  If Graph returns 404 or the account
    is disabled, the session is revoked.

    Returns the count of sessions revoked.

    If ``app_token`` is None (Graph not configured or token unavailable),
    skips the Graph check and only updates last_revalidated_at timestamps
    so sessions aren't rechecked every sweep.
    """
    from ..db import models as m

    reval_cutoff = _utcnow() - timedelta(
        minutes=settings.session_revalidation_interval_minutes
    )

    try:
        candidates = (
            db.query(m.UserSession)
            .filter(
                m.UserSession.status == "Active",
                (m.UserSession.last_revalidated_at == None)  # noqa: E711
                | (m.UserSession.last_revalidated_at < reval_cutoff),
            )
            .all()
        )
    except Exception as exc:
        log.warning("revalidate_active_accounts: DB error: %s", exc)
        return 0

    if not candidates:
        return 0

    revoked_count = 0
    now = _utcnow()

    if app_token:
        import httpx
        from ..graph.http import verify as graph_tls_verify

        unique_emails = {row.email for row in candidates}
        disabled_emails: set[str] = set()

        for email in unique_emails:
            try:
                with httpx.Client(verify=graph_tls_verify(), timeout=8.0) as client:
                    resp = client.get(
                        f"{settings.graph_base_url}/users/{email}",
                        params={"$select": "accountEnabled,id"},
                        headers={"Authorization": f"Bearer {app_token}"},
                    )
                if resp.status_code == 404:
                    disabled_emails.add(email)
                elif resp.status_code == 200:
                    data = resp.json()
                    if data.get("accountEnabled") is False:
                        disabled_emails.add(email)
            except Exception as exc:
                log.debug("Graph revalidation failed for %s: %s", email, exc)

        for row in candidates:
            row.last_revalidated_at = now
            if row.email in disabled_emails:
                row.status = "Revoked"
                row.logout_time = now
                write_audit(
                    db,
                    email=row.email,
                    event="session_revoked",
                    session_id=row.session_id,
                    ip_address=row.ip_address,
                    browser=row.browser,
                    user_agent=row.user_agent,
                    login_time=row.login_time,
                    logout_time=now,
                    session_status="Revoked",
                    detail="Azure AD account disabled or not found",
                    flush=False,
                )
                revoked_count += 1
    else:
        # No Graph token — just update the timestamp so we don't re-check every sweep
        for row in candidates:
            row.last_revalidated_at = now

    try:
        db.commit()
    except Exception as exc:
        log.warning("revalidate_active_accounts: commit failed: %s", exc)

    if revoked_count:
        log.info("Revoked %d sessions (account disabled in Azure AD)", revoked_count)
    return revoked_count


def list_user_sessions(db: "DbSession", email: str) -> list:
    """Return all sessions for a user, newest first."""
    from ..db import models as m

    try:
        return (
            db.query(m.UserSession)
            .filter_by(email=email)
            .order_by(m.UserSession.login_time.desc())
            .all()
        )
    except Exception:
        return []


def write_audit(
    db: "DbSession",
    email: str,
    event: str,
    session_id: str | None = None,
    ip_address: str | None = None,
    browser: str | None = None,
    user_agent: str | None = None,
    detail: str | None = None,
    login_time: "datetime | None" = None,
    logout_time: "datetime | None" = None,
    session_status: str | None = None,
    flush: bool = True,
) -> None:
    """Append a row to session_audit_log.  Never raises.

    When login_time / logout_time are not explicitly provided but a session_id
    is given, this function will look up the user_sessions row and pull those
    timestamps automatically, so callers only need to pass them when overriding.
    """
    from ..db import models as m

    try:
        # If session timing wasn't provided explicitly, try to look it up from
        # the live user_sessions row so we always persist full context.
        if session_id and (login_time is None or logout_time is None or session_status is None):
            try:
                sess_row = db.query(m.UserSession).filter_by(session_id=session_id).one_or_none()
                if sess_row is not None:
                    if login_time is None:
                        login_time = sess_row.login_time
                    if logout_time is None and sess_row.logout_time is not None:
                        logout_time = sess_row.logout_time
                    if session_status is None:
                        session_status = sess_row.status
                    # Also fill in browser / ip / user_agent from session when absent
                    if ip_address is None:
                        ip_address = sess_row.ip_address
                    if browser is None:
                        browser = sess_row.browser
                    if user_agent is None:
                        user_agent = sess_row.user_agent
            except Exception:
                pass  # Never let a lookup failure break the audit write

        # Calculate active session duration
        active_duration: int | None = None
        active_duration_formatted: str | None = None
        if login_time is not None:
            end_time = logout_time if logout_time is not None else _utcnow()
            delta_seconds = int((end_time - login_time).total_seconds())
            if delta_seconds >= 0:
                active_duration = delta_seconds
                active_duration_formatted = _format_duration(delta_seconds)

        # Derive a human-readable status for the event when not specified
        if session_status is None:
            _event_status_map = {
                "session_created": "Active",
                "session_logged_out": "Logged Out",
                "session_expired": "Expired",
                "session_revoked": "Revoked",
                "invalid_session_attempt": "Failed",
                "auth_failure": "Failed",
            }
            session_status = _event_status_map.get(event)

        entry = m.SessionAuditLog(
            id=_new_uuid(),
            session_id=session_id,
            email=email,
            event=event,
            detail=detail,
            ip_address=ip_address,
            browser=browser,
            user_agent=user_agent,
            login_time=login_time,
            logout_time=logout_time,
            active_duration=active_duration,
            active_duration_formatted=active_duration_formatted,
            status=session_status,
        )
        db.add(entry)
        if flush:
            db.flush()
    except Exception as exc:
        log.debug("write_audit(%s, %s) failed: %s", email, event, exc)
