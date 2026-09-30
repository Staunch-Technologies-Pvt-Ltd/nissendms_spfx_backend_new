"""Discovers everything the reviewer selected under a Site-to-Site job's
source root: each selected *folder* is walked fully and recursively (every
nested subfolder and file underneath it comes along, no partial skipping),
and each selected *file* is discovered on its own. Both kinds of selection
can coexist in the same job, and both preserve their path relative to the
job's source root exactly as it is at the source — nothing is flattened.

Runs as a background asyncio task, same convention as
`migration_scanner.run_scan`.
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

logger = logging.getLogger("migration_assistant")

SCAN_CONCURRENCY = 5

# Statuses meaning "already landed at the destination" — permanently skip on
# a re-scan, same dedupe philosophy as migration_scanner._ALREADY_MOVED_STATUSES.
_ALREADY_COPIED_STATUSES = ("copied", "metadata_done", "metadata_attention")

_EXCLUDED_FILENAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def create_job(
    source_site_key: str,
    source_drive_id: str,
    source_folder_path: str,
    selected_folders: list[str],
    selected_files: list[str],
    dest_site_key: str,
    dest_drive_id: str,
    dest_folder_path: str,
    dest_folder_id: str,
) -> int:
    with SessionLocal() as db:
        job = models.SiteToSiteJob(
            status="running",
            source_site_key=source_site_key,
            source_drive_id=source_drive_id,
            source_folder_path=source_folder_path,
            selected_folders=json.dumps(selected_folders),
            selected_files=json.dumps(selected_files),
            dest_site_key=dest_site_key,
            dest_drive_id=dest_drive_id,
            dest_folder_path=dest_folder_path,
            dest_folder_id=dest_folder_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def _discover_or_readopt(db, entry: dict, kind: str, relative_path: str, job_id: int) -> int | None:
    existing = (
        db.query(models.SiteToSiteItem).filter_by(source_drive_item_id=entry["id"]).one_or_none()
    )
    if existing:
        if existing.status in _ALREADY_COPIED_STATUSES:
            return None
        # Re-adopt into the new job under *this* scan's path context — the
        # same physical file can be rediscovered with a different relative
        # path than it had last time (e.g. previously picked individually
        # relative to a narrower source root, now swept up by a whole-
        # folder selection relative to a wider one). Keeping the stale path
        # here silently placed the file at the wrong destination location
        # on retry — the identity that must persist across re-adoption is
        # only `source_drive_item_id`, never the path.
        existing.job_id = job_id
        existing.relative_path = relative_path
        existing.name = entry["name"]
        existing.size = entry.get("size") or 0
        existing.status = "discovered"
        existing.dest_item_id = None
        existing.metadata_report = None
        existing.error = None
        db.flush()
        return existing.id
    item = models.SiteToSiteItem(
        job_id=job_id,
        source_drive_item_id=entry["id"],
        kind=kind,
        relative_path=relative_path,
        name=entry["name"],
        size=entry.get("size") or 0,
        status="discovered",
    )
    db.add(item)
    db.flush()
    return item.id


async def _walk(
    drive_id: str, folder_id: str, relative_path: str, job_id: int, sem: asyncio.Semaphore
) -> list[int]:
    async with sem:
        children = await gd.list_children(drive_id, folder_id)
    folders = [c for c in children if "folder" in c]
    files = [c for c in children if "file" in c and c["name"].lower() not in _EXCLUDED_FILENAMES]

    new_ids: list[int] = []
    with SessionLocal() as db:
        for f in folders:
            rel = f"{relative_path}/{f['name']}" if relative_path else f["name"]
            item_id = _discover_or_readopt(db, f, "folder", rel, job_id)
            if item_id is not None:
                new_ids.append(item_id)
        for f in files:
            rel = f"{relative_path}/{f['name']}" if relative_path else f["name"]
            item_id = _discover_or_readopt(db, f, "file", rel, job_id)
            if item_id is not None:
                new_ids.append(item_id)
        db.commit()

    sub_results = await asyncio.gather(
        *(
            _walk(drive_id, c["id"], f"{relative_path}/{c['name']}" if relative_path else c["name"], job_id, sem)
            for c in folders
        )
    )
    for r in sub_results:
        new_ids.extend(r)
    return new_ids


def _fail_job(job_id: int, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(models.SiteToSiteJob, job_id)
        if job:
            job.status = "failed"
            job.error = message
            job.finished_at = datetime.utcnow()
            db.commit()


async def _discover_one_file(
    drive_id: str, source_root: str, relative_path: str, job_id: int, sem: asyncio.Semaphore
) -> int | None:
    async with sem:
        item = await s2s_common.resolve_item_path(drive_id, f"{source_root}/{relative_path}" if source_root else relative_path)
    if "file" not in item:
        raise GraphError(0, f"'{relative_path}' is a folder, not a file — select it as a folder instead")
    with SessionLocal() as db:
        item_id = _discover_or_readopt(db, item, "file", relative_path, job_id)
        db.commit()
        return item_id


async def run_scan(
    job_id: int,
    source_drive_id: str,
    source_folder_path: str,
    selected_folders: list[str],
    selected_files: list[str],
) -> None:
    try:
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def _walk_selected_folder(rel: str) -> list[int]:
            path = f"{source_folder_path}/{rel}" if source_folder_path else rel
            folder = await s2s_common.resolve_folder_path(source_drive_id, path)
            return await _walk(source_drive_id, folder["id"], rel, job_id, sem)

        folder_results = await asyncio.gather(*(_walk_selected_folder(rel) for rel in selected_folders))
        file_results = await asyncio.gather(
            *(_discover_one_file(source_drive_id, source_folder_path, rel, job_id, sem) for rel in selected_files)
        )

        # A file individually selected AND already covered by a selected
        # folder's walk resolves to the same DB row (deduped by
        # source_drive_item_id in _discover_or_readopt) — dedupe the id list
        # here too so it's only counted once, not twice, in total_found.
        seen: set[int] = set()
        new_ids: list[int] = []
        for i in [*(i for ids in folder_results for i in ids), *file_results]:
            if i is not None and i not in seen:
                seen.add(i)
                new_ids.append(i)

        with SessionLocal() as db:
            job = db.get(models.SiteToSiteJob, job_id)
            job.total_found = len(new_ids)
            job.processed = len(new_ids)
            job.status = "done"
            job.finished_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        logger.exception("Site-to-Site scan %s failed", job_id)
        _fail_job(job_id, str(e))
