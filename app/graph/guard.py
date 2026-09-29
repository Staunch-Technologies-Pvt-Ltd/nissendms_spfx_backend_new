"""Protected-SharePoint-target guard.

DISABLED 2026-09-24: this used to hard-block, outside production, any
SharePoint / Graph operation (read-scan, create, write or delete) against
NKSDocMan. Per explicit instruction, that block has been removed — NKSDocMan
is no longer treated as protected by default, in any environment, because it
was locking legitimate folders/files out of the app. The machinery below is
left in place (inert) only as an opt-in escape hatch: set
PROTECTED_SITE_PATTERNS and/or PROTECTED_DRIVE_IDS in backend/.env if a site
ever needs this guard again. With those unset (the default now), nothing is
protected and assert_allowed()/is_protected() never block.

The guard is enforced at the Graph choke points (GraphClient.request /
GraphClient.sp_request, see graph/client.py) plus the few helpers in
graph/drive.py that call SharePoint REST directly, so every caller —
existing endpoints, the pool, and the Folder Structure Mode service — is
covered without per-call changes, IF re-enabled via the env vars below.

A target is protected when, case-insensitively and ignoring punctuation,
  * the request URL / site URL / path contains a protected pattern, or
  * the request URL contains the drive id or site id of a configured site
    whose site key, display name, site name or computed web URL contains a
    protected pattern (Graph URLs carry ids, not names).

Environment variables (backend/.env):
  DMS_ENVIRONMENT          dev | test | prod.  Only "prod"/"production"
                           disables the guard.  When unset, falls back to
                           APP_ENV / ACTIVE_SITE == "prod".
  PROTECTED_SITE_PATTERNS  comma list, default "" (none — nothing protected
                           unless explicitly set here).
  PROTECTED_DRIVE_IDS      optional comma list of extra drive/site ids.
  PROTECTED_SITE_GUARD     "all" (default) — every Graph/SharePoint call;
                           "folder_mode" — only Folder Structure Mode
                           operations are checked (escape hatch for a dev
                           machine that must still browse production data
                           read-only through the legacy screens).
"""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .client import GraphError

log = logging.getLogger(__name__)

# Previously ("nksdocman", "nksdocsman") — NKSDocMan is no longer protected
# by default. Left empty so protection is strictly opt-in via
# PROTECTED_SITE_PATTERNS in backend/.env.
DEFAULT_PATTERNS: tuple[str, ...] = ()
_CACHE_TTL_SECONDS = 300

_ids_cache: tuple[float, frozenset[str]] | None = None
_raw_cache: object | None = None
_protected_read_override: ContextVar[bool] = ContextVar(
    "protected_read_override", default=False
)
_protected_delete_override: ContextVar[bool] = ContextVar(
    "protected_delete_override", default=False
)


class ProtectedTargetError(GraphError):
    """Raised when a non-production process targets a protected site.

    A GraphError (status 423 Locked) so every existing `except GraphError`
    path and API error mapper handles it as a hard, non-retryable failure.
    """

    def __init__(self, target: str, reason: str):
        self.target = target
        self.reason = reason
        super().__init__(423, (
            f"Blocked: SharePoint target is protected ({reason}). "
            "This site is listed in PROTECTED_SITE_PATTERNS / "
            "PROTECTED_DRIVE_IDS and must not be scanned, read or written "
            "outside production. Remove it from those settings, or point "
            "ACTIVE_SITE / the selected site at a different site."
        ))


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _raw_env():
    global _raw_cache
    if _raw_cache is None:
        try:
            from ..config import _RawEnv

            _raw_cache = _RawEnv()
        except Exception:  # pragma: no cover - config import failure
            _raw_cache = object()
    return _raw_cache


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val is None:
        val = getattr(_raw_env(), key.lower(), None)
    return str(val).strip() if val is not None else default


def deployment_env() -> str:
    explicit = _env("DMS_ENVIRONMENT").lower()
    if explicit:
        return explicit
    for key in ("APP_ENV", "ACTIVE_SITE"):
        if _env(key).lower() in ("prod", "production"):
            return "prod"
    return "dev"


def is_production() -> bool:
    return deployment_env() in ("prod", "production")


def guard_scope() -> str:
    scope = _env("PROTECTED_SITE_GUARD", "all").lower()
    return scope if scope in ("all", "folder_mode") else "all"


