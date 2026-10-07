"""Vessel folder template: validation and the additive folder creation used
on vessel creation and "Apply to existing vessels". Runs against an
in-memory fake of the Graph drive API — no tenant or database needed."""
import asyncio
import unittest

from app.services import vessel_folder_template as vft


class FakeClient:
    """Folders keyed by id with a parent map; counts listing calls."""

    def __init__(self):
        self.items = {"root": {"id": "root", "name": "", "folder": {}}}
        self.parent = {}
        self.lists = 0
        self.posts = 0
        self.fail_names: set[str] = set()
        self._n = 0

    def add(self, parent, name):
        self._n += 1
        iid = f"f{self._n}"
        self.items[iid] = {"id": iid, "name": name, "folder": {}}
        self.parent[iid] = parent
        return iid

    def tree(self, pid="root", prefix=""):
        out = []
        for iid, p in self.parent.items():
            if p == pid:
                path = f"{prefix}/{self.items[iid]['name']}" if prefix else self.items[iid]["name"]
                out.append(path)
                out += self.tree(iid, path)
        return sorted(out)

    async def get(self, url):
        self.lists += 1
        pid = url.split("/items/")[1].split("/children")[0]
        return {"value": [dict(self.items[i]) for i, p in self.parent.items() if p == pid]}

    async def post(self, url, json):
        self.posts += 1
        pid = url.split("/items/")[1].split("/children")[0]
        if json["name"] in self.fail_names:
            raise RuntimeError("Graph 403: Access denied")
        return dict(self.items[self.add(pid, json["name"])])


def run(coro):
    return asyncio.run(coro)


class ValidateTests(unittest.TestCase):
    def test_default_template_is_the_requested_structure(self):
        paths = vft.flatten(vft.validate(vft.DEFAULT_FOLDERS))
        self.assertIn("Drawings and Manuals/Drawings/Other Drawings", paths)
        self.assertIn("Drawings and Manuals/Manuals/Steering Gear", paths)
        self.assertIn("Drawings and Manuals/To Be Classified", paths)
        self.assertEqual(vft.count_folders(vft.DEFAULT_FOLDERS), 1 + 1 + 7 + 1 + 15 + 1)

    def test_cleans_names_and_accepts_strings(self):
        out = vft.validate([{"name": "  Deck   Logs ", "children": ["Bridge"]}])
        self.assertEqual(out, [{"name": "Deck Logs", "children": [{"name": "Bridge", "children": []}]}])

    def test_rejects_bad_input_with_clear_messages(self):
        cases = [
            ([{"name": ""}], "has no name"),
            ([{"name": "A/B"}], "characters SharePoint doesn't allow"),
            ([{"name": "Hull"}, {"name": "hull"}], "appears twice"),
            ([{"name": ".hidden"}], "dot"),
        ]
        for folders, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                vft.validate(folders)
        deep = {"name": "L1", "children": []}
        node = deep
        for i in range(2, vft.MAX_DEPTH + 2):
            child = {"name": f"L{i}", "children": []}
            node["children"].append(child)
            node = child
        with self.assertRaisesRegex(ValueError, "nested at most"):
            vft.validate([deep])


class EnsureTreeTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.vessel = self.client.add("root", "MV Aurora")
        self.folders = vft.validate(vft.DEFAULT_FOLDERS)

    def test_creates_full_structure_under_new_vessel(self):
        r = run(vft.ensure_tree(self.client, "d", self.vessel, self.folders))
        self.assertEqual(len(r["created"]), vft.count_folders(self.folders))
        self.assertEqual(r["failed"], [])
        self.assertIn("MV Aurora/Drawings and Manuals/Manuals/Thrusters", self.client.tree())
        # Only the vessel folder itself is listed; new folders are known empty.
        self.assertEqual(self.client.lists, 1)

    def test_reuses_existing_folders_and_only_adds_missing(self):
        dm = self.client.add(self.vessel, "Drawings  and manuals")  # different case + double space
        self.client.add(dm, "Drawings")
        r = run(vft.ensure_tree(self.client, "d", self.vessel, self.folders))
        self.assertEqual(r["existing"], 2)
        self.assertEqual(len(r["created"]), vft.count_folders(self.folders) - 2)
        tree = self.client.tree()
        self.assertNotIn("MV Aurora/Drawings and Manuals", tree)  # the existing one was reused, not duplicated
        self.assertIn("MV Aurora/Drawings  and manuals/Drawings/Hull", tree)
        # A second run creates nothing.
        again = run(vft.ensure_tree(self.client, "d", self.vessel, self.folders))
        self.assertEqual(again["created"], [])

    def test_dry_run_writes_nothing(self):
        r = run(vft.ensure_tree(self.client, "d", self.vessel, self.folders, dry_run=True))
        self.assertEqual(len(r["created"]), vft.count_folders(self.folders))
        self.assertEqual(self.client.posts, 0)
        self.assertEqual(self.client.tree(), ["MV Aurora"])

    def test_also_matches_names_prevent_near_duplicates(self):
        dm = self.client.add(self.vessel, "Drawings and Manuals")
        manuals = self.client.add(dm, "Manuals")
        self.client.add(manuals, "Auxilliary Engine")  # existing misspelling
        r = run(vft.ensure_tree(self.client, "d", self.vessel, self.folders))
        self.assertNotIn("Drawings and Manuals/Manuals/Auxiliary Engine", r["created"])
        self.assertNotIn("MV Aurora/Drawings and Manuals/Manuals/Auxiliary Engine", self.client.tree())
        with self.assertRaisesRegex(ValueError, "appears twice"):
            vft.validate([{"name": "Hull", "aliases": ["Hulls"]}, {"name": "hulls"}])

    def test_a_failed_folder_is_reported_and_others_continue(self):
        self.client.fail_names = {"Manuals"}
        r = run(vft.ensure_tree(self.client, "d", self.vessel, self.folders))
        self.assertEqual([f["path"] for f in r["failed"]], ["Drawings and Manuals/Manuals"])
        self.assertIn("MV Aurora/Drawings and Manuals/Drawings/Archive", self.client.tree())
        self.assertNotIn("MV Aurora/Drawings and Manuals/Manuals/Boiler", self.client.tree())


if __name__ == "__main__":
    unittest.main()
