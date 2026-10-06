"""Migration Assistant configuration (merged into the Vessel DMS backend).

Everything lives in the DMS's single ``backend/.env``:

- **Credentials** (tenant, client id/secret) are the DMS's own — the Migration
  Assistant uses the same Entra app, so nothing is repeated. Set
  ``MIGRATION_AZURE_TENANT_ID`` / ``MIGRATION_GRAPH_CLIENT_ID`` /
  ``MIGRATION_GRAPH_CLIENT_SECRET`` only to use a different app.
- **Migration-only options** use a ``MIGRATION_`` prefix in ``.env``
  (``MIGRATION_SITE_HOSTNAME``, ``MIGRATION_DESTINATION_ROOT``, ...), so they
  can never collide with DMS names like DATABASE_URL or ALLOWED_ORIGINS.

Where values come from (highest priority first):
  1. Process env vars prefixed ``MIGRATION_`` — for servers / CI.
  2. ``backend/.env``, ``MIGRATION_``-prefixed keys only.
  3. ``backend/.env.migration`` (legacy, un-prefixed names from the old
     standalone project) — still read if present, so older setups keep working.
  4. Credentials not set by any of the above: the DMS's own settings.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # .../backend
ENV_FILE = _BACKEND_DIR / ".env.migration"  # legacy; optional
DMS_ENV_FILE = _BACKEND_DIR / ".env"
_DEFAULT_DB = "sqlite:///" + (_BACKEND_DIR / "migration_assistant.db").as_posix()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MIGRATION_", extra="ignore", populate_by_name=True
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return (
            init_settings,
            env_settings,  # MIGRATION_* process env vars
            DotEnvSettingsSource(  # MIGRATION_* keys in the single DMS .env
                settings_cls, env_file=DMS_ENV_FILE, env_file_encoding="utf-8", env_prefix="MIGRATION_"
            ),
            DotEnvSettingsSource(  # legacy .env.migration, un-prefixed
                settings_cls, env_file=ENV_FILE, env_file_encoding="utf-8", env_prefix=""
            ),
        )

    def model_post_init(self, __context) -> None:
        """Fill missing credentials from the DMS's own settings (same Entra app)."""
        if self.azure_tenant_id and self.graph_client_id and self.graph_client_secret:
            return
        try:
            from ..config import settings as dms
        except Exception:  # pragma: no cover - DMS config unavailable
            return
        self.azure_tenant_id = self.azure_tenant_id or (dms.azure_tenant_id or "")
        self.graph_client_id = self.graph_client_id or (dms.graph_client_id or "")
        self.graph_client_secret = self.graph_client_secret or (dms.graph_client_secret or "")

    # --- Microsoft Entra / Graph (app-only, client-credentials) ---
    azure_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_authority: str = "https://login.microsoftonline.com"
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_verify_ssl: bool = True  # set false only to bypass TLS verification

    # --- Source SharePoint Online site to migrate documents out of ---
    site_hostname: str = ""  # e.g. "contoso.sharepoint.com"
    site_path: str = ""  # e.g. "sites/VesselDocs" (no leading slash)

    # --- Site-to-Site migration: an allow-list of sites the app has been
    # granted Sites.Selected access to, for the site/library pickers in that
    # mode. Not a free-text field and not a tenant-wide site search — Graph's
    # Sites.Selected model requires each site to be explicitly granted to
    # this app first (see README "Graph API configuration"), so only sites
    # already set up that way are worth showing in the UI.
    # JSON list: [{"key": "...", "label": "...", "hostname": "...", "site_path": "..."}]
    allowed_sites: str = "[]"

    # --- Site-to-Site migration: Managed Metadata (Term Store) writes go
    # through SharePoint REST's ValidateUpdateListItem, not Graph — see
    # services/term_mapping.py and graph/sharepoint_rest.py docstrings for
    # why. That call needs a second, SharePoint-audience app-only token from
    # the same Entra app registration, which requires the tenant's root
    # SharePoint URL (used as the token resource / `.default` scope).
    sharepoint_tenant_url: str = ""  # e.g. "https://contoso.sharepoint.com"

    # --- Destination ---
    # The classify pipeline's destination is a DIFFERENT SharePoint site from
    # the source (SITE_HOSTNAME/SITE_PATH above) — vessel folders, category
    # hierarchies, and Confirm Move's writes all happen on this site instead.
    # Must be a key already present in ALLOWED_SITES (the same allow-list
    # Site-to-Site migration uses — Sites.Selected requires each site be
    # explicitly granted to this app first). See services/migration_common.py
    # get_destination_drive_id and services/migration_mover.py's module
    # docstring for why Confirm Move is a cross-site copy here, not a move.
    destination_site_key: str = "nks-docman"
    # Root folder (at the destination site's Documents library top level)
    # containing one subfolder per vessel — the user picks one of those as
    # the move target.
    destination_root: str = "Technical and Crewing"
    # Fallback folder created (if missing) under the selected vessel for any
    # document the AI can't confidently place — never AI-invented, always this
    # exact name.
    to_be_classified_folder_name: str = "To Be Classified"

    # --- Classification ---
    # Minimum keyword-match confidence (0-1) to pre-fill a suggestion instead
    # of leaving the item as needs_review. See classifier/keyword_classifier.py.
    confidence_threshold: float = 0.75

    # --- Same-site auto vessel detection (picking the destination root itself
    # instead of one specific vessel folder) ---
    # An existing vessel folder whose category structure is cloned when a
    # brand-new vessel folder must be created (see services/migration_mover.py
    # confirm_job). Required for auto-detect scans to ever create a new vessel.
    template_vessel_name: str = ""
    # Separate knob from confidence_threshold since matching a short candidate
    # vessel name behaves differently from matching a whole document's text.
    vessel_match_confidence_threshold: float = 0.75

    # --- Same-site Managed Metadata (Term Store) tagging on Confirm Move ---
    # Internal name of the source file column whose value names the
    # destination category folder. Leave blank to use text classification.
    source_category_field_name: str = "Category"
    # Explicit internal field name + bound term-set id per column, looked up
    # once via SharePoint's Term Store admin center — NOT auto-discovered via
    # the column's `termColumn` facet, because this tenant's own Category/
    # Group/Vessel Name columns don't reliably expose that facet (see
    # services/term_mapping.py's docstring). Leave a pair blank to skip
    # tagging that field entirely.
    category_field_name: str = ""
    category_term_set_id: str = ""
    group_field_name: str = ""
    group_term_set_id: str = ""
    subcategory_field_name: str = ""
    subcategory_term_set_id: str = ""
    vessel_field_name: str = ""
    vessel_term_set_id: str = ""

    # --- Text extraction ---
    ocr_min_confidence: float = 0.5

    # --- Database (own store — independent of the DMS's Postgres database;
    # defaults to backend/migration_assistant.db) ---
    database_url: str = _DEFAULT_DB

    @property
    def graph_configured(self) -> bool:
        """Graph credentials are present. The source site is picked per scan
        (from Site Management); MIGRATION_SITE_HOSTNAME/PATH are only the
        default used when no site is picked."""
        return bool(self.azure_tenant_id and self.graph_client_id and self.graph_client_secret)

    @property
    def default_source_configured(self) -> bool:
        return bool(self.site_hostname and self.site_path)

    @property
    def authority_url(self) -> str:
        return f"{self.graph_authority}/{self.azure_tenant_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
