"""APScheduler jobs for Vessel DMS.

Jobs:
  precreate_next_month  – daily @ 01:00, creates next month's SP folders.
  session_sweep         – every 15 min, expires stale sessions and runs
                          periodic Graph spot-checks for active accounts.
  reconcile_pool        – every 5 min, retries stuck vessel-folder-pool
                          replenishments and tops up the pool deficit.

All jobs are only active when Graph + DB are configured (real mode).
"""
from datetime import date, datetime, timedelta
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from .config import settings
from .db import models
from .db.base import SessionLocal
from .services.classify import classify
from .ocr.dates import month_label

log = logging.getLogger(__name__)

PRECREATE_DAY = 20

# --------------------------------------------------------------- vessel pool
POOL_TARGET_SIZE = 5
# How long before reconcile_pool declares a 'building' PoolSlot / 'pending'
# ReplenishJob as stuck and marks it 'failed' so it can be retried.
# Must be comfortably larger than SLOT_BUILD_TIMEOUT_SECONDS (in minutes).
STUCK_BUILD_TIMEOUT_MINUTES = 25
# Hard cap per _build_pool_slot() call.  Under Graph 429 throttling a single
# folder POST can retry up to 6× with up to 60s back-off, and a full vessel
# template spans many folders across several main departments — measured wall
# time under throttling is 5-10 minutes.  180s was far too tight and caused
# all the accumulated 'failed' slots; 600s (10 min) gives safe headroom.
SLOT_BUILD_TIMEOUT_SECONDS = 600
# Arbitrary fixed key for the advisory lock — must be the same constant
# everywhere this job runs (all pods) so they actually contend on the same
# lock instead of each getting their own.
_POOL_RECONCILE_LOCK_KEY = 987654321


def _next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


async def precreate_next_month(force: bool = False) -> int:
    """Ensure next month's folders exist. Returns how many were processed."""
    from .services import get_backend
    from .services.real_backend import RealBackend
    from .graph import drive as gd

    if not (settings.graph_configured and settings.db_configured):
        return 0
    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return 0
    today = date.today()
    if not force and today.day < PRECREATE_DAY:
        return 0

    if not hasattr(backend, "_drive") or not hasattr(backend, "_ensure_month"):
        log.warning(
            "[precreate_next_month] Backend %s is missing _drive or _ensure_month; skipping.",
            backend.__class__.__name__,
        )
        return 0

    ny, nm = _next_month(today.year, today.month)
    label = month_label(ny, nm) 
    drive_id = await backend._drive()
    with SessionLocal() as db:
        rows = [
            (r.drive_item_id, r.path, r.vessel_id)
            for r in db.query(models.Folder).filter_by(month_driven=True).all()
        ]
    if not rows:
        return 0

    # Pass 1: batch-create the month folder itself for every vessel/main in one go.
    month_items = await gd.batch_create_folders(
        drive_id, [(item_id, label) for item_id, _, _ in rows]
    )

    with SessionLocal() as db:
        month_paths = {}  # (item_id) -> (mpath, month_item_id)
        for item_id, path, vessel_id in rows:
            item = month_items.get((item_id, label))
            if not item:
                continue
            mpath = f"{path}/{label}"
            backend._upsert(db, mpath, label, "month", item["id"], False, vessel_id)
            month_paths[item_id] = (mpath, item["id"], vessel_id, path)
        db.commit()

    # Pass 2: batch-create every category subfolder across every month folder in one go.
    cat_targets = []  # (month_item_id, cat_name, mpath, vessel_id)
    for item_id, path, vessel_id in rows:
        entry = month_paths.get(item_id)
        if not entry:
            continue
        mpath, month_item_id, vid, orig_path = entry
        categories = classify(orig_path.split("/")).get("categories", [])
        for cat in categories:
            cat_targets.append((month_item_id, cat, mpath, vid))

    if cat_targets:
        cat_items = await gd.batch_create_folders(
            drive_id, [(mid, cat) for mid, cat, _, _ in cat_targets]
        )
        with SessionLocal() as db:
            for month_item_id, cat, mpath, vid in cat_targets:
                item = cat_items.get((month_item_id, cat))
                if item:
                    backend._upsert(db, f"{mpath}/{cat}", cat, "leaf", item["id"], False, vid)
            db.commit()

    return len(rows)
