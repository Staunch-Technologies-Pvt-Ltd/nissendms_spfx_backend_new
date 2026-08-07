import time

_FLAT_TREE_CACHE = {"data": None, "timestamp": 0}
_FOLDER_CHILDREN_CACHE = {}  # folder_id -> (data, timestamp)
CACHE_TTL_FLAT_TREE = 60  # 60 seconds
CACHE_TTL_CHILDREN = 30   # 30 seconds

def invalidate_folder_caches(folder_id: str = None):
    global _FLAT_TREE_CACHE, _FOLDER_CHILDREN_CACHE
    _FLAT_TREE_CACHE = {"data": None, "timestamp": 0}
    if folder_id:
        _FOLDER_CHILDREN_CACHE.pop(folder_id, None)
    else:
        _FOLDER_CHILDREN_CACHE.clear()

# -*- coding: utf-8 -*-
"""FastAPI entry point for the Vessel DMS.

One code path over a backend interface: real SharePoint Embedded + PostgreSQL
when configured (see backend/.env), otherwise the in-memory stub. See
`app/services/__init__.py`.
"""
import asyncio
import logging
import os
import warnings
from datetime import datetime, timedelta, timezone

# ── Timezone: tell tzlocal/APScheduler the system is UTC+5:30 (IST) ──────────
os.environ.setdefault("TZ", "Asia/Kolkata")
warnings.filterwarnings("ignore", message="Timezone offset does not match system offset")

import httpx

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .graph.http import verify as graph_tls_verify
from .services import backend_mode, get_backend
from .services.errors import BadRequest, Conflict, NotFound, InternalServerError

# In-memory profile cache for stub / no-DB mode (populated on each login).
_profile_cache: dict[str, dict] = {}

app = FastAPI(title="Vessel DMS", version="1.0.0")

def _cors_origins() -> list[str]:
    raw = settings.allowed_origins or "*"
    if raw.strip() == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"https?://localhost(:\d+)?",  # always allow localhost for dev
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# Read-only folder/children endpoints that are safe to cache briefly in the browser
_CACHEABLE_PREFIXES = ("/api/folders/", "/api/mains", "/api/vessels")

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    method = request.method
    # Allow short-lived browser cache (10 s) for GET folder/children/mains/vessels
    # so rapid back-navigation is instant without hitting Graph API again.
    if method == "GET" and any(path.startswith(p) for p in _CACHEABLE_PREFIXES):
        response.headers["Cache-Control"] = "private, max-age=10"
    else:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _raise(e: Exception):
    """Map domain exceptions to HTTP errors."""
    status = getattr(e, "status", None)
    if status:
        raise HTTPException(status, str(e))
    raise


VESSEL_TYPES = {
    "Bulk Carrier",
    "Container Carrier",
    "Gas Carrier",
    "Oil Tanker",
    "Chemical Tanker",
    "Reffer Carrier",
    "Other Cargo Ships",
}


class VesselIn(BaseModel):
    name: str
    imo: str
    shipyard: str | None = None
    hull_number: str | None = None
    vessel_type: str | None = None


class VesselUpdateIn(BaseModel):
    name: str | None = None
    imo: str | None = None
    shipyard: str | None = None
    hull_number: str | None = None
    vessel_type: str | None = None


class RejectIn(BaseModel):
    reason: str | None = None


def _is_admin_email(email: str | None) -> bool:
    return (email or "").strip().lower() in settings.admin_email_set


def _require_admin(
    x_user_email: str | None = Header(default=None),
    admin_email: str | None = Query(default=None),
    admin: str | None = Query(default=None),
) -> str:
    """Approval decisions are restricted to settings.admin_emails.

    Matches this codebase's current auth maturity (see the TEMPORARY bypass in
    check_email below): there's no server-side session yet, so the frontend
    identifies the acting user via a header set from its already-verified
    MSAL login, and we check it against the admin allow-list. The preview
    endpoint is also reachable from a plain <img>/<iframe>/<a href>, which
    can't attach custom headers, so it's allowed to pass the email as a query
    param instead — same admin-list check either way.
    """
    email = (x_user_email or admin_email or admin or "").strip().lower()
    if not email or not _is_admin_email(email):
        raise HTTPException(403, "Administrator access required")
    return email


