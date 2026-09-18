"""Application configuration.

Single .env file holds all three environments (local / dev / prod).
Set APP_ENV=local|dev|prod to activate the right block.
Shared keys (no prefix) are always read directly.
"""
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


def compute_sp_site_url(site_key: str = "", site_name: str = "", base_url: str = "") -> str:
    """Compute the correct SharePoint site collection URL for a logical site.
    
    Ensures secondary site collections (like NKSDocMan or NissenKaiunExternal)
    are routed to their actual site paths (/sites/NKSDocMan) instead of the
    tenant root Communication site.
    """
    base = (base_url or "https://nissenkaiunsingapore.sharepoint.com").strip().rstrip("/")
    key_clean = (site_key or "").strip().lower()
    name_clean = (site_name or "").strip().lower()

    # Root Communication site
    if key_clean in ("dev", "communication", "root") and not any(k in name_clean for k in ("nks", "docman", "external")):
        return base
    if name_clean in ("communication site", "communication", "root"):
        return base

    # NKSDocMan site
    if "nks" in key_clean or "docman" in key_clean or "nks" in name_clean or "docman" in name_clean or key_clean == "local":
        return f"{base}/sites/NKSDocMan"

    # External site
    if "external" in key_clean or "external" in name_clean:
        return f"{base}/sites/NissenKaiunExternal"

    # If site_name or site_key starts with http, return it
    if (site_name or "").startswith("http://") or (site_name or "").startswith("https://"):
        return site_name

    target = site_name.strip() if site_name and site_name.strip().lower() not in ("vessel dms", "") else site_key.strip()
    if target and target.lower() not in ("dev", "communication site", "root", "default", "vessel dms"):
        return f"{base}/sites/{target}"

    return base


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
                "web_url": compute_sp_site_url(site_key, sp_site_name, configured_url),
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
            _prefixed(p, "SHAREPOINT_SITE_URL", raw, "")
        )
        
        # Cache it
        _SITE_CONFIGS_CACHE[clean_site] = new_settings
        _SITE_CONFIGS_CACHE[site_name] = new_settings
        return new_settings


# Per-session and global site override tracking
_SESSION_SITE_OVERRIDES: dict[str, str] = {}
_GLOBAL_ACTIVE_SITE_OVERRIDE: str | None = None
_PERSISTED_SITE_INITIALIZED: bool = False


def _init_persisted_active_site():
    global _GLOBAL_ACTIVE_SITE_OVERRIDE, _PERSISTED_SITE_INITIALIZED
    if _PERSISTED_SITE_INITIALIZED:
        return
    _PERSISTED_SITE_INITIALIZED = True
    try:
        from .db.base import engine
        from sqlalchemy import text
        if engine:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT site_key FROM site_configurations WHERE is_default_provisioning = TRUE LIMIT 1")
                ).mappings().first()
                if row and row["site_key"]:
                    _GLOBAL_ACTIVE_SITE_OVERRIDE = str(row["site_key"]).strip().lower()
    except Exception:
        pass


def set_global_active_site(site_name: str):
    """Set the global active site override across all sessions and persist to DB."""
    global _GLOBAL_ACTIVE_SITE_OVERRIDE
    clean_name = site_name.strip().lower()
    _GLOBAL_ACTIVE_SITE_OVERRIDE = clean_name
    try:
        from .db.base import engine
        from sqlalchemy import text
        if engine:
            with engine.connect() as conn:
                conn.execute(
                    text("UPDATE site_configurations SET is_default_provisioning = (LOWER(site_key) = :target OR LOWER(display_name) = :target)"),
                    {"target": clean_name}
                )
                conn.commit()
    except Exception:
        pass


def get_global_active_site() -> str | None:
    _init_persisted_active_site()
    return _GLOBAL_ACTIVE_SITE_OVERRIDE


def set_session_site(session_id: str, site_name: str):
    """Set the active site for a specific session."""
    clean_name = site_name.strip().lower()
    _SESSION_SITE_OVERRIDES[session_id] = clean_name
    set_global_active_site(clean_name)


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

