import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import main as app_main
from app.main import (
    _FOLDER_PARENT_MAP,
    _FOLDER_RECURSIVE_COUNTS_CACHE,
    _GRAPH_RECURSIVE_SEMAPHORE,
    get_folder_recursive_counts,
    invalidate_folder_caches,
)


class TestFolderRecursiveCounts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _FOLDER_RECURSIVE_COUNTS_CACHE.clear()
        _FOLDER_PARENT_MAP.clear()

    async def asyncTearDown(self):
        _FOLDER_RECURSIVE_COUNTS_CACHE.clear()
        _FOLDER_PARENT_MAP.clear()

    async def test_nested_hierarchy_counts_matching_real_vessel(self):
        """
        Verify recursive subtree calculation mirrors real vessel hierarchy:
        'Manuals' has 16 direct subfolders with 34 files distributed across them.
        """
        mock_tree = {
            "root_manuals": [
                {"id": f"sub_{i}", "name": f"Subfolder_{i}", "folder": {"childCount": 0}}
                for i in range(16)
            ],
            # 16 subfolders with files distributed (total 34 files, matching Maersk Frio real data)
            "sub_0": [],                                             # Automation (0 files)
            "sub_1": [],                                             # Aux Engine (0 files)
            "sub_2": [],                                             # Boiler (0 files)
            "sub_3": [{"id": "f_cargo_1", "name": "cargo.pdf"}],     # Cargo (1 file)
            "sub_4": [{"id": "f_deck_1", "name": "deck.pdf"}],       # Deck Machinery (1 file)
            "sub_5": [],                                             # Electrical (0 files)
            "sub_6": [],                                             # Main Engine (0 files)
            "sub_7": [{"id": f"f_other_{i}", "name": f"other_{i}.pdf"} for i in range(23)], # Other Manuals (23 files)
            "sub_8": [],                                             # Pollution (0 files)
            "sub_9": [],                                             # Propulsion (0 files)
            "sub_10": [{"id": "f_refrig_1", "name": "refrig.pdf"}],  # Refrigeration (1 file)
            "sub_11": [{"id": "f_safety_1", "name": "safety.pdf"}],  # Safety (1 file)
            "sub_12": [],                                            # Shafting (0 files)
            "sub_13": [{"id": f"f_steer_{i}", "name": f"steer_{i}.pdf"} for i in range(3)], # Steering Gear (3 files)
            "sub_14": [],                                            # Thrusters (0 files)
            "sub_15": [{"id": f"f_tbc_{i}", "name": f"tbc_{i}.pdf"} for i in range(4)],     # To Be Classified (4 files)
        }

        async def mock_list_children(drive_id, folder_id):
            return mock_tree.get(folder_id, [])

        with patch("app.main.gd.list_children", side_effect=mock_list_children):
            res = await get_folder_recursive_counts("drive1", "root_manuals")

        self.assertEqual(res["direct_subfolders"], 16)
        self.assertEqual(res["direct_files"], 0)
        self.assertEqual(res["total_subfolders"], 16)
        self.assertEqual(res["total_files"], 34)

        # Also verify leaf folder results
        with patch("app.main.gd.list_children", side_effect=mock_list_children):
            leaf_other = await get_folder_recursive_counts("drive1", "sub_7")
            leaf_empty = await get_folder_recursive_counts("drive1", "sub_0")

        self.assertEqual(leaf_other["direct_subfolders"], 0)
        self.assertEqual(leaf_other["direct_files"], 23)
        self.assertEqual(leaf_other["total_files"], 23)

        self.assertEqual(leaf_empty["direct_subfolders"], 0)
        self.assertEqual(leaf_empty["direct_files"], 0)
        self.assertEqual(leaf_empty["total_files"], 0)

    async def test_global_semaphore_caps_peak_concurrency(self):
        """
        Prove that _GRAPH_RECURSIVE_SEMAPHORE actually caps peak in-flight Graph calls at 6,
        even when fan-out attempts 25 parallel subfolder walks with an artificial delay.
        """
        mock_tree = {
            "wide_container": [
                {"id": f"child_{i}", "name": f"Child_{i}", "folder": {"childCount": 0}}
                for i in range(25)
            ]
        }
        for i in range(25):
            mock_tree[f"child_{i}"] = [{"id": f"file_{i}", "name": f"file_{i}.pdf"}]

        active_calls = 0
        peak_active = 0

        async def mock_list_children(drive_id, folder_id):
            nonlocal active_calls, peak_active
            active_calls += 1
            if active_calls > peak_active:
                peak_active = active_calls
            await asyncio.sleep(0.04)  # artificial network latency
            active_calls -= 1
            return mock_tree.get(folder_id, [])

        with patch("app.main.gd.list_children", side_effect=mock_list_children):
            res = await get_folder_recursive_counts("drive1", "wide_container")

        self.assertEqual(res["direct_subfolders"], 25)
        self.assertEqual(res["total_files"], 25)
        # CRUCIAL ASSERTION: Peak concurrency must NEVER exceed semaphore limit of 6
        self.assertLessEqual(peak_active, 6)
        self.assertGreater(peak_active, 1)

    async def test_targeted_ancestor_invalidation(self):
        """
        Verify that invalidating a leaf folder evicts only that folder and its ancestors,
        preserving cached counts for all unrelated branches in the tenant.
        """
        mock_tree = {
            "root": [
                {"id": "branch_a", "name": "Branch_A", "folder": {"childCount": 1}},
                {"id": "branch_b", "name": "Branch_B", "folder": {"childCount": 1}},
            ],
            "branch_a": [
                {"id": "leaf_a", "name": "Leaf_A", "folder": {"childCount": 0}},
            ],
            "leaf_a": [
                {"id": "fa1", "name": "fa1.pdf"},
            ],
            "branch_b": [
                {"id": "leaf_b", "name": "Leaf_B", "folder": {"childCount": 0}},
            ],
            "leaf_b": [
                {"id": "fb1", "name": "fb1.pdf"},
            ],
        }

        async def mock_list_children(drive_id, folder_id):
            return mock_tree.get(folder_id, [])

        with patch("app.main.gd.list_children", side_effect=mock_list_children):
            await get_folder_recursive_counts("drive1", "root")

        self.assertIn("drive1:leaf_a", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertIn("drive1:branch_a", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertIn("drive1:leaf_b", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertIn("drive1:branch_b", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertIn("drive1:root", _FOLDER_RECURSIVE_COUNTS_CACHE)

        invalidate_folder_caches("leaf_a")

        self.assertNotIn("drive1:leaf_a", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertNotIn("drive1:branch_a", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertNotIn("drive1:root", _FOLDER_RECURSIVE_COUNTS_CACHE)

        self.assertIn("drive1:leaf_b", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertIn("drive1:branch_b", _FOLDER_RECURSIVE_COUNTS_CACHE)

    async def test_dual_sided_move_invalidation(self):
        """
        Verify that invalidating both source_folder_id and target_folder_id
        evicts both ancestor chains.
        """
        _FOLDER_PARENT_MAP["source_folder"] = "source_parent"
        _FOLDER_PARENT_MAP["source_parent"] = "root"
        _FOLDER_PARENT_MAP["target_folder"] = "target_parent"
        _FOLDER_PARENT_MAP["target_parent"] = "root"

        _FOLDER_RECURSIVE_COUNTS_CACHE["d:source_folder"] = (1.0, {})
        _FOLDER_RECURSIVE_COUNTS_CACHE["d:source_parent"] = (1.0, {})
        _FOLDER_RECURSIVE_COUNTS_CACHE["d:target_folder"] = (1.0, {})
        _FOLDER_RECURSIVE_COUNTS_CACHE["d:target_parent"] = (1.0, {})
        _FOLDER_RECURSIVE_COUNTS_CACHE["d:unrelated"] = (1.0, {})

        invalidate_folder_caches("source_folder")
        invalidate_folder_caches("target_folder")

        self.assertNotIn("d:source_folder", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertNotIn("d:source_parent", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertNotIn("d:target_folder", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertNotIn("d:target_parent", _FOLDER_RECURSIVE_COUNTS_CACHE)
        self.assertIn("d:unrelated", _FOLDER_RECURSIVE_COUNTS_CACHE)

    async def test_pagination_following_nextlink(self):
        """
        Assert that list_children follows @odata.nextLink across multiple pages.
        """
        from app.graph import drive as gd

        pages = [
            {"value": [{"id": f"item_{i}", "name": f"item_{i}.txt", "file": {}} for i in range(200)], "@odata.nextLink": "/next_page_1"},
            {"value": [{"id": f"item_{i}", "name": f"item_{i}.txt", "file": {}} for i in range(200, 350)]},
        ]
        page_idx = 0

        async def mock_graph_get(url):
            nonlocal page_idx
            p = pages[page_idx]
            page_idx += 1
            return p

        with patch("app.graph.drive.graph") as mock_graph:
            mock_client = AsyncMock()
            mock_client.get = mock_graph_get
            mock_graph.return_value = mock_client

            items = await gd.list_children("drive1", "folder1")

        self.assertEqual(len(items), 350)
        self.assertEqual(items[0]["id"], "item_0")
        self.assertEqual(items[349]["id"], "item_349")


if __name__ == "__main__":
    unittest.main()
