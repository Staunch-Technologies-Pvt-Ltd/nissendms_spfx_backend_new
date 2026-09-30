import unittest
from unittest.mock import AsyncMock, patch

from app.migration_assistant.classifier.taxonomy_classifier import (
    classify_document,
    load_destination_taxonomy,
    resolve_category_path,
)
from app.migration_assistant.services import migration_tagging


class TaxonomyClassifierTests(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_destination_taxonomy(
            [
                "Drawings/Hull",
                "Drawings/Piping",
                "Drawings/Electrical",
                "Drawings/Fire Protection",
                "Manuals/Main Engine",
                "Manuals/Fire Fighting",
                "Manuals/Safety",
                "Manuals/Operations",
            ]
        )

    def test_clear_drawing_document(self):
        result = classify_document(
            filename="SS378_EE-12_Electrical_Single_Line.pdf",
            document_text="ELECTRICAL SINGLE LINE DIAGRAM FOR MAIN SWITCHBOARD AND DISTRIBUTION PANELS. POWER DISTRIBUTION CIRCUITS AND CONTROL CABLING.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["group"], "Drawing")
        self.assertEqual(result["category"], "Electrical")
        self.assertEqual(result["vessel"], "PEISSY")
        self.assertTrue(result["destination_path"].startswith("Drawings/"))
        self.assertEqual(result["status"], "classified")

    def test_clear_manual_document(self):
        result = classify_document(
            filename="SS378_ME-01_MAIN_ENGINE_MANUAL.pdf",
            document_text="MAIN ENGINE OPERATION AND MAINTENANCE MANUAL. START-UP, SHUTDOWN, LUBRICATION SYSTEM, COOLING WATER, AND TROUBLESHOOTING.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["group"], "Manual")
        self.assertEqual(result["category"], "Main Engine")
        self.assertEqual(result["vessel"], "PEISSY")
        self.assertEqual(result["status"], "classified")

    def test_similar_but_not_identical_category_is_selected(self):
        result = classify_document(
            filename="SS378_MAINTENANCE_GUIDE.pdf",
            document_text="ENGINE ROOM MAIN PROPULSION UNIT OVERHAUL AND ROUTINE MAINTENANCE GUIDE. INSPECTION, ALIGNMENT, COOLING, LUBRICATION, AND TROUBLESHOOTING.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["group"], "Manual")
        self.assertEqual(result["category"], "Main Engine")
        self.assertGreaterEqual(result["category_confidence"], 0.6)

    def test_contextual_matching_selects_correct_category(self):
        taxonomy = load_destination_taxonomy(
            [
                "Drawings/Hull",
                "Drawings/Piping",
                "Drawings/Electrical",
                "Manuals/Main Engine",
                "Manuals/Fire Fighting",
                "Manuals/Safety",
                "Manuals/Operations",
            ]
        )
        result = classify_document(
            filename="BYPASS-VALVE-ARRANGEMENT.pdf",
            document_text="FIRE FIGHTING WATER MAIN AND DELUGE SYSTEM LAYOUT. EMERGENCY PUMP ARRANGEMENT, SPRINKLER CONTROL VALVES, AND ZONE DISTRIBUTION PLAN.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=taxonomy,
        )
        self.assertEqual(result["group"], "Manual")
        self.assertEqual(result["category"], "Fire Fighting")

    def test_low_group_evidence_uses_best_valid_group(self):
        result = classify_document(
            filename="random-note.pdf",
            document_text="A meeting note about office work and administration tasks for shipping operations staff.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["status"], "classified")
        self.assertIn(result["group"], ("Drawing", "Manual"))
        self.assertIsNotNone(result["category"])
        self.assertIn("selected", result["reason"].lower())

    def test_low_category_evidence_uses_best_valid_category(self):
        taxonomy = load_destination_taxonomy(
            [
                "Drawings/Hull",
                "Drawings/Piping",
                "Drawings/Electrical",
                "Manuals/Main Engine",
                "Manuals/Fire Fighting",
                "Manuals/Safety",
            ]
        )
        result = classify_document(
            filename="uncertain-drawing.pdf",
            document_text="A generic memo on required review and approval process for records handling across the vessel.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=taxonomy,
        )
        self.assertEqual(result["status"], "classified")
        self.assertIn(result["group"], ("Drawing", "Manual"))
        self.assertIn(result["category"], self.taxonomy[result["group"]])
        self.assertIn("closest valid", result["category_reason"].lower())

    def test_existing_vessel_is_used(self):
        result = classify_document(
            filename="SS378_ELECTRICAL-LOAD-DIAGRAM.pdf",
            document_text="ELECTRICAL LOAD DIAGRAM FOR SWITCHBOARD, GENERATOR, AND DISTRIBUTION NETWORK.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY", "MS-204"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["vessel"], "PEISSY")

    def test_new_vessel_uses_source_folder_name(self):
        result = classify_document(
            filename="SS378-PEISSY-ELECTRICAL-DIAGRAM.pdf",
            document_text="ELECTRICAL SINGLE LINE DIAGRAM FOR MAIN SWITCHBOARD AND DISTRIBUTION PANELS.",
            source_folder="SS378-PEISSY",
            destination_vessels=["OTHER_VESSEL"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["vessel"], "SS378-PEISSY")
        self.assertEqual(result["vessel_source"], "source_folder")
        self.assertEqual(result["status"], "classified")

    def test_multiple_vessels_are_processed_per_file(self):
        results = [
            classify_document(
                filename="A1.pdf",
                document_text="ELECTRICAL DISTRIBUTION DIAGRAM FOR SWITCHBOARD.",
                source_folder="VesselA",
                destination_vessels=["VesselA", "VesselB"],
                taxonomy=self.taxonomy,
            ),
            classify_document(
                filename="B2.pdf",
                document_text="MAIN ENGINE OPERATION AND MAINTENANCE MANUAL.",
                source_folder="VesselB",
                destination_vessels=["VesselA", "VesselB"],
                taxonomy=self.taxonomy,
            ),
        ]
        self.assertEqual(results[0]["vessel"], "VesselA")
        self.assertEqual(results[1]["vessel"], "VesselB")
        self.assertEqual(results[0]["group"], "Drawing")
        self.assertEqual(results[1]["group"], "Manual")

    def test_tagging_verification_requires_metadata_match(self):
        result = classify_document(
            filename="SS378_FIRE_ALARM_MANUAL.pdf",
            document_text="FIRE FIGHTING SYSTEM MANUAL INCLUDING ALARM PANEL, DETECTION SYSTEM, AND EMERGENCY PROCEDURES.",
            source_folder="SS378-PEISSY",
            destination_vessels=["PEISSY"],
            taxonomy=self.taxonomy,
        )
        self.assertEqual(result["group"], "Manual")
        self.assertEqual(result["category"], "Fire Fighting")
        self.assertIn("vessel", result)

    def test_live_wrapper_path_is_authoritative(self):
        paths = [
            "Drawings and Manuals/Drawings/Electrical",
            "Drawings and Manuals/Manuals/Main Engine",
        ]
        taxonomy = load_destination_taxonomy(paths)
        result = classify_document(
            filename="MAIN_ENGINE_MANUAL.pdf",
            document_text="MAIN ENGINE OPERATION AND MAINTENANCE MANUAL.",
            source_folder="SS378-PEISSY",
            destination_vessels=["SS378-PEISSY"],
            taxonomy=taxonomy,
            selected_vessel="SS378-PEISSY",
        )
        self.assertEqual(
            resolve_category_path(result["group"], result["category"], paths),
            "Drawings and Manuals/Manuals/Main Engine",
        )


class TaggingVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tags_resolve_write_and_verify(self):
        tags = {"group": "Manual", "category": "Main Engine", "vessel": "SS378-PEISSY"}
        settings_patch = patch.multiple(
            migration_tagging.settings,
            group_field_name="Group", group_term_set_id="group-set",
            category_field_name="Category", category_term_set_id="category-set",
            vessel_field_name="Vessel", vessel_term_set_id="vessel-set",
        )
        with settings_patch, patch.object(
            migration_tagging.term_store, "find_term",
            new=AsyncMock(side_effect=lambda _set, term_id, label: {"id": f"{label}-guid", "label": label}),
        ), patch.object(
            migration_tagging.sprest, "validate_update_list_item", new=AsyncMock(return_value=[])
        ), patch.object(
            migration_tagging.sprest, "get_taxonomy_field_value",
            new=AsyncMock(side_effect=lambda _url, _title, _id, field: {
                "term_id": "guid", "label": {"Group": "Manual", "Category": "Main Engine", "Vessel": "SS378-PEISSY"}[field]
            }),
        ):
            written = await migration_tagging.apply_term_tags("https://tenant", "Docs", 7, tags)
            verified = await migration_tagging.verify_term_tags("https://tenant", "Docs", 7, tags)

        self.assertTrue(all(entry["status"] == "applied" for entry in written))
        self.assertTrue(all(entry["status"] == "verified" for entry in verified))

    async def test_verification_mismatch_is_not_successful(self):
        tags = {"group": "Drawing", "category": "Electrical", "vessel": "SS378-PEISSY"}
        settings_patch = patch.multiple(
            migration_tagging.settings,
            group_field_name="Group", group_term_set_id="group-set",
            category_field_name="Category", category_term_set_id="category-set",
            vessel_field_name="Vessel", vessel_term_set_id="vessel-set",
        )
        with settings_patch, patch.object(
            migration_tagging.sprest, "get_taxonomy_field_value",
            new=AsyncMock(return_value={"term_id": "guid", "label": "Wrong Value"}),
        ):
            verified = await migration_tagging.verify_term_tags("https://tenant", "Docs", 7, tags)

        self.assertTrue(all(entry["status"] == "error" for entry in verified))


if __name__ == "__main__":
    unittest.main()
