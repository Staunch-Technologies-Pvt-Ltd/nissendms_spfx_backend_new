"""SharePoint -> database synchronisation for the ``folders`` / ``vessels`` tables.

Two cooperating jobs (both registered in scheduler.py, both best-effort and
non-raising):

* ``reconcile_vessel_folders`` -- for every provisioned vessel, confirm its
  SharePoint ship folder still exists. A vessel whose folder was deleted
  directly in SharePoint is soft-deleted into the app's Recycle Bin (the same
  path as an in-app delete). Never acts on a single failed lookup: the folder
  is looked up by stable item id, then by path, then by name (it may have been
  moved/renamed), the drive must be reachable, and the miss must repeat after a
  short delay. A per-run cap refuses to remove many vessels at once, which
  protects against a permissions / outage problem looking like mass deletion.

* ``sync_folder_table`` -- incremental Graph *delta* per drive. Folder rows are
  matched by ``drive_item_id`` (the identity the table already stores):
  deleted folder -> row (and its descendants) removed; renamed -> row name and
  path updated; moved -> path rebuilt from the new parent's row. A deleted
  *ship* folder is routed to the vessel check above instead of silently
  dropping the row.

Nothing here writes to SharePoint. Every decision is logged.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from ..db import models
from ..db.base import SessionLocal
from ..graph import drive as gd
from ..graph.client import GraphError, graph

log = logging.getLogger(__name__)

# Do not judge a vessel younger than this: a just-created folder may not be in
# Graph's search index yet, which the name fallback relies on.
MIN_VESSEL_AGE = timedelta(minutes=30)
# Refuse to auto-remove more than this many vessels in one run.
MAX_AUTO_REMOVALS = 3
# Pause before the confirming second lookup.
RECHECK_DELAY_SECONDS = 5.0
_DELTA_KEY_PREFIX = "folder_sync_delta:"


def _q(db, *entities):
    """Folder queries here span every drive; opt out of the per-active-site scoping
    that app/db/base.py applies to Folder selects."""
    return db.query(*entities).execution_options(all_sites=True)


def _squash(value: str | None) -> str:
    """Case-insensitive name key with repeated spaces collapsed."""
    return " ".join((value or "").split()).casefold()


@dataclass
class _Target:
    vessel_id: int
    vessel_name: str
    drive_id: str
    item_id: str | None
    path: str | None


# ---------------------------------------------------------------- vessel check
def _collect_targets(now: datetime | None = None) -> list[_Target]:
    """One target per known SharePoint ship folder of every provisioned vessel
    old enough to judge. Vessels with no known location are skipped."""
    targets: list[_Target] = []
    with SessionLocal() as db:
        if now is None:
            # created_at is stamped by the database (server_default now()), so age
            # must be measured on the database's own clock/timezone.
            db_now = db.execute(select(func.now())).scalar()
            now = db_now.replace(tzinfo=None) if db_now is not None else datetime.now()
        vessels = db.query(models.Vessel).all()
        for v in vessels:
            created = v.created_at
            if created is not None and (now - created.replace(tzinfo=None)) < MIN_VESSEL_AGE:
                continue
            rows = (
                _q(db, models.Folder)
                .filter(models.Folder.vessel_id == v.id, models.Folder.kind == "ship")
                .all()
            )
            found = False
            for r in rows:
                if r.site_id and (r.drive_item_id or r.path):
                    targets.append(_Target(v.id, v.name, r.site_id, r.drive_item_id or None, r.path or None))
                    found = True
            if found or not (v.is_provisioned and v.vessel_folder_path):
                continue
            # Vessel created at a custom site/path: only the path is recorded.
            site_ref = v.provisioned_site_key or next(iter(v.provisioned_site_ids or []), None)
            if not site_ref:
                continue
            # The drive is resolved later (async) by _resolve_deferred_drives.
            targets.append(_Target(v.id, v.name, f"@site:{site_ref}", None, v.vessel_folder_path))
    return targets


async def _resolve_deferred_drives(targets: list[_Target]) -> list[_Target]:
    """Resolve ``@site:<ref>`` placeholders to a drive id; drop unresolvable ones."""
    out: list[_Target] = []
    cache: dict[str, str | None] = {}
    for t in targets:
        if not t.drive_id.startswith("@site:"):
            out.append(t)
            continue
        ref = t.drive_id[len("@site:"):]
        if ref not in cache:
            try:
                from .site_provisioning import resolve_site_drive
                with SessionLocal() as db:
                    _, drive_id, _ = await resolve_site_drive(ref, db=db)
                cache[ref] = drive_id
            except Exception as exc:
                log.warning("[folder_sync] cannot resolve drive for site '%s': %s", ref, exc)
                cache[ref] = None
        if cache[ref]:
            out.append(_Target(t.vessel_id, t.vessel_name, cache[ref], t.item_id, t.path))
    return out


async def _drive_reachable(drive_id: str) -> bool:
    try:
        await graph().get(f"/drives/{drive_id}/root?$select=id")
        return True
    except Exception:
        return False


async def _target_state(t: _Target) -> tuple[str, dict | None]:
    """Return ("present", item) | ("missing", None) | ("unknown", None)."""
    g = graph()
    if t.item_id:
        try:
            item = await g.get(
                f"/drives/{t.drive_id}/items/{t.item_id}?$select=id,name,folder,parentReference,deleted"
            )
            if item.get("deleted") is None and item.get("folder") is not None:
                return "present", item
        except GraphError as exc:
            if exc.status != 404:
                return "unknown", None
        except Exception:
            return "unknown", None
    if t.path:
        try:
            item = await gd.get_item_by_path(t.drive_id, t.path, select="id,name,folder,parentReference")
            if item and item.get("folder") is not None:
                return "present", item
        except GraphError as exc:
            if exc.status != 404:
                return "unknown", None
        except Exception:
            return "unknown", None
    # id and path both gone. Only conclude "missing" when the drive itself is
    # readable and no folder of that name exists anywhere (it may have moved).
    if not await _drive_reachable(t.drive_id):
        return "unknown", None
    try:
        hits = await gd.search_items(t.drive_id, t.vessel_name)
    except Exception:
        return "unknown", None
    for hit in hits or []:
        if hit.get("folder") is not None and _squash(hit.get("name")) == _squash(t.vessel_name):
            return "present", hit
    return "missing", None


async def _confirmed_missing_vessels(
    targets: list[_Target],
    state_fn: Callable[[_Target], Awaitable[tuple[str, dict | None]]] = _target_state,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> tuple[dict[int, str], dict[int, tuple[_Target, dict]]]:
    """Return ({vessel_id: name} confirmed missing, {vessel_id: (target, item)} present)."""
    by_vessel: dict[int, list[_Target]] = {}
    for t in targets:
        by_vessel.setdefault(t.vessel_id, []).append(t)

    present: dict[int, tuple[_Target, dict]] = {}
    candidates: dict[int, list[_Target]] = {}
    for vid, ts in by_vessel.items():
        states = [await state_fn(t) for t in ts]
        for t, (state, item) in zip(ts, states):
            if state == "present" and item:
                present[vid] = (t, item)
        if any(s == "present" for s, _ in states):
            continue
        if all(s == "missing" for s, _ in states):
            candidates[vid] = ts

    if not candidates:
        return {}, present

    await sleep(RECHECK_DELAY_SECONDS)
    confirmed: dict[int, str] = {}
    for vid, ts in candidates.items():
        again = [await state_fn(t) for t in ts]
        if all(s == "missing" for s, _ in again):
            confirmed[vid] = ts[0].vessel_name
        else:
            log.info("[folder_sync] vessel '%s' looked missing but recovered on recheck", ts[0].vessel_name)
    return confirmed, present


async def reconcile_vessel_folders(backend: Any, force: bool = True) -> dict:
    """Soft-delete vessels whose SharePoint ship folder was deleted in SharePoint."""
    log.info("[folder_sync] vessel folder check started")
    targets = await _resolve_deferred_drives(_collect_targets())
    if not targets:
        return {"checked": 0, "removed": [], "skipped": "no_targets"}

    confirmed, present = await _confirmed_missing_vessels(targets)
    checked = len({t.vessel_id for t in targets})

    # Keep Folder rows in step with folders that were renamed / moved.
    renamed = _refresh_present_rows(present)

    removed: list[str] = []
    if confirmed:
        limit = max(MAX_AUTO_REMOVALS, 0)
        if len(confirmed) > limit or (checked >= 4 and len(confirmed) * 4 > checked):
            log.warning(
                "[folder_sync] CONFLICT: %d of %d vessels look deleted in SharePoint (%s); "
                "refusing to remove them automatically (cap=%d). Check Graph access, then remove manually.",
                len(confirmed), checked, ", ".join(sorted(confirmed.values())), limit,
            )
            return {"checked": checked, "removed": [], "renamed": renamed, "refused": sorted(confirmed.values())}
        for vid, name in confirmed.items():
            try:
                res = await backend._execute_delete_vessel(
                    str(vid), requesting_email=None, requesting_name="SharePoint sync",
                    reason="Folder deleted directly in SharePoint", source="native_spo",
                )
                if res.get("deleted"):
                    removed.append(name)
                    log.info("[folder_sync] vessel '%s' removed: its SharePoint folder was deleted", name)
                else:
                    log.warning("[folder_sync] vessel '%s' not removed: %s", name, res.get("message"))
            except Exception:
                log.exception("[folder_sync] failed removing vessel '%s'", name)
    log.info("[folder_sync] vessel folder check done: checked=%d removed=%d renamed=%d", checked, len(removed), renamed)
    return {"checked": checked, "removed": removed, "renamed": renamed}


def _parent_logical_path(item: dict) -> str:
    raw = ((item.get("parentReference") or {}).get("path")) or ""
    marker = "root:"
    idx = raw.find(marker)
    return raw[idx + len(marker):].strip("/") if idx != -1 else ""


def _refresh_present_rows(present: dict[int, tuple[_Target, dict]]) -> int:
    """If a present ship folder's name differs from its Folder row, rename the row."""
    changed = 0
    with SessionLocal() as db:
        for _vid, (t, item) in present.items():
            if not t.item_id or item.get("id") != t.item_id:
                continue
            row = (
                _q(db, models.Folder)
                .filter(models.Folder.site_id == t.drive_id, models.Folder.drive_item_id == t.item_id)
                .one_or_none()
            )
            if row and _apply_row_update(db, row, item.get("name") or row.name, None):
                changed += 1
        db.commit()
    return changed


