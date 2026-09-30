"""The Site-to-Site copy engine — the only place in this feature that writes
to Graph or SharePoint REST, run only when the reviewer clicks Confirm &
Copy. Mirrors `services/migration_mover.py`'s shape (continue past
individual failures, prechecked destination collisions, one final summary)
but copies instead of moves and additionally migrates column metadata,
including Managed Metadata, per file.

The source is only ever read here (`gd.copy_item` copies; nothing calls
`move_item` or `delete`) — required since, unlike the same-site mode, this
is explicitly a copy that must leave the source SharePoint site untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from ..db import SessionLocal
from ..graph import drive as gd
from ..graph.client import GraphError
from ..models import db_models as models

from . import site_to_site_common as s2s_common
from . import term_mapping
from .errors import BadRequest, Conflict, NotFound

logger = logging.getLogger("migration_assistant")

COPY_CONCURRENCY = 4


async def confirm_job(job_id: str, decided_by_email: str) -> dict:
    if not job_id.isdigit():
        raise NotFound("Copy job not found")
    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, int(job_id))
        if job is None:
            raise NotFound("Copy job not found")
        if job.status != "done":
            raise BadRequest("This job's scan hasn't finished yet")
        if job.confirmed_at is not None:
            raise Conflict("This job has already been confirmed")

        all_items = db.query(models.SiteToSiteItem).filter_by(job_id=job.id).all()
        pending = [i for i in all_items if i.status in ("discovered", "failed", "metadata_attention")]
        pending_rows = [
            {
                "id": i.id,
                "kind": i.kind,
                "relative_path": i.relative_path,
                "name": i.name,
                "source_drive_item_id": i.source_drive_item_id,
                "dest_item_id": i.dest_item_id,
                "status": i.status,
            }
            for i in pending
        ]
        total_items = len(all_items)
        source_drive_id = job.source_drive_id
        dest_site_key = job.dest_site_key
        dest_drive_id = job.dest_drive_id
        dest_folder_id = job.dest_folder_id
        dest_folder_path = job.dest_folder_path

    dest_site = await s2s_common.resolve_site(dest_site_key)
    dest_site_url = s2s_common.site_url(dest_site)

    counters = {
        "folders_created": 0, "folders_existing": 0, "folders_failed": 0,
        "files_copied": 0, "files_failed": 0,
        "metadata_migrated": 0, "metadata_attention": 0,
        "managed_metadata_applied": 0, "managed_metadata_unmapped": 0,
    }
    failed_items: list[dict] = []
    now = datetime.utcnow()

    # Folders first, shallowest path first, so every file's parent already
    # exists by the time files are processed — `ensure_folder` is create-or-
    # reuse, so re-running a partially-failed job never duplicates a folder.
    folder_rows = sorted((r for r in pending_rows if r["kind"] == "folder"), key=lambda r: r["relative_path"].count("/"))
    folder_id_by_path: dict[str, str] = {"": dest_folder_id}

    async def _ensure_folder_chain(dir_path: str) -> str | None:
        """Resolve `dir_path`'s destination folder id, creating any part of
        the chain that isn't already known. This covers two things: an
        ordinary nested folder discovered by a selected folder's own
        recursive walk (its own parent segment usually already has its own
        row below it) *and* the case that trips up a plain per-row parent
        lookup — a selected folder's own root is never itself recorded as a
        `SiteToSiteItem` (only its children are, per the scanner), so the
        first level directly under it has no row to find as "its parent"
        without this recursing all the way up to the seeded destination
        root. Also used for an individually-selected file's ancestor chain,
        which never gets folder rows at all. Two callers racing to create
        the same missing ancestor concurrently is safe (if redundant) —
        `ensure_folder` itself is create-or-reuse, so both resolve to the
        same final folder id."""
        if dir_path in folder_id_by_path:
            return folder_id_by_path[dir_path]
        parent_path = dir_path.rsplit("/", 1)[0] if "/" in dir_path else ""
        parent_id = await _ensure_folder_chain(parent_path)
        if parent_id is None:
            return None
        name = dir_path.rsplit("/", 1)[-1]
        try:
            folder = await gd.ensure_folder(dest_drive_id, parent_id, name)
        except GraphError:
            return None
        folder_id_by_path[dir_path] = folder["id"]
        counters["folders_created"] += 1
        return folder["id"]

    for row in folder_rows:
        parent_path = row["relative_path"].rsplit("/", 1)[0] if "/" in row["relative_path"] else ""
        parent_id = await _ensure_folder_chain(parent_path)
        if parent_id is None:
            # The chain up to this folder's parent could not be created.
            _mark_failed(row["id"], "Parent folder was not created", decided_by_email, now)
            counters["folders_failed"] += 1
            failed_items.append({"path": row["relative_path"], "error": "Parent folder was not created"})
            continue
        try:
            folder = await gd.ensure_folder(dest_drive_id, parent_id, row["name"])
            existed = folder.get("id") == row.get("dest_item_id")
            folder_id_by_path[row["relative_path"]] = folder["id"]
            _mark_copied(row["id"], folder["id"], decided_by_email, now)
            if existed:
                counters["folders_existing"] += 1
            else:
                counters["folders_created"] += 1
        except GraphError as e:
            _mark_failed(row["id"], str(e), decided_by_email, now)
            counters["folders_failed"] += 1
            failed_items.append({"path": row["relative_path"], "error": str(e)})

    # Also seed folder_id_by_path with folders from *previous* runs of this
    # job that already succeeded, so files under them resolve correctly.
    with SessionLocal() as db:
        prior_folders = (
            db.query(models.SiteToSiteItem)
            .filter_by(job_id=job.id, kind="folder", status="copied")
            .all()
        )
        for f in prior_folders:
            folder_id_by_path.setdefault(f.relative_path, f.dest_item_id)

    file_rows = [r for r in pending_rows if r["kind"] == "file"]
    sem = asyncio.Semaphore(COPY_CONCURRENCY)

    async def _copy_one_file(row: dict) -> None:
        async with sem:
            parent_path = row["relative_path"].rsplit("/", 1)[0] if "/" in row["relative_path"] else ""
            parent_id = await _ensure_folder_chain(parent_path)
            if parent_id is None:
                _mark_failed(row["id"], "Destination folder could not be created", decided_by_email, now)
                counters["files_failed"] += 1
                failed_items.append({"path": row["relative_path"], "error": "Destination folder could not be created"})
                return
            try:
                existing = await gd.find_child(dest_drive_id, parent_id, row["name"])
                if existing and "file" in existing and existing.get("id") == row.get("dest_item_id"):
                    dest_item_id = existing["id"]  # already copied by a prior run of this job
                elif existing and "file" in existing:
                    _mark_failed(row["id"], f"'{row['name']}' already exists in the destination folder", decided_by_email, now)
                    counters["files_failed"] += 1
                    failed_items.append({"path": row["relative_path"], "error": f"'{row['name']}' already exists at destination"})
                    return
                else:
                    monitor_url = await gd.copy_item(
                        source_drive_id, row["source_drive_item_id"], dest_drive_id, parent_id, row["name"]
                    )
                    result = await gd.poll_copy_status(monitor_url)
                    if result.get("status") != "completed":
                        raise GraphError(0, result.get("error", "Copy did not complete"))
                    dest_item_id = result.get("resourceId")
                    if not dest_item_id:
                        # Some tenants omit resourceId on the monitor payload — look the file up by name instead.
                        found = await gd.find_child(dest_drive_id, parent_id, row["name"])
                        dest_item_id = found["id"] if found else None
                    if dest_item_id:
                        # Graph's async-copy monitor has been observed to
                        # report "completed" with a resourceId that then
                        # 404s — a real, if rare, inconsistency. Never trust
                        # "completed" alone; confirm the item is actually
                        # there before recording this as done, since a
                        # phantom success here means the file is silently
                        # skipped forever on every future re-scan (deduped
                        # by an id that never truly copied).
                        try:
                            await gd.get_item(dest_drive_id, dest_item_id)
                        except GraphError:
                            dest_item_id = None
                    if dest_item_id:
                        counters["files_copied"] += 1
            except GraphError as e:
                _mark_failed(row["id"], str(e), decided_by_email, now)
                counters["files_failed"] += 1
                failed_items.append({"path": row["relative_path"], "error": str(e)})
                return

            if not dest_item_id:
                _mark_failed(row["id"], "Copy reported success but the destination file could not be verified", decided_by_email, now)
                counters["files_failed"] += 1
                failed_items.append({"path": row["relative_path"], "error": "Copy reported success but the destination file could not be verified"})
                return

            # Metadata migration — a failure here never undoes the copy; the
            # file just stays "metadata_attention" for a later retry.
            try:
                report = await term_mapping.migrate_item_fields(
                    source_drive_id=source_drive_id,
                    source_item_id=row["source_drive_item_id"],
                    dest_site_url=dest_site_url,
                    dest_site_id=dest_site["site_id"],
                    dest_drive_id=dest_drive_id,
                    dest_item_id=dest_item_id,
                )
            except Exception as e:
                logger.exception("Metadata migration failed for item %s", row["id"])
                report = [{"field": "*", "status": "error", "detail": str(e)}]

            needs_attention = any(r["status"] in ("unmapped", "error") for r in report)
            for r in report:
                if r["status"] in ("applied", "label_matched"):
                    counters["metadata_migrated"] += 1
                    if r.get("kind") == "managed_metadata":
                        counters["managed_metadata_applied"] += 1
                elif r["status"] == "unmapped":
                    counters["metadata_attention"] += 1
                    if r.get("kind") == "managed_metadata":
                        counters["managed_metadata_unmapped"] += 1
                elif r["status"] == "error":
                    counters["metadata_attention"] += 1

            _mark_copied(
                row["id"], dest_item_id, decided_by_email, now,
                status="metadata_attention" if needs_attention else "metadata_done",
                metadata_report=report,
            )

    await asyncio.gather(*(_copy_one_file(r) for r in file_rows))

    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, job.id)
        job.confirmed_at = now
        job.confirmed_by_email = decided_by_email
        db.commit()

    status = "completed"
    if counters["folders_failed"] or counters["files_failed"]:
        status = "completed_with_errors"
    elif counters["metadata_attention"]:
        status = "completed_with_warnings"

    return {
        "total": total_items,
        **counters,
        "failed_items": failed_items,
        "status": status,
    }


def _mark_copied(item_id: int, dest_item_id: str, decided_by_email: str, now: datetime, *, status: str = "copied", metadata_report: list | None = None) -> None:
    with SessionLocal() as db:
        item = db.get(models.SiteToSiteItem, item_id)
        item.status = status
        item.dest_item_id = dest_item_id
        item.error = None
        if metadata_report is not None:
            item.metadata_report = json.dumps(metadata_report)
        item.updated_at = now
        db.commit()


def _mark_failed(item_id: int, error: str, decided_by_email: str, now: datetime) -> None:
    with SessionLocal() as db:
        item = db.get(models.SiteToSiteItem, item_id)
        item.status = "failed"
        item.error = error
        item.updated_at = now
        db.commit()
