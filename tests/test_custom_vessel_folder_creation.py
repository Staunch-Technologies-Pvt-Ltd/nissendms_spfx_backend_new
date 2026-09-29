import os
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import site_alias_matches
from app.services import site_provisioning
from app.services.errors import BadRequest
from app.services.real_backend import RealBackend


class TestCustomVesselFolderCreation(unittest.IsolatedAsyncioTestCase):
    async def test_retry_provisioning_uses_recorded_sites_only(self):
        backend = RealBackend()
        vessel = SimpleNamespace(
            name="MV Recorded Site",
            is_provisioned=False,
            provisioned_site_ids=["selected-site"],
        )

        class Query:
            def filter_by(self, **kwargs):
                return self

            def one_or_none(self):
                return vessel

        class Db:
            def query(self, model):
                return Query()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with (
            patch("app.services.real_backend.SessionLocal", return_value=Db()),
            patch("app.services.site_provisioning.provision_vessel_multi_site", new=AsyncMock(return_value={
                "provisioned_sites": ["selected-site"],
                "results": {"selected-site": {"status": "success"}},
            })) as provision_multi,
        ):
            result = await backend.start_vessel_provisioning("42")

        provision_multi.assert_awaited_once_with(42, "MV Recorded Site", ["selected-site"])
        self.assertEqual(result["status"], "completed")

    async def test_create_vessel_at_path_creates_vessel_and_custom_subfolders(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=[
            {"id": "root-id"},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
        ])
        client.post = AsyncMock(side_effect=[
            {"id": "projects-1", "name": "Projects"},
            {"id": "marine-1", "name": "Marine"},
            {"id": "vessel-1", "name": "MV Test"},
            {"id": "cert-1", "name": "Certificates"},
            {"id": "inv-1", "name": "Invoices"},
        ])

        with (
            patch(
                "app.services.site_provisioning._site_config_for_reference",
                return_value=("demo", SimpleNamespace(drive_id="drive-123")),
            ),
            patch("app.services.site_provisioning.graph", return_value=client),
        ):
            result = await site_provisioning.create_vessel_at_path(
                "demo",
                "Projects/Marine",
                "MV Test",
                ["Certificates", "Invoices"],
            )

        self.assertEqual(result["site_key"], "demo")
        self.assertEqual(result["vessel_folder_path"], "Projects/Marine/MV Test")
        self.assertEqual(result["subfolders"], ["Certificates", "Invoices"])
        self.assertEqual(client.post.await_count, 5)

        root_parent_url = client.post.await_args_list[0].args[0]
        self.assertIn("/items/root-id/children", root_parent_url)

        vessel_url = client.post.await_args_list[2].args[0]
        self.assertIn("/items/marine-1/children", vessel_url)

        subfolder_urls = [call.args[0] for call in client.post.await_args_list[3:]]
        self.assertTrue(all("/items/vessel-1/children" in url for url in subfolder_urls))

    async def test_create_vessel_at_path_creates_nested_subfolder_tree(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=[
            {"id": "root-id"},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
        ])
        client.post = AsyncMock(side_effect=[
            {"id": "projects-1", "name": "Projects"},
            {"id": "vessel-1", "name": "MV Test"},
            {"id": "cert-1", "name": "Certificates"},
            {"id": "stat-1", "name": "Statutory"},
            {"id": "report-1", "name": "Reports"},
        ])

        with (
            patch(
                "app.services.site_provisioning._site_config_for_reference",
                return_value=("demo", SimpleNamespace(drive_id="drive-123")),
            ),
            patch("app.services.site_provisioning.graph", return_value=client),
        ):
            result = await site_provisioning.create_vessel_at_path(
                "demo", "Projects", "MV Test", ["Certificates/Statutory/Reports"]
            )

        self.assertEqual(result["subfolders"], ["Certificates/Statutory/Reports"])
        self.assertEqual(client.post.await_count, 5)
        first_post = client.post.await_args_list[0].args[0]
        self.assertIn("/items/root-id/children", first_post)
        nested_urls = [call.args[0] for call in client.post.await_args_list[1:]]
        self.assertIn("/items/projects-1/children", nested_urls[0])
        self.assertIn("/items/vessel-1/children", nested_urls[1])
        self.assertIn("/items/cert-1/children", nested_urls[2])
        self.assertIn("/items/stat-1/children", nested_urls[3])

    async def test_create_vessel_at_path_creates_missing_parent_chain(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=[
            {"id": "root-id"},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
            {"value": []},
        ])
        client.post = AsyncMock(side_effect=[
            {"id": "projects-1", "name": "Projects"},
            {"id": "marine-1", "name": "Marine"},
            {"id": "vessel-1", "name": "MV Test"},
            {"id": "cert-1", "name": "Certificates"},
        ])

        with (
            patch(
                "app.services.site_provisioning._site_config_for_reference",
                return_value=("demo", SimpleNamespace(drive_id="drive-123")),
            ),
            patch("app.services.site_provisioning.graph", return_value=client),
        ):
            result = await site_provisioning.create_vessel_at_path(
                "demo", "Projects/Marine", "MV Test", ["Certificates"]
            )

        self.assertEqual(result["vessel_folder_path"], "Projects/Marine/MV Test")
        self.assertEqual(result["subfolders"], ["Certificates"])
        self.assertEqual(client.post.await_count, 4)

    async def test_create_vessel_does_not_create_dms_folder_structure_without_explicit_target(self):
        backend = RealBackend()
        name = f"MV No DMS {uuid.uuid4().hex[:6]}"
        imo = str(uuid.uuid4().int % 10_000_000).zfill(7)

        with (
            patch.object(backend.__class__, "_validate_vessel_input", return_value=(name, imo)),
            patch.object(backend.__class__, "_claim_pool_slot", return_value=None),
            patch("app.services.site_provisioning.create_vessel_at_path", new=AsyncMock()) as create_at_path,
            patch("app.services.site_provisioning.provision_vessel_multi_site", new=AsyncMock()) as provision_multi,
            patch.object(backend.__class__, "_create_activity", new=AsyncMock()),
        ):
            await backend.create_vessel(
                name,
                imo,
                requesting_email="user@example.com",
            )

        create_at_path.assert_not_awaited()
        provision_multi.assert_not_awaited()

    async def test_create_vessel_ignores_custom_site_provisioning_for_unknown_site(self):
        backend = RealBackend()
        name = f"MV Bad Site {uuid.uuid4().hex[:6]}"
        imo = str(uuid.uuid4().int % 10_000_000).zfill(7)

        with (
            patch.object(backend.__class__, "_validate_vessel_input", return_value=(name, imo)),
            patch.object(backend.__class__, "_claim_pool_slot", return_value=None),
            patch("app.services.site_provisioning.create_vessel_at_path", new=AsyncMock()) as create_at_path,
            patch("app.services.site_provisioning.provision_vessel_multi_site", new=AsyncMock()) as provision_multi,
            patch.object(backend.__class__, "_create_activity", new=AsyncMock()),
        ):
            await backend.create_vessel(
                name,
                imo,
                site_key="bad_site",
                parent_folder_path="Test Root",
                requesting_email="user@example.com",
            )

        create_at_path.assert_not_awaited()
        provision_multi.assert_not_awaited()

    async def test_create_vessel_creates_sharepoint_folder_for_custom_site(self):
        backend = RealBackend()
        name = f"MV Custom Site {uuid.uuid4().hex[:6]}"
        imo = str(uuid.uuid4().int % 10_000_000).zfill(7)

        with (
            patch.object(backend.__class__, "_validate_vessel_input", return_value=(name, imo)),
            patch.object(backend.__class__, "_claim_pool_slot", return_value=None),
            patch("app.services.real_backend.Settings.load_site_config", return_value=SimpleNamespace(drive_id="drive-123")),
            patch("app.services.site_provisioning.create_vessel_at_path", new=AsyncMock(return_value={
                "site_key": "demo",
                "drive_id": "drive-123",
                "vessel_folder_id": "folder-123",
                "vessel_folder_path": "Projects/Marine/MV Custom Site",
                "subfolders": [],
            })) as create_at_path,
            patch("app.services.site_provisioning.provision_vessel_multi_site", new=AsyncMock()) as provision_multi,
            patch.object(backend.__class__, "_create_activity", new=AsyncMock()),
        ):
            result = await backend.create_vessel(
                name,
                imo,
                site_key="demo",
                parent_folder_path="Projects/Marine",
                requesting_email="user@example.com",
            )

        create_at_path.assert_awaited_once_with(
            site_key="demo",
            parent_path="Projects/Marine",
            vessel_name=name,
            subfolders=None,
        )
        provision_multi.assert_not_awaited()
        self.assertEqual(result["result"]["vessel_folder_path"], "Projects/Marine/MV Custom Site")

    async def test_restore_deleted_vessel_recreates_active_record(self):
        backend = RealBackend()

        class FakeQuery:
            def __init__(self, rows):
                self.rows = rows
                self._kwargs = {}

            def filter_by(self, **kwargs):
                self._kwargs = kwargs
                return self

            def filter(self, *args, **kwargs):
                self._kwargs = kwargs
                return self

            def one_or_none(self):
                for row in self.rows:
                    ok = True
                    for key, value in self._kwargs.items():
                        if getattr(row, key, None) != value:
                            ok = False
                            break
                    if ok:
                        return row
                return None

            def all(self):
                return list(self.rows)

        class FakeSession:
            def __init__(self):
                self.vessels = []
                self.deleted_rows = [
                    SimpleNamespace(
                        id=42,
                        vessel_name="MV Restored",
                        vessel_imo="1234567",
                        vessel_type="Bulk Carrier",
                        drive_item_id="db_vessel_42",
                        original_path="Vessels/Specific Vessels/MV Restored",
                        site_name="Communication Site",
                        site_key="dev",
                        deleted_at=None,
                    )
                ]
                self.added = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def query(self, model):
                if model.__name__ == "DeletedVessel":
                    return FakeQuery(self.deleted_rows)
                if model.__name__ == "Vessel":
                    return FakeQuery(self.vessels)
                return FakeQuery([])

            def add(self, obj):
                self.added.append(obj)
                if obj.__class__.__name__ == "Vessel":
                    self.vessels.append(obj)
                elif obj.__class__.__name__ == "DeletedVessel":
                    self.deleted_rows.append(obj)

            def delete(self, obj):
                if obj in self.deleted_rows:
                    self.deleted_rows.remove(obj)
                if obj in self.vessels:
                    self.vessels.remove(obj)

            def commit(self):
                pass

        fake_session = FakeSession()
        with (
            patch("app.services.real_backend.SessionLocal", return_value=fake_session),
            patch("app.main.invalidate_folder_caches"),
        ):
            result = await backend._execute_restore_deleted("db_vessel_42")

        self.assertTrue(result["restored"])
        self.assertTrue(any(v.name == "MV Restored" for v in fake_session.vessels))
        self.assertFalse(any(d.vessel_name == "MV Restored" for d in fake_session.deleted_rows))

    def test_kaizen_knowledge_bank_is_not_auto_provisioned(self):
        from app import template

        self.assertNotIn("Kaizen - Knowledge Bank", template.MAIN_FOLDERS)
        self.assertNotIn("Kaizen - Knowledge Bank", template.FLAT_MAIN_FOLDERS)
        self.assertNotIn("Kaizen - Knowledge Bank", template.ALL_MAIN_FOLDERS)

    def test_site_alias_matches_communication_site_variants(self):
        self.assertTrue(site_alias_matches("dev", "communication site"))
        self.assertTrue(site_alias_matches("communication site", "dev"))
        self.assertTrue(site_alias_matches("communication", "dev"))
        self.assertTrue(site_alias_matches("Vessel DMS (dev)", "dev"))
        self.assertTrue(site_alias_matches("Vessel DMS (dev)", "communication site"))
        self.assertFalse(site_alias_matches("Vessel DMS (External)", "dev"))
        self.assertTrue(site_alias_matches("Vessel DMS (External)", "external"))
        self.assertTrue(site_alias_matches("local", "local"))
        self.assertFalse(site_alias_matches("local", "external"))


if __name__ == "__main__":
    unittest.main()