# ------------------------------------------------------------- row maintenance
def _apply_row_update(db, row: "models.Folder", new_name: str, new_parent_row_path: str | None) -> bool:
    """Rename and/or re-parent one Folder row (and its descendants' paths).

    ``new_parent_row_path`` is the parent row's own ``path`` when the folder
    moved (None = same parent). Paths follow the table's own convention: a
    rename replaces only the last segment, a move re-roots under the parent
    row. Skips (and logs) rather than violating the (site_id, path) unique key.
    """
    old_path = row.path or ""
    old_name = row.name or ""
    if new_parent_row_path is None:
        parent = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
    else:
        parent = new_parent_row_path
    new_path = f"{parent}/{new_name}" if parent else new_name
    if _squash(new_name) == _squash(old_name) and new_path == old_path:
        return False
    clash = (
        _q(db, models.Folder)
        .filter(models.Folder.site_id == row.site_id, models.Folder.path == new_path, models.Folder.id != row.id)
        .first()
    )
    if clash:
        log.warning("[folder_sync] CONFLICT: cannot move/rename folder row '%s' -> '%s' (path already in use)", old_path, new_path)
        return False
    descendants = (
        _q(db, models.Folder)
        .filter(models.Folder.site_id == row.site_id, models.Folder.path.like(f"{old_path}/%"))
        .all()
    ) if old_path else []
    row.name = new_name
    row.path = new_path
    for d in descendants:
        d.path = new_path + d.path[len(old_path):]
    log.info("[folder_sync] folder row updated: '%s' -> '%s' (%d descendant row(s))", old_path, new_path, len(descendants))
    return True


