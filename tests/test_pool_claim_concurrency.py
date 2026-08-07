"""Proves the atomic-claim guarantee for the vessel folder pool.

Requires a real configured Postgres DB (settings.db_configured) — SKIP
LOCKED is a Postgres-specific semantic this test is specifically checking,
so it's skipped rather than faked against SQLite.

Run two claims concurrently from separate threads (separate DB sessions,
mirroring two separate API request handlers) against a pool of exactly one
available slot, and assert:
  - exactly one thread gets a non-None result
  - the other gets None (pool empty -> caller falls back to slow path)
  - no exception / no double-claim of the same slot id
"""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

try:
    from app.db.base import SessionLocal
    from app.db import models
    from app.services.real_backend import RealBackend
except Exception:  # pragma: no cover - import-time DB/driver issues
    SessionLocal = None


@unittest.skipUnless(
    getattr(settings, "db_configured", False) and SessionLocal is not None,
    "Requires a real configured Postgres DB (SKIP LOCKED is Postgres-specific).",
)
class TestPoolClaimConcurrency(unittest.TestCase):
    def setUp(self):
        self.backend = RealBackend()
        with SessionLocal() as db:
            self.slot = models.PoolSlot(slug="Pool-testonly-concurrency", status="available")
            db.add(self.slot)
            db.commit()
            db.refresh(self.slot)
            self.slot_id = self.slot.id

    def tearDown(self):
        with SessionLocal() as db:
            db.query(models.PoolSlot).filter_by(id=self.slot_id).delete()
            db.commit()

    def test_only_one_of_two_concurrent_claims_succeeds(self):
        def claim():
            return self.backend._claim_pool_slot()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(claim)
            f2 = pool.submit(claim)
            r1, r2 = f1.result(timeout=10), f2.result(timeout=10)

        results = [r1, r2]
        non_none = [r for r in results if r is not None]
        self.assertEqual(
            len(non_none), 1,
            f"Expected exactly one winner, got: {results}",
        )
        self.assertEqual(non_none[0]["slot_id"], self.slot_id)

        with SessionLocal() as db:
            slot = db.query(models.PoolSlot).filter_by(id=self.slot_id).one()
            self.assertEqual(slot.status, "claimed")


if __name__ == "__main__":
    unittest.main()
