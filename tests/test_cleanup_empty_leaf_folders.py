"""Tests for app.jobs.cleanup_empty_leaf_folders using an in-memory fake drive."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.jobs.cleanup_empty_leaf_folders import (
    CleanupConfig,
    LeafFolderCleanup,
    load_template,
    norm_path,
)

APP = {"application": {"id": "app-1", "displayName": "SharePoint App"}}
USER = {"user": {"email": "someone@contoso.com", "displayName": "Someone"}}


class FakeDrive:
    """Minimal driveItem tree implementing the DriveAPI protocol."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.parent: dict[str, str | None] = {}
        self.hidden_counts: dict[str, int] = {}   # items invisible to listing
        self.rollup_missing: set[str] = set()
        self.deleted: list[str] = []
        self.mutate_before_delete: dict[str, callable] = {}
        self._n = 0

    def add(self, parent_id, name, folder=True, created_by=APP):
        self._n += 1
        iid = f"i{self._n}"
        item = {"id": iid, "name": name, "eTag": f"e{self._n}-1", "createdBy": created_by,
                "createdDateTime": "2026-09-23T00:00:00Z"}
        if folder:
            item["folder"] = {}
        else:
            item["file"] = {}
        self.items[iid] = item
        self.parent[iid] = parent_id
        return iid

    def _children(self, iid):
        return [c for c, p in self.parent.items() if p == iid and c in self.items]

    def _view(self, iid):
        item = dict(self.items[iid])
        if "folder" in item:
            item["folder"] = {"childCount": len(self._children(iid)) + self.hidden_counts.get(iid, 0)}
        return item

    def path_of(self, iid):
        parts = []
        while iid is not None:
            parts.append(self.items[iid]["name"])
            iid = self.parent[iid]
        return "/".join(reversed(parts[:-1]))  # drop library root

    async def resolve_path(self, path):
        for iid in self.items:
            if self.parent.get(iid) is not None and norm_path(self.path_of(iid)) == norm_path(path):
                return self._view(iid)
        return None

    async def list_children(self, item_id):
        return [self._view(c) for c in self._children(item_id)]

    async def get_item(self, item_id):
        return self._view(item_id) if item_id in self.items else None

    async def get_rollup_counts(self, item_id):
        if item_id in self.rollup_missing:
            return None, None
        kids = self._children(item_id)
        files = sum(1 for k in kids if "file" in self.items[k]) + self.hidden_counts.get(item_id, 0)
        folders = sum(1 for k in kids if "folder" in self.items[k])
        return files, folders

    async def delete_item(self, item_id, etag):
        hook = self.mutate_before_delete.pop(item_id, None)
        if hook:
            hook()
        if item_id not in self.items:
            return "gone"
        if etag != self.items[item_id]["eTag"]:
            return "changed"
        stack = [item_id]
        while stack:
            cur = stack.pop()
            stack.extend(self._children(cur))
            self.items.pop(cur, None)
        self.deleted.append(item_id)
        return "deleted"


def build_kaizen(drive: FakeDrive):
    lib = drive.add(None, "<library root>")
    root = drive.add(lib, "Kaizen - Knowledge Bank")
    ids = {"root": root}
    for name in ("Templates", "Procedures and Work Instructions", "Lessons Learned"):
        ids[name] = drive.add(root, name)
    circ = drive.add(root, "Circulars and Guidance")
    ids["Circulars and Guidance"] = circ
    for name in ("Equipment Maker", "Class", "Flag - Port State", "SIRE-OCIMF-RightShip", "Shipyard"):
        ids[name] = drive.add(circ, name)
    return ids


TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "jobs" / "cleanup_templates" / "kaizen_knowledge_bank.json"


class LeafCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_file = Path(self.tmp.name) / "audit.jsonl"
        self.root_name, self.paths = load_template(TEMPLATE)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **kw):
        base = dict(drive_id="d", root_path=self.root_name, template_paths=self.paths,
                    apply=True, log_file=self.log_file, delete_delay=0)
        base.update(kw)
        return CleanupConfig(**base)

    def run_job(self, drive, **kw):
        return asyncio.run(LeafFolderCleanup(self.cfg(**kw), drive).run())

    def audit(self):
        return [json.loads(l) for l in self.log_file.read_text(encoding="utf-8").splitlines()]

    def test_deletes_only_true_leaves_without_cascade(self):
        d = FakeDrive(); ids = build_kaizen(d)
        r = self.run_job(d)
        self.assertEqual(r.deleted, 8)  # 3 top-level leaves + 5 circulars leaves
        self.assertIn(ids["Circulars and Guidance"], d.items)  # parent kept
        self.assertIn(ids["root"], d.items)
        deleted = [a for a in self.audit() if a["action"] == "deleted"]
        self.assertTrue(all(a["path"].startswith("Kaizen - Knowledge Bank/") for a in deleted))
        self.assertTrue(all(a["ts"].endswith("Z") for a in deleted))

    def test_cascade_removes_parent_but_never_root(self):
        d = FakeDrive(); ids = build_kaizen(d)
        r = self.run_job(d, cascade=True)
        self.assertEqual(r.deleted, 9)
        self.assertNotIn(ids["Circulars and Guidance"], d.items)
        self.assertIn(ids["root"], d.items)

    def test_folder_with_file_or_empty_subfolder_is_kept(self):
        d = FakeDrive(); ids = build_kaizen(d)
        d.add(ids["Templates"], "form.docx", folder=False)
        extra = d.add(ids["Lessons Learned"], "2026", created_by=USER)  # manual empty subfolder
        r = self.run_job(d, cascade=True)
        self.assertIn(ids["Templates"], d.items)
        self.assertIn(ids["Lessons Learned"], d.items)   # has a subfolder -> not a leaf
        self.assertIn(extra, d.items)                     # manual folder not in template
        skipped = {a["path"]: a["reason"] for a in self.audit() if a["action"] == "skipped"}
        self.assertIn("Kaizen - Knowledge Bank/Lessons Learned/2026", skipped)
        self.assertEqual(r.errors, 0)

    def test_hidden_items_block_delete(self):
        d = FakeDrive(); ids = build_kaizen(d)
        d.hidden_counts[ids["Class"]] = 1
        self.run_job(d)
        self.assertIn(ids["Class"], d.items)

    def test_missing_rollup_is_fail_safe(self):
        d = FakeDrive(); ids = build_kaizen(d)
        d.rollup_missing.add(ids["Shipyard"])
        self.run_job(d)
        self.assertIn(ids["Shipyard"], d.items)
        d2 = FakeDrive(); ids2 = build_kaizen(d2)
        d2.rollup_missing.add(ids2["Shipyard"])
        self.run_job(d2, require_rollup_counts=False)
        self.assertNotIn(ids2["Shipyard"], d2.items)

    def test_creator_guard(self):
        d = FakeDrive(); ids = build_kaizen(d)
        d.items[ids["Templates"]]["createdBy"] = USER
        self.run_job(d, created_by={"sharepoint app"})
        self.assertIn(ids["Templates"], d.items)
        self.assertNotIn(ids["Lessons Learned"], d.items)

    def test_race_file_added_before_delete_is_skipped(self):
        d = FakeDrive(); ids = build_kaizen(d)

        def add_file():
            d.add(ids["Shipyard"], "late.pdf", folder=False)
            d.items[ids["Shipyard"]]["eTag"] = "changed"
        d.mutate_before_delete[ids["Shipyard"]] = add_file
        r = self.run_job(d)
        self.assertIn(ids["Shipyard"], d.items)
        self.assertEqual(r.skipped, 1)

    def test_dry_run_deletes_nothing_and_rerun_is_idempotent(self):
        d = FakeDrive(); build_kaizen(d)
        before = set(d.items)
        r = self.run_job(d, apply=False, cascade=True)
        self.assertEqual(set(d.items), before)
        self.assertEqual(r.would_delete, 9)
        self.run_job(d, cascade=True)
        r2 = self.run_job(d, cascade=True)
        self.assertEqual((r2.deleted, r2.errors, r2.candidates), (0, 0, 0))

    def test_max_deletes_aborts_before_any_delete(self):
        d = FakeDrive(); build_kaizen(d)
        before = set(d.items)
        r = self.run_job(d, max_deletes=3)
        self.assertTrue(r.aborted)
        self.assertEqual(set(d.items), before)

    def test_guard_required(self):
        with self.assertRaises(ValueError):
            CleanupConfig(drive_id="d", root_path="X").validate()


if __name__ == "__main__":
    unittest.main()
