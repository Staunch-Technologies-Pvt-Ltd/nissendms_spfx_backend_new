import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import (
    _build_sharepoint_metadata_payload,
    _metadata_issue_reasons,
    _normalize_metadata_group,
)
from app.graph.client import GraphClient


class TestSharePointMetadataMapping(unittest.TestCase):
    def test_shifted_values_are_detected(self):
        reasons = _metadata_issue_reasons(
            department="Technical & Crewing",
            vessel="Senegal Express",
            group="Technical & Crewing",
            category="Senegal Express",
            sub_category="Technical Management",
        )
        self.assertIn("group_is_department", reasons)
        self.assertIn("category_equals_vessel", reasons)
        self.assertIn("category_not_in_taxonomy", reasons)

    def test_group_normalization_from_classifier_values(self):
        self.assertEqual(_normalize_metadata_group("Drawing", "Basic"), "Drawings")
        self.assertEqual(_normalize_metadata_group("Manual", "Main Engine"), "Manuals")
        self.assertEqual(_normalize_metadata_group("Drawing", "To be Classified"), "Drawings")
        self.assertEqual(_normalize_metadata_group("Manual", "Hull"), "Drawings")
        self.assertEqual(_normalize_metadata_group("Drawing", "Main Engine"), "Manuals")

    def test_payload_fields_are_distinct_and_correct(self):
        payload = _build_sharepoint_metadata_payload(
            department="Technical & Crewing",
            vessel="Senegal Express",
            group="Drawings",
            category="Basic",
            sub_category="Capacity Plan & Dead Weight",
        )

        self.assertEqual(payload["Department"], "Technical & Crewing")
        self.assertEqual(payload["VesselName"], "Senegal Express")
        self.assertEqual(payload["Group"], "Drawings")
        self.assertEqual(payload["Category"], "Basic")
        self.assertEqual(payload["SubCategory"], "Capacity Plan & Dead Weight")

        self.assertNotEqual(payload["Group"], payload["Department"])
        self.assertNotEqual(payload["Category"], payload["VesselName"])
        self.assertNotEqual(payload["SubCategory"], payload["Category"])

    def test_payload_keeps_group_category_subcategory_when_vessel_missing(self):
        payload = _build_sharepoint_metadata_payload(
            department="Technical & Crewing",
            vessel="",
            group="Manuals",
            category="To be Classified",
            sub_category="To be Classified",
        )

        self.assertEqual(payload["Department"], "Technical & Crewing")
        self.assertEqual(payload["VesselName"], "")
        self.assertEqual(payload["Group"], "Manuals")
        self.assertEqual(payload["Category"], "To be Classified")
        self.assertEqual(payload["SubCategory"], "To be Classified")

        # Alias keys should mirror primary values for resilient SP internal-name mapping.
        self.assertEqual(payload["group"], "Manuals")
        self.assertEqual(payload["category"], "To be Classified")
        self.assertEqual(payload["subcategory"], "To be Classified")

    def test_payload_with_vessel_only(self):
        payload = _build_sharepoint_metadata_payload(vessel="Norse Evolution")
        self.assertEqual(payload["VesselName"], "Norse Evolution")
        self.assertEqual(payload["Department"], "")
        self.assertEqual(payload["Group"], "")
        self.assertEqual(payload["Category"], "")
        self.assertNotIn("SubCategory", payload)
        filtered = {k: v for k, v in payload.items() if v}
        self.assertEqual(filtered.get("VesselName"), "Norse Evolution")
        self.assertNotIn("Department", filtered)
        self.assertNotIn("Group", filtered)
        self.assertNotIn("Category", filtered)

    def test_delegated_graph_token_overrides_app_only_token(self):
        headers = GraphClient._headers(None, access_token="delegated-user-token")

        self.assertEqual(headers["Authorization"], "Bearer delegated-user-token")

    def test_extract_vessel_from_path_and_folder_fallback(self):
        from app.main import _extract_vessel_from_path
        from app.ocr.drawing_category import _extract_vessel_from_folder_path

        # SC413 with "type of vessel" container segment before Drawings and Manuals
        p1 = "Technical/SC413/type of vessel/Drawings and Manuals/Drawings/ELECTRIC PART"
        self.assertEqual(_extract_vessel_from_folder_path(p1), "SC413")
        self.assertEqual(_extract_vessel_from_path(p1, []), "SC413")

        # Shared Documents root prefix
        p2 = "Shared Documents/Technical/SC413/type of vessel/Drawings and Manuals/Drawings/ELECTRIC PART"
        self.assertEqual(_extract_vessel_from_folder_path(p2), "SC413")
        self.assertEqual(_extract_vessel_from_path(p2, []), "SC413")

        # Peissy in path
        p3 = "Shared Documents/Technical/Peissy/Drawings and Manuals/Manuals/Other Manuals/Pipeline manuals"
        self.assertEqual(_extract_vessel_from_folder_path(p3), "Peissy")
        self.assertEqual(_extract_vessel_from_path(p3, []), "Peissy")

        # Generic path without vessel should return None
        p4 = "Shared Documents/Technical/Drawings and Manuals/Drawings/Basic"
        self.assertIsNone(_extract_vessel_from_folder_path(p4))
        self.assertIsNone(_extract_vessel_from_path(p4, []))

    def test_classify_vessel_tiered_folder_fallback_and_cues(self):
        from app.ocr.drawing_category import _classify_vessel_tiered

        path = "Technical/SC413/type of vessel/Drawings and Manuals/Drawings/ELECTRIC PART"

        # Document with S.NO. SC-413 text cue matches folder SC413
        t1 = _classify_vessel_tiered("TITLE BLOCK\nS.NO. SC-413", filename="EA-1 LIST OF FINISHED DRAWINGS (ELECTRIC PART).pdf", source_path=path)
        self.assertEqual(t1["value"], "SC413")
        self.assertGreaterEqual(t1["confidence"], 0.85)

        # Document without text cue falls back to folder SC413
        t2 = _classify_vessel_tiered("WATERTIGHT CABLE PENETRATION REGISTER", filename="EA-10 WATERTIGHT CABLE PENETRATION REGISTER.pdf", source_path=path)
        self.assertEqual(t2["value"], "SC413")
        self.assertGreaterEqual(t2["confidence"], 0.80)

    def test_drawings_and_manuals_august_and_mb_engine(self):
        from app.main import _extract_vessel_from_path
        from app.ocr.drawing_category import _extract_vessel_from_folder_path, classify_all_fields_tiered

        p = "Technical/Drawings and Manuals August/MB MAIN ENGINE"
        self.assertIsNone(_extract_vessel_from_folder_path(p))
        self.assertIsNone(_extract_vessel_from_path(p, []))

        # Test engine manual classification
        res = classify_all_fields_tiered(
            "MITSUBISHI UE DIESEL ENGINE OPERATION & DATA DRAWING NO 123",
            filename="MB-1 OPERATION & DATA.pdf",
            source_path=p,
        )
        self.assertEqual(res["group"]["value"], "Manual")
        self.assertEqual(res["category"]["value"], "Main Engine")
        self.assertEqual(res["sub_category"]["value"], "Operation & Maintenance Manual")
        self.assertEqual(res["vessel"]["value"], "")

    def test_machinery_spare_parts_tool_list_is_drawing(self):
        from app.ocr.drawing_category import classify_all_fields_tiered

        res = classify_all_fields_tiered(
            "PRINCIPAL PARTICULARS BOW FRATERNITY N-2120 SPARE PARTS & TOOL LIST (MACHINERY PART) FINISHED PLAN",
            filename="N-2120_M-51_SPARE PART'S & TOOL LIST (MACHINERY PART).pdf",
            source_path="Technical/Bow Fraternity/Drawings and Manuals/Drawings/Machinery",
        )
        self.assertEqual(res["group"]["value"], "Drawing")
        self.assertEqual(res["category"]["value"], "Machinery")
        self.assertEqual(res["sub_category"]["value"], "Machinery Makers List")

    def test_windows_thumbnail_database_is_unclassified(self):
        from app.ocr.drawing_category import classify_all_fields_tiered

        res = classify_all_fields_tiered(
            "binary thumbnail cache noise and drawing hull arrangement text",
            filename="Thumbs.db",
            source_path="Technical/Cameroun Express/Drawings and Manuals/Drawings/Hull",
        )
        self.assertEqual(res["category"]["value"], "To Be Classified")
        self.assertEqual(res["sub_category"]["value"], "To Be Classified")
        self.assertIn("To be Classified", res["suggested_path"])

    def test_folder_vessel_overrides_wrong_ocr_vessel(self):
        from app.ocr.drawing_category import classify_all_fields_tiered

        res = classify_all_fields_tiered(
            "SHIP NAME GHANA EXPRESS TEST RESULTS OF ANTI-HEELING SYSTEM ONBOARD FUNCTIONAL TEST",
            filename="SNo.718 FT-11-1 TEST RESULTS OF ANTI-HEELING SYSTEM ONBOARD FUNCTIONALTEST.pdf",
            source_path="Technical and Crewing New/Cameroun Express/Drawings and Manuals/Drawings/Test Results",
        )
        self.assertEqual(res["vessel"]["value"], "Cameroun Express")


if __name__ == "__main__":
    unittest.main()

