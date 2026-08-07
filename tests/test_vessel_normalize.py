import sys
import unittest
from unittest.mock import MagicMock

# Add backend directory to sys.path
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.normalize import normalize_vessel_name, normalize_folder_name, clean_folder_name
from app.services.errors import Conflict, BadRequest
from app.services.stub_backend import StubBackend
from app.store import Store

class TestVesselNormalization(unittest.TestCase):
    def test_normalization_basic(self):
        """Test standard input casing and spaces."""
        self.assertEqual(normalize_vessel_name("MV 1307"), "mv1307")
        self.assertEqual(normalize_vessel_name("mv 1307"), "mv1307")

    def test_normalization_cosmetic_characters(self):
        """Test spaces, underscores, single quotes, and double quotes removal."""
        self.assertEqual(normalize_vessel_name("_MV 1307_"), "mv1307")
        self.assertEqual(normalize_vessel_name("MV1307"), "mv1307")
        self.assertEqual(normalize_vessel_name("'MV 1307'"), "mv1307")
        self.assertEqual(normalize_vessel_name('"MV 1307"'), "mv1307")
        self.assertEqual(normalize_vessel_name("MV '1307'"), "mv1307")
        self.assertEqual(normalize_vessel_name('MV "1307"'), "mv1307")
        self.assertEqual(normalize_vessel_name("mv_1307"), "mv1307")
        self.assertEqual(normalize_vessel_name("_mv 1307_"), "mv1307")
        self.assertEqual(normalize_vessel_name("  MV   1307  "), "mv1307")

    def test_normalization_combinations(self):
        """Test combination of multiple consecutive spaces, underscores, quotes, and double quotes."""
        self.assertEqual(normalize_vessel_name(' _"\'mv_  \' 1307_" '), "mv1307")
        self.assertEqual(normalize_vessel_name(""), "")
        self.assertEqual(normalize_vessel_name(None), "")

class TestBackendVesselValidation(unittest.IsolatedAsyncioTestCase):
    async def test_stub_backend_duplicate_validation(self):
        """Test validation logic inside StubBackend against duplicate names."""
        backend = StubBackend()
        
        # Mock store.vessels list to contain an existing vessel
        from app.store import store
        store.vessels = [
            {"id": "1", "name": "MV 1307", "imo": "1234567"}
        ]
        
        # Expect Conflict exceptions for all duplicate variations
        duplicates = [
            "_MV 1307_",
            "MV1307",
            "mv 1307",
            "MV  1307",
            "'MV 1307'",
            '"MV 1307"',
            "MV '1307'",
            'MV "1307"',
            "mv_1307",
            "_mv 1307_",
        ]
        
        for duplicate_name in duplicates:
            with self.subTest(duplicate_name=duplicate_name):
                with self.assertRaises(Conflict) as context:
                    await backend.create_vessel(duplicate_name, "7654321")
                self.assertEqual(str(context.exception), "Vessel name already exists.")

class TestFolderNormalization(unittest.TestCase):
    def test_normalization_basic(self):
        """Test folder name casing and spaces."""
        self.assertEqual(normalize_folder_name("July 2026"), "july2026")
        self.assertEqual(normalize_folder_name("july 2026"), "july2026")

    def test_normalization_cosmetic_characters(self):
        """Test quotes, underscores, spaces, dashes/hyphens, equals, and brackets removal."""
        self.assertEqual(normalize_folder_name("_July 2026_"), "july2026")
        self.assertEqual(normalize_folder_name("July2026"), "july2026")
        self.assertEqual(normalize_folder_name("'July 2026'"), "july2026")
        self.assertEqual(normalize_folder_name('"July 2026"'), "july2026")
        self.assertEqual(normalize_folder_name("July-2026"), "july2026")
        self.assertEqual(normalize_folder_name("July_2026"), "july2026")
        self.assertEqual(normalize_folder_name("_july 2026_"), "july2026")
        self.assertEqual(normalize_folder_name("=July 2026==="), "july2026")
        self.assertEqual(normalize_folder_name("[July 2026]"), "july2026")
        self.assertEqual(normalize_folder_name("(July 2026)"), "july2026")

    def test_folder_cleaning(self):
        """Test clean_folder_name function correctly keeps spaces, removes special characters, and uppercases months."""
        self.assertEqual(clean_folder_name("===July 2027---"), "JULY 2027")
        self.assertEqual(clean_folder_name("'July 2026'"), "JULY 2026")
        self.assertEqual(clean_folder_name("=July 2026==="), "JULY 2026")
        self.assertEqual(clean_folder_name("_July 2026_"), "JULY 2026")
        self.assertEqual(clean_folder_name("July_2026"), "JULY 2026")
        self.assertEqual(clean_folder_name("July-2026"), "JULY 2026")
        self.assertEqual(clean_folder_name("[July 2026]"), "JULY 2026")
        self.assertEqual(clean_folder_name("jULY 13/05/2025"), "JULY 13052025")

class TestBackendFolderValidation(unittest.IsolatedAsyncioTestCase):
    async def test_stub_backend_folder_duplicate_validation(self):
        """Test duplicate folder checks in StubBackend."""
        backend = StubBackend()
        from app.store import store
        
        # Build parent folder and existing child
        parent_id = "test_parent_id"
        store.nodes[parent_id] = {
            "id": parent_id,
            "name": "Test Parent",
            "kind": "month_driven",
            "parent_id": None,
            "children": ["existing_child_id"],
            "month_driven": True,
            "month_children": []
        }
        store.nodes["existing_child_id"] = {
            "id": "existing_child_id",
            "name": "July 2026",
            "kind": "month",
            "parent_id": parent_id,
            "children": []
        }
        
        duplicates = [
            "_July 2026_",
            "July2026",
            "july 2026",
            "July  2026",
            "'July 2026'",
            '"July 2026"',
            "July-2026",
            "july_2026",
            "_july 2026_",
            "=July 2026===",
            "[July 2026]",
            "(July 2026)",
        ]
        
        for duplicate_name in duplicates:
            with self.subTest(duplicate_name=duplicate_name):
                with self.assertRaises(Conflict) as context:
                    await backend.create_subfolder(parent_id, duplicate_name)
                self.assertIn("already exists here (ignoring casing, spaces, and special characters)", str(context.exception))

    async def test_stub_backend_folder_no_letters_validation(self):
        """Test BadRequest is raised if a folder name contains no alphabetic letters."""
        backend = StubBackend()
        from app.store import store
        
        parent_id = "test_parent_id"
        store.nodes[parent_id] = {
            "id": parent_id,
            "name": "Test Parent",
            "kind": "month_driven",
            "parent_id": None,
            "children": [],
            "month_driven": True,
            "month_children": []
        }
        
        invalid_names = [
            "2026",
            "===2026===",
            "123",
            "  ",
            "",
            "===",
        ]
        
        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaises(BadRequest) as context:
                    await backend.create_subfolder(parent_id, name)
                self.assertTrue("required" in str(context.exception).lower() or "alphabetic" in str(context.exception).lower())

if __name__ == "__main__":
    unittest.main()
