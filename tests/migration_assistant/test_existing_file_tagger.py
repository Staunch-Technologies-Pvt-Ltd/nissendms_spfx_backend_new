import unittest

from app.migration_assistant.services.existing_file_tagger import _derive_tags


class ExistingFileTaggerPathTests(unittest.TestCase):
    def test_drawing_category_vessel(self):
        tags, error = _derive_tags(
            "Technical and Crewing/SS378-PEISSY/Drawing/Electrical",
            "Technical and Crewing",
        )
        self.assertIsNone(error)
        self.assertEqual(tags, {"group": "Drawing", "category": "Electrical", "vessel": "SS378-PEISSY"})

    def test_manual_category_from_vessel_root(self):
        tags, error = _derive_tags(
            "Technical and Crewing/SS378-PEISSY/Manual/Maintenance",
            "Technical and Crewing/SS378-PEISSY",
        )
        self.assertIsNone(error)
        self.assertEqual(tags, {"group": "Manual", "category": "Maintenance", "vessel": "SS378-PEISSY"})

    def test_nested_wrappers_use_deepest_folder_as_category(self):
        tags, error = _derive_tags(
            "Technical and Crewing/SS378-PEISSY/Drawings and Manuals/Drawings/Hull/Structural",
            "Technical and Crewing",
        )
        self.assertIsNone(error)
        self.assertEqual(tags, {"group": "Drawing", "category": "Structural", "vessel": "SS378-PEISSY"})

    def test_multiple_files_share_same_folder_mapping(self):
        paths = [
            "Technical and Crewing/Vessel A/Drawing/Electrical",
            "Technical and Crewing/Vessel A/Drawing/Electrical",
        ]
        self.assertEqual(_derive_tags(paths[0], "Technical and Crewing")[0], _derive_tags(paths[1], "Technical and Crewing")[0])

    def test_invalid_structure_is_not_tagged(self):
        tags, error = _derive_tags("Technical and Crewing/Vessel A/Loose Files", "Technical and Crewing")
        self.assertIsNone(tags)
        self.assertEqual(error, "Unable to determine tags from folder path")

    def test_recent_scan_summary_includes_counts_needed_for_ui(self):
        from app.migration_assistant.services.existing_file_tagger import _recent_scan_summary

        payload = {
            "root_path": "Technical and Crewing",
            "summary": {"total_files": 3, "ready_files": 2, "missing_taxonomy": 1, "missing_terms": 1},
            "files": [{"ready": True}, {"ready": False}, {"ready": True}],
        }

        summary = _recent_scan_summary(payload)
        self.assertEqual(summary["root_path"], "Technical and Crewing")
        self.assertEqual(summary["total_files"], 3)
        self.assertEqual(summary["ready_files"], 2)
        self.assertEqual(summary["missing_terms"], 1)


if __name__ == "__main__":
    unittest.main()