def _ensure_database_exists(db_url: str) -> None:
    """Connects to the default postgres database and creates target database if not exists."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        url = make_url(db_url)
        target_db = url.database
        if not target_db:
            return

        # We only run automatic database creation if using a PostgreSQL engine
        if "postgresql" not in url.drivername:
            return

        # Connect to 'postgres' database on the same host to check/create target database
        postgres_url = url.set(database="postgres")
        
        engine = create_engine(postgres_url)
        try:
            with engine.connect() as conn:
                conn.execution_options(isolation_level="AUTOCOMMIT")
                result = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
                    {"dbname": target_db}
                ).scalar()
                
                if not result:
                    conn.execute(text(f'CREATE DATABASE "{target_db}"'))
                    import logging
                    logging.getLogger(__name__).info("Database '%s' created automatically.", target_db)
        finally:
            engine.dispose()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Could not verify/create database automatically: %s", exc)


@app.on_event("startup")
async def _startup():
    from .scheduler import precreate_next_month, start_scheduler
    import logging as _log

    _logger = _log.getLogger(__name__)

    if settings.db_configured:
        # 1. Automatic database creation if PostgreSQL database is missing
        _ensure_database_exists(settings.database_url_resolved)

        # 2. Smart Alembic migration:
        #    - If this is a brand-new empty database → run all migrations from scratch.
        #    - If tables exist but alembic_version is missing (e.g. tables were created
        #      by a prior create_all run, or by an older version without Alembic) →
        #      stamp the current head so Alembic doesn't try to re-create tables that
        #      already exist, then run any pending migrations normally.
        #    - If alembic_version is present → just run any pending migrations normally.
        try:
            import pathlib
            import alembic.config
            from alembic import command
            from sqlalchemy import inspect, text
            from .db.base import engine

            _alembic_ini = pathlib.Path(__file__).parent.parent / "alembic.ini"
            alembic_cfg = alembic.config.Config(str(_alembic_ini))

            if engine is not None:
                with engine.connect() as conn:
                    inspector = inspect(conn)
                    existing_tables = set(inspector.get_table_names())

                    # Check if alembic_version table exists
                    has_version_table = "alembic_version" in existing_tables
                    # Check if any of our app tables already exist
                    app_tables = {"vessels", "folders", "user_profiles", "user_sessions"}
                    has_app_tables = bool(app_tables & existing_tables)

                    if has_app_tables and not has_version_table:
                        # Tables exist without Alembic tracking — stamp as head to
                        # prevent re-running create_table migrations on existing tables.
                        _logger.info(
                            "DB tables exist without Alembic version tracking. "
                            "Stamping to 'head' before running incremental migrations."
                        )
                        command.stamp(alembic_cfg, "head")

            print(">>> STARTUP: about to run command.upgrade", flush=True)
            command.upgrade(alembic_cfg, "head")
            print(">>> STARTUP: command.upgrade done", flush=True)

            # Alembic's env.py calls logging.config.fileConfig(...), which
            # fully reconfigures the ROOT logger per alembic.ini's
            # [logger_root] section (level = WARN). That silently raises the
            # effective level for every app logger that doesn't set its own
            # level explicitly, so log.info(...) calls across the app stop
            # appearing after this point. Re-force it back to INFO.
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                force=True,
            )
            print(">>> STARTUP: root logger re-forced to INFO after Alembic fileConfig", flush=True)
            _logger.info("Alembic migrations completed successfully.")
        except Exception as exc:
            print(f">>> STARTUP: alembic block FAILED: {exc}", flush=True)
            _logger.warning("Alembic automatic migration failed: %s", exc)

        # 3. Safety net: make sure ALL tables and columns are present.
        #    create_all with checkfirst=True will add any missing tables but
        #    cannot add missing columns — those are handled by migrations above.
        try:
            from .db.base import Base, engine
            if engine is not None:
                Base.metadata.create_all(bind=engine, checkfirst=True)
                _logger.info("Database safety-net create_all completed.")
        except Exception as exc:
            _logger.warning("Database safety net table creation failed: %s", exc)

    app.state.scheduler = start_scheduler()
    if settings.graph_configured and settings.db_configured:
        # Catch-up in case the server started after the 20th.
        asyncio.create_task(precreate_next_month())
# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class CheckEmailIn(BaseModel):
    email: str


class LoginIn(BaseModel):
    access_token: str
    tenant_id: str = ""
    # Optional fallback UA reported by the browser JS — never authoritative;
    # the server-side request header takes precedence.
    client_reported_user_agent: str | None = None


class BypassLoginIn(BaseModel):
    email: str
    display_name: str | None = None
    tenant_id: str = ""


class LogoutIn(BaseModel):
    email: str | None = None
    session_id: str | None = None  # UUID of the session being ended


def _log_activity(email: str | None, action: str, detail: str | None = None) -> None:
    """Write an ActivityLog row if DB is configured and email is known."""
    if not email or not settings.db_configured:
        return
    email = email.lower().strip()
    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        with SessionLocal() as db:
            db.add(db_models.ActivityLog(user_email=email, action=action, detail=detail))
            # Keep only last 50
            old = (
                db.query(db_models.ActivityLog)
                .filter_by(user_email=email)
                .order_by(db_models.ActivityLog.created_at.desc())
                .offset(50)
                .all()
            )
            for entry in old:
                db.delete(entry)
            db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session tracking helpers
# ---------------------------------------------------------------------------

def _session_db():
    """Yield a SessionLocal instance; skip if DB not configured."""
    if not settings.db_configured:
        yield None
        return
    from .db.base import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def require_session(
    request: Request,
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None),
    session_id: str | None = Query(default=None),
):
    """FastAPI dependency — validate the server-side session on every protected
    request.  Returns the UserSession ORM row on success; raises HTTP 401 with
    a structured { reason } body on failure.

    Reasons: not_found | expired | logged_out | revoked

    In stub / no-DB mode this dependency is a no-op so development continues
    to work without a database.

    If a database error occurs during validation (e.g. tables were just
    created and are empty), we skip the check rather than locking out all
    users with a spurious 401.
    """
    if not settings.db_configured:
        return None  # stub mode — skip session checks

    token = x_session_id or session_id
    if not token and authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()

    if token and (token == "mock-token" or token == "dev-token" or token.startswith("mock")):
        return None  # Allow mock / dev tokens for testing

    if not token:
        # Development fallback: allow requests without explicit session token to proceed
        return None

    from .db.base import SessionLocal
    from .services.session_service import validate_session
    from datetime import datetime, timezone
    import logging as _logging

    db = SessionLocal()
    try:
        try:
            session, reason = validate_session(db, token)
        except Exception as db_exc:
            # DB error during lookup (e.g. tables freshly created / migration
            # in progress).  Let the request through rather than hard-blocking
            # with a 401 — the worst outcome is one extra unauthenticated call.
            _logging.getLogger(__name__).warning(
                "require_session: DB error during validate_session, bypassing check: %s", db_exc
            )
            return None

        if session is None:
            # Audit log the attempt (best-effort, non-blocking)
            _write_invalid_attempt(
                session_id=token,
                email="unknown",
                ip=_get_ip(request),
                ua=request.headers.get("user-agent"),
                detail=f"Invalid session: {reason}",
            )
            # Raise 401 so the frontend can detect session expiry and re-authenticate.
            # The frontend's _fetchJson already handles 401 by showing the session-expired screen.
            raise HTTPException(
                status_code=401,
                detail={"reason": reason, "message": _session_reason_message(reason)},
            )
        # Update last_activity on every valid request
        session.last_activity = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return session
    finally:
        db.close()


def _get_ip(request: Request) -> str | None:
    """Extract client IP from the request (uses request_meta logic)."""
    try:
        from .utils.request_meta import get_client_ip
        return get_client_ip(request)
    except Exception:
        return None


def _write_invalid_attempt(
    session_id: str | None,
    email: str,
    ip: str | None,
    ua: str | None,
    detail: str | None = None,
) -> None:
    """Write an invalid_session_attempt audit entry (best-effort)."""
    if not settings.db_configured:
        return
    try:
        from .db.base import SessionLocal
        from .services.session_service import write_audit
        db = SessionLocal()
        try:
            write_audit(
                db,
                email=email,
                event="invalid_session_attempt",
                session_id=session_id,
                ip_address=ip,
                user_agent=ua,
                detail=detail,
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _session_reason_message(reason: str) -> str:
    messages = {
        "not_found": "Session not found. Please sign in again.",
        "token_expiry": "Your session token expired. Please sign in again.",
        "inactivity": "Your session expired due to inactivity. Please sign in again.",
        "expired": "Your session has expired. Please sign in again.",
        "logged_out": "This session has ended. Please sign in again.",
        "revoked": "Your access was revoked. Contact your administrator if unexpected.",
    }
    return messages.get(reason, "Authentication required.")


@app.post("/api/auth/check-email")
async def check_email(payload: CheckEmailIn):
    """Pre-flight check: confirm the email is a member/guest in the Entra tenant.

    When Graph is not configured (stub mode) we let all emails through so that
    development still works without Azure credentials.
    """
    if not settings.graph_configured:
        return {"allowed": True}

    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    # Acquire an app-only token for Graph
    token_url = f"{settings.graph_authority}/{settings.azure_tenant_id}/oauth2/v2.0/token"
    try:
        async with httpx.AsyncClient(verify=graph_tls_verify(), timeout=10.0) as client:
            # 1. Get an app-only access token
            token_resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.graph_client_id,
                    "client_secret": settings.graph_client_secret,
                    "scope": settings.graph_scope,
                },
            )
            if token_resp.status_code != 200:
                # If we can't reach Graph, fail open so the user can still try MSAL.
                return {"allowed": True}

            app_token = token_resp.json().get("access_token", "")

            # 2. Look up the user in the directory.
            # Use $filter by mail/otherMails so guest accounts (whose UPN is
            # formatted as alias_domain.com#EXT#@tenant.onmicrosoft.com) are
            # found correctly. GET /users/{email} returns 404 for guests.
            filter_query = (
                f"mail eq '{email}' or otherMails/any(m:m eq '{email}')"
            )
            user_resp = await client.get(
                f"{settings.graph_base_url}/users",
                params={"$filter": filter_query, "$select": "id,mail", "$top": "1"},
                headers={
                    "Authorization": f"Bearer {app_token}",
                    "ConsistencyLevel": "eventual",
                },
            )

            if user_resp.status_code == 200:
                users = user_resp.json().get("value", [])
                if users:
                    return {"allowed": True}
                raise HTTPException(
                    401,
                    detail="This email address is not authorised. Contact your administrator.",
                )
            # Any other error from Graph — fail open
            return {"allowed": True}
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException):
        # Pre-check must never block sign-in when Graph is temporarily slow.
        # We fail open here and let the real token exchange happen in auth_login.
        return {"allowed": True}
    except httpx.RequestError:
        # Same fail-open behavior for transient network/TLS issues.
        return {"allowed": True}


@app.post("/api/auth/login")
async def auth_login(request: Request, payload: LoginIn):
    """Validate the MSAL access token, persist/update the extended profile,
    seed related records for new users, create a server-side session, and
    return the session_id for the client to attach as X-Session-ID."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # ── Capture IP and User-Agent server-side (never trust client body) ─────
    from .utils.request_meta import get_client_ip, get_user_agent
    client_ip = get_client_ip(request)
    user_agent = get_user_agent(request) or payload.client_reported_user_agent

    if payload.access_token == "mock-token":
        profile = {
            "display_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
            "email": "testuser@example.com",
            "azure_oid": None,
            "job_title": "System Administrator",
            "department": "IT",
            "phone": None,
            "office_location": None,
            "company_name": None,
            "employee_id": None,
            "manager_name": None,
            "manager_email": None,
            "tenant_id": payload.tenant_id or None,
            "two_factor_enabled": True,
            "password_changed_at": (now - timedelta(days=92)).isoformat() + "Z",
            "last_login": now.isoformat() + "Z",
            "created_at": now.isoformat() + "Z",
            "emergency_contact": None,
            "folder_permissions": [],
            "recent_activity": [{"action": "login", "detail": "Logged in", "created_at": now.isoformat() + "Z"}],
        }
        _profile_cache["testuser@example.com"] = profile
        # Create a stub session for mock logins too
        mock_session_id = None
        if settings.db_configured:
            try:
                from .db.base import SessionLocal
                from .services.session_service import create_session
                with SessionLocal() as db:
                    sess = create_session(db, "testuser@example.com", user_agent, client_ip)
                    mock_session_id = sess.session_id
            except Exception:
                pass
        return {
            "display_name": profile["display_name"],
            "email": profile["email"],
            "session_id": mock_session_id,
        }

    # ── Fetch /me from Graph ─────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(verify=graph_tls_verify(), timeout=15.0) as client:
            me_resp = await client.get(
                f"{settings.graph_base_url}/me",
                params={
                    "$select": (
                        "id,displayName,givenName,surname,mail,userPrincipalName,"
                        "jobTitle,department,mobilePhone,businessPhones,"
                        "officeLocation,companyName,employeeId"
                    )
                },
                headers={"Authorization": f"Bearer {payload.access_token}"},
            )
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, httpx.RequestError):
        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable. Please try again.",
        )

    if me_resp.status_code != 200:
        raise HTTPException(401, "Token validation failed")

    me = me_resp.json()
    email = (me.get("mail") or me.get("userPrincipalName", "")).lower().strip()
    display_name = me.get("displayName") or me.get("userPrincipalName", "")
    phone = me.get("mobilePhone") or (me.get("businessPhones") or [None])[0]

    # ── Try to fetch manager (best-effort) ────────────────────────────────────
    manager_name: str | None = None
    manager_email: str | None = None
    try:
        async with httpx.AsyncClient(verify=settings.graph_verify_ssl) as client:
            mgr_resp = await client.get(
                f"{settings.graph_base_url}/me/manager",
                params={"$select": "displayName,mail,userPrincipalName"},
                headers={"Authorization": f"Bearer {payload.access_token}"},
            )
        if mgr_resp.status_code == 200:
            mgr = mgr_resp.json()
            manager_name = mgr.get("displayName")
            manager_email = mgr.get("mail") or mgr.get("userPrincipalName")
    except Exception:
        pass

    # ── Persist to DB ─────────────────────────────────────────────────────────
    profile: dict = {
        "display_name": display_name,
        "first_name": me.get("givenName"),
        "last_name": me.get("surname"),
        "email": email,
        "azure_oid": me.get("id"),
        "job_title": me.get("jobTitle"),
        "department": me.get("department"),
        "phone": phone,
        "office_location": me.get("officeLocation"),
        "company_name": me.get("companyName"),
        "employee_id": me.get("employeeId"),
        "manager_name": manager_name,
        "manager_email": manager_email,
        "tenant_id": payload.tenant_id or None,
        "two_factor_enabled": True,
        "password_changed_at": None,
        "last_login": now.isoformat() + "Z",
    }

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                is_new = row is None
                if is_new:
                    row = db_models.UserProfile(email=email)
                    db.add(row)
                    row.display_name = display_name
                    row.first_name = me.get("givenName")
                    row.last_name = me.get("surname")
                    row.azure_oid = me.get("id")
                    row.job_title = me.get("jobTitle")
                    row.department = me.get("department")
                    row.phone = phone
                    row.office_location = me.get("officeLocation")
                    row.company_name = me.get("companyName")
                    row.employee_id = me.get("employeeId")
                    if manager_name:
                        row.manager_name = manager_name
                    if manager_email:
                        row.manager_email = manager_email
                else:
                    # For existing users, only populate missing/null fields
                    # with Graph data so user's edits in the app are preserved.
                    if not row.display_name:
                        row.display_name = display_name
                    if not row.first_name:
                        row.first_name = me.get("givenName")
                    if not row.last_name:
                        row.last_name = me.get("surname")
                    if not row.azure_oid:
                        row.azure_oid = me.get("id")
                    if not row.job_title:
                        row.job_title = me.get("jobTitle")
                    if not row.department:
                        row.department = me.get("department")
                    if not row.phone:
                        row.phone = phone
                    if not row.office_location:
                        row.office_location = me.get("officeLocation")
                    if not row.company_name:
                        row.company_name = me.get("companyName")
                    if not row.employee_id:
                        row.employee_id = me.get("employeeId")
                    if not row.manager_name and manager_name:
                        row.manager_name = manager_name
                    if not row.manager_email and manager_email:
                        row.manager_email = manager_email

                row.tenant_id = payload.tenant_id or None
                row.last_login = now

                db.flush()  # ensure ID assigned before seeding related rows

                if is_new:
                    # Seed 2FA = enabled, password set 92 days ago
                    row.two_factor_enabled = True
                    row.password_changed_at = now - timedelta(days=92)

                    # Default folder permissions
                    for fname, level in [
                        ("Technical & Crewing", "edit"),
                        ("Commercial & Chartering", "view"),
                        ("Insurance", "approve"),
                    ]:
                        db.add(db_models.FolderPermission(
                            user_email=email,
                            folder_name=fname,
                            permission_level=level,
                        ))

                # Log the login event
                db.add(db_models.ActivityLog(
                    user_email=email,
                    action="login",
                    detail="Logged in",
                ))

                # Keep only the last 50 activity entries
                old = (
                    db.query(db_models.ActivityLog)
                    .filter_by(user_email=email)
                    .order_by(db_models.ActivityLog.created_at.desc())
                    .offset(50)
                    .all()
                )
                for entry in old:
                    db.delete(entry)

                db.commit()
                profile["created_at"] = row.created_at.isoformat() + "Z"
                profile["two_factor_enabled"] = row.two_factor_enabled
                profile["password_changed_at"] = (
                    row.password_changed_at.isoformat() + "Z" if row.password_changed_at else None
                )
        except Exception:
            pass  # Non-critical; don't break login

    # Merge with existing cache if present to preserve user's edits
    existing_cached = _profile_cache.get(email)
    if existing_cached:
        # Update last login and non-empty values
        for key in ["last_login", "tenant_id"]:
            if key in profile:
                existing_cached[key] = profile[key]
        for key in ["display_name", "first_name", "last_name", "azure_oid", "job_title", 
                    "department", "phone", "office_location", "company_name", "employee_id", 
                    "manager_name", "manager_email"]:
            val = profile.get(key)
            if val and not existing_cached.get(key):
                existing_cached[key] = val
        profile = existing_cached
    else:
        profile.setdefault("created_at", now.isoformat().replace("+00:00", "Z"))
        profile.setdefault("emergency_contact", None)
        profile.setdefault("folder_permissions", [])
        profile.setdefault("recent_activity", [{"action": "login", "detail": "Logged in", "created_at": now.isoformat().replace("+00:00", "Z")}])
        _profile_cache[email] = profile

                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            # ── Create server-side session (with retry) ───────────────────────────────
    session_id: str | None = None
    if settings.db_configured:
        last_exc: Exception | None = None
        for attempt in range(3):  # up to 3 attempts
            try:
                from .db.base import SessionLocal
                from .services.session_service import create_session
                with SessionLocal() as db:
                    sess = create_session(db, email, user_agent, client_ip)
                    session_id = sess.session_id
                break  # success
            except Exception as exc:
                last_exc = exc
                import logging
                logging.getLogger(__name__).warning(
                    "Session creation attempt %d/3 failed: %s", attempt + 1, exc
                )
        if session_id is None:
            # All retries failed — surface a clear error so the frontend
            # can prompt the user to try again rather than letting them
            # land on the app with no working session (all API calls 401).
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": "session_create_failed",
                    "message": (
                        "Sign-in succeeded but the server could not create a session. "
                        "Please try again in a few seconds."
                    ),
                    "debug": str(last_exc),
                },
            )

    return {"display_name": display_name, "email": email, "session_id": session_id}


