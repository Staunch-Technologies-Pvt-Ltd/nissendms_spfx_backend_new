"""The Site-to-Site copy engine — the only place in this feature that writes
to Graph or SharePoint REST, run only when the reviewer clicks Confirm &
Copy. Mirrors `services/migration_mover.py`'s shape (continue past
individual failures, one final summary) but copies instead of moves and
additionally migrates column metadata, including Managed Metadata, per file
— and optionally version history and unique permissions.

The copy runs as a background task (`start_copy` returns immediately), so a
large job can never hit an HTTP timeout. Progress, pause/resume and cancel go
through `site_to_site_progress.CopyRun`; every item's outcome is written to
the database as it happens, so a cancelled, failed or interrupted (server
restart) run can be resumed later and only re-processes what isn't done.

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
from . import site_to_site_progress as progress
from . import site_to_site_verify
from . import term_mapping
from .errors import BadRequest, Conflict, NotFound

logger = logging.getLogger("migration_assistant")

COPY_CONCURRENCY = 4

CONFLICT_POLICIES = ("skip", "replace", "rename", "fail")
_DONE_STATUSES = ("copied", "metadata_done", "metadata_attention")
# copy_status values from which a new run may start (None = never started).
_STARTABLE = (None, "cancelled", "interrupted", "failed", "completed", "completed_with_warnings", "completed_with_errors")


def _load_job(job_id: str) -> models.SiteToSiteJob:
    if not job_id.isdigit():
        raise NotFound("Copy job not found")
    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, int(job_id))
        if job is None:
            raise NotFound("Copy job not found")
        db.expunge(job)
        return job


def _update_job(job_id: int, **fields) -> None:
    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, job_id)
        for k, v in fields.items():
            setattr(job, k, v)
        db.commit()


async def start_copy(
    job_id: str,
    decided_by_email: str,
    *,
    conflict_policy: str = "skip",
    copy_permissions: bool = False,
    copy_versions: bool = False,
) -> None:
    """Validate and kick off a copy run in the background. Also used to
    resume a cancelled/interrupted run or retry a finished one's failures —
    items already copied are never copied again."""
    if conflict_policy not in CONFLICT_POLICIES:
        raise BadRequest(f"conflict_policy must be one of {', '.join(CONFLICT_POLICIES)}")
    job = _load_job(job_id)
    if job.status != "done":
        raise BadRequest("This job's scan hasn't finished yet")
    if progress.is_active(job.id) or job.copy_status in ("running", "paused"):
        raise Conflict("This job is already copying — pause, resume or cancel it instead")
    if job.copy_status not in _STARTABLE:
        raise Conflict(f"This job can't be started from state '{job.copy_status}'")

    now = datetime.utcnow()
    _update_job(
        job.id,
        copy_status="running",
        conflict_policy=conflict_policy,
        copy_permissions=copy_permissions,
        copy_versions=copy_versions,
        confirmed_at=job.confirmed_at or now,
        confirmed_by_email=decided_by_email,
        copy_started_at=now,
        copy_finished_at=None,
        verify_status=None,
    )
    run = progress.start(job.id)
    run.task = asyncio.create_task(
        _run_copy(job.id, run, conflict_policy=conflict_policy, copy_permissions=copy_permissions, copy_versions=copy_versions)
    )


def pause(job_id: str) -> None:
    job = _load_job(job_id)
    run = progress.get(job.id)
    if run is None or not progress.is_active(job.id) or run.state != "running":
        raise Conflict("Only a running copy can be paused")
    run.pause()
    _update_job(job.id, copy_status="paused")


async def resume(job_id: str, decided_by_email: str) -> None:
    """Un-pause a paused run in this process, or start a fresh run (with the
    job's saved options) for one that was cancelled, interrupted or failed."""
    job = _load_job(job_id)
    run = progress.get(job.id)
    if run is not None and progress.is_active(job.id):
        if run.state != "paused":
            raise Conflict("This copy isn't paused")
        run.resume()
        _update_job(job.id, copy_status="running")
        await run.notify()
        return
    if job.copy_status == "paused":
        # Paused in a process that no longer owns it — treat as interrupted.
        _update_job(job.id, copy_status="interrupted")
    await start_copy(
        job_id, decided_by_email,
        conflict_policy=job.conflict_policy or "skip",
        copy_permissions=bool(job.copy_permissions),
        copy_versions=bool(job.copy_versions),
    )


async def cancel(job_id: str) -> None:
    """Stop after the files currently in flight. Nothing already copied is
    undone; the rest stays "discovered" so the job can be resumed."""
    job = _load_job(job_id)
    run = progress.get(job.id)
    if run is None or not progress.is_active(job.id):
        if job.copy_status in ("running", "paused"):
            _update_job(job.id, copy_status="cancelled", copy_finished_at=datetime.utcnow())
            return
        raise Conflict("This job isn't copying")
    run.cancel()
    await run.notify()


async def _run_copy(job_id: int, run: progress.CopyRun, *, conflict_policy: str, copy_permissions: bool, copy_versions: bool) -> None:
    try:
        await _copy_all(job_id, run, conflict_policy=conflict_policy, copy_permissions=copy_permissions, copy_versions=copy_versions)
        if run.cancel_requested:
            final = "cancelled"
        else:
            run.phase = run.state = "verifying"
            await run.notify()
            try:
                await site_to_site_verify.verify_job(job_id, run)
            except Exception:
                logger.exception("Verification of Site-to-Site job %s failed", job_id)
            final = "completed"
            if run.files_failed or run.folders_failed:
                final = "completed_with_errors"
            elif run.metadata_attention or run.permissions_attention:
                final = "completed_with_warnings"
        run.state = final
        run.phase = "done"
        _update_job(job_id, copy_status=final, copy_finished_at=datetime.utcnow(), copy_summary=json.dumps(run.counters()))
    except Exception as e:
        logger.exception("Site-to-Site copy %s failed", job_id)
        run.state = "failed"
        run.phase = "done"
        _update_job(
            job_id, copy_status="failed", copy_finished_at=datetime.utcnow(),
            copy_summary=json.dumps({**run.counters(), "error": str(e)}),
        )
    finally:
        await run.notify()


async def _copy_all(job_id: int, run: progress.CopyRun, *, conflict_policy: str, copy_permissions: bool, copy_versions: bool) -> None:
    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, job_id)
        all_items = db.query(models.SiteToSiteItem).filter_by(job_id=job_id).all()
        retry_statuses = {"discovered", "failed", "metadata_attention"}
        if conflict_policy != "skip":
            retry_statuses.add("skipped")  # a new policy may now copy them
        rows = [
            {
                "id": i.id, "kind": i.kind, "relative_path": i.relative_path, "name": i.name, "size": i.size or 0,
                "source_drive_item_id": i.source_drive_item_id, "dest_item_id": i.dest_item_id, "status": i.status,
            }
            for i in all_items
        ]
        source_drive_id = job.source_drive_id
        dest_site_key = job.dest_site_key
        dest_drive_id = job.dest_drive_id
        dest_folder_id = job.dest_folder_id

    pending = [r for r in rows if r["status"] in retry_statuses]
    files = [r for r in rows if r["kind"] == "file"]
    run.files_total = len(files)
    run.bytes_total = sum(r["size"] for r in files)
    run.folders_total = sum(1 for r in rows if r["kind"] == "folder")
    # Count what earlier runs already finished so a resumed job's progress
    # bar starts where it left off, not at zero.
    run.files_done = sum(1 for r in files if r["status"] in ("copied", "metadata_done"))
    run.bytes_done = sum(r["size"] for r in files if r["status"] in ("copied", "metadata_done"))
    run.files_skipped = sum(1 for r in files if r["status"] == "skipped" and "skipped" not in retry_statuses)
    run.folders_done = sum(1 for r in rows if r["kind"] == "folder" and r["status"] not in retry_statuses)
    await run.notify()

    dest_site = await s2s_common.resolve_site(dest_site_key)
    dest_site_url = s2s_common.site_url(dest_site)

    # Destination folder listings, cached per parent so collision checks cost
    # one listing per folder instead of one per file.
    children: dict[str, dict[str, dict]] = {}
    child_locks: dict[str, asyncio.Lock] = {}

    async def _children(parent_id: str) -> dict[str, dict]:
        lock = child_locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            if parent_id not in children:
                kids = await gd.list_children(dest_drive_id, parent_id)
                children[parent_id] = {c["name"].lower(): c for c in kids}
        return children[parent_id]

    folder_id_by_path: dict[str, str] = {"": dest_folder_id}
    for r in rows:
        if r["kind"] == "folder" and r["status"] == "copied" and r["dest_item_id"]:
            folder_id_by_path.setdefault(r["relative_path"], r["dest_item_id"])
    chain_lock = asyncio.Lock()

    async def _ensure_folder_chain(dir_path: str) -> str | None:
        """Resolve `dir_path`'s destination folder id, creating any part of
        the chain that isn't already known. A selected folder's own root is
        never itself recorded as a `SiteToSiteItem` (only its children are),
        and an individually-selected file has no folder rows at all, so a
        plain per-row parent lookup isn't enough — this recurses up to the
        seeded destination root. `ensure_folder` is create-or-reuse, and the
        lock keeps concurrent workers from racing to create the same one."""
        if dir_path in folder_id_by_path:
            return folder_id_by_path[dir_path]
        parent_path = dir_path.rsplit("/", 1)[0] if "/" in dir_path else ""
        parent_id = await _ensure_folder_chain(parent_path)
        if parent_id is None:
            return None
        async with chain_lock:
            if dir_path in folder_id_by_path:
                return folder_id_by_path[dir_path]
            name = dir_path.rsplit("/", 1)[-1]
            try:
                folder = await gd.ensure_folder(dest_drive_id, parent_id, name)
            except GraphError:
                return None
            folder_id_by_path[dir_path] = folder["id"]
            return folder["id"]

    # ── Folders first, shallowest first ────────────────────────────────────
    run.phase = "folders"
    for row in sorted((r for r in pending if r["kind"] == "folder"), key=lambda r: r["relative_path"].count("/")):
        await run.pause_gate.wait()
        if run.cancel_requested:
            return
        parent_path = row["relative_path"].rsplit("/", 1)[0] if "/" in row["relative_path"] else ""
        parent_id = await _ensure_folder_chain(parent_path)
        if parent_id is None:
            _mark(row["id"], status="failed", error="Parent folder was not created")
            run.folders_failed += 1
            run.event(row["relative_path"], "folder", "failed", "Parent folder was not created")
            await run.notify()
            continue
        try:
            folder = await gd.ensure_folder(dest_drive_id, parent_id, row["name"])
        except GraphError as e:
            _mark(row["id"], status="failed", error=str(e))
            run.folders_failed += 1
            run.event(row["relative_path"], "folder", "failed", str(e))
            await run.notify()
            continue
        folder_id_by_path[row["relative_path"]] = folder["id"]
        perm_report = None
        if copy_permissions:
            perm_report = await _copy_permissions(source_drive_id, row["source_drive_item_id"], dest_drive_id, folder["id"])
            if any(p["status"] == "error" for p in perm_report):
                run.permissions_attention += 1
        _mark(row["id"], status="copied", dest_item_id=folder["id"], permissions_report=perm_report)
        run.folders_done += 1
        run.event(row["relative_path"], "folder", "created")
        await run.notify()

    # ── Files, through a small worker pool ─────────────────────────────────
    run.phase = "files"
    queue: asyncio.Queue = asyncio.Queue()
    for r in pending:
        if r["kind"] == "file":
            queue.put_nowait(r)

    def _fail(row: dict, error: str) -> None:
        _mark(row["id"], status="failed", error=error)
        run.files_failed += 1
        run.event(row["relative_path"], "file", "failed", error)

    async def _copy_new(row: dict, parent_id: str, behavior: str) -> tuple[str | None, str | None]:
        """Run one Graph copy. Returns (dest_item_id, note)."""
        note = None
        try:
            monitor_url = await gd.copy_item(
                source_drive_id, row["source_drive_item_id"], dest_drive_id, parent_id, row["name"],
                conflict_behavior=behavior, include_versions=copy_versions,
            )
        except GraphError as e:
            if not (copy_versions and e.status == 400):
                raise
            # Tenants without version-history copy reject the flag — fall
            # back to the current version rather than failing the file.
            monitor_url = await gd.copy_item(
                source_drive_id, row["source_drive_item_id"], dest_drive_id, parent_id, row["name"],
                conflict_behavior=behavior,
            )
            note = "Version history could not be copied on this tenant — only the current version was copied"
        # Server-side copies of big files (and their versions) take a while:
        # allow ~2 s per MB, never less than 3 minutes.
        timeout = max(180.0, row["size"] / 1_000_000 * 2)
        result = await gd.poll_copy_status(monitor_url, timeout_seconds=timeout)
        if result.get("status") != "completed":
            err = result.get("error")
            if isinstance(err, dict):
                err = err.get("message") or err.get("code")
            raise GraphError(0, err or "Copy did not complete")
        dest_item_id = result.get("resourceId")
        if not dest_item_id:
            # Some tenants omit resourceId — look the new file up instead.
            # With "rename" its final name isn't known, so take the newest
            # entry that wasn't there before.
            before = await _children(parent_id)
            fresh = {c["name"].lower(): c for c in await gd.list_children(dest_drive_id, parent_id)}
            if behavior == "rename":
                new = [c for k, c in fresh.items() if k not in before and "file" in c]
                new.sort(key=lambda c: c.get("createdDateTime", ""), reverse=True)
                dest_item_id = new[0]["id"] if new else None
            else:
                found = fresh.get(row["name"].lower())
                dest_item_id = found["id"] if found else None
        if dest_item_id:
            # Graph's monitor has been observed to report "completed" with a
            # resourceId that then 404s. Never trust "completed" alone — a
            # phantom success would be skipped forever on every re-scan.
            try:
                item = await gd.get_item(dest_drive_id, dest_item_id)
                (await _children(parent_id))[item["name"].lower()] = item
            except GraphError:
                dest_item_id = None
        return dest_item_id, note

    async def _process_file(row: dict) -> None:
        parent_path = row["relative_path"].rsplit("/", 1)[0] if "/" in row["relative_path"] else ""
        parent_id = await _ensure_folder_chain(parent_path)
        if parent_id is None:
            _fail(row, "Destination folder could not be created")
            return
        try:
            existing = (await _children(parent_id)).get(row["name"].lower())
            note = None
            if existing and "folder" in existing:
                _fail(row, f"A folder named '{row['name']}' already exists at the destination")
                return
            if existing and row["dest_item_id"] and existing.get("id") == row["dest_item_id"]:
                dest_item_id = existing["id"]  # copied by an earlier run; just redo metadata
            elif existing and conflict_policy == "skip":
                _mark(row["id"], status="skipped", error="Already exists at the destination — skipped")
                run.files_skipped += 1
                run.event(row["relative_path"], "file", "skipped", "Already exists at the destination")
                return
            elif existing and conflict_policy == "fail":
                _fail(row, f"'{row['name']}' already exists at the destination")
                return
            else:
                behavior = conflict_policy if existing else "fail"
                dest_item_id, note = await _copy_new(row, parent_id, behavior)
        except GraphError as e:
            _fail(row, str(e))
            return

        if not dest_item_id:
            _fail(row, "Copy reported success but the destination file could not be verified")
            return
        run.files_done += 1
        run.add_bytes(row["size"])

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
        if note:
            report = [*report, {"field": "_versions", "kind": "versions", "status": "unmapped", "detail": note}]
        needs_attention = any(r["status"] in ("unmapped", "error") for r in report)
        if needs_attention:
            run.metadata_attention += 1

        perm_report = None
        if copy_permissions:
            perm_report = await _copy_permissions(source_drive_id, row["source_drive_item_id"], dest_drive_id, dest_item_id)
            if any(p["status"] == "error" for p in perm_report):
                run.permissions_attention += 1

        _mark(
            row["id"],
            status="metadata_attention" if needs_attention else "metadata_done",
            dest_item_id=dest_item_id,
            metadata_report=report,
            permissions_report=perm_report,
        )
        run.event(row["relative_path"], "file", "attention" if needs_attention else "copied")

    async def _worker() -> None:
        while True:
            await run.pause_gate.wait()
            if run.cancel_requested:
                return
            try:
                row = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            run.in_flight.add(row["relative_path"])
            await run.notify()
            try:
                await _process_file(row)
            except Exception as e:  # one bad file must never stop the worker
                logger.exception("Unexpected error copying item %s", row["id"])
                _fail(row, f"Unexpected error: {e}")
            finally:
                run.in_flight.discard(row["relative_path"])
                await run.notify()

    await asyncio.gather(*(_worker() for _ in range(COPY_CONCURRENCY)))


