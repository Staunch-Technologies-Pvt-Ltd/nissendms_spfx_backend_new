"""SQLAlchemy models.

- Vessel:    one row per ship (name + IMO).
- Folder:    cache of logical-path -> SharePoint driveItem id, so we don't have
             to re-walk the Graph tree on every request. Also stores the semantic
             flags (kind / month_driven) derived from the folder template.
- UploadJob: tracks an upload's OCR + filing lifecycle for the polling UI.
"""
from datetime import date, datetime

# pyrefly: ignore [missing-import]
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, Integer, func, LargeBinary, JSON, UniqueConstraint
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from ..config import get_active_drive_id


class Vessel(Base):
    __tablename__ = "vessels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    imo: Mapped[str | None] = mapped_column(String(7), unique=True, nullable=True)
    shipyard: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hull_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vessel_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # A vessel is registered before its SharePoint tree is built.  This flag
    # is the source of truth for the UI's Provision / Already Provisioned
    # state; it is only set after the complete tree has been created.
    is_provisioned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Target SharePoint site keys/IDs provisioned for this vessel (multi-site support)
    provisioned_site_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=list)
    # User-selected SharePoint location for newly created vessels.
    provisioned_site_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    vessel_folder_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Set when a vessel is restored from Recycle Bin and re-activated in DB.
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Settings → Vessel Settings → Folder Structure Mode (services/folder_structure.py):
    # empty_pool | full_template | adopt_existing | adopt_create. NULL = empty_pool.
    folder_structure_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    folders: Mapped[list["Folder"]] = relationship(
        back_populates="vessel", cascade="all, delete-orphan"
    )


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("site_id", "path", name="uq_folders_site_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Logical path within the container, e.g.
    # "Technical & Crewing/MV Horizon/Month End Reports".
    site_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True, default=get_active_drive_id)
    path: Mapped[str] = mapped_column(String(1024), index=True)
    name: Mapped[str] = mapped_column(String(400))
    kind: Mapped[str] = mapped_column(String(40))  # main/ship/folder/leaf/month_driven/month
    drive_item_id: Mapped[str] = mapped_column(String(256), index=True)
    month_driven: Mapped[bool] = mapped_column(Boolean, default=False)

    vessel_id: Mapped[int | None] = mapped_column(
        ForeignKey("vessels.id", ondelete="CASCADE"), nullable=True
    )
    vessel: Mapped["Vessel | None"] = relationship(back_populates="folders")

    # Only set on kind="ship" rows that belong to a pre-provisioned pool
    # slot (see PoolSlot below). NULL for every normal, already-linked
    # vessel folder. Kept after claiming as a historical marker that this
    # folder originated from the pool, not a from-scratch provision.
    pool_slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pool_slots.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PoolSlot(Base):
    """One pre-provisioned vessel folder tree, not yet assigned to a real
    vessel. A slot's ship-level Folder rows (one per non-flat main folder)
    all share this row's id via Folder.pool_slot_id.

    Claiming a slot is a single-row lock (SELECT...FOR UPDATE SKIP LOCKED
    on THIS table) rather than locking the Folder rows directly, so two
    concurrent claims can never end up with overlapping subsets of the same
    slot's folders.
    """
    __tablename__ = "pool_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique placeholder name used for the ship folders until claimed, e.g.
    # "Pool-3f9a2b1c". Never a fixed numbered scheme (Reserved-01..10) —
    # fixed names collide across concurrent replenishments.
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # building -> available -> claimed. "building" means the background
    # provisioning task hasn't finished yet (used by the reconciliation
    # job to detect a crashed/stuck build).
    status: Mapped[str] = mapped_column(String(20), default="building", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ReplenishJob(Base):
    """Tracks a background pool-replenishment task so a process restart
    mid-build can be detected and resumed by the scheduler's reconciliation
    check, instead of silently leaving an incomplete pool slot forever."""
    __tablename__ = "replenish_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    triggering_slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pool_slots.id", ondelete="SET NULL"), nullable=True
    )
    new_slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pool_slots.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/done/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UploadJob(Base):
    __tablename__ = "upload_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="processing")
    destination: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    detected_month: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """Legacy/auxiliary user table captured by earlier migrations."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    azure_oid: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(320), nullable=True)
    given_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    surname: Mapped[str | None] = mapped_column(String(160), nullable=True)
    preferred_username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class VesselJob(Base):
    """Simple background-job status table from earlier migrations."""

    __tablename__ = "vessel_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DeletedItem(Base):
    """Audit table for deleted SharePoint items."""

    __tablename__ = "deleted_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    item_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DeletedVessel(Base):
    """Tracks vessels deleted from the app so the Recycle Bin page always
    shows all deleted vessels, even when the SharePoint recycle bin API
    has a propagation delay or returns a partial list."""

    __tablename__ = "deleted_vessels"

    id: Mapped[int] = mapped_column(primary_key=True)
    vessel_name: Mapped[str] = mapped_column(String(200), index=True)
    vessel_imo: Mapped[str | None] = mapped_column(String(7), nullable=True)
    vessel_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    original_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    site_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    deleted_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)


class DeletionLog(Base):
    """Single source of truth for the 'who/what/where/when/why' of every
    deletion (vessel, folder, or file), across all SharePoint sites this app
    covers.

    Populated two ways:
    - source="app": written synchronously right after a successful delete —
      either by RealBackend's own delete_vessel/delete_folder/delete_file
      execution path, or (for the common case of a file/folder deleted
      client-side straight against Graph from the SPFx web part) via the
      POST /api/recycle-bin/log-deletion endpoint the frontend calls right
      after the Graph delete succeeds.
    - source="native_spo": backfilled by the reconcile_native_deletions
      scheduler job for anything that shows up in SharePoint's own recycle
      bin with no matching row here (e.g. deleted directly in SharePoint's
      native UI, outside this app entirely). deleted_by_* is populated on a
      best-effort basis from the SharePoint REST recycle bin API, which is
      not always resolvable, so it may be NULL for these rows.

    Both the enhanced Recycle Bin (Deleted By / Reason columns) and the live
    deletion popup (top-header alert bell) read from this one table.
    """

    __tablename__ = "deletion_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    item_name: Mapped[str] = mapped_column(String(400))
    # vessel / folder / file — what kind of node was deleted.
    item_type: Mapped[str] = mapped_column(String(20), index=True)
    # vessel / normal_folder / file — which Recycle Bin tab this belongs in,
    # decided once at capture time via services.classify.classify_deletion()
    # so it never depends on who's viewing or which path heuristic runs client-side.
    classification: Mapped[str] = mapped_column(String(20), index=True)
    original_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    vessel_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sub_category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    site_key: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    deleted_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    deleted_by_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # app | native_spo — how this row was captured (see class docstring).
    source: Mapped[str] = mapped_column(String(20), default="app")
    deleted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # Popup-dismissal bookkeeping for the alert bell, mirroring FolderAlert.
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    azure_oid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    department: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    office_location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    office_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(300), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(300), nullable=True)
    area_locality: Mapped[str | None] = mapped_column(String(200), nullable=True)
    landmark: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    employee_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    manager_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    manager_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    photo_base64: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_of_joining: Mapped[date | None] = mapped_column(Date, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="User", server_default="User")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    permissions_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    folder_permissions: Mapped[list["FolderPermission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    site_permissions: Mapped[list["UserSitePermission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    activity_logs: Mapped[list["ActivityLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    emergency_contacts: Mapped[list["EmergencyContact"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class EmergencyContact(Base):
    __tablename__ = "emergency_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_email: Mapped[str] = mapped_column(
        String(320), ForeignKey("user_profiles.email", ondelete="CASCADE"), index=True
    )
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    relationship_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["UserProfile"] = relationship(back_populates="emergency_contacts")


class FolderPermission(Base):
    __tablename__ = "folder_permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_email: Mapped[str] = mapped_column(
        String(320), ForeignKey("user_profiles.email", ondelete="CASCADE"), index=True
    )
    folder_name: Mapped[str] = mapped_column(String(400))
    permission_level: Mapped[str] = mapped_column(String(20))  # edit / view / approve

    user: Mapped["UserProfile"] = relationship(back_populates="folder_permissions")


class UserSitePermission(Base):
    __tablename__ = "user_site_permissions"
    __table_args__ = (UniqueConstraint("user_email", "site_key", name="uq_user_site_permission"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_email: Mapped[str] = mapped_column(
        String(320), ForeignKey("user_profiles.email", ondelete="CASCADE"), index=True
    )
    site_key: Mapped[str] = mapped_column(String(100), index=True)
    can_view: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_upload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_tag_on_upload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    granted_by_email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=datetime.utcnow)

    user: Mapped["UserProfile"] = relationship(back_populates="site_permissions")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_email: Mapped[str] = mapped_column(
        String(320), ForeignKey("user_profiles.email", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["UserProfile"] = relationship(back_populates="activity_logs")


class ArchivedItem(Base):
    __tablename__ = "archived_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    item_type: Mapped[str] = mapped_column(String(20))  # "folder" or "file"
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FolderAnomaly(Base):
    __tablename__ = "folder_anomalies"

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_item_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    parent_drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    name: Mapped[str] = mapped_column(String(400))
    item_type: Mapped[str] = mapped_column(String(20), default="folder")  # "folder" or "file"
    anomaly_type: Mapped[str] = mapped_column(String(50), index=True)  # main_folder_unmatched / vessel_level_unmatched / subfolder_unmatched
    department: Mapped[str] = mapped_column(String(100))
    vessel_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    spo_path: Mapped[str] = mapped_column(String(1024))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApprovalRequest(Base):
    """Pending approval requests AND completed admin-activity notifications.

    Originally upload-only: a user uploads to a month-driven or
    approval-gated folder, the file is placed in a temporary "Pending
    Approvals" area (SharePoint) and a row is inserted here; an admin then
    approves or rejects via the /approvals/* endpoints.

    Generalized to cover non-upload mutating actions too (delete document,
    delete folder, create folder, create vessel, update vessel):
    - entry_kind='approval': a non-admin's request, status starts 'pending',
      the underlying mutation is deferred until an admin approves it.
    - entry_kind='activity': an SPE Admin's action, executed immediately;
      the row is inserted already 'completed', purely for audit visibility.

    filename/content_type/size/destination_folder_id/destination_path/
    drive_item_id are upload-specific and nullable for non-upload rows.
    action_type/department/vessel_*/target_*/payload_json/changes_json/
    message are generic and apply to every action type.
    """

    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str | None] = mapped_column(String(400), nullable=True)
    content_type: Mapped[str | None] = mapped_column(
        String(200), default="application/octet-stream", nullable=True
    )
    size: Mapped[int | None] = mapped_column(default=0, nullable=True)
    uploaded_by_email: Mapped[str] = mapped_column(String(320), index=True)
    uploaded_by_name: Mapped[str] = mapped_column(String(200), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    destination_folder_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    destination_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_month_upload: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detected_month: Mapped[str | None] = mapped_column(String(40), nullable=True)
    drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/approved/rejected/completed
    decided_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # --- Generic approval/activity fields (added for the admin-bypass +
    # activity-notification workflow; apply to every action_type) ---
    entry_kind: Mapped[str] = mapped_column(String(20), default="approval")  # approval/activity
    action_type: Mapped[str] = mapped_column(String(40), default="upload")
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vessel_id: Mapped[int | None] = mapped_column(
        ForeignKey("vessels.id", ondelete="SET NULL"), nullable=True
    )
    vessel_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserSession(Base):
    """One row per login session.

    Every MSAL login creates a fresh row.  Multiple rows with status=Active
    for the same user email are expected and supported (multi-device/tab).
    Status lifecycle: Active → Expired | Logged Out | Revoked.
    """

    __tablename__ = "user_sessions"

    # Internal surrogate PK (UUID stored as String for broad DB compat)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Opaque token sent to and verified from the client (X-Session-ID header)
    session_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    # Soft FK to user_profiles — nullable because the profile row may not
    # exist yet in stub/no-DB startup situations.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="SET NULL"), nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    login_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_activity: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Hard expiry = login_time + max_lifetime_hours (never extended)
    expiry_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    logout_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Timestamp of last periodic Graph account-enabled spot-check
    last_revalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Raw User-Agent string from the HTTP request header (server-captured)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Parsed UA components
    browser: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operating_system: Mapped[str | None] = mapped_column(String(100), nullable=True)
    device_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Desktop/Mobile/Tablet
    # Server-captured client IP (never trusted from the request body)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    authentication_method: Mapped[str] = mapped_column(String(50), default="AzureAD")
    # Active | Expired | Logged Out | Revoked
    status: Mapped[str] = mapped_column(String(20), default="Active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SessionAuditLog(Base):
    """Immutable audit trail for session lifecycle events.

    Intentionally has NO foreign key to user_sessions so that entries are
    preserved even if the session row is cleaned up later.  This table is
    append-only — no updates or deletes should ever be issued against it.

    Events: session_created | session_logged_out | session_expired |
            session_revoked | invalid_session_attempt | auth_failure
    """

    __tablename__ = "session_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Reference to user_sessions.session_id — string copy, not an FK
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    event: Mapped[str] = mapped_column(String(50), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    browser: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    login_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    logout_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_duration_formatted: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class EmailLog(Base):
    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    datasource_tag_requested: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vessel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    datasource_tag_used: Mapped[str] = mapped_column(String(100))
    tag_was_valid: Mapped[bool] = mapped_column(Boolean)
    subject_final: Mapped[str] = mapped_column(String(600))
    recipient: Mapped[str] = mapped_column(String(320))

    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    attachments: Mapped[list["EmailAttachment"]] = relationship(
        back_populates="email_log", cascade="all, delete-orphan"
    )


class EmailAttachment(Base):
    __tablename__ = "email_attachment"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email_log_id: Mapped[int] = mapped_column(ForeignKey("email_log.id"), nullable=False)

    filename: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)

    email_log: Mapped["EmailLog"] = relationship(back_populates="attachments")


class DocumentCategory(Base):
    """User-managed document category definitions that drive OCR classification and tagging.

    Each row defines:
    - name               : display name (e.g. "Drawing", "Manual", "Invoice")
    - department         : which DMS main folder this belongs to
    - dms_path_template  : path template with {key} placeholders, e.g.
                           "{group}/{vessel}/Drawings and Manuals/{category}/{sub_category}"
    - tag_fields_json    : JSON array of TagFieldDef objects — the dynamic set of
                           metadata fields shown in the staging review UI.
                           Shape: [{"key": str, "label": str, "type": str,
                                    "required": bool, "options": list[str] | null}]
                           Allowed types: text | textarea | select_vessel | select_dept
                                          | select_category | select
    - ocr_hints_json     : JSON array of keyword strings used for scoring during
                           OCR classification (supplements the built-in taxonomy).
    - is_active          : soft-delete flag
    """
    __tablename__ = "document_categories"

    _DEFAULT_TAG_FIELDS = (
        '[{"key":"vessel","label":"Vessel Name","type":"select_vessel","required":true,"options":null},'
        '{"key":"group","label":"Department","type":"select_dept","required":true,"options":null},'
        '{"key":"category","label":"Category","type":"text","required":true,"options":null},'
        '{"key":"sub_category","label":"Sub-Category","type":"text","required":false,"options":null}]'
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dms_path_template: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # JSON-encoded list of TagFieldDef objects (see docstring)
    tag_fields_json: Mapped[str] = mapped_column(Text, default=_DEFAULT_TAG_FIELDS)
    # JSON-encoded list of keyword hint strings for OCR scoring
    ocr_hints_json: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    staging_files: Mapped[list["OcrStagingFile"]] = relationship(
        back_populates="category", cascade="save-update, merge"
    )


class OcrStagingFile(Base):
    """Unified OCR staging queue entry — one row per file queued for review.

    Populated by POST /api/ocr/stage-file regardless of whether the file came
    from a folder upload (BulkUploadModal) or a single-file upload.

    Lifecycle:
        ocr_pending   → file is queued; OCR extraction has not run yet
        ocr_complete  → text extracted; classification in progress
        tag_suggested → category matched + suggested_tags populated (confidence ≥ 0.40)
                        OR low-confidence placeholder (category_id=NULL, tags empty)
        moved         → user approved; file relocated in SharePoint
        dismissed     → user dismissed without moving

    Upsert rule: if a row with the same drive_item_id already exists and its status
    is NOT 'moved' or 'dismissed', the existing row is returned (no duplicate insert).
    When drive_item_id is NULL, fall back to filename+source_folder_id uniqueness
    within a 60-second window.
    """
    __tablename__ = "ocr_staging_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(400))
    # SharePoint drive item ID of the uploaded file (nullable before Graph confirms it)
    drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    source_folder_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_subfolder_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    vessel_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # "folder" = BulkUploadModal; "direct" = single-file upload
    upload_source: Mapped[str] = mapped_column(String(20), default="direct", index=True)
    # ocr_pending | ocr_complete | tag_suggested | moved | dismissed
    status: Mapped[str] = mapped_column(String(30), default="ocr_pending", index=True)

    # FK to the matched DocumentCategory (NULL when confidence < 0.40 or no match)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_categories.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped["DocumentCategory | None"] = relationship(back_populates="staging_files")

    # JSON dict of suggested tag values, e.g. {"vessel": "MV Aurora", "group": "Technical & Crewing"}
    # Keys match the category's tag_fields_json[*].key values
    suggested_tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # First 1000 chars of extracted OCR text for UI preview
    ocr_text_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0.0–1.0; NULL when OCR has not run yet
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    # JSON array of matched keyword strings (up to 10)
    matched_keywords_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Resolved SharePoint path after placeholder substitution — set just before the move call
    final_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    uploaded_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Non-fatal OCR error message (file still queued for manual review)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class FolderAlert(Base):
    """Alert emitted when a new folder is created in SharePoint Online.

    Surfaced to users via the top-header alert bell in the SPFx web part,
    replacing the old per-module (Documents / Vessels) notifications. Covers
    both manual sub-folder creation (create_subfolder) and vessel provisioning
    (the ship folder + main/category sub-tree created for a new vessel).
    """

    __tablename__ = "folder_alerts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    folder_name: Mapped[str] = mapped_column(String(400))
    folder_path: Mapped[str] = mapped_column(String(1024))
    parent_folder_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    vessel_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    department: Mapped[str] = mapped_column(String(100), default="All Departments")
    created_by_email: Mapped[str] = mapped_column(String(320), default="")
    created_by_name: Mapped[str] = mapped_column(String(200), default="")
    alert_type: Mapped[str] = mapped_column(String(40), default="folder_created")  # folder_created / vessel_provisioned
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TagFailure(Base):
    """Persistent retry queue for SharePoint metadata tagging failures."""
    __tablename__ = "tag_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str] = mapped_column(String(256), index=True)
    site_id: Mapped[str] = mapped_column(String(256), index=True)
    drive_id: Mapped[str] = mapped_column(String(256), index=True)
    filename: Mapped[str] = mapped_column(String(500), default="")
    parent_path: Mapped[str] = mapped_column(String(1024), default="")
    error_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="needs_retry", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_attempted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    dismissed_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SiteConfigurationChange(Base):
    """Audit log for site configuration changes made by admins.
    
    Tracks when an admin switches the active SharePoint site/database via the UI.
    Used for compliance, debugging, and rollback purposes.
    """
    __tablename__ = "site_configuration_changes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Admin email who performed the change
    changed_by_email: Mapped[str] = mapped_column(String(320), index=True)
    changed_by_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    
    # Site keys (e.g., "local", "dev", "prod")
    previous_site: Mapped[str] = mapped_column(String(100), index=True)
    new_site: Mapped[str] = mapped_column(String(100), index=True)
    
    # Previous configuration details (for rollback reference)
    previous_db_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    previous_drive_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    previous_site_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    # New configuration details
    new_db_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    new_drive_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    new_site_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    # Status of the change
    status: Mapped[str] = mapped_column(String(50), default="success")  # success / failed / rolled_back
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Reason for the change
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SiteConfiguration(Base):
    """Persisted SharePoint site and document-library selections."""
    __tablename__ = "site_configurations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    site_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    site_name: Mapped[str] = mapped_column(String(256))
    site_id: Mapped[str] = mapped_column(String(512))
    drive_id: Mapped[str] = mapped_column(String(512))
    is_available_for_provisioning: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default_provisioning: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Permanently excludes this site_key from the Site Management list, even
    # though it may be rediscovered every request from an .env config block
    # (LOCAL_*/DEV_*/PROD_*). Unlike is_hidden, a removed site is not shown
    # for "unhide" — there is intentionally no UI path back; a schema fix or
    # direct DB edit is required to restore it.
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AppSetting(Base):
    """Small key/value store for app-wide admin settings, e.g. the default
    Folder Structure Mode applied to newly created vessels."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TagConfigItem(Base):
    """Settings → Tag Configuration: one tag value at one level, per client.

    Levels (optional-level hierarchy, parent must be a higher level):
    domain > main_folder > group > category > sub_category.
    Vessel Name is NOT stored here — it comes only from the Term Store.
    site_key "__template__" holds the default template for new clients;
    a client's own rows are created copy-on-write on its first change.
    See services/tag_config.py.
    """
    __tablename__ = "tag_config_items"
    __table_args__ = (
        UniqueConstraint("site_key", "level", "parent_id", "name_key", name="uq_tag_config_sibling_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_key: Mapped[str] = mapped_column(String(100), index=True)
    level: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # lower/trimmed name — enforces "unique within the same parent".
    name_key: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128))
    folder_name: Mapped[str] = mapped_column(String(128))
    code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("tag_config_items.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="Active", index=True)  # Active/Inactive/Archived
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(10), default="Custom")  # Default/Custom/Imported
    replaced_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Extra names that still resolve to this item (legacy spellings, old names after a rename).
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    # Behaviour flags, e.g. {"path_mode": "vessel"} on domains.
    attributes_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    modified_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    modified_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TagConfigSnapshot(Base):
    """Full copy of one client's tag configuration taken before every
    Replace / Import / Reset / Restore, so the change can be rolled back."""
    __tablename__ = "tag_config_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_key: Mapped[str] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    changed_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    mode: Mapped[str] = mapped_column(String(10))  # Add/Replace/Import/Reset/Restore
    level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    snapshot_json: Mapped[str] = mapped_column(Text)