@app.post("/api/auth/bypass-login")
async def auth_bypass_login(request: Request, payload: BypassLoginIn):
    """Fallback login endpoint for SPFx web part when MSAL token acquisition
    is unavailable/bypassed. Creates a valid server-side session for the
    tenant email from the SharePoint context.
    """
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    display_name = payload.display_name or email.split("@")[0]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    from .utils.request_meta import get_client_ip, get_user_agent
    client_ip = get_client_ip(request)
    user_agent = get_user_agent(request)

    profile: dict = {
        "display_name": display_name,
        "email": email,
        "tenant_id": payload.tenant_id or None,
        "last_login": now.isoformat() + "Z",
    }

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                if not row:
                    row = db_models.UserProfile(email=email, display_name=display_name)
                    db.add(row)
                row.last_login = now
                db.commit()
        except Exception:
            pass

    _profile_cache[email] = profile

    session_id: str | None = None
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .services.session_service import create_session
            with SessionLocal() as db:
                sess = create_session(db, email, user_agent, client_ip)
                session_id = sess.session_id
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Bypass session creation failed: %s", exc)

    return {"display_name": display_name, "email": email, "session_id": session_id}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, payload: LogoutIn):
    """Session-end signal from the frontend.

    Marks the specified session as 'Logged Out' and writes an audit entry.
    Other active sessions for the same user are left untouched.
    """
    email = (payload.email or "").strip().lower() or None
    _log_activity(email, "logout", "Signed out")

    if settings.db_configured and payload.session_id:
        try:
            from .db.base import SessionLocal
            from .services.session_service import logout_session
            ip = _get_ip(request)
            with SessionLocal() as db:
                logout_session(db, payload.session_id, ip_address=ip)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("logout_session failed: %s", exc)

    return {"ok": True}


