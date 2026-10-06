"""APScheduler jobs for Vessel DMS.

Jobs:
  session_sweep         – every 15 min, expires stale sessions and runs
                          periodic Graph spot-checks for active accounts.
  reconcile_pool        – every 5 min, retries stuck vessel-folder-pool
                          replenishments and tops up the pool deficit.

All jobs are only active when Graph + DB are configured (real mode).
"""
from datetime import date, datetime, timedelta, timezone
import asyncio
import os
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from .config import settings
from .db import models
from .db.base import SessionLocal

log = logging.getLogger(__name__)

# --------------------------------------------------------------- vessel pool
# Number of empty "Pool-xxxx" vessel folders kept ready in SharePoint. 0 (the
# default) turns the pool off: a new vessel is then created directly, with no
# pre-built folder to claim. Set POOL_TARGET_SIZE in .env to bring it back.
POOL_TARGET_SIZE = int(os.getenv("POOL_TARGET_SIZE", "0") or 0)
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
                await asyncio.sleep(10)  # 10s gap to let Graph API rate-limit quota reset

    if stuck_jobs or stuck_slots or built:
        log.info(
            "[reconcile_pool] retried_stuck=%d built=%d",
            len(stuck_jobs) + len(stuck_slots), built,
        )
    return {"retried_stuck": len(stuck_jobs) + len(stuck_slots), "built": built}

async def refresh_dashboard_stats_cache() -> dict:
    """Re-scan every SharePoint site's drive and re-warm the Home dashboard's
    stats cache ("all sites" view) in the background.

    Without this, the counters/site table on Home are computed live on
    whichever request happens to land after the cache (CACHE_TTL_DASHBOARD_STATS,
    120s) expires — the request that pays for the full multi-site Graph scan
    is whichever user opens the page at the wrong moment, and with 17k+ files
    across a large library that scan is slow. Running the scan here instead,
    on a schedule slightly faster than the cache TTL, means real page loads
    almost always hit a warm cache and just get "last refreshed" answered
    instantly.
    """
    from .services import get_backend
    from .services import real_backend as rb
    from .services.real_backend import RealBackend

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return {"skipped": "not_real_backend"}

    # Back off for a few minutes after a scan that hit Graph throttling,
    # instead of re-scanning every 100s into a still-exhausted quota. Time-
    # based (not "skip while any cached row has an error"), so a stale error
    # can never stop the next real scan from running and clearing it.
    since = time.time() - rb._DASHBOARD_LAST_THROTTLED_AT
    if since < 300:
        log.info(
            "[refresh_dashboard_stats_cache] skipped — last scan was throttled %.0fs ago",
            since,
        )
        return {"skipped": "recently_throttled"}

    try:
        stats = await backend.get_dashboard_stats(force_refresh=True, site_key=None)
        log.info(
            "[refresh_dashboard_stats_cache] files=%s folders=%s sites=%s truncated=%s",
            stats.get("total_files"), stats.get("total_folders"),
            stats.get("total_sites"), stats.get("truncated"),
        )
        return {"total_files": stats.get("total_files"), "total_sites": stats.get("total_sites")}
    except asyncio.CancelledError:
        # asyncio.CancelledError is a BaseException (not Exception) since
        # Python 3.8, so the handler below never caught it — it fell through
        # to APScheduler's own executor, which logs an uncaught job error as
        # a multi-frame ERROR traceback. That looked like a crash, but it's
        # just this scan being torn down mid-flight because the app itself
        # is shutting down or restarting (e.g. uvicorn --reload picking up a
        # code change while this job happened to be running) — normal during
        # development, not a real failure. Log it briefly and return: the
        # process is stopping anyway, and re-raising made APScheduler print
        # the same multi-frame ERROR traceback this handler exists to avoid.
        log.info("[refresh_dashboard_stats_cache] cancelled (server shutting down/restarting)")
        return {"cancelled": True}
    except Exception as exc:
        # Best-effort — a failed background refresh just leaves the previous
        # cached figures in place (or falls through to a live scan on the
        # next request) rather than breaking anything.
        log.warning("[refresh_dashboard_stats_cache] Unhandled error: %s", exc)
        return {"error": str(exc)}


async def reconcile_native_deletions() -> dict:
    """Backfill Deleted By / reason for folder & file deletions this backend
    didn't capture directly (native SharePoint UI deletions, or a client-side
    Graph delete whose log-deletion call didn't land) — see
    RealBackend.reconcile_native_deletions. This is also what feeds the live
    deletion popup (top-header alert bell) for those items, since a new
    deletion_log row is what /api/alerts/all surfaces."""
    from .services import get_backend
    from .services.real_backend import RealBackend

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return {"skipped": "not_real_backend"}
    try:
        return await backend.reconcile_native_deletions()
    except Exception as exc:
        log.warning("[reconcile_native_deletions] Unhandled error: %s", exc)
        return {"error": str(exc)}