async def _copy_permissions(src_drive_id: str, src_item_id: str, dest_drive_id: str, dest_item_id: str) -> list[dict]:
    """Re-grant an item's *unique* (non-inherited) user/group permissions on
    its copy. Inherited permissions come from the destination's own parent
    and are left alone. Sharing links and SharePoint groups can't be
    recreated through Graph — they're reported as "skipped" so the report
    shows exactly what needs doing by hand."""
    try:
        perms = await gd.list_permissions(src_drive_id, src_item_id)
    except GraphError as e:
        return [{"principal": "*", "roles": [], "status": "error", "detail": f"Could not read source permissions: {e}"}]

    report: list[dict] = []
    grants: dict[tuple[str, ...], list[str]] = {}
    for p in perms:
        if p.get("inheritedFrom"):
            continue
        source_roles = p.get("roles") or []
        roles = ["write"] if ("write" in source_roles or "owner" in source_roles) else ["read"]
        if p.get("link"):
            scope = (p["link"] or {}).get("scope", "link")
            report.append({"principal": f"sharing link ({scope})", "roles": source_roles, "status": "skipped",
                           "detail": "Sharing links can't be copied — re-share from the destination if still needed"})
            continue
        identities = []
        for key in ("grantedToV2", "grantedTo"):
            if p.get(key):
                identities.append(p[key])
                break
        identities.extend(p.get("grantedToIdentitiesV2") or [])
        for ident in identities:
            principal = ident.get("user") or ident.get("group") or ident.get("siteUser") or ident.get("siteGroup") or {}
            name = principal.get("displayName") or principal.get("email") or "unknown"
            email = principal.get("email")
            if not email and principal.get("loginName") and "|" in principal["loginName"]:
                candidate = principal["loginName"].rsplit("|", 1)[-1]
                email = candidate if "@" in candidate else None
            if not email:
                report.append({"principal": name, "roles": source_roles, "status": "skipped",
                               "detail": "No email to grant to (e.g. a SharePoint group) — recreate it on the destination site"})
                continue
            grants.setdefault(tuple(roles), []).append(email)

    for roles, emails in grants.items():
        for email in sorted(set(emails)):
            try:
                await gd.invite(dest_drive_id, dest_item_id, [email], list(roles))
                report.append({"principal": email, "roles": list(roles), "status": "applied", "detail": None})
            except GraphError as e:
                report.append({"principal": email, "roles": list(roles), "status": "error", "detail": str(e)})
    return report


def _mark(
    item_id: int,
    *,
    status: str,
    dest_item_id: str | None = None,
    error: str | None = None,
    metadata_report: list | None = None,
    permissions_report: list | None = None,
) -> None:
    with SessionLocal() as db:
        item = db.get(models.SiteToSiteItem, item_id)
        item.status = status
        if dest_item_id is not None:
            item.dest_item_id = dest_item_id
        item.error = error
        if metadata_report is not None:
            item.metadata_report = json.dumps(metadata_report)
        if permissions_report is not None:
            item.permissions_report = json.dumps(permissions_report)
        item.verify_status = None
        item.verify_detail = None
        item.updated_at = datetime.utcnow()
        db.commit()