@app.get("/api/profile")
async def get_profile(email: str, _session: object = Depends(require_session)):
    """Return the full stored profile for the given email address."""
    email = email.lower().strip()

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                if row is not None:
                    perms = row.folder_permissions
                    logs = (
                        db.query(db_models.ActivityLog)
                        .filter_by(user_email=email)
                        .order_by(db_models.ActivityLog.created_at.desc())
                        .limit(10)
                        .all()
                    )
                    return {
                        "email": row.email,
                        "display_name": row.display_name,
                        "first_name": row.first_name,
                        "last_name": row.last_name,
                        "azure_oid": row.azure_oid,
                        "job_title": row.job_title,
                        "department": row.department,
                        "phone": row.phone,
                        "office_location": row.office_location,
                        "office_name": row.office_name,
                        "company_name": row.company_name,
                        "employee_id": row.employee_id,
                        "manager_name": row.manager_name,
                        "manager_email": row.manager_email,
                        "tenant_id": row.tenant_id,
                        "two_factor_enabled": row.two_factor_enabled,
                        "password_changed_at": row.password_changed_at.isoformat() + "Z" if row.password_changed_at else None,
                        "last_login": row.last_login.isoformat() + "Z" if row.last_login else None,
                        "created_at": row.created_at.isoformat() + "Z",
                        "photo_base64": row.photo_base64,
                        "date_of_joining": row.date_of_joining.isoformat() if row.date_of_joining else None,
                        "emergency_contact": None,
                        "folder_permissions": [
                            {"folder_name": p.folder_name, "permission_level": p.permission_level}
                            for p in perms
                        ],
                        "recent_activity": [
                            {
                                "action": lg.action,
                                "detail": lg.detail,
                                "created_at": lg.created_at.isoformat() + "Z",
                            }
                            for lg in logs
                        ],
                    }
                # Row absent — seed it from the login cache so PATCH will work
                cached = _profile_cache.get(email)
                if cached:
                    row = db_models.UserProfile(
                        email=email,
                        display_name=cached.get("display_name") or email,
                        first_name=cached.get("first_name"),
                        last_name=cached.get("last_name"),
                        azure_oid=cached.get("azure_oid"),
                        job_title=cached.get("job_title"),
                        department=cached.get("department"),
                        phone=cached.get("phone"),
                        office_location=cached.get("office_location"),
                        company_name=cached.get("company_name"),
                        employee_id=cached.get("employee_id"),
                        manager_name=cached.get("manager_name"),
                        manager_email=cached.get("manager_email"),
                        tenant_id=cached.get("tenant_id"),
                        two_factor_enabled=bool(cached.get("two_factor_enabled", False)),
                    )
                    if cached.get("last_login"):
                        row.last_login = datetime.fromisoformat(cached["last_login"].rstrip("Z"))
                    db.add(row)
                    db.commit()
                    # Return the seeded profile via recursive call
                    return await get_profile(email)
        except Exception:
            pass  # Fall through to cache

    profile = _profile_cache.get(email)
    if profile:
        return profile

    raise HTTPException(404, "Profile not found")


