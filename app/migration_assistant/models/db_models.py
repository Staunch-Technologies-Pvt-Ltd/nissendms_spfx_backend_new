"""SQLAlchemy models — this project's own database, independent of any other
application's schema.

- MigrationScanJob: one user-driven batch — a source folder, the subfolders
  the user checked within it, and the destination vessel they picked. Scopes
  a whole scan -> preview -> confirm cycle.
- MigrationItem:    a document discovered under one of that job's selected
  subfolders, together with its AI classification suggestion and status.
- MigrationFeedback: correction log, recorded whenever a user overrides the
  AI's suggested category before confirming.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class MigrationScanJob(Base):
    __tablename__ = "migration_scan_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/done/failed

    source_folder: Mapped[str] = mapped_column(String(1024), default="")  # e.g. "SS378-PEISSY-Drawings, Plans, Manuals"
    subfolders: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of checked subfolder names
    # JSON list of individual loose files (siblings of the checked subfolders,
    # sitting directly in source_folder) the user checked one-by-one.
    files: Mapped[str] = mapped_column(Text, default="[]")

    vessel_name: Mapped[str] = mapped_column(String(400), default="")  # e.g. "Peissy"
    vessel_path: Mapped[str] = mapped_column(String(1024), default="")  # e.g. "Technical and Crewing/Peissy"
    vessel_folder_id: Mapped[str] = mapped_column(String(256), default="")

    # True when the reviewer picked the destination ROOT itself (e.g.
    # "Technical and Crewing") rather than one specific vessel folder — the
    # vessel/vessel_path/vessel_folder_id fields above then hold the root
    # itself, and each item's real vessel is resolved independently (see
    # MigrationItem.detected_vessel_* below and classifier/migration_classifier.py).
    auto_detect_vessel: Mapped[bool] = mapped_column(Boolean, default=False)

    total_found: Mapped[int] = mapped_column(default=0)
    processed: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set once Confirm Move has been run — the frozen final summary.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    items: Mapped[list["MigrationItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class MigrationItem(Base):
    """`status` lifecycle: discovered -> extracting -> suggested | needs_review |
    failed (classification phase) -> moved | to_be_classified | failed (only
    after the job's Confirm Move runs). The last three are terminal.
    """

    __tablename__ = "migration_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("migration_scan_jobs.id", ondelete="CASCADE"), index=True
    )

    source_drive_item_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    source_path: Mapped[str] = mapped_column(String(1024))
    filename: Mapped[str] = mapped_column(String(400))
    # The name this file had the very first time it was ever discovered —
    # set once, never touched again (including on re-adoption by a later
    # scan). Renaming is always computed from this, not from `filename`
    # (which reflects whatever name resulted from the last rename), so a
    # file can always be re-renamed correctly on a future re-scan even if an
    # earlier rename used a wrong heading or never happened at all.
    original_filename: Mapped[str | None] = mapped_column(String(400), nullable=True)
    content_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size: Mapped[int] = mapped_column(default=0)

    status: Mapped[str] = mapped_column(String(20), default="discovered", index=True)

    # Truncated preview of extracted text — not the full document text.
    extracted_text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI suggestion, relative to the job's vessel folder (e.g. "Drawings" or
    # "Drawings/Hull") — all null until classification runs. Category/
    # subcategory for display are derived by splitting this on "/", not
    # stored as separate columns.
    suggested_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    suggested_folder_drive_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded list[str]
    # Frozen single-file classification used by destination routing, tagging,
    # verification, and rename logic.
    classification_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Only populated for jobs with auto_detect_vessel=True — this item's own
    # resolved vessel, independent of any other item in the same job. See
    # classifier/migration_classifier.py._candidate_vessel_name /
    # services/migration_mover.py's new-vessel-creation pass in confirm_job.
    detected_vessel_name: Mapped[str | None] = mapped_column(String(400), nullable=True)
    detected_vessel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # True = matched a real existing vessel folder at classify time.
    # False = no existing vessel matched; a new one is created at Confirm
    # Move from the template vessel. NULL = job isn't auto-detect.
    vessel_exists: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vessel_confidence: Mapped[float | None] = mapped_column(nullable=True)

    # Set once Confirm Move has attempted Managed Metadata tagging for this
    # item. JSON: [{"field", "status": "applied"|"unmapped"|"skipped"|"error", "detail"}]
    term_tag_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    term_tag_report: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set if a reviewer overrode the AI's suggestion in the preview table
    # before confirming (still just a draft — no Graph call yet).
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)

    # Outcome, once Confirm Move has processed this item.
    final_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    decided_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped["MigrationScanJob"] = relationship(back_populates="items")
    feedback: Mapped[list["MigrationFeedback"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class MigrationFeedback(Base):
    """Captured for future use as few-shot examples / analytics — not yet fed
    back into the classifier."""

    __tablename__ = "migration_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    migration_item_id: Mapped[int] = mapped_column(
        ForeignKey("migration_items.id", ondelete="CASCADE"), index=True
    )
    ai_suggested_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    user_final_path: Mapped[str] = mapped_column(String(1024))
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped["MigrationItem"] = relationship(back_populates="feedback")


class ExistingFileTagScanJob(Base):
    """A recent tag-application scan for a destination root. This lets a
    reviewer reopen the same preview results without re-running the Graph scan.
    """

    __tablename__ = "existing_file_tag_scan_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    root_path: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(20), default="ready")
    summary: Mapped[str] = mapped_column(Text, default="{}")
    files: Mapped[str] = mapped_column(Text, default="[]")
    bindings: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SiteToSiteJob(Base):
    """One Site-to-Site copy job: a source site/library/folder and a
    destination site/library/folder, scoped the same way `MigrationScanJob`
    scopes a same-site move — but this is always a full recursive copy of
    everything under the chosen source folder (no subfolder checkboxes;
    there's no classification step to selectively include/exclude from).

    A separate table from `MigrationScanJob` on purpose: this feature must
    never be able to touch or corrupt the existing same-site move data.
    """

    __tablename__ = "site_to_site_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/done/failed

    source_site_key: Mapped[str] = mapped_column(String(100), default="")
    source_drive_id: Mapped[str] = mapped_column(String(256), default="")
    # The browse root everything below is relative to, e.g. "Technical and
    # Crewing/Vessel A" — not itself copied as a unit; only what's listed in
    # `selected_folders`/`selected_files` (both relative to this root) is.
    source_folder_path: Mapped[str] = mapped_column(String(1024), default="")
    # JSON list of paths relative to source_folder_path, each copied fully
    # and recursively (e.g. ["Drawings"]).
    selected_folders: Mapped[str] = mapped_column(Text, default="[]")
    # JSON list of individual file paths relative to source_folder_path, each
    # copied alone (e.g. ["Manuals/Engine Manual.pdf"]) — may sit at any
    # depth, independent of what's listed in selected_folders. A file that's
    # both individually listed here and already covered by a selected folder
    # is only ever discovered/copied once (deduped by source drive-item id).
    selected_files: Mapped[str] = mapped_column(Text, default="[]")

    dest_site_key: Mapped[str] = mapped_column(String(100), default="")
    dest_drive_id: Mapped[str] = mapped_column(String(256), default="")
    dest_folder_path: Mapped[str] = mapped_column(String(1024), default="")
    dest_folder_id: Mapped[str] = mapped_column(String(256), default="")

    total_found: Mapped[int] = mapped_column(default=0)
    processed: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    items: Mapped[list["SiteToSiteItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class SiteToSiteItem(Base):
    """One discovered folder or file under a `SiteToSiteJob`'s source
    folder. `status` lifecycle: discovered -> copying -> copied ->
    metadata_done | metadata_attention, or failed at any step. Folders don't
    go through the metadata_* states (Managed Metadata only applies to list
    items with real column values, not plain folders).
    """

    __tablename__ = "site_to_site_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("site_to_site_jobs.id", ondelete="CASCADE"), index=True
    )

    source_drive_item_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(10))  # "folder" | "file"
    # Path relative to the job's chosen source folder, e.g. "Engine/Drawing.pdf"
    relative_path: Mapped[str] = mapped_column(String(1024))
    name: Mapped[str] = mapped_column(String(400))
    size: Mapped[int] = mapped_column(default=0)

    status: Mapped[str] = mapped_column(String(20), default="discovered", index=True)

    dest_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # JSON: [{"field": ..., "status": "applied"|"label_matched"|"unmapped"|"error", "detail": ...}]
    metadata_report: Mapped[str | None] = mapped_column(Text, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped["SiteToSiteJob"] = relationship(back_populates="items")
