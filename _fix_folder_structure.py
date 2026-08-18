"""
Fix: purge ALL stale/old-structure folder rows from the DB and re-verify
the correct hierarchy. Only these paths are valid:

  Vessels                                          (root)
  Vessels/Specific Vessels                         (root)
  Vessels/Common for all ships                     (root)
  Vessels/Common for all ships/<Main>              (main)
  Vessels/Specific Vessels/<Ship>                  (ship)
  Vessels/Specific Vessels/<Ship>/<Main>           (main)
  Kaizen - Knowledge Bank                          (main)

Everything else at root/main level is old-structure garbage and is deleted.

Run from the backend project root:
    python _fix_folder_structure.py
"""
import asyncio

from app.db.base import SessionLocal
from app.db import models
from app.services.real_backend import RealBackend
from app import template

KAIZEN = template.FLAT_MAIN_FOLDERS[0]          # "Kaizen - Knowledge Bank"
VESSELS = template.VESSELS_ROOT                  # "Vessels"
SPECIFIC = template.SPECIFIC_VESSELS_ROOT        # "Specific Vessels"
COMMON = template.COMMON_SHIPS_ROOT              # "Common for all ships"


def _is_valid(path: str, kind: str) -> bool:
    """Return True only for paths that belong to the new structure."""
    if path == KAIZEN:                                          # top-level Kaizen ✓
        return True
    if path == VESSELS:                                         # Vessels root ✓
        return True
    if path == f"{VESSELS}/{SPECIFIC}":                         # Specific Vessels ✓
        return True
    if path == f"{VESSELS}/{COMMON}":                           # Common for all ships ✓
        return True
    if path.startswith(f"{VESSELS}/{COMMON}/"):                 # Vessels/Common/... ✓
        return True
    if path.startswith(f"{VESSELS}/{SPECIFIC}/"):               # Vessels/Specific/... ✓
        return True
    return False


async def main():
    with SessionLocal() as db:
        all_structural = db.query(models.Folder).filter(
            models.Folder.kind.in_(["root", "main"])
        ).all()

        stale = [r for r in all_structural if not _is_valid(r.path, r.kind)]

        if stale:
            print(f"Removing {len(stale)} stale DB rows:")
            for row in stale:
                print(f"  [{row.kind:6}] {row.path!r}")
                db.delete(row)
            db.commit()
            print()
        else:
            print("No stale rows found.\n")

    be = RealBackend()
    print("Running ensure_base_structure() ...")
    await be.ensure_base_structure()
    print("Done.\n")

    with SessionLocal() as db:
        rows = db.query(models.Folder).filter(
            models.Folder.kind.in_(["root", "main"])
        ).order_by(models.Folder.path).all()
        print("Root/main folders in DB:")
        for r in rows:
            print(f"  [{r.kind:6}] {r.path}")


if __name__ == "__main__":
    asyncio.run(main())