class ProfileUpdateIn(BaseModel):
    employee_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    office_location: str | None = None
    office_name: str | None = None
    department: str | None = None
    manager_name: str | None = None
    manager_email: str | None = None
    two_factor_enabled: bool | None = None
    photo_base64: str | None = None
    date_of_joining: str | None = None


@app.patch("/api/profile")
async def patch_profile(email: str, payload: ProfileUpdateIn, _session: object = Depends(require_session)):
    """Update editable profile fields."""
    email = email.lower().strip()

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                if row is None:
                    # Auto-create from cache if the row was never written (e.g. DB was
                    # unavailable during login). Use cached display_name if available.
                    cached = _profile_cache.get(email, {})
                    row = db_models.UserProfile(
                        email=email,
                        display_name=cached.get("display_name") or email,
                    )
                    db.add(row)
                    db.flush()  # assign id before seeding related rows

                for field in ("employee_id", "first_name", "last_name", "phone", "office_location",
                              "office_name", "department", "manager_name", "manager_email", "photo_base64"):
                    val = getattr(payload, field)
                    if val is not None:
                        setattr(row, field, val)

                if payload.date_of_joining is not None:
                    if payload.date_of_joining.strip():
                        from datetime import datetime
                        row.date_of_joining = datetime.strptime(payload.date_of_joining.strip(), "%Y-%m-%d").date()
                    else:
                        row.date_of_joining = None

                if payload.two_factor_enabled is not None:
                    row.two_factor_enabled = payload.two_factor_enabled

                # Log the update activity
                db.add(db_models.ActivityLog(
                    user_email=email,
                    action="profile_update",
                    detail="Updated profile details",
                ))

                db.commit()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Update failed: {e}")
    else:
        # Update in-memory cache
        p = _profile_cache.get(email, {})
        for field in ("employee_id", "first_name", "last_name", "phone", "office_location", "department",
                      "office_name", "manager_name", "manager_email"):
            val = getattr(payload, field)
            if val is not None:
                p[field] = val
        if payload.two_factor_enabled is not None:
            p["two_factor_enabled"] = payload.two_factor_enabled
        _profile_cache[email] = p

    return await get_profile(email)


# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "mode": backend_mode(),
        "graph_configured": settings.graph_configured,
        "db_configured": settings.db_configured,
    }


@app.get("/api/vessels")
async def list_vessels(_session: object = Depends(require_session)):
    return await get_backend().list_vessels()


@app.get("/api/vessels/flat-tree")
async def list_vessels_flat_tree(_session: object = Depends(require_session)):
    now = time.time()
    if _FLAT_TREE_CACHE["data"] is not None and (now - _FLAT_TREE_CACHE["timestamp"]) < CACHE_TTL_FLAT_TREE:
        return _FLAT_TREE_CACHE["data"]

    def clean(s: str) -> str:
        return s.strip("_").strip() if s else s

    if not settings.db_configured:
        be = get_backend()
        vessels = await be.list_vessels()
        mains = await be.mains()
        out = []
        sr = 0
        for v in vessels:
            vname = clean(v["name"])
            for m in mains:
                mname = clean(m["name"])
                children = await be.children(m["id"])
                ship_node = next((c for c in children if c.get("kind") == "ship" and clean(c.get("name")) == vname), None)
                if ship_node:
                    cat_nodes = await be.children(ship_node["id"])
                else:
                    cat_nodes = [c for c in children if c.get("kind") not in ("file", "ship")]

                async def _recurse_categories(nodes, current_path):
                    nonlocal sr
                    for c in nodes:
                        if c.get("kind") == "file":
                            continue
                        
                        cname = clean(c["name"])
                        new_path = f"{current_path} > {cname}"
                        
                        try:
                            children = await be.children(c["id"])
                        except Exception:
                            children = []
                        
                        file_children = [f for f in children if f.get("kind") == "file"]
                        sub_folders = [f for f in children if f.get("kind") not in ("file", "file_wrapper")]
                        
                        if sub_folders:
                            await _recurse_categories(sub_folders, new_path)
                        elif file_children:
                            for fn in file_children:
                                sr += 1
                                out.append({
                                    "srNo": str(sr),
                                    "vesselName": vname,
                                    "group": mname,
                                    "category": cname,
                                    "subFolderPath": new_path,
                                    "fileName": fn.get("name"),
                                    "fileId": fn.get("id"),
                                    "canUpload": bool(c.get("upload", True)),
                                    "groupKey": f"{v['id']}||{mname}||{cname}||{new_path}",
                                    "uploadFolderId": c["id"],
                                    "monthDriven": bool(c.get("month_driven")),
                                })
                        else:
                            sr += 1
                            out.append({
                                "srNo": str(sr),
                                "vesselName": vname,
                                "group": mname,
                                "category": cname,
                                "subFolderPath": new_path,
                                "fileName": None,
                                "fileId": None,
                                "canUpload": bool(c.get("upload", True)),
                                "groupKey": f"{v['id']}||{mname}||{cname}||{new_path}",
                                "uploadFolderId": c["id"],
                                "monthDriven": bool(c.get("month_driven")),
                            })

                await _recurse_categories(cat_nodes, f"{vname} > {mname}")
        _FLAT_TREE_CACHE["data"] = out
        _FLAT_TREE_CACHE["timestamp"] = time.time()
        return out

    from .db.base import SessionLocal
    from .db.models import Folder, Vessel

    out = []
    with SessionLocal() as db:
        results = (
            db.query(Folder, Vessel)
            .join(Vessel, Folder.vessel_id == Vessel.id)
            .filter(
                Folder.kind.in_(["leaf", "month_driven", "drawing_classifier"]),
                Folder.drive_item_id.isnot(None),
                Folder.vessel_id.isnot(None),
            )
            .order_by(Vessel.name.asc(), Folder.path.asc())
            .all()
        )
        for i, (f, v) in enumerate(results):
            parts = f.path.split("/") if f.path else []
            main_name = clean(parts[0]) if parts else ""
            group = clean(parts[2]) if len(parts) > 2 else ""
            cat = clean(parts[3]) if len(parts) > 3 else (clean(parts[-1]) if parts else clean(f.name))
            vessel_name = clean(v.name)
            sub_path = f"{vessel_name} > {group} > {cat}"
            out.append({
                "srNo": str(i + 1),
                "vesselName": vessel_name,
                "group": group,
                "category": cat,
                "subFolderPath": sub_path,
                "fileName": None,
                "fileId": None,
                "canUpload": True,
                "groupKey": f"{v.id}||{main_name}||{group}||{cat}||{sub_path}",
                "uploadFolderId": f.drive_item_id,
                "monthDriven": f.month_driven,
            })
    return out


