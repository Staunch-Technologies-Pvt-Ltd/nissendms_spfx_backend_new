import sys
import unittest
from app.ocr.drawing_category import (
    normalize_ocr_text,
    is_text_usable_for_classification,
    classify_all_fields_tiered,
    VESSEL_MASTER_LIST,
)
from app.ocr.extract import (
    normalize_ocr_text as extract_normalize_ocr_text,
    is_text_usable_for_classification as extract_is_text_usable,
)


class TestGarbledIndexRegression(unittest.TestCase):
    def setUp(self):
        self.garbled_index_text = "I N D E X 7-1 7-2 12 13 6 10 11"
        self.filename = "2-1_RESULTS OF OFFICIAL SHOP TEST_C50LSH-250129T1.pdf"

    def test_normalize_ocr_text(self):
        """(a) normalize_ocr_text() correctly rejoins wide single-character spacing."""
        normalized = normalize_ocr_text(self.garbled_index_text)
        self.assertEqual(normalized, "INDEX 7-1 7-2 12 13 6 10 11")

        # Also test extract.py version
        extracted_norm = extract_normalize_ocr_text(self.garbled_index_text)
        self.assertEqual(extracted_norm, "INDEX 7-1 7-2 12 13 6 10 11")

    def test_text_quality_gate(self):
        """(b) is_text_usable_for_classification() flags noisy index/fragment text as unusable."""
        self.assertFalse(is_text_usable_for_classification(self.garbled_index_text))
        self.assertFalse(extract_is_text_usable(self.garbled_index_text))

        # Real multi-word document text should pass
        real_text = "TECHNICAL & CREWING\nPEISSY\nS.No.SS378\nARRANGEMENT OF ELECTRIC EQUIPMENT (WHEELHOUSE)"
        self.assertTrue(is_text_usable_for_classification(real_text))

    def test_unusable_text_vessel_needs_review(self):
        """(c) If no usable vessel signal is found, vessel confidence is < 60% (Needs Review),
        never falsely guessing or defaulting to any specific vessel name.
        """
        # Test without vessel in path -> vessel must be empty string and path has {vessel} placeholder
        result_no_path = classify_all_fields_tiered(
            self.garbled_index_text,
            filename=self.filename,
            known_vessels=VESSEL_MASTER_LIST,
            source_path="",
        )
        self.assertEqual(result_no_path["vessel"]["value"], "")
        self.assertEqual(result_no_path["vessel"]["confidence"], 0.0)
        self.assertNotIn("Belle Lune", result_no_path["suggested_path"])
        self.assertIn("{vessel}", result_no_path["suggested_path"])

        # Test with source path containing a vessel
        result = classify_all_fields_tiered(
            self.garbled_index_text,
            filename=self.filename,
            known_vessels=VESSEL_MASTER_LIST,
            source_path="Technical & Crewing/Belle Lune",
        )

        vessel_res = result["vessel"]
        self.assertLess(vessel_res["confidence"], 0.60, f"Vessel confidence {vessel_res['confidence']} should be < 0.60 (Needs Review)")
        
        # Overall document confidence should be below auto-fill threshold (< 0.85)
        self.assertLess(result["overall_confidence"], 0.85)

        # But category & subcategory from filename are preserved
        self.assertEqual(result["category"]["value"], "Machinery")
        self.assertEqual(result["sub_category"]["value"], "Test Record of Official Sea Trial")
        self.assertEqual(result["group"]["value"], "Drawing")


if __name__ == "__main__":
    unittest.main()
