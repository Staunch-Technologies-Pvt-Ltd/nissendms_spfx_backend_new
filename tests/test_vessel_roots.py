"""Per-site vessel folders: discovery only looks inside the folders an admin
chose; "none" disables it; no setting keeps automatic discovery."""
import asyncio
import unittest
from unittest import mock

from app.services import vessel_roots, vessel_sync


class FakeClient:
    TREE = {
        "type of vessel": ["Peissy", "Snow Flake", "Pool-ab12"],
        "Technical and Crewing  New": ["Belle Lune"],
    }

    async def get(self, url):
        if "/root:/" in url:
            name = url.split("/root:/", 1)[1].split("?")[0].replace("%20", " ")
            if name not in self.TREE:
                raise RuntimeError("Graph 404")
            return {"id": f"id:{name}", "name": name, "folder": {}}
        parent = url.split("/items/id:", 1)[1].split("/children")[0].replace("%20", " ")
        return {"value": [{"id": f"id:{c}", "name": c, "folder": {}} for c in self.TREE[parent]]}


class VesselRootsTests(unittest.TestCase):
    def test_children_of_chosen_folders_only(self):
        found = asyncio.run(vessel_roots.child_folders_of_roots(FakeClient(), "d", ["type of vessel", "Missing folder"]))
        self.assertEqual([(i["name"], p) for i, p in found], [("Peissy", "type of vessel"), ("Snow Flake", "type of vessel"), ("Pool-ab12", "type of vessel")])

    def test_sync_candidates_follow_the_setting(self):
        with mock.patch.object(vessel_sync, "graph", lambda: FakeClient()):
            with mock.patch.object(vessel_roots, "get_for_drive", lambda d: {"mode": "folders", "paths": ["type of vessel"]}):
                cands = asyncio.run(vessel_sync.list_root_vessel_candidates("d", set()))
            self.assertEqual([(c.name, c.path) for c in cands], [("Peissy", "type of vessel/Peissy"), ("Snow Flake", "type of vessel/Snow Flake")])
            with mock.patch.object(vessel_roots, "get_for_drive", lambda d: {"mode": "none", "paths": []}):
                self.assertEqual(asyncio.run(vessel_sync.list_root_vessel_candidates("d", set())), [])

    def test_save_validates(self):
        with self.assertRaisesRegex(ValueError, "at least one folder"):
            vessel_roots.save("d", "s", "folders", ["  ", ""], "a@b.c")
        with self.assertRaisesRegex(ValueError, "mode must be"):
            vessel_roots.save("d", "s", "everything", [], "a@b.c")


if __name__ == "__main__":
    unittest.main()
