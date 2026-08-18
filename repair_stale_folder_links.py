"""
Repair stale SharePoint folder links after a container/drive change.

Problem:
    The `folders` table caches drive_item_id per logical path (main/ship/leaf).
    ensure_base_structure() and reprovision_vessel() trust that cached id
    without checking it still exists in the CURRENTLY configured DRIVE_ID.
    If the container changed (as it has here — see .env history), every
    cached id from the old container is now dead, but the app never notices
    because it never re-validates.

What this does:
    1. Resolves the live drive_id from the current .env (settings.drive_id
       or a fresh container lookup — same logic real_backend._drive() uses).
    2. Walks every row in `folders`.
    3. Calls Graph get_item(drive_id, row.drive_item_id) for each one.
    4. Any row that 404s / errors against the CURRENT drive is deleted.
       (Safe: ensure_folder / ensure_base_structure / reprovision_vessel are
       all create-or-fetch, so a missing row just gets recreated fresh
       against the correct drive on the next call — nothing in SharePoint
       itself is touched or deleted.)
    5. Prints a summary: how many were valid vs. stale/removed, per vessel.

Run from the backend project root (same venv the app uses):
    python repair_stale_folder_links.py            # dry run (default)
    python repair_stale_folder_links.py --apply     # actually delete stale rows

After running with --apply, hit:
    POST /api/vessels/repair-links      (repair_vessel_links, if you also
                                          have orphaned ship folders)
or just reload the Vessels/Documents pages — mains()/reprovision_vessel()
will silently recreate anything that was cleared, against the live drive.
"""
import asyncio
import sys

from app.config import settings
from app.db.base import SessionLocal
from app.db import models
from app.graph import drive as gd


async def resolve_live_drive_id() -> str:
    if settings.drive_id:
        return settings.drive_id
    return await gd.get_container_drive_id(settings.container_id)


async def main(apply: bool) -> None:
    drive_id = await resolve_live_drive_id()
    print(f"Live drive_id (from current .env): {drive_id}")
    print(f"Mode: {'APPLY (deleting stale rows)' if apply else 'DRY RUN (no changes)'}\n")

    with SessionLocal() as db:
        folders = db.query(models.Folder).order_by(models.Folder.path).all()
        vessels_by_id = {v.id: v.name for v in db.query(models.Vessel).all()}

    valid = 0
    stale = []

    # Check items concurrently in small batches to avoid hammering Graph.
    sem = asyncio.Semaphore(3)

    async def check(folder):
        nonlocal valid
        async with sem:
            try:
                await gd.get_item(drive_id, folder.drive_item_id)
                valid += 1
            except Exception as exc:
                err_str = str(exc)
                # 429 = throttled — item may still exist; skip, don't mark stale
                if "429" in err_str or "activityLimitReached" in err_str or "throttl" in err_str.lower():
                    valid += 1  # assume valid, retry later
                    return
                stale.append((folder, err_str))

    await asyncio.gather(*(check(f) for f in folders))

    print(f"Checked {len(folders)} folder rows: {valid} valid, {len(stale)} stale.\n")

    if stale:
        print("Stale rows (path -> vessel, error):")
        for folder, err in stale:
            vname = vessels_by_id.get(folder.vessel_id, "-") if folder.vessel_id else "-"
            print(f"  [{folder.kind:12}] {folder.path!r:55} vessel={vname:20} err={err[:80]}")

    if apply and stale:
        with SessionLocal() as db:
            ids = [f.id for f, _ in stale]
            db.query(models.Folder).filter(models.Folder.id.in_(ids)).delete(
                synchronize_session=False
            )
            db.commit()
        print(f"\nDeleted {len(stale)} stale rows. They will be recreated fresh "
              f"against the live drive next time mains()/reprovision_vessel() runs.")
    elif stale:
        print("\nDry run only — re-run with --apply to delete these rows.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))