"""Application configuration.

Single .env file holds all three environments (local / dev / prod).
Set APP_ENV=local|dev|prod to activate the right block.
Shared keys (no prefix) are always read directly.
"""
import re
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus
from contextvars import ContextVar

from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Global cache for all available site configurations (loaded once at startup)
_SITE_CONFIGS_CACHE: dict[str, 'Settings'] = {}
_CURRENT_SESSION_ID: ContextVar[str | None] = ContextVar("current_session_id", default=None)


class _RawEnv(BaseSettings):
    """Reads every key from .env without validation — used only to resolve ACTIVE_SITE / APP_ENV."""
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="allow"
    )
    active_site: str | None = None
    app_env: str = "local"


def _prefixed(prefix: str, key: str, raw: _RawEnv, default: str = "") -> str:
    """Return PREFIX_KEY from raw env, falling back to un-prefixed KEY, then default."""
    val = getattr(raw, f"{prefix}_{key}".lower(), None)
    if val is not None and str(val).strip():
        return str(val)
    val = getattr(raw, key.lower(), None)
    if val is not None and str(val).strip():
        return str(val)
    return default


def _prefixed_int(prefix: str, key: str, raw: _RawEnv, default: int = 0) -> int:
    val = getattr(raw, f"{prefix}_{key}".lower(), None)
    if val is None or not str(val).strip():
        val = getattr(raw, key.lower(), None)
    try:
        return int(val) if val is not None and str(val).strip() else default
    except (TypeError, ValueError):
        return default