async def reconcile_pool() -> dict:
    """Retry stuck vessel-folder-pool replenishments and top up the pool to
    POOL_TARGET_SIZE if it's short.

    Two things can leave the pool short of its target:
      1. A ReplenishJob / PoolSlot stuck in 'pending'/'building' because the
         process that started it crashed or was redeployed mid-build (the
         fire-and-forget asyncio task died with it). We mark these
         'failed' (never delete — deleting would strand their partially
         created Folder rows via the ondelete=SET NULL relationship) and
         count them out of the pool.
      2. Normal usage simply outpacing replenishment.

    Guarded by a Postgres advisory lock (pg_try_advisory_lock) so multiple
    app instances/pods running this job on the same schedule don't each try
    to fill the same deficit — only one wins the lock per tick; the rest
    skip this run and try again next tick.
    """
    from .services import get_backend
    from .services.real_backend import RealBackend

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return {"skipped": "not_real_backend"}

    with SessionLocal() as db:
        got_lock = db.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _POOL_RECONCILE_LOCK_KEY}
        ).scalar()
        if not got_lock:
            return {"skipped": "lock_held_by_another_instance"}
        try:
            cutoff = datetime.utcnow() - timedelta(minutes=STUCK_BUILD_TIMEOUT_MINUTES)

            stuck_jobs = (
                db.query(models.ReplenishJob)
                .filter(models.ReplenishJob.status == "pending",
                        models.ReplenishJob.created_at < cutoff)
                .all()
            )
            for job in stuck_jobs:
                job.status = "failed"

            stuck_slots = (
                db.query(models.PoolSlot)
                .filter(models.PoolSlot.status == "building",
                        models.PoolSlot.created_at < cutoff)
                .all()
            )
            for slot in stuck_slots:
                slot.status = "failed"

            db.commit()
            db.commit()

            available = db.query(models.PoolSlot).filter_by(status="available").count()
            building = db.query(models.PoolSlot).filter_by(status="building").count()
            claimed = db.query(models.PoolSlot).filter_by(status="claimed").count()
            failed = db.query(models.PoolSlot).filter_by(status="failed").count()
            total = db.query(models.PoolSlot).count()
            deficit = max(0, POOL_TARGET_SIZE - available - building)

            log.info(
                "[reconcile_pool] SNAPSHOT target=%d available=%d building=%d "
                "claimed=%d failed=%d total=%d deficit=%d",
                POOL_TARGET_SIZE, available, building, claimed, failed, total, deficit,
            )
        finally:
            db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _POOL_RECONCILE_LOCK_KEY})
            db.commit()
    # Hard cap per slot: a container under sustained RU throttling can make
    # a single _build_pool_slot() run take a very long time (each Graph
    # call inside it retries up to 6x with backoff). Left unbounded, one
    # slow build can eat every future reconcile_pool tick's max_instances=1
    # slot indefinitely — which is exactly what happened. A timed-out build
    # is treated the same as any other failure: its PoolSlot/ReplenishJob
    # rows are left as 'building'/'pending' and get picked up as "stuck" by
    # the next tick's cleanup pass (STUCK_BUILD_TIMEOUT_MINUTES) instead of
    # retried immediately — we just got throttled, retrying instantly
    # would too.
    built = 0
    for _ in range(deficit):
        try:
            slot_id = await asyncio.wait_for(
                backend._build_pool_slot(), timeout=SLOT_BUILD_TIMEOUT_SECONDS
            )
            built += 1
            log.info(
                "[reconcile_pool] Built replacement slot_id=%s (%d/%d built this tick)",
                slot_id, built, deficit,
            )
        except asyncio.TimeoutError:
            log.warning(
                "[reconcile_pool] _build_pool_slot exceeded %ss — abandoning "
                "for this tick; will be retried as a stuck job later",
                SLOT_BUILD_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.warning("[reconcile_pool] Failed to build a pool slot: %s", exc)
        else:
            if deficit > 1:
                await asyncio.sleep(5)  # gap between successful builds too

    if stuck_jobs or stuck_slots or built:
        log.info(
            "[reconcile_pool] retried_stuck=%d built=%d",
            len(stuck_jobs) + len(stuck_slots), built,
        )
    return {"retried_stuck": len(stuck_jobs) + len(stuck_slots), "built": built}

def _sweep_sessions() -> None:
    """Synchronous job: expire stale sessions + Graph account revalidation.

    Designed to be resilient — any exception is caught and logged so the
    scheduler doesn't drop the job entirely on a transient DB error.

    Multi-instance safety: expire_old_sessions / revalidate_active_accounts
    are both idempotent (status transitions are only written once per row),
    so running on multiple pods produces at-most-one audit entry per event
    in practice (the second write hits a row already in Expired/Revoked state
    and is a no-op).
    """
    if not settings.db_configured:
        return

    try:
        from .services.session_service import expire_old_sessions, revalidate_active_accounts

        with SessionLocal() as db:
            expired = expire_old_sessions(db)
            if expired:
                log.info("[session_sweep] Expired %d session(s)", expired)

        # Acquire an app-only token for Graph spot-checks (best-effort)
        app_token: str | None = None
        if settings.graph_configured:
            try:
                import httpx
                from .graph.http import verify as graph_tls_verify

                token_url = (
                    f"{settings.graph_authority}/{settings.azure_tenant_id}"
                    "/oauth2/v2.0/token"
                )
                resp = httpx.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": settings.graph_client_id,
                        "client_secret": settings.graph_client_secret,
                        "scope": settings.graph_scope,
                    },
                    verify=graph_tls_verify(),
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    app_token = resp.json().get("access_token")
            except Exception as exc:
                log.debug("[session_sweep] Could not acquire app-only token: %s", exc)

        with SessionLocal() as db:
            revoked = revalidate_active_accounts(db, app_token=app_token)
            if revoked:
                log.info("[session_sweep] Revoked %d session(s) (account disabled)", revoked)

    except Exception as exc:
        log.warning("[session_sweep] Unhandled error: %s", exc)

