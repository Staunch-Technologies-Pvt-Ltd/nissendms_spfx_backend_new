"""SQLAlchemy models.

- Vessel:    one row per ship (name + IMO).
- Folder:    cache of logical-path -> SharePoint driveItem id, so we don't have
             to re-walk the Graph tree on every request. Also stores the semantic
             flags (kind / month_driven) derived from the folder template.
- UploadJob: tracks an upload's OCR + filing lifecycle for the polling UI.
"""
from datetime import date, datetime

# pyrefly: ignore [missing-import]
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, Integer, func, LargeBinary
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Vessel(Base):
    __tablename__ = "vessels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    imo: Mapped[str | None] = mapped_column(String(7), unique=True, nullable=True)
    shipyard: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hull_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vessel_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    folders: Mapped[list["Folder"]] = relationship(
        back_populates="vessel", cascade="all, delete-orphan"
    )


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Logical path within the container, e.g.
    # "Technical & Crewing/MV Horizon/Month End Reports".
    path: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
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

    folder_permissions: Mapped[list["FolderPermission"]] = relationship(
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