def protected_patterns() -> tuple[str, ...]:
    """Patterns are strictly opt-in now (DEFAULT_PATTERNS is empty): with
    PROTECTED_SITE_PATTERNS unset, nothing is protected."""
    raw = _env("PROTECTED_SITE_PATTERNS")
    items = [_norm(p) for p in raw.split(",")] if raw else list(DEFAULT_PATTERNS)
    items = [p for p in items if p]
    return tuple(items)


def _matches_pattern(value: str | None) -> str | None:
    n = _norm(value)
    if not n:
        return None
    for p in protected_patterns():
        if p in n:
            return p
    return None


def protected_ids(force: bool = False) -> frozenset[str]:
    """Drive / site ids of every configured site that is protected."""
    global _ids_cache
    now = time.monotonic()
    if not force and _ids_cache and now - _ids_cache[0] < _CACHE_TTL_SECONDS:
        return _ids_cache[1]

    ids: set[str] = set()
    for extra in _env("PROTECTED_DRIVE_IDS").split(","):
        if extra.strip():
            ids.add(extra.strip().lower())
    try:
        from ..config import Settings

        for key, info in (Settings.discover_available_sites() or {}).items():
            labels = [
                key,
                info.get("name"),
                info.get("sp_site_name"),
                info.get("display_name"),
                info.get("site_name"),
                info.get("web_url"),
            ]
            if any(_matches_pattern(v) for v in labels):
                for id_key in ("drive_id", "site_id"):
                    val = str(info.get(id_key) or "").strip().lower()
                    if val:
                        ids.add(val)
    except Exception as exc:  # DB down etc. — keep explicit ids only
        log.debug("protected_ids: site discovery failed: %s", exc)
    _ids_cache = (now, frozenset(ids))
    return _ids_cache[1]


def is_protected(*targets: str | None) -> str | None:
    """Return a reason string if any target is protected, else None."""
    ids = None
    for target in targets:
        if not target:
            continue
        hit = _matches_pattern(target)
        if hit:
            return f"name matches '{hit}'"
        low = str(target).lower()
        if ids is None:
            ids = protected_ids()
        for pid in ids:
            if pid and pid in low:
                return "drive/site id belongs to a protected site"
    return None


@contextmanager
def allow_protected_reads():
    """Temporarily allow protected-site GETs for an explicit read-only flow."""
    token = _protected_read_override.set(True)
    try:
        yield
    finally:
        _protected_read_override.reset(token)


def protected_read_endpoint(endpoint):
    """Wrap a read-only API endpoint with the protected GET scope."""
    @wraps(endpoint)
    async def wrapped(*args, **kwargs):
        with allow_protected_reads():
            return await endpoint(*args, **kwargs)
    return wrapped


def protected_delete_operation(endpoint):
    """Scope protected GET/DELETE access to the vessel-delete workflow."""
    @wraps(endpoint)
    async def wrapped(*args, **kwargs):
        token = _protected_delete_override.set(True)
        try:
            return await endpoint(*args, **kwargs)
        finally:
            _protected_delete_override.reset(token)
    return wrapped


def assert_allowed(*targets: str | None, operation: str = "graph", folder_mode: bool = False) -> None:
    """Raise ProtectedTargetError when a non-prod process targets NKSDocMan.

    `folder_mode=True` marks calls made by the Folder Structure Mode service;
    those are always checked outside production, whatever PROTECTED_SITE_GUARD
    says.
    """
    if is_production():
        return
    if not folder_mode and guard_scope() != "all":
        return
    if not folder_mode and operation.lower().startswith("graph get") and _protected_read_override.get():
        return
    if not folder_mode and _protected_delete_override.get() and operation.lower() in {"graph get", "graph delete"}:
        return
    reason = is_protected(*targets)
    if reason:
        shown = next((t for t in targets if t), "")
        log.error(
            "[protected-site-guard] BLOCKED %s on %r (%s) env=%s",
            operation, str(shown)[:300], reason, deployment_env(),
        )
        raise ProtectedTargetError(str(shown), reason)


def reset_cache() -> None:
    """Test helper / call after site configuration changes."""
    global _ids_cache, _raw_cache
    _ids_cache = None
    _raw_cache = None
