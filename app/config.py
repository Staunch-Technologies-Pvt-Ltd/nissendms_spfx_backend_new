"""Application configuration.

Single .env file holds all three environments (local / dev / prod).
Set APP_ENV=local|dev|prod to activate the right block.
Shared keys (no prefix) are always read directly.
"""
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class _RawEnv(BaseSettings):
    """Reads every key from .env without validation — used only to resolve APP_ENV."""
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="allow"
    )
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


class Settings:
    def __init__(self):
        raw = _RawEnv()
        env = raw.app_env.strip().lower()
        if env not in ("local", "dev", "prod"):
            raise ValueError(f"APP_ENV must be local, dev, or prod — got '{env}'")

        p = env.upper()  # prefix: LOCAL / DEV / PROD

        # --- Microsoft Entra / Graph ---
        self.azure_tenant_id: str       = _prefixed(p, "AZURE_TENANT_ID", raw)
        self.graph_client_id: str       = _prefixed(p, "GRAPH_CLIENT_ID", raw)
        self.graph_client_secret: str   = _prefixed(p, "GRAPH_CLIENT_SECRET", raw)
        self.graph_authority: str       = "https://login.microsoftonline.com"
        self.graph_scope: str           = "https://graph.microsoft.com/.default"
        self.graph_base_url: str        = "https://graph.microsoft.com/v1.0"

        # --- SharePoint Embedded ---
        self.container_type_id: str     = _prefixed(p, "CONTAINER_TYPE_ID", raw)
        self.container_id: str          = _prefixed(p, "CONTAINER_ID", raw)
        self.container_display_name: str = _prefixed(p, "CONTAINER_DISPLAY_NAME", raw, "Vessel DMS Documents")
        self.drive_id: str              = _prefixed(p, "DRIVE_ID", raw)

        # --- Database ---
        self.database_url: str          = ""  # not used directly; resolved below
        self.db_host: str               = _prefixed(p, "DB_HOST", raw)
        self.db_port: int               = _prefixed_int(p, "DB_PORT", raw, 5432)
        self.db_name: str               = _prefixed(p, "DB_NAME", raw)
        self.db_user: str               = _prefixed(p, "DB_USER", raw)
        self.db_password: str           = _prefixed(p, "DB_PASSWORD", raw)

        # --- CORS ---
        self.allowed_origins: str       = _prefixed(p, "ALLOWED_ORIGINS", raw, "*")

        # --- Email automation ---
        self.graph_sender_mailbox: str  = _prefixed(p, "GRAPH_SENDER_MAILBOX", raw)
        self.ai_banto_recipient: str    = _prefixed(p, "AI_BANTO_RECIPIENT", raw)

        # --- Approval workflow ---
        self.admin_emails: str          = _prefixed(p, "ADMIN_EMAILS", raw)
        self.notify_sender_email: str   = ""

        # --- Shared / fixed ---
        self.month_folder_format: str   = getattr(raw, "month_folder_format", "%B %Y")
        self.ocr_min_confidence: float  = 0.5
        self.graph_verify_ssl: bool     = str(getattr(raw, "graph_verify_ssl", "true")).lower() != "false"
        self.trusted_proxy_hops: int    = int(getattr(raw, "trusted_proxy_hops", 1) or 1)
        self.session_idle_timeout_minutes: int       = int(getattr(raw, "session_idle_timeout_minutes", 30) or 30)
        self.session_max_lifetime_hours: int         = int(getattr(raw, "session_max_lifetime_hours", 8) or 8)
        self.session_revalidation_interval_minutes: int = int(getattr(raw, "session_revalidation_interval_minutes", 60) or 60)

        # Store active env name for logging/health endpoint
        self.app_env: str = env

    # ------------------------------------------------------------------
    @property
    def graph_configured(self) -> bool:
        return bool(
            self.azure_tenant_id
            and self.graph_client_id
            and self.graph_client_secret
            and self.container_type_id
        )

    @property
    def db_configured(self) -> bool:
        return bool(self.database_url_resolved)

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
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}


def get_settings() -> Settings:
    return Settings()


class _SettingsProxy:
    def __getattr__(self, name):
        return getattr(get_settings(), name)

settings = _SettingsProxy()