def process_delta_items(db, drive_id: str, items: list[dict]) -> dict:
    """Apply one batch of Graph delta items to the folders table.

    Returns {"deleted": n, "updated": n, "ship_deleted": [(vessel_id, name)]}.
    Ship-folder deletions are *reported*, not applied: the vessel check owns that.
    """
    deleted = updated = 0
    ship_deleted: list[tuple[int, str]] = []
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        rows = (
            _q(db, models.Folder)
            .filter(models.Folder.site_id == drive_id, models.Folder.drive_item_id == iid)
            .all()
        )
        if not rows:
            continue
        if it.get("deleted") is not None:
            for row in rows:
                if row.kind == "ship" and row.vessel_id:
                    ship_deleted.append((row.vessel_id, row.name))
                    continue
                if row.path:
                    _q(db, models.Folder).filter(
                        models.Folder.site_id == drive_id, models.Folder.path.like(f"{row.path}/%")
                    ).delete(synchronize_session=False)
                db.delete(row)
                deleted += 1
                log.info("[folder_sync] folder row removed (deleted in SharePoint): '%s'", row.path)
            continue
        if it.get("folder") is None:
            continue
        for row in rows:
            new_name = it.get("name") or row.name
            parent_id = (it.get("parentReference") or {}).get("id")
            new_parent_path: str | None = None
            if parent_id:
                parent_row = (
                    _q(db, models.Folder)
                    .filter(models.Folder.site_id == drive_id, models.Folder.drive_item_id == parent_id)
                    .first()
                )
                if parent_row is not None:
                    current_parent = row.path.rsplit("/", 1)[0] if "/" in (row.path or "") else ""
                    if (parent_row.path or "") != current_parent:
                        new_parent_path = parent_row.path or ""
            if _apply_row_update(db, row, new_name, new_parent_path):
                updated += 1
    return {"deleted": deleted, "updated": updated, "ship_deleted": ship_deleted}


