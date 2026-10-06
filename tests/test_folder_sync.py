import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.db import models
from app.db.base import Base
from app.services import folder_sync
from app.services.folder_sync import _Target


async def _no_sleep(_seconds):
    return None


def _target(vid=1, name="MV One", item="item-1"):
    return _Target(vid, name, "drive-1", item, name)


class TestConfirmedMissing(unittest.IsolatedAsyncioTestCase):
    async def test_present_vessel_is_not_flagged(self):
        state = AsyncMock(return_value=("present", {"id": "item-1", "name": "MV One"}))
        confirmed, present = await folder_sync._confirmed_missing_vessels([_target()], state, _no_sleep)
        self.assertEqual(confirmed, {})
        self.assertIn(1, present)

    async def test_missing_on_both_checks_is_confirmed(self):
        state = AsyncMock(return_value=("missing", None))
        confirmed, _ = await folder_sync._confirmed_missing_vessels([_target()], state, _no_sleep)
        self.assertEqual(confirmed, {1: "MV One"})
        self.assertEqual(state.await_count, 2)  # first look + confirming recheck

    async def test_recovers_on_recheck_is_not_removed(self):
        state = AsyncMock(side_effect=[("missing", None), ("present", {"id": "x"})])
        confirmed, _ = await folder_sync._confirmed_missing_vessels([_target()], state, _no_sleep)
        self.assertEqual(confirmed, {})

    async def test_unknown_never_confirms(self):
        state = AsyncMock(return_value=("unknown", None))
        confirmed, _ = await folder_sync._confirmed_missing_vessels([_target()], state, _no_sleep)
        self.assertEqual(confirmed, {})

    async def test_multi_site_vessel_kept_if_any_folder_present(self):
        a = _Target(1, "MV One", "drive-A", "a", "MV One")
        b = _Target(1, "MV One", "drive-B", "b", "MV One")
        state = AsyncMock(side_effect=[("missing", None), ("present", {"id": "b"})])
        confirmed, _ = await folder_sync._confirmed_missing_vessels([a, b], state, _no_sleep)
        self.assertEqual(confirmed, {})


class TestReconcileVesselFolders(unittest.IsolatedAsyncioTestCase):
    def _backend(self):
        backend = MagicMock()
        backend._execute_delete_vessel = AsyncMock(return_value={"deleted": True})
        return backend

    async def _run(self, targets, confirmed):
        backend = self._backend()
        with (
            patch.object(folder_sync, "_collect_targets", return_value=targets),
            patch.object(folder_sync, "_resolve_deferred_drives", new=AsyncMock(return_value=targets)),
            patch.object(folder_sync, "_confirmed_missing_vessels", new=AsyncMock(return_value=(confirmed, {}))),
            patch.object(folder_sync, "_refresh_present_rows", return_value=0),
        ):
            result = await folder_sync.reconcile_vessel_folders(backend)
        return backend, result

    async def test_deleted_vessel_is_soft_deleted_as_native_spo(self):
        targets = [_target(i, f"MV {i}", f"i{i}") for i in range(1, 11)]
        backend, result = await self._run(targets, {3: "MV 3"})
        backend._execute_delete_vessel.assert_awaited_once()
        kwargs = backend._execute_delete_vessel.await_args.kwargs
        self.assertEqual(kwargs["source"], "native_spo")
        self.assertEqual(backend._execute_delete_vessel.await_args.args[0], "3")
        self.assertEqual(result["removed"], ["MV 3"])

    async def test_mass_deletion_is_refused(self):
        targets = [_target(i, f"MV {i}", f"i{i}") for i in range(1, 11)]
        confirmed = {i: f"MV {i}" for i in range(1, 6)}  # 5 of 10 "missing"
        backend, result = await self._run(targets, confirmed)
        backend._execute_delete_vessel.assert_not_awaited()
        self.assertEqual(len(result["refused"]), 5)

    async def test_no_targets_does_nothing(self):
        backend = self._backend()
        with (
            patch.object(folder_sync, "_collect_targets", return_value=[]),
            patch.object(folder_sync, "_resolve_deferred_drives", new=AsyncMock(return_value=[])),
        ):
            result = await folder_sync.reconcile_vessel_folders(backend)
        self.assertEqual(result["checked"], 0)
        backend._execute_delete_vessel.assert_not_awaited()