async def reconcile_vessel_folders() -> dict:
    """Remove vessels from the DB whose SharePoint ship folder was deleted
    directly in SharePoint Online (outside the app), so the Vessels list
    stays in sync with SharePoint even if nobody opens the page to trigger
    the on-demand check in RealBackend.list_vessels()."""
    from .services import get_backend
    from .services.real_backend import RealBackend

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return {"skipped": "not_real_backend"}
    try:
        return await backend.reconcile_vessel_folders(force=True)
    except Exception as exc:
        log.warning("[reconcile_vessel_folders] Unhandled error: %s", exc)
        return {"error": str(exc)}


async def sync_folder_table() -> dict:
    """Apply SharePoint-side renames/moves/deletes of folders to the folders
    table (incremental Graph delta, every couple of minutes)."""
    from .services import get_backend
    from .services.real_backend import RealBackend

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return {"skipped": "not_real_backend"}
    try:
        return await backend.sync_folder_table()
    except asyncio.CancelledError:
        log.info("[sync_folder_table] cancelled (server shutting down/restarting)")
        raise
    except Exception as exc:
        log.warning("[sync_folder_table] Unhandled error: %s", exc)
        return {"error": str(exc)}


async def ensure_template_month_folders() -> dict:
    """See services/folder_structure.ensure_current_month_folders."""
    from .services import get_backend
    from .services.real_backend import RealBackend
    from .services import folder_structure

    backend = get_backend()
    if not isinstance(backend, RealBackend):
        return {"skipped": "not_real_backend"}
    try:
        result = await folder_structure.ensure_current_month_folders(backend)
        log.info("[ensure_template_month_folders] %s", result)
        return result
    except Exception as exc:
        log.warning("[ensure_template_month_folders] Unhandled error: %s", exc)
        return {"error": str(exc)}


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

    # NOTE: the former precreate_next_month job (daily pre-creation of next
    # month's folders + category leaves) was removed — the app no longer
    # auto-creates folder/leaf template structures in SharePoint Online.
    # A month folder is created only when a file is uploaded into it.

    # Folder Structure Mode (Modes 2 & 4 only): create the current month's
    # folder + template categories under month-driven folders. Daily and on
    # startup; idempotent. Vessels in Modes 1/3 are never touched.
    sched.add_job(
        ensure_template_month_folders,
        "cron",
        hour=0,
        minute=20,
        id="ensure_template_month_folders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        ensure_template_month_folders,
        "date",
        run_date=datetime.now(),
        id="ensure_template_month_folders_startup",
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

    # Vessel folder pool: retry stuck replenishments + top up deficit (every 5 minutes, also runs immediately on startup)
    sched.add_job(
        reconcile_pool,
        "interval",
        minutes=5,
        id="reconcile_pool",
        next_run_time=datetime.now(),
        replace_existing=True,
    )

    # Vessels: detect folders deleted directly in SharePoint Online and
    # remove the matching vessel from the DB (every 10 minutes).
    sched.add_job(
        reconcile_vessel_folders,
        "interval",
        minutes=10,
        id="reconcile_vessel_folders",
        next_run_time=datetime.now(),
        replace_existing=True,
    )

    # Folders table: apply folders renamed / moved / deleted directly in
    # SharePoint (incremental Graph delta per drive, every 2 minutes).
    sched.add_job(
        sync_folder_table,
        "interval",
        minutes=2,
        id="sync_folder_table",
        next_run_time=datetime.now() + timedelta(seconds=30),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Home dashboard stats: keep the "all sites" cache warm (every 100s,
    # just under CACHE_TTL_DASHBOARD_STATS's 120s) so page loads read from
    # cache instead of triggering a live multi-site scan. Also runs once on
    # startup so the first Home load after a deploy/restart is fast too.
    sched.add_job(
        refresh_dashboard_stats_cache,
        "interval",
        seconds=100,
        id="refresh_dashboard_stats_cache",
        next_run_time=datetime.now(),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Recycle Bin: backfill Deleted By / reason for deletions this backend
    # didn't capture directly — native SharePoint UI deletes, or a missed
    # client-side log-deletion call (every 45 seconds; near-real-time for
    # the live deletion popup without needing Graph webhook subscriptions).
    sched.add_job(
        reconcile_native_deletions,
        "interval",
        seconds=45,
        id="reconcile_native_deletions",
        next_run_time=datetime.now(),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    sched.start()
    log.info(
        "Scheduler started: session_sweep (15 min) "
        "+ reconcile_pool (5 min) + reconcile_vessel_folders (10 min) "
        "+ sync_folder_table (2 min) "
        "+ refresh_dashboard_stats_cache (100 sec) "
        "+ reconcile_native_deletions (45 sec) "
        "+ ensure_template_month_folders (daily 00:20)"
    )
    return sched