# ------------------------------------------------------------------ delta loop
def _get_setting(db, key: str) -> str | None:
    row = db.query(models.AppSetting).filter_by(key=key).one_or_none()
    return row.value if row else None


def _put_setting(db, key: str, value: str | None) -> None:
    row = db.query(models.AppSetting).filter_by(key=key).one_or_none()
    if row is None:
        db.add(models.AppSetting(key=key, value=value, updated_by="folder_sync"))
    else:
        row.value = value
        row.updated_by = "folder_sync"


async def _drain_delta(url: str) -> tuple[list[dict], str | None]:
    items: list[dict] = []
    delta_link: str | None = None
    guard = 0
    while url and guard < 200:
        guard += 1
        data = await graph().get(url)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        delta_link = data.get("@odata.deltaLink") or delta_link
    return items, delta_link


async def sync_folder_table(backend: Any) -> dict:
    """Run one delta pass for every drive that has cached Folder rows."""
    with SessionLocal() as db:
        drive_ids = [d for (d,) in _q(db, models.Folder.site_id).distinct().all() if d]
    summary: dict[str, Any] = {"drives": 0, "deleted": 0, "updated": 0}
    ship_deleted_all: list[tuple[int, str]] = []
    for drive_id in drive_ids:
        key = f"{_DELTA_KEY_PREFIX}{drive_id}"
        with SessionLocal() as db:
            stored = _get_setting(db, key)
        try:
            if not stored:
                # First run for this drive: take a baseline token only (no items),
                # so nothing is judged from an incomplete picture.
                data = await graph().get(f"/drives/{drive_id}/root/delta?token=latest")
                link = data.get("@odata.deltaLink")
                if link:
                    with SessionLocal() as db:
                        _put_setting(db, key, link)
                        db.commit()
                    log.info("[folder_sync] delta baseline stored for drive %s", drive_id)
                continue
            items, new_link = await _drain_delta(stored)
        except GraphError as exc:
            if exc.status in (400, 404, 410):
                log.warning("[folder_sync] delta token for drive %s no longer valid (%s); re-baselining next run", drive_id, exc.status)
                with SessionLocal() as db:
                    _put_setting(db, key, None)
                    db.commit()
            else:
                log.warning("[folder_sync] delta failed for drive %s: %s", drive_id, exc)
            continue
        except Exception as exc:
            log.warning("[folder_sync] delta error for drive %s: %s", drive_id, exc)
            continue
        summary["drives"] += 1
        if items:
            with SessionLocal() as db:
                res = process_delta_items(db, drive_id, items)
                if new_link:
                    _put_setting(db, key, new_link)
                db.commit()
            summary["deleted"] += res["deleted"]
            summary["updated"] += res["updated"]
            ship_deleted_all.extend(res["ship_deleted"])
        elif new_link:
            with SessionLocal() as db:
                _put_setting(db, key, new_link)
                db.commit()
    if ship_deleted_all:
        log.info("[folder_sync] ship folder delete(s) reported by delta: %s -> running vessel check",
                 ", ".join(n for _, n in ship_deleted_all))
        summary["vessel_check"] = await reconcile_vessel_folders(backend, force=True)
    return summary