class TestTargetState(unittest.IsolatedAsyncioTestCase):
    async def test_moved_folder_found_by_id_is_present(self):
        g = MagicMock()
        g.get = AsyncMock(return_value={"id": "item-1", "name": "MV One", "folder": {}})
        with patch.object(folder_sync, "graph", return_value=g):
            state, item = await folder_sync._target_state(_target())
        self.assertEqual(state, "present")

    async def test_404_everywhere_but_name_found_elsewhere_is_present(self):
        from app.graph.client import GraphError
        g = MagicMock()
        g.get = AsyncMock(side_effect=[GraphError(404, "nf"), {"id": "root"}])  # by id, then drive root
        with (
            patch.object(folder_sync, "graph", return_value=g),
            patch.object(folder_sync.gd, "get_item_by_path", new=AsyncMock(side_effect=GraphError(404, "nf"))),
            patch.object(folder_sync.gd, "search_items", new=AsyncMock(return_value=[{"name": "mv  one", "folder": {}}])),
        ):
            state, _ = await folder_sync._target_state(_target())
        self.assertEqual(state, "present")  # moved/renamed-spacing, not deleted

    async def test_404_everywhere_and_not_found_is_missing(self):
        from app.graph.client import GraphError
        g = MagicMock()
        g.get = AsyncMock(side_effect=[GraphError(404, "nf"), {"id": "root"}])
        with (
            patch.object(folder_sync, "graph", return_value=g),
            patch.object(folder_sync.gd, "get_item_by_path", new=AsyncMock(side_effect=GraphError(404, "nf"))),
            patch.object(folder_sync.gd, "search_items", new=AsyncMock(return_value=[])),
        ):
            state, _ = await folder_sync._target_state(_target())
        self.assertEqual(state, "missing")

    async def test_unreachable_drive_is_unknown_not_missing(self):
        from app.graph.client import GraphError
        g = MagicMock()
        g.get = AsyncMock(side_effect=[GraphError(404, "nf"), GraphError(403, "denied")])
        with (
            patch.object(folder_sync, "graph", return_value=g),
            patch.object(folder_sync.gd, "get_item_by_path", new=AsyncMock(side_effect=GraphError(404, "nf"))),
        ):
            state, _ = await folder_sync._target_state(_target())
        self.assertEqual(state, "unknown")

    async def test_permission_error_is_unknown_not_missing(self):
        from app.graph.client import GraphError
        g = MagicMock()
        g.get = AsyncMock(side_effect=GraphError(403, "denied"))
        with patch.object(folder_sync, "graph", return_value=g):
            state, _ = await folder_sync._target_state(_target())
        self.assertEqual(state, "unknown")


class TestProcessDeltaItems(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine, tables=[models.Vessel.__table__, models.Folder.__table__, models.AppSetting.__table__])
        self.db = sessionmaker(bind=engine)()
        D = "drive-1"
        self.db.add_all([
            models.Folder(path="Technical", name="Technical", kind="main", drive_item_id="main", site_id=D),
            models.Folder(path="Technical/MV One", name="MV One", kind="ship", drive_item_id="ship1", site_id=D, vessel_id=None),
            models.Folder(path="Technical/MV One/Certs", name="Certs", kind="folder", drive_item_id="certs", site_id=D),
            models.Folder(path="Technical/MV One/Certs/Safety", name="Safety", kind="folder", drive_item_id="safety", site_id=D),
            models.Folder(path="Technical/MV One/Manuals", name="Manuals", kind="folder", drive_item_id="manuals", site_id=D),
        ])
        self.db.commit()

    def paths(self):
        return sorted(r.path for r in self.db.query(models.Folder).execution_options(all_sites=True).all())

    def test_rename_updates_row_and_descendants(self):
        res = folder_sync.process_delta_items(self.db, "drive-1", [
            {"id": "certs", "name": "Certificates", "folder": {}, "parentReference": {"id": "ship1"}},
        ])
        self.db.commit()
        self.assertEqual(res["updated"], 1)
        self.assertIn("Technical/MV One/Certificates", self.paths())
        self.assertIn("Technical/MV One/Certificates/Safety", self.paths())
        self.assertNotIn("Technical/MV One/Certs", self.paths())

    def test_move_rebuilds_path_under_new_parent(self):
        folder_sync.process_delta_items(self.db, "drive-1", [
            {"id": "safety", "name": "Safety", "folder": {}, "parentReference": {"id": "manuals"}},
        ])
        self.db.commit()
        self.assertIn("Technical/MV One/Manuals/Safety", self.paths())
        self.assertNotIn("Technical/MV One/Certs/Safety", self.paths())

    def test_delete_removes_row_and_descendants(self):
        res = folder_sync.process_delta_items(self.db, "drive-1", [{"id": "certs", "deleted": {}}])
        self.db.commit()
        self.assertEqual(res["deleted"], 1)
        self.assertNotIn("Technical/MV One/Certs", self.paths())
        self.assertNotIn("Technical/MV One/Certs/Safety", self.paths())
        self.assertIn("Technical/MV One/Manuals", self.paths())

    def test_unknown_items_and_files_are_ignored(self):
        before = self.paths()
        res = folder_sync.process_delta_items(self.db, "drive-1", [
            {"id": "not-in-db", "name": "x", "folder": {}},
            {"id": "certs", "name": "report.pdf", "file": {}},  # same id but a file facet: not a folder
        ])
        self.db.commit()
        self.assertEqual(res["updated"], 0)
        self.assertEqual(before, self.paths())

    def test_rename_never_creates_duplicate_path(self):
        res = folder_sync.process_delta_items(self.db, "drive-1", [
            {"id": "certs", "name": "Manuals", "folder": {}, "parentReference": {"id": "ship1"}},
        ])
        self.db.commit()
        self.assertEqual(res["updated"], 0)  # would collide with existing "Manuals": skipped, logged
        self.assertEqual(len(self.paths()), len(set(self.paths())))

    def test_deleted_ship_folder_is_reported_not_dropped(self):
        v = models.Vessel(name="MV One", is_provisioned=True)
        self.db.add(v)
        self.db.commit()
        row = self.db.query(models.Folder).execution_options(all_sites=True).filter_by(drive_item_id="ship1").one()
        row.vessel_id = v.id
        self.db.commit()
        res = folder_sync.process_delta_items(self.db, "drive-1", [{"id": "ship1", "deleted": {}}])
        self.db.commit()
        self.assertEqual(res["ship_deleted"], [(v.id, "MV One")])
        self.assertIn("Technical/MV One", self.paths())  # left for the vessel check to clean up


if __name__ == "__main__":
    unittest.main()