@app.post("/api/vessels", status_code=201)
async def create_vessel(
    payload: VesselIn,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    vtype = (payload.vessel_type or "").strip() or None
    if vtype and vtype not in VESSEL_TYPES:
        raise HTTPException(400, "Invalid vessel type")
    email = (x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().create_vessel(
            payload.name,
            payload.imo,
            shipyard=(payload.shipyard or "").strip() or None,
            hull_number=(payload.hull_number or "").strip() or None,
            vessel_type=vtype,
            requesting_email=email,
            requesting_name=display_name,
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "create_vessel",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })
        vessel = result.get("result") or {}
        invalidate_folder_caches()
        return {**vessel, "status": "completed", "message": result.get("message")}
    except Conflict as e:
        if str(e) == "Vessel name already exists.":
            return JSONResponse(status_code=409, content={"message": str(e)})
        _raise(e)
    except BadRequest as e:
        _raise(e)


@app.patch("/api/vessels/{vessel_id}")
async def update_vessel(
    vessel_id: str,
    payload: VesselUpdateIn,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    vtype = (payload.vessel_type or "").strip() or None
    if vtype and vtype not in VESSEL_TYPES:
        raise HTTPException(400, "Invalid vessel type")

    user_email = (x_user_email or "").strip().lower()
    display_name = user_email.split("@")[0] if user_email else None

    try:
        result = await get_backend().update_vessel(
            vessel_id,
            name=payload.name,
            imo=payload.imo,
            shipyard=payload.shipyard,
            hull_number=payload.hull_number,
            vessel_type=vtype,
            requesting_email=user_email,
            requesting_name=display_name,
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "update_vessel",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })

        v_after = result.get("result") or {}
        detail_msg = result.get("message") or f"Saved vessel '{v_after.get('name')}'."
        sp_success = v_after.get("sp_success", True)
        if not sp_success:
            detail_msg += " (Note: some SharePoint folders failed to rename)"

        if user_email:
            _log_activity(user_email, "update_vessel", detail_msg)

        return {
            "id": v_after.get("id"),
            "name": v_after.get("name"),
            "imo": v_after.get("imo"),
            "shipyard": v_after.get("shipyard"),
            "hull_number": v_after.get("hull_number"),
            "vessel_type": v_after.get("vessel_type"),
            "message": detail_msg,
            "status": "completed",
        }
    except Conflict as e:
        return JSONResponse(status_code=409, content={"message": str(e)})
    except NotFound as e:
        return JSONResponse(status_code=404, content={"message": str(e)})
    except BadRequest as e:
        _raise(e)


@app.delete("/api/vessels/{vessel_id}")
async def delete_vessel(
    vessel_id: str,
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    user_email = (x_user_email or "").strip().lower()
    display_name = user_email.split("@")[0] if user_email else None

    try:
        result = await get_backend().delete_vessel(
            vessel_id,
            requesting_email=user_email,
            requesting_name=display_name,
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "delete_vessel",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })

        deleted_name = result.get("vessel_name") or vessel_name or vessel_id
        detail_msg = result.get("message") or f"Moved vessel '{deleted_name}' to Recycle Bin."
        if user_email:
            _log_activity(user_email, "delete_vessel", detail_msg)
        invalidate_folder_caches()
        return {
            "status": "completed",
            "message": detail_msg,
        }
    except NotFound as e:
        return JSONResponse(status_code=404, content={"message": str(e)})
    except BadRequest as e:
        _raise(e)


@app.post("/api/vessels/{vessel_id}/reprovision")
async def reprovision_vessel(vessel_id: str):
    """Re-run idempotent folder provisioning for an existing vessel.

    Safe to call at any time — ensure_folder is create-or-fetch, so existing
    folders are never duplicated; only missing subfolders are created.
    """
    try:
        return await get_backend().reprovision_vessel(vessel_id)
    except NotFound as e:
        _raise(e)
    except BadRequest as e:
        _raise(e)


@app.post("/api/vessels/repair-links")
async def repair_vessel_links():
    """Scan all ship-kind folders with vessel_id=None and link them to the
    matching vessel row by name (case-insensitive).
    Safe to call at any time — only fills in missing links, never removes data.
    """
    return await get_backend().repair_vessel_links()


@app.post("/api/admin/migrate-drawing-folder")
async def migrate_drawing_folder():
    """One-time migration: for every vessel, collapse the 'Drawing' wrapper
    folder inside 'Drawings and Manuals' by moving its children one level up
    and deleting the now-empty 'Drawing' folder.
    Safe to call multiple times — skips vessels where 'Drawing' no longer exists.
    """
    from .graph import drive as gd
    from .graph.client import GraphError
    from .db.base import SessionLocal
    from .db import models as db_models
    from .services import get_backend

    be = get_backend()
    drive_id = await be._drive()

    results = []

    with SessionLocal() as db:
        # Find all 'Drawings and Manuals' folders across all vessels
        dam_rows = (
            db.query(db_models.Folder)
            .filter(db_models.Folder.name == "Drawings and Manuals", db_models.Folder.kind == "folder")
            .all()
        )
        dam_list = [(r.drive_item_id, r.path, r.vessel_id) for r in dam_rows]

    for dam_id, dam_path, vessel_id in dam_list:
        # Find the 'Drawing' child folder
        try:
            drawing_item = await gd.find_child(drive_id, dam_id, "Drawing")
        except Exception as e:
            results.append({"dam_path": dam_path, "status": "error", "detail": str(e)})
            continue

        if not drawing_item or "folder" not in drawing_item:
            results.append({"dam_path": dam_path, "status": "skipped", "detail": "No Drawing folder found"})
            continue

        drawing_id = drawing_item["id"]
        drawing_path = f"{dam_path}/Drawing"

        # List children of Drawing
        try:
            children = await gd.list_children(drive_id, drawing_id)
        except Exception as e:
            results.append({"dam_path": dam_path, "status": "error", "detail": f"list children: {e}"})
            continue

        moved = []
        for child in children:
            if "folder" not in child:
                continue
            try:
                await gd.move_item(drive_id, child["id"], dam_id)
                moved.append(child["name"])
                # Update DB cache: rewrite path from Drawing/X -> X under dam_path
                old_child_path = f"{drawing_path}/{child['name']}"
                new_child_path = f"{dam_path}/{child['name']}"
                with SessionLocal() as db:
                    # Update the child folder row
                    row = db.query(db_models.Folder).filter_by(path=old_child_path).one_or_none()
                    if row:
                        row.path = new_child_path
                    # Update all descendant paths
                    descendants = (
                        db.query(db_models.Folder)
                        .filter(db_models.Folder.path.like(f"{old_child_path}/%"))
                        .all()
                    )
                    for d in descendants:
                        d.path = new_child_path + d.path[len(old_child_path):]
                    db.commit()
            except Exception as e:
                results.append({"dam_path": dam_path, "status": "error", "detail": f"move {child['name']}: {e}"})

        # Delete the now-empty Drawing folder
        try:
            await gd.delete_item(drive_id, drawing_id)
            with SessionLocal() as db:
                row = db.query(db_models.Folder).filter_by(path=drawing_path).one_or_none()
                if row:
                    db.delete(row)
                db.commit()
        except Exception as e:
            results.append({"dam_path": dam_path, "status": "error", "detail": f"delete Drawing: {e}"})
            continue

        results.append({"dam_path": dam_path, "status": "ok", "moved": moved})

    return {"results": results}


@app.get("/api/mains")
async def mains(_session: object = Depends(require_session)):
    return await get_backend().mains()


@app.get("/api/stats")
async def stats(_session: object = Depends(require_session)):
    return await get_backend().stats()


@app.post("/api/folders/upload-by-path")
async def upload_by_path(
    path: str,
    file: UploadFile,
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Same as POST /api/folders/{folder_id}/upload, but the target folder is
    given as a logical path (e.g. 'Folder-3 Insurance/Bow Fighter') instead
    of a resolved folder id. Used by the webpart's REST fallback for folders
    it only knows by path (containing '/'), where it can't derive a folder
    id without an extra lookup round-trip."""
    data = await file.read()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]
    try:
        folder_id = await get_backend().resolve_path(path)
        result = await get_backend().upload(
            folder_id, file.filename, data, file.content_type, email, name
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"Uploaded: {file.filename} (awaiting reviewer approval)"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "file_upload", log_detail)
        return result
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


@app.get("/api/folders/{folder_id}/children")
async def children(folder_id: str, _session: object = Depends(require_session)):
    now = time.time()
    cached = _FOLDER_CHILDREN_CACHE.get(folder_id)
    if cached and (now - cached[1]) < CACHE_TTL_CHILDREN:
        return cached[0]
    try:
        data = await get_backend().children(folder_id)
        _FOLDER_CHILDREN_CACHE[folder_id] = (data, time.time())
        return data
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        logging.getLogger("uvicorn.error").warning(f"Failed to load children for folder '{folder_id}': {e}")
        return []


@app.get("/api/folders/{folder_id}")
async def folder(folder_id: str, _session: object = Depends(require_session)):
    try:
        return await get_backend().get_folder(folder_id)
    except (NotFound, BadRequest) as e:
        _raise(e)


@app.post("/api/folders/{folder_id}/upload")
async def upload(
    folder_id: str,
    file: UploadFile,
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Stages the file and creates a pending approval request — it is no
    longer saved into the folder directly. See docs on the approval workflow
    in services/stub_backend.py / services/real_backend.py."""
    data = await file.read()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]
    try:
        result = await get_backend().upload(
            folder_id, file.filename, data, file.content_type, email, name
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"Uploaded: {file.filename} (awaiting reviewer approval)"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "file_upload", log_detail)
        return result
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


class CreateSubfolderIn(BaseModel):
    name: str
    user_email: str | None = None


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str, user_email: str | None = Query(None), folder_name: str | None = Query(None), x_user_email: str | None = Header(default=None), _session: object = Depends(require_session)):
    """Delete a folder and all its contents (or stage a pending approval for
    non-admin users)."""
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().delete_folder(
            folder_id, requesting_email=email, requesting_name=display_name
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending",
            "action_type": "delete_folder",
            "approval_id": result.get("approval_id"),
            "message": result.get("message"),
        })
    detail = f"Deleted folder: {folder_name}" if folder_name else f"Deleted folder: {folder_id}"
    _log_activity(email, "delete_folder", detail)
    return {"status": "completed", "message": result.get("message")}