async def fill_pool_on_startup() -> None:
    """Build pool slots one-by-one at startup until POOL_TARGET_SIZE is reached.
    ...
    """
    log.info("[fill_pool_on_startup] Task started")
    from .services import get_backend
    from .services.real_backend import RealBackend

    if not (settings.graph_configured and settings.db_configured):
        log.info("[fill_pool_on_startup] Graph or DB not configured — skipping pool fill")
        return

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        log.info("[fill_pool_on_startup] Backend is %s (not RealBackend) — skipping pool fill", type(backend).__name__)
        return
    log.info("[fill_pool_on_startup] Backend confirmed RealBackend — proceeding")

    with SessionLocal() as db:
        got_lock = db.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _POOL_RECONCILE_LOCK_KEY}
        ).scalar()
        if not got_lock:
            log.info("[fill_pool_on_startup] Another instance holds the lock — skipping.")
            return
        try:
            available = db.query(models.PoolSlot).filter_by(status="available").count()
            building = db.query(models.PoolSlot).filter_by(status="building").count()
            claimed = db.query(models.PoolSlot).filter_by(status="claimed").count()
            failed = db.query(models.PoolSlot).filter_by(status="failed").count()
            total = db.query(models.PoolSlot).count()
            deficit = max(0, POOL_TARGET_SIZE - available - building)
            log.info(
                "[fill_pool_on_startup] SNAPSHOT target=%d available=%d building=%d "
                "claimed=%d failed=%d total=%d deficit=%d",
                POOL_TARGET_SIZE, available, building, claimed, failed, total, deficit,
            )
        finally:
            db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _POOL_RECONCILE_LOCK_KEY})
            db.commit()

    if deficit == 0:
        log.info("[fill_pool_on_startup] Pool already at target (%d slots). Nothing to build.", POOL_TARGET_SIZE)
        return

    log.info(
        "[fill_pool_on_startup] Pool has %d/%d slots. Building %d more one-by-one...",
        POOL_TARGET_SIZE - deficit, POOL_TARGET_SIZE, deficit,
    )
    for i in range(deficit):
        try:
            slot_id = await asyncio.wait_for(
                backend._build_pool_slot(), timeout=SLOT_BUILD_TIMEOUT_SECONDS
            )
            log.info(
                "[fill_pool_on_startup] Built slot %d/%d (slot_id=%d).",
                i + 1, deficit, slot_id,
            )
        except asyncio.TimeoutError:
            log.warning(
                "[fill_pool_on_startup] Slot %d/%d exceeded %ss — abandoning; "
                "will be retried as a stuck job later",
                i + 1, deficit, SLOT_BUILD_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.warning("[fill_pool_on_startup] Failed to build slot %d/%d: %s", i + 1, deficit, exc)
        # Always wait between slots — even after a failure — so a partial
        # throttle doesn't cascade into all remaining builds failing too.
        if i < deficit - 1:
            await asyncio.sleep(30)

    log.info("[fill_pool_on_startup] Done. Pool top-up complete.")


def start_scheduler() -> AsyncIOScheduler | None:
    if not (settings.graph_configured and settings.db_configured):
        return None
    sched = AsyncIOScheduler()

    # Pre-create next month's SharePoint folders (daily at 01:00)
    sched.add_job(
        precreate_next_month,
        "cron",
        hour=1,
        minute=0,
        id="precreate_next_month",
        replace_existing=True,
    )

    # Session expiry sweep + Graph account revalidation (every 15 minutes)
    sched.add_job(
        _sweep_sessions,
        "interval",
        minutes=15,
        id="session_sweep",
        replace_existing=True,
    )

    # Vessel folder pool: retry stuck replenishments + top up deficit (every 5 minutes)
    sched.add_job(
        reconcile_pool,
        "interval",
        minutes=5,
        id="reconcile_pool",
        replace_existing=True,
    )

    sched.start()
    log.info(
        "Scheduler started: precreate_next_month (daily) + session_sweep (15 min) "
        "+ reconcile_pool (5 min)"
    )
    return sched