def _normalize_site_token(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_SITE_REGISTRY_CACHE: dict[str, object] = {"data": None, "ts": 0.0}
_SITE_REGISTRY_TTL_SECONDS = 30.0


def _get_site_registry() -> dict[str, dict]:
    """The live, config/env-driven site registry (see Settings.discover_
    available_sites — one entry per configured .env prefix, e.g. LOCAL_*,
    DEV_*, NKSDOCMAN_*, NISSENKAIUNEXTERNAL_*), short-TTL-cached so
    site_alias_matches (called many times per request) doesn't re-read
    .env and hit the DB on every comparison."""
    now = time.time()
    cached = _SITE_REGISTRY_CACHE["data"]
    if cached is not None and (now - _SITE_REGISTRY_CACHE["ts"]) < _SITE_REGISTRY_TTL_SECONDS:
        return cached  # type: ignore[return-value]
    try:
        sites = Settings.discover_available_sites()
    except Exception:
        sites = cached or {}
    _SITE_REGISTRY_CACHE["data"] = sites
    _SITE_REGISTRY_CACHE["ts"] = now
    return sites  # type: ignore[return-value]


def _resolve_configured_site_key(normalized_token: str) -> str | None:
    """Resolve an already-normalized site token to the real, configured
    site_key it identifies — using only the live site registry (env/config
    + site_configurations DB rows), never a hardcoded list of site names.
    Returns None if it doesn't match any currently configured site."""
    if not normalized_token:
        return None
    try:
        sites = _get_site_registry()
    except Exception:
        return None

    # 1. Exact match on the configured site_key itself (the .env prefix,
    # lowercased — e.g. "local", "dev", "nksdocman", "nissenkaiunexternal").
    if normalized_token in sites:
        return normalized_token

    tokens = normalized_token.split()

    # 2. The site_key appears as a whole word in the value (handles values
    # like "Vessel DMS (dev)" -> normalized "vessel dms dev", which
    # contains the configured site_key "dev" as a token).
    for key in sites:
        if key and key in tokens:
            return key

    # 3. Match against that site's own configured display name / SharePoint
    # URL (handles a value recorded under its human label rather than its
    # site_key, e.g. sp_site_name "Communication Site" or "NKSDocMan").
    for key, info in sites.items():
        for raw_candidate in (info.get("sp_site_name"), info.get("web_url"), info.get("name")):
            candidate = _normalize_site_token(raw_candidate)
            if candidate and (candidate == normalized_token or candidate in normalized_token or normalized_token in candidate):
                return key

    return None


def site_alias_matches(site_a: str | None, site_b: str | None) -> bool:
    """Treat two site references as the same site only when they resolve to
    the same CONFIGURED site (see _resolve_configured_site_key) — driven
    entirely by the live env/config site registry, never by a hardcoded
    list of site-name synonyms in code. A value that happens to be
    "local" is only the same site as "nksdocman" if the site registry
    itself says so (e.g. both prefixes point at the same drive_id);
    otherwise they are two distinct configured sites and must not be
    merged, however similar their names look.
    """
    if site_a is None and site_b is None:
        return True
    if site_a is None or site_b is None:
        return False

    a_norm = _normalize_site_token(site_a)
    b_norm = _normalize_site_token(site_b)
    if not a_norm or not b_norm:
        return a_norm == b_norm
    if a_norm == b_norm:
        return True

    a_key = _resolve_configured_site_key(a_norm)
    b_key = _resolve_configured_site_key(b_norm)
    if not (a_key and b_key):
        return False
    if a_key == b_key:
        return True
    # Two configured site keys are the same site when the registry maps
    # them to the same document library (drive_id) — e.g. an .env prefix
    # and a site_configurations row describing the same SharePoint site.
    try:
        sites = _get_site_registry()
    except Exception:
        return False
    a_drive = str((sites.get(a_key) or {}).get("drive_id") or "").strip()
    b_drive = str((sites.get(b_key) or {}).get("drive_id") or "").strip()
    return bool(a_drive and a_drive == b_drive)


_ENV_SITE_URL_CACHE: dict[str, object] = {"data": None, "ts": 0.0}
_SITE_CONFIG_ROWS_CACHE: dict[str, object] = {"data": None, "ts": 0.0}


def _env_site_urls() -> dict[str, str]:
    """{env prefix (lowercased): <PREFIX>_SHAREPOINT_SITE_URL} for every site
    that sets one in .env, plus "" -> SHAREPOINT_TENANT_URL if defined.
    Short-TTL cached (same window as the site registry)."""
    now = time.time()
    cached = _ENV_SITE_URL_CACHE["data"]
    if cached is not None and (now - _ENV_SITE_URL_CACHE["ts"]) < _SITE_REGISTRY_TTL_SECONDS:
        return cached  # type: ignore[return-value]
    urls: dict[str, str] = {}
    try:
        raw = _RawEnv()
        entries = dict(raw.__dict__)
        entries.update(raw.model_extra or {})
        for key, value in entries.items():
            k = str(key).lower()
            v = str(value or "").strip()
            if not v or v.startswith("<"):
                continue
            if k.endswith("_sharepoint_site_url"):
                urls[k[: -len("_sharepoint_site_url")]] = v.rstrip("/")
            elif k == "sharepoint_tenant_url":
                urls[""] = v.rstrip("/")
    except Exception:
        urls = cached or {}  # type: ignore[assignment]
    _ENV_SITE_URL_CACHE["data"] = urls
    _ENV_SITE_URL_CACHE["ts"] = now
    return urls


def _site_config_rows() -> list[dict]:
    """site_configurations rows (site_key, site_name, display_name, drive_id),
    short-TTL cached. [] if the DB isn't reachable."""
    now = time.time()
    cached = _SITE_CONFIG_ROWS_CACHE["data"]
    if cached is not None and (now - _SITE_CONFIG_ROWS_CACHE["ts"]) < _SITE_REGISTRY_TTL_SECONDS:
        return cached  # type: ignore[return-value]
    rows: list[dict] = []
    try:
        from .db.base import engine
        if engine:
            from sqlalchemy import text
            with engine.connect() as conn:
                rows = [
                    dict(r) for r in conn.execute(
                        text("SELECT site_key, site_name, display_name, drive_id FROM site_configurations")
                    ).mappings().all()
                ]
    except Exception:
        rows = cached or []  # type: ignore[assignment]
    _SITE_CONFIG_ROWS_CACHE["data"] = rows
    _SITE_CONFIG_ROWS_CACHE["ts"] = now
    return rows


def _url_origin(url: str | None) -> str:
    from urllib.parse import urlparse
    parsed = urlparse((url or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _url_has_site_path(url: str | None) -> bool:
    from urllib.parse import urlparse
    path = urlparse((url or "").strip()).path.lower()
    return path.startswith("/sites/") or path.startswith("/teams/")


def _is_generic_site_label(name: str | None, site_key: str | None) -> bool:
    """True for the placeholder labels the app generates itself when no real
    site name is configured (e.g. "Vessel DMS (local)") — not a site path."""
    n = (name or "").strip().lower()
    return not n or n == "vessel dms" or n == f"vessel dms ({(site_key or '').strip().lower()})"


def compute_sp_site_url(site_key: str = "", site_name: str = "", base_url: str = "", drive_id: str = "") -> str:
    """SharePoint site collection URL for a logical site — resolved only
    from configuration (.env + the site_configurations table), never from
    site names hardcoded in code. In priority order:

    1. The site's own ``<SITE_KEY>_SHAREPOINT_SITE_URL`` from .env, when it
       points at a site path (``/sites/...`` or ``/teams/...``).
    2. When that .env URL is only the tenant root and ``drive_id`` is given:
       a site_configurations row under a *different* site_key that uses the
       same drive describes the same site — use its URL. (This is how
       LOCAL_*, whose drive is NKSDocMan's, resolves to /sites/NKSDocMan.)
    3. The site's own .env URL as-is (a tenant-root site, e.g. the
       Communication site configured as DEV_SHAREPOINT_SITE_URL).
    4. ``site_name`` itself if it's already a URL.
    5. ``<tenant root>/sites/<site_name>`` using the name from config/DB.
    6. The tenant root.

    The tenant root comes from ``base_url``, the site's own .env URL, any
    other ``*_SHAREPOINT_SITE_URL`` in .env, or ``SHAREPOINT_TENANT_URL``.
    ``base_url`` is only ever used for its scheme+host (callers pass the
    default site's URL there as a tenant hint).
    """
    key_clean = (site_key or "").strip().lower()
    env_urls = _env_site_urls()
    own_url = env_urls.get(key_clean, "") if key_clean else ""

    tenant_root = (
        _url_origin(base_url)
        or _url_origin(own_url)
        or next((_url_origin(u) for k, u in sorted(env_urls.items()) if _url_origin(u)), "")
    )

    # 1. Explicit, full site URL configured for this site.
    if own_url and _url_has_site_path(own_url):
        return own_url

    # 2. Same drive registered under another site_key in site_configurations.
    drive_clean = (drive_id or "").strip()
    if drive_clean:
        for row in _site_config_rows():
            row_key = (row.get("site_key") or "").strip().lower()
            if not row_key or row_key == key_clean:
                continue
            if (row.get("drive_id") or "").strip() != drive_clean:
                continue
            row_name = row.get("site_name") or row.get("display_name") or ""
            return compute_sp_site_url(row_key, row_name, tenant_root)

    # 3. The site's own .env URL (tenant-root site).
    if own_url:
        return own_url

    # 4. site_name is already a URL.
    name = (site_name or "").strip()
    if name.lower().startswith(("http://", "https://")):
        return name.rstrip("/")

    # 5. Named site collection under the tenant root.
    if tenant_root and not _is_generic_site_label(name, key_clean):
        return f"{tenant_root}/sites/{name}"

    # 6. Tenant root.
    return tenant_root


class Settings:
    # --- Site Identity ---
    active_site: str
    app_env: str
    sp_site_name: str

    # --- Microsoft Entra / Graph ---
    azure_tenant_id: str
    graph_client_id: str
    graph_client_secret: str
    graph_authority: str
    graph_scope: str
    graph_base_url: str
    sharepoint_site_url: str

    # --- Legacy container / SPO drive settings ---
    container_type_id: str
    container_id: str
    container_display_name: str
    drive_id: str
    sp_drive_id: str

    # --- Database ---
    database_url: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # --- CORS ---
    allowed_origins: str

    # --- Email automation ---
    graph_sender_mailbox: str
    ai_banto_recipient: str

    # --- Approval workflow ---
    admin_emails: str
    notify_sender_email: str

    # --- Shared / fixed ---
    month_folder_format: str
    ocr_min_confidence: float
    graph_verify_ssl: bool
    trusted_proxy_hops: int
    session_idle_timeout_minutes: int
    session_max_lifetime_hours: int
    session_revalidation_interval_minutes: int

    def __init__(self):
        raw = _RawEnv()
        # Resolve active site / environment key
        site_raw = (raw.active_site or raw.app_env or "local").strip().lower()
        if not site_raw:
            site_raw = "local"

        p = site_raw.upper()  # prefix: e.g. LOCAL / DEV / PROD / NISSENKAIUN_SG / SITE_A
        base_p = "LOCAL" if _prefixed("LOCAL", "AZURE_TENANT_ID", raw) else "DEV"

        # --- Site Identity ---
        self.active_site           = site_raw
        self.app_env               = site_raw
        self.sp_site_name          = _prefixed(p, "SP_SITE_NAME", raw, f"Vessel DMS ({site_raw})")

        # --- Microsoft Entra / Graph ---
        self.azure_tenant_id       = _prefixed(p, "AZURE_TENANT_ID", raw) or _prefixed(base_p, "AZURE_TENANT_ID", raw)
        self.graph_client_id       = _prefixed(p, "GRAPH_CLIENT_ID", raw) or _prefixed(base_p, "GRAPH_CLIENT_ID", raw)
        self.graph_client_secret   = _prefixed(p, "GRAPH_CLIENT_SECRET", raw) or _prefixed(base_p, "GRAPH_CLIENT_SECRET", raw)
        self.graph_authority       = "https://login.microsoftonline.com"
        self.graph_scope           = "https://graph.microsoft.com/.default"
        self.graph_base_url        = "https://graph.microsoft.com/v1.0"
        self.sharepoint_site_url   = _prefixed(p, "SHAREPOINT_SITE_URL", raw) or _prefixed(base_p, "SHAREPOINT_SITE_URL", raw, "")

        # --- Legacy container settings (unused in SPO-only runtime) ---
        self.container_type_id     = ""
        self.container_id          = ""
        self.container_display_name = ""
        self.drive_id              = _prefixed(p, "DRIVE_ID", raw)
        self.sp_drive_id           = self.drive_id

        # --- Database ---
        self.database_url          = ""  # not used directly; resolved below
        self.db_host               = _prefixed(p, "DB_HOST", raw) or _prefixed(base_p, "DB_HOST", raw)
        self.db_port               = _prefixed_int(p, "DB_PORT", raw, 0) or _prefixed_int(base_p, "DB_PORT", raw, 5432)
        self.db_name               = _prefixed(p, "DB_NAME", raw) or _prefixed(base_p, "DB_NAME", raw)
        self.db_user               = _prefixed(p, "DB_USER", raw) or _prefixed(base_p, "DB_USER", raw)
        self.db_password           = _prefixed(p, "DB_PASSWORD", raw) or _prefixed(base_p, "DB_PASSWORD", raw)

        # --- CORS ---
        self.allowed_origins       = _prefixed(p, "ALLOWED_ORIGINS", raw) or _prefixed(base_p, "ALLOWED_ORIGINS", raw, "*")

        # --- Email automation ---
        self.graph_sender_mailbox  = _prefixed(p, "GRAPH_SENDER_MAILBOX", raw) or _prefixed(base_p, "GRAPH_SENDER_MAILBOX", raw, "")
        self.ai_banto_recipient    = _prefixed(p, "AI_BANTO_RECIPIENT", raw) or _prefixed(base_p, "AI_BANTO_RECIPIENT", raw, "")

        # --- Approval workflow ---
        self.admin_emails          = _prefixed(p, "ADMIN_EMAILS", raw) or _prefixed(base_p, "ADMIN_EMAILS", raw, "")
        self.notify_sender_email   = ""

        # --- Shared / fixed ---
        self.month_folder_format   = getattr(raw, "month_folder_format", "%B %Y")
        self.ocr_min_confidence  = 0.5
        self.graph_verify_ssl     = str(getattr(raw, "graph_verify_ssl", "true")).lower() != "false"
        self.trusted_proxy_hops    = int(getattr(raw, "trusted_proxy_hops", 1) or 1)
        self.session_idle_timeout_minutes       = int(getattr(raw, "session_idle_timeout_minutes", 30) or 30)
        self.session_max_lifetime_hours         = int(getattr(raw, "session_max_lifetime_hours", 8) or 8)
        self.session_revalidation_interval_minutes = int(getattr(raw, "session_revalidation_interval_minutes", 60) or 60)

    # ------------------------------------------------------------------
    @property
    def graph_configured(self) -> bool:
        return bool(
            self.azure_tenant_id
            and self.graph_client_id
            and self.graph_client_secret
        )

    @property
    def sp_configured(self) -> bool:
        return bool(
            self.graph_configured
            and self.drive_id
        )

    @property
    def db_configured(self) -> bool:
        return bool(self.database_url_resolved)

    @property
    def admin_email_set(self) -> set[str]:
        """Return normalized administrator email addresses from configuration."""
        return {
            email.strip().lower()
            for email in self.admin_emails.split(",")
            if email.strip()
        }

    @property
    def database_url_resolved(self) -> str:
        if all([self.db_host, self.db_name, self.db_user, self.db_password]):
            user = quote_plus(self.db_user)
            password = quote_plus(self.db_password)
            return (
                f"postgresql+psycopg2://{user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return ""

    @property
    def authority_url(self) -> str:
        return f"{self.graph_authority}/{self.azure_tenant_id}"

    @property
    def sharepoint_scope(self) -> str:
        """Return the correct token scope for SharePoint REST API calls.

        SharePoint REST validates the token's ``aud`` claim against the tenant
        root URL, NOT the Graph audience.  Using the Graph token against
        ``_api/web`` endpoints always returns 401 regardless of permissions.
        """
        if self.sharepoint_site_url:
            # Normalise: strip trailing slash / path, keep scheme+host only
            from urllib.parse import urlparse
            parsed = urlparse(self.sharepoint_site_url)
            host = f"{parsed.scheme}://{parsed.netloc}"
            return f"{host}/.default"
        return ""

    @staticmethod
    def discover_available_sites() -> dict[str, dict]:
        """Discover all configured sites from .env file and site_configurations table.
        
        Returns a dict mapping site_name -> {
            'name': site_name,
            'sp_site_name': human-readable name,
            'configured': bool (has Graph + Drive configured)
        }
        """
        raw = _RawEnv()
        sites: dict[str, dict] = {}
        
        # Common prefixes that typically have site configs
        common_prefixes = ["LOCAL", "DEV", "PROD"]
        all_env_keys = set(raw.__dict__.keys()) | set(raw.model_extra or {})
        
        # Extract unique prefixes from environment variables
        prefixes = set(common_prefixes)
        for key in all_env_keys:
            parts = key.upper().split("_")
            if len(parts) >= 2 and parts[-1] in ["DRIVE_ID", "AZURE_TENANT_ID", "DB_HOST"]:
                prefix = "_".join(parts[:-1])
                if prefix and prefix not in ("SESSION", "MONTH", "GRAPH", "TRUSTED", "ALLOWED", "ACTIVE", "APP"):
                    prefixes.add(prefix)
        
        # For each prefix, check if it has Graph + Drive configured
        for prefix in sorted(prefixes):
            azure_tenant = _prefixed(prefix, "AZURE_TENANT_ID", raw)
            graph_client_id = _prefixed(prefix, "GRAPH_CLIENT_ID", raw)
            graph_client_secret = _prefixed(prefix, "GRAPH_CLIENT_SECRET", raw)
            drive_id = _prefixed(prefix, "DRIVE_ID", raw)
            sp_site_name = _prefixed(prefix, "SP_SITE_NAME", raw, "")
            
            site_key = prefix.lower()
            # Must not be unconfigured placeholders (e.g. <prod-drive-id>)
            is_configured = bool(
                azure_tenant and not azure_tenant.startswith("<")
                and graph_client_id and not graph_client_id.startswith("<")
                and graph_client_secret and not graph_client_secret.startswith("<")
                and drive_id and not drive_id.startswith("<")
            )
            if not is_configured:
                continue

            if not sp_site_name:
                sp_site_name = f"Vessel DMS ({site_key})"
            
            site_id = _prefixed(prefix, "SITE_ID", raw, "") or _prefixed(prefix, "SP_SITE_ID", raw, "")
            configured_url = _prefixed(prefix, "SHAREPOINT_SITE_URL", raw, "")
            sites[site_key] = {
                "name": site_key,
                "sp_site_name": sp_site_name,
                "configured": is_configured,
                "site_id": site_id,
                "drive_id": drive_id,
                "web_url": compute_sp_site_url(site_key, sp_site_name, configured_url, drive_id=drive_id),
            }
        
        # Enrich display names from DB SiteConfiguration table
        try:
            from .db.base import engine
            if engine:
                from sqlalchemy import text
                with engine.connect() as conn:
                    rows = conn.execute(
                        text("SELECT site_key, display_name, site_name, site_id, drive_id FROM site_configurations")
                    ).mappings().all()
                    for r in rows:
                        k = (r["site_key"] or "").lower()
                        d_name = r["display_name"] or r["site_name"] or k
                        s_name = r.get("site_name") or d_name
                        s_id = r.get("site_id") or ""
                        d_id = r.get("drive_id") or ""
                        computed_url = compute_sp_site_url(k, s_name)
                        if k in sites:
                            sites[k]["sp_site_name"] = d_name
                            if s_id and not sites[k].get("site_id"):
                                sites[k]["site_id"] = s_id
                            if d_id and not sites[k].get("drive_id"):
                                sites[k]["drive_id"] = d_id
                            if "/sites/" in computed_url or not sites[k].get("web_url"):
                                sites[k]["web_url"] = computed_url
                        elif d_id and not d_id.startswith("<"):
                            sites[k] = {
                                "name": k,
                                "sp_site_name": d_name,
                                "configured": True,
                                "site_id": s_id,
                                "drive_id": d_id,
                                "web_url": computed_url,
                            }
        except Exception:
            pass

        return sites

    @staticmethod
    def discover_visible_sites() -> dict[str, dict]:
        """Like discover_available_sites(), minus any site an admin removed
        or hid from Site Management (site_configurations.is_removed /
        is_hidden).

        discover_available_sites() re-derives .env-backed sites (LOCAL_*,
        DEV_*, PROD_*, ...) on every call, so it can never "forget" one of
        those — removal is recorded as a flag on the site_configurations
        row instead (see DELETE /api/admin/site-configurations/{site_key}).
        Any caller that lists sites for something an admin-facing screen —
        the dashboard's counters/site table included — should call this
        instead of discover_available_sites() directly, or a removed site
        keeps showing up everywhere except Site Management.
        """
        sites = Settings.discover_available_sites()
        try:
            from .db.base import engine
            if engine:
                from sqlalchemy import text
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            "SELECT site_key, COALESCE(is_hidden, FALSE) AS is_hidden, "
                            "COALESCE(is_removed, FALSE) AS is_removed FROM site_configurations"
                        )
                    ).mappings().all()
                    excluded = {
                        (r["site_key"] or "").strip().lower()
                        for r in rows if r["is_hidden"] or r["is_removed"]
                    }
                    sites = {k: v for k, v in sites.items() if k not in excluded}
        except Exception:
            pass
        return sites

    @staticmethod
    def load_site_config(site_name: str) -> 'Settings':
        """Load and return Settings for a specific site.
        
        Args:
            site_name: The site prefix (e.g., 'dev', 'prod', 'local', 'nksdocman')
            
        Returns:
            A new Settings instance configured for the requested site
            
        Raises:
            ValueError: If site is not configured
        """
        clean_site = site_name.strip().lower()
        if clean_site in _SITE_CONFIGS_CACHE:
            return _SITE_CONFIGS_CACHE[clean_site]
        if site_name in _SITE_CONFIGS_CACHE:
            return _SITE_CONFIGS_CACHE[site_name]
        
        # Create a new raw env to read settings for this site
        raw = _RawEnv()
        p = site_name.upper()
        
        # Verify this site has required configuration in .env
        azure_tenant = _prefixed(p, "AZURE_TENANT_ID", raw)
        graph_client_id = _prefixed(p, "GRAPH_CLIENT_ID", raw)
        graph_client_secret = _prefixed(p, "GRAPH_CLIENT_SECRET", raw)
        drive_id = _prefixed(p, "DRIVE_ID", raw)
        
        if not all([azure_tenant, graph_client_id, graph_client_secret, drive_id]) or any(
            str(v).startswith("<") for v in [azure_tenant, graph_client_id, graph_client_secret, drive_id]
        ):
            # Check DB site_configurations table using direct engine connection
            # (avoids ORM Session event listener recursion)
            base_tenant = _prefixed("LOCAL", "AZURE_TENANT_ID", raw) or _prefixed("DEV", "AZURE_TENANT_ID", raw)
            base_client = _prefixed("LOCAL", "GRAPH_CLIENT_ID", raw) or _prefixed("DEV", "GRAPH_CLIENT_ID", raw)
            base_secret = _prefixed("LOCAL", "GRAPH_CLIENT_SECRET", raw) or _prefixed("DEV", "GRAPH_CLIENT_SECRET", raw)
            base_db_host = _prefixed("LOCAL", "DB_HOST", raw) or _prefixed("DEV", "DB_HOST", raw)
            base_db_name = _prefixed("LOCAL", "DB_NAME", raw) or _prefixed("DEV", "DB_NAME", raw)
            base_db_user = _prefixed("LOCAL", "DB_USER", raw) or _prefixed("DEV", "DB_USER", raw)
            base_db_pass = _prefixed("LOCAL", "DB_PASSWORD", raw) or _prefixed("DEV", "DB_PASSWORD", raw)
            base_db_port = _prefixed_int("LOCAL", "DB_PORT", raw, 5432) or _prefixed_int("DEV", "DB_PORT", raw, 5432)

            db_record = None
            if all([base_db_host, base_db_name, base_db_user, base_db_pass]):
                try:
                    from .db.base import engine
                    from sqlalchemy import text
                    if engine:
                        with engine.connect() as conn:
                            # Prefer exact site_key match; fall back to display_name match
                            exact = conn.execute(
                                text("SELECT site_key, display_name, site_name, site_id, drive_id FROM site_configurations WHERE LOWER(site_key) = :k LIMIT 1"),
                                {"k": clean_site}
                            ).mappings().first()
                            db_record = exact or conn.execute(
                                text("SELECT site_key, display_name, site_name, site_id, drive_id FROM site_configurations WHERE LOWER(display_name) = :n LIMIT 1"),
                                {"n": site_name.strip().lower()}
                            ).mappings().first()
                except Exception:
                    db_record = None

            if db_record and db_record["drive_id"] and base_tenant and base_client and base_secret:
                new_settings = Settings()
                # Use the *requested* key as active_site so the session knows
                # which logical site was switched to (e.g. 'nksdocman' not 'local')
                new_settings.active_site = clean_site
                new_settings.app_env = clean_site
                new_settings.sp_site_name = db_record["display_name"] or db_record["site_name"] or site_name
                new_settings.azure_tenant_id = base_tenant
                new_settings.graph_client_id = base_client
                new_settings.graph_client_secret = base_secret
                new_settings.drive_id = db_record["drive_id"]
                new_settings.sp_drive_id = db_record["drive_id"]
                new_settings.db_host = base_db_host
                new_settings.db_port = base_db_port
                new_settings.db_name = base_db_name
                new_settings.db_user = base_db_user
                new_settings.db_password = base_db_pass
                new_settings.sharepoint_site_url = compute_sp_site_url(
                    clean_site,
                    db_record["site_name"] or db_record["display_name"],
                    _prefixed("LOCAL", "SHAREPOINT_SITE_URL", raw, "") or _prefixed("DEV", "SHAREPOINT_SITE_URL", raw, "")
                )
                _SITE_CONFIGS_CACHE[clean_site] = new_settings
                _SITE_CONFIGS_CACHE[site_name] = new_settings
                if db_record["site_key"].lower() != clean_site:
                    _SITE_CONFIGS_CACHE[db_record["site_key"].lower()] = new_settings
                return new_settings

            raise ValueError(f"Site '{site_name}' is not fully configured")
        
        # Create new Settings for this site by simulating the init with this prefix
        new_settings = Settings()
        # Override the active_site to be this site
        new_settings.active_site = site_name
        new_settings.app_env = site_name
        new_settings.sp_site_name = _prefixed(p, "SP_SITE_NAME", raw, "")
        new_settings.azure_tenant_id = azure_tenant
        new_settings.graph_client_id = graph_client_id
        new_settings.graph_client_secret = graph_client_secret
        new_settings.drive_id = drive_id
        new_settings.db_host = _prefixed(p, "DB_HOST", raw)
        new_settings.db_port = _prefixed_int(p, "DB_PORT", raw, 5432)
        new_settings.db_name = _prefixed(p, "DB_NAME", raw)
        new_settings.db_user = _prefixed(p, "DB_USER", raw)
        new_settings.db_password = _prefixed(p, "DB_PASSWORD", raw)
        new_settings.allowed_origins = _prefixed(p, "ALLOWED_ORIGINS", raw, "*")
        new_settings.graph_sender_mailbox = _prefixed(p, "GRAPH_SENDER_MAILBOX", raw)
        new_settings.ai_banto_recipient = _prefixed(p, "AI_BANTO_RECIPIENT", raw)
        new_settings.admin_emails = _prefixed(p, "ADMIN_EMAILS", raw)
        new_settings.sp_drive_id = new_settings.drive_id

        # Enrich display name from DB if not set in .env
        if not new_settings.sp_site_name:
            try:
                from .db.base import engine as _e
                from sqlalchemy import text as _t
                if _e:
                    with _e.connect() as _c:
                        _row = _c.execute(
                            _t("SELECT display_name, site_name FROM site_configurations WHERE LOWER(site_key)=:k LIMIT 1"),
                            {"k": clean_site}
                        ).mappings().first()
                        if _row:
                            new_settings.sp_site_name = _row["display_name"] or _row.get("site_name") or ""
            except Exception:
                pass
        if not new_settings.sp_site_name:
            new_settings.sp_site_name = f"Vessel DMS ({site_name})"

        new_settings.sharepoint_site_url = compute_sp_site_url(
            site_name,
            new_settings.sp_site_name,
            _prefixed(p, "SHAREPOINT_SITE_URL", raw, ""),
            drive_id=new_settings.drive_id,
        )
        
        # Cache it
        _SITE_CONFIGS_CACHE[clean_site] = new_settings
        _SITE_CONFIGS_CACHE[site_name] = new_settings
        return new_settings


# Per-session site override tracking.
#
# There used to also be a "global active site override" here that got
# written to site_configurations.is_default_provisioning and re-read at
# startup. It was mutated as a side effect of set_session_site() (see
# below) and of the per-call provisioning tokens in site_provisioning.py,
# so switching one user's viewed site — or provisioning one vessel to a
# non-default site — silently reassigned the default provisioning site for
# the entire app, for every other user, persisted across restarts. That
# was the root cause of vessels/folders ending up provisioned against the
# wrong site (Phase 1 Part A). It has been removed: the global default
# provisioning site is now simply whatever ACTIVE_SITE is set to in .env
# (see Settings.active_site). Changing the app-wide default requires
# updating .env and restarting; there is no in-app action that changes it.
_SESSION_SITE_OVERRIDES: dict[str, str] = {}


def get_global_active_site() -> str | None:
    """No global override exists anymore — the default site is always
    whatever ACTIVE_SITE resolves to in .env. Kept as a function (rather
    than inlining `None` at call sites) so get_session_site()'s fallback
    reads clearly, and so a future admin-facing override has one place to
    plug into if ever added deliberately."""
    return None


def set_session_site(session_id: str, site_name: str):
    """Set the active site for a specific session only.

    Deliberately does NOT touch the global/persisted default site. It used
    to call set_global_active_site() here, which meant switching the site
    for *one* session (or scoping *one* background provisioning call to a
    site, via the synthetic tokens in site_provisioning.py) silently
    reassigned the default provisioning site for the entire app — for every
    other user and request, persisted to site_configurations in the DB —
    and never got reverted. That is the root cause of vessels/folders
    ending up provisioned against the wrong site. The global default is
    now fixed at startup from ACTIVE_SITE (see Settings/_init_persisted_active_site);
    changing it requires updating .env and restarting.
    """
    clean_name = site_name.strip().lower()
    _SESSION_SITE_OVERRIDES[session_id] = clean_name


def get_session_site(session_id: str | None) -> str | None:
    """Get the active site for a specific session, or fall back to global override."""
    if session_id and session_id in _SESSION_SITE_OVERRIDES:
        return _SESSION_SITE_OVERRIDES[session_id]
    return get_global_active_site()


def get_settings() -> Settings:
    active_override = get_global_active_site()
    if active_override:
        try:
            return Settings.load_site_config(active_override)
        except Exception:
            pass
    return Settings()


def get_settings_for_session(session_id: str | None) -> Settings:
    """Get the appropriate Settings object for a session, considering site overrides."""
    site_override = get_session_site(session_id)
    if site_override:
        try:
            return Settings.load_site_config(site_override)
        except ValueError:
            pass  # Fall back to default
    return get_settings()


def get_active_drive_id() -> str:
    """Return the stable SharePoint drive identity used to scope folder cache rows."""
    try:
        current_session_id = _CURRENT_SESSION_ID.get()
        overridden_site = get_session_site(current_session_id)
        if overridden_site and overridden_site in _SITE_CONFIGS_CACHE:
            return str(_SITE_CONFIGS_CACHE[overridden_site].drive_id or "")
    except Exception:
        pass
    return str(getattr(settings, "drive_id", "") or settings.active_site or "default")


def clear_session_site(session_id: str):
    """Clear the site override for a session."""
    _SESSION_SITE_OVERRIDES.pop(session_id, None)


class _SettingsProxy:
    def __init__(self):
        pass
    
    def set_current_session(self, session_id: str | None):
        """Set the current session ID for this request context."""
        return _CURRENT_SESSION_ID.set(session_id)

    def reset_current_session(self, token) -> None:
        _CURRENT_SESSION_ID.reset(token)
    
    def __getattr__(self, name: str):
        settings_obj = get_settings()
        
        # Check if there's a session-specific site override or global override
        current_session_id = _CURRENT_SESSION_ID.get()
        overridden_site = get_session_site(current_session_id)
        if overridden_site and overridden_site != settings_obj.active_site:
            try:
                settings_obj = Settings.load_site_config(overridden_site)
            except ValueError:
                # Fall back to default if override site is not configured
                pass
        
        return getattr(settings_obj, name)


settings: _SettingsProxy = _SettingsProxy()  # type: ignore[assignment]