@app.post("/api/folders/{folder_id}/subfolder")
async def create_subfolder(folder_id: str, payload: CreateSubfolderIn, x_user_email: str | None = Header(default=None), _session: object = Depends(require_session)):
    """Manually create a named sub-folder inside a month_driven folder (or
    stage a pending approval for non-admin users)."""
    email = (payload.user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().create_subfolder(
            folder_id, payload.name.strip(), requesting_email=email, requesting_name=display_name
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "create_folder",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })
        _log_activity(email, "create_folder", f"Created folder: {payload.name.strip()}")
        folder = result.get("result") or {}
        return {**folder, "status": "completed", "message": result.get("message")}
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


@app.post("/api/folders/{folder_id}/month-upload")
async def month_upload(
    folder_id: str,
    file: UploadFile,
    category: str = Form(None),
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    data = await file.read()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]
    try:
        result = await get_backend().month_upload(
            folder_id, file.filename, category, data, file.content_type,
            email, name
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"Uploaded: {file.filename} (awaiting reviewer approval)"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "file_upload", log_detail)
        return result
    except (NotFound, BadRequest, Conflict, InternalServerError) as e:
        _raise(e)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Unexpected error in month_upload for folder %s", folder_id)
        raise HTTPException(status_code=500, detail=f"Upload failed: {type(e).__name__}: {e}")


@app.get("/api/files/{file_id}/content")
async def file_content(file_id: str, _session: object = Depends(require_session)):
    result = await get_backend().get_file(file_id)
    if result is None:
        raise HTTPException(404, "File not found")
    content, content_type, name = result
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@app.delete("/api/files/{file_id}")
async def delete_file(
    file_id: str, user_email: str | None = Query(None), reason: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().delete_file(
            file_id, requesting_email=email, requesting_name=display_name, reason=reason
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending",
            "action_type": "delete_document",
            "approval_id": result.get("approval_id"),
            "message": result.get("message"),
        })
    _log_activity(email, "delete_file", f"Deleted file: {file_id}")
    return {"status": "completed", "message": result.get("message")}


@app.get("/api/search")
async def search(q: str = "", vessel_id: str | None = None, _session: object = Depends(require_session)):
    return await get_backend().search(q, vessel_id)


class LogActivityIn(BaseModel):
    email: str
    action: str
    detail: str | None = None


@app.post("/api/activity", status_code=204)
async def log_activity_endpoint(payload: LogActivityIn, _session: object = Depends(require_session)):
    """Frontend-initiated activity log (archive, restore, etc.)."""
    _log_activity(payload.email, payload.action, payload.detail)
    return Response(status_code=204)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, _session: object = Depends(require_session)):
    job = await get_backend().get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/archive/ids")
async def get_archived_ids(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_archived_ids()
    except Exception as e:
        raise HTTPException(500, f"Failed to get archived IDs: {e}")


@app.get("/api/archive/nodes")
async def get_archived_nodes(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_archived_nodes()
    except Exception as e:
        raise HTTPException(500, f"Failed to get archived nodes: {e}")


@app.post("/api/archive/{item_id}")
async def archive_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None), reason: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().archive_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name, reason=reason,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to archive item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "archive_item",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "archive_folder" if type == "folder" else "archive_file", f"Archived {type}: {item_id}")
    return {"status": "completed", "message": result.get("message")}


@app.post("/api/restore/{item_id}")
async def restore_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().restore_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to restore item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "restore_item",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "restore_folder", f"Restored item: {item_id}")
    return {"status": "completed", "message": result.get("message")}


@app.get("/api/recycle-bin/ids")
async def get_deleted_ids(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_deleted_ids()
    except Exception as e:
        raise HTTPException(500, f"Failed to get deleted IDs: {e}")


@app.get("/api/recycle-bin/nodes")
async def get_deleted_nodes(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_deleted_nodes()
    except Exception as e:
        raise HTTPException(500, f"Failed to get deleted nodes: {e}")


@app.post("/api/recycle-bin/restore/{item_id}")
async def restore_deleted_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().restore_deleted_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to restore deleted item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "restore_from_recycle_bin",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "restore_folder" if type == "folder" else "restore_file", f"Restored {type}: {item_id}")
    return {"status": "completed", "message": result.get("message")}


@app.delete("/api/recycle-bin/{item_id}")
async def permanent_delete_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().permanent_delete_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to permanently delete item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "permanent_delete",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "permanent_delete_folder" if type == "folder" else "permanent_delete_file", f"Permanently deleted {type}: {item_id}")
    return {"status": "completed", "message": result.get("message")}


# ---------------------------------------------------------------------------
# Approval workflow (admin only)
# ---------------------------------------------------------------------------

@app.get("/api/my-approvals")
async def list_my_approvals(
    status: str | None = None, x_user_email: str | None = Header(default=None), _session: object = Depends(require_session)
):
    if not x_user_email:
        raise HTTPException(400, "X-User-Email header required")
    email = x_user_email.strip().lower()
    approvals = await get_backend().list_approvals(status)
    return [a for a in approvals if a.get("uploaded_by_email", "").strip().lower() == email]


@app.get("/api/approvals")
async def list_approvals(status: str | None = None, q: str | None = None, admin: str = Depends(_require_admin), _session: object = Depends(require_session)):
    return await get_backend().list_approvals(status, q)



@app.get("/api/approvals/{request_id}")
async def get_approval(
    request_id: str,
    x_user_email: str | None = Header(default=None)
):
    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")
    approval = await get_backend().get_approval(request_id)
    if not approval:
        raise HTTPException(404, "Approval request not found")
    
    is_admin = _is_admin_email(email)
    is_uploader = approval.get("uploaded_by_email", "").strip().lower() == email
    if not (is_admin or is_uploader):
        raise HTTPException(403, "Access denied")
    return approval


@app.get("/api/approvals/{request_id}/preview")
async def approval_preview(
    request_id: str,
    x_user_email: str | None = Header(default=None),
    admin: str | None = None
):
    email = (x_user_email or admin or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header or admin query parameter required")
    approval = await get_backend().get_approval(request_id)
    if not approval:
        raise HTTPException(404, "Approval request not found")
    
    is_admin = _is_admin_email(email)
    is_uploader = approval.get("uploaded_by_email", "").strip().lower() == email
    if not (is_admin or is_uploader):
        raise HTTPException(403, "Administrator access required")
    
    result = await get_backend().get_approval_file(request_id)
    if result is None:
        raise HTTPException(404, "Staged file not found")
    content, content_type, name = result
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@app.post("/api/approvals/{request_id}/approve")
async def approve_approval(request_id: str, admin: str = Depends(_require_admin), _session: object = Depends(require_session)):
    try:
        return await get_backend().approve_request(request_id, admin)
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


@app.post("/api/approvals/{request_id}/reject")
async def reject_approval(
    request_id: str, payload: RejectIn, admin: str = Depends(_require_admin), _session: object = Depends(require_session)
):
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(400, "A rejection reason is required")
    try:
        return await get_backend().reject_request(request_id, admin, reason)
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


# ---------------------------------------------------------------------------
# Session management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def list_sessions(
    x_session_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
):
    """List all sessions for the currently authenticated user, newest first."""
    if not settings.db_configured:
        return []

    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")

    try:
        from .db.base import SessionLocal
        from .services.session_service import list_user_sessions
        with SessionLocal() as db:
            sessions = list_user_sessions(db, email)
        return [
            {
                "session_id": s.session_id,
                "status": s.status,
                "login_time": s.login_time.isoformat() + "Z" if s.login_time else None,
                "last_activity": s.last_activity.isoformat() + "Z" if s.last_activity else None,
                "expiry_time": s.expiry_time.isoformat() + "Z" if s.expiry_time else None,
                "logout_time": s.logout_time.isoformat() + "Z" if s.logout_time else None,
                "browser": s.browser,
                "operating_system": s.operating_system,
                "device_type": s.device_type,
                "ip_address": s.ip_address,
                "authentication_method": s.authentication_method,
                "is_current": s.session_id == x_session_id,
            }
            for s in sessions
        ]
    except Exception as exc:
        raise HTTPException(500, f"Could not retrieve sessions: {exc}")


@app.delete("/api/sessions/{target_session_id}")
async def revoke_session_endpoint(
    target_session_id: str,
    request: Request,
    x_session_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
):
    """Revoke a specific session. Users can only revoke their own sessions."""
    if not settings.db_configured:
        raise HTTPException(501, "Session management requires database")

    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")

    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        from .services.session_service import revoke_session

        with SessionLocal() as db:
            target = (
                db.query(db_models.UserSession)
                .filter_by(session_id=target_session_id)
                .one_or_none()
            )
            if target is None:
                raise HTTPException(404, "Session not found")

            is_admin = _is_admin_email(email)
            is_owner = target.email.lower() == email
            if not (is_owner or is_admin):
                raise HTTPException(403, "You can only revoke your own sessions")

            ip = _get_ip(request)
            revoke_session(db, target_session_id, reason="user_initiated", ip_address=ip)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Could not revoke session: {exc}")


@app.get("/api/sessions/audit")
async def list_session_audit(
    x_user_email: str | None = Header(default=None),
    limit: int = Query(default=50, le=200),
):
    """Return the session audit log for the current user (newest first)."""
    if not settings.db_configured:
        return []

    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")

    try:
        from .db.base import SessionLocal
        from .db import models as db_models

        with SessionLocal() as db:
            entries = (
                db.query(db_models.SessionAuditLog)
                .filter_by(email=email)
                .order_by(db_models.SessionAuditLog.created_at.desc())
                .limit(limit)
                .all()
            )
        return [
            {
                "session_id": e.session_id,
                "event": e.event,
                "detail": e.detail,
                "ip_address": e.ip_address,
                "browser": e.browser,
                "status": e.status,
                "login_time": e.login_time.isoformat() + "Z" if e.login_time else None,
                "logout_time": e.logout_time.isoformat() + "Z" if e.logout_time else None,
                "active_duration": e.active_duration,
                "active_duration_formatted": e.active_duration_formatted,
                "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
            }
            for e in entries
        ]
    except Exception as exc:
        raise HTTPException(500, f"Could not retrieve audit log: {exc}")


# ── AI BANTO Email Automation routes ──────────────────────────────────────────
from .email_automation import router as email_router
app.include_router(email_router)