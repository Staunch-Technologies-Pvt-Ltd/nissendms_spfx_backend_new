import unittest
from app.ocr.drawing_category import (
    _classify_vessel_tiered,
    classify_all_fields_tiered,
    extract_vessel_name_from_text,
    _match_known_vessel,
    VESSEL_MASTER_LIST,
    VESSEL_CONFIDENCE_FLOOR,
)


class TestVesselFormLabelRegression(unittest.TestCase):
    """Regression test suite ensuring form field labels and section headers
    (e.g., 'Shipyard Name', 'Owner', '2. Application', 'S. The Temperature Sensor')
    are never extracted as vessel names, and that folder path breadcrumbs
    (e.g., 'Snow Flower/Incidents/Level Gauge') correctly surface as Tier 2 suggestions.
    """

    def test_known_bad_form_labels_rejected(self):
        """Assert NONE of the known-bad extracted strings are ever returned as a vessel
        match at any confidence level when fed through _classify_vessel_tiered().
        """
        known_bad_cases = [
            ("Shipyard Name", "Ship's Name: Shipyard Name\nTechnical Specification\nLevel Gauge"),
            ("Owner", "Name of Ship: Owner\nLoading Computer System\nOperation Manual"),
            ("2. Application", "Vessel: 2. Application\nLevel Watch System Type 2\nInstallation Manual"),
            ("S. The Temperature Sensor", "Ship Name: S. The Temperature Sensor\nTemperature Monitoring Unit"),
            ("Shipyard Name", "Vessel Name: Shipyard Name: XYZ Corp\nModel Level Aeran X"),
            ("Owner", "Ship's Name: Owner's Spec 2024\nManual"),
            ("2. Application", "Name of Vessel: 2. Application Scope\nTechnical Manual"),
            ("S. The Temperature Sensor", "vessel : S. The Temperature Sensor unit 4"),
        ]

        for bad_label, text in known_bad_cases:
            # Direct _match_known_vessel check
            self.assertIsNone(
                _match_known_vessel(bad_label, VESSEL_MASTER_LIST),
                f"'{bad_label}' must not match known vessels"
            )

            # _classify_vessel_tiered check with text
            res = _classify_vessel_tiered(
                text=text,
                filename="USER'S MANUAL_LEVEL WATCH.pdf",
                known_vessels=VESSEL_MASTER_LIST,
                source_path="",
            )
            self.assertNotEqual(
                res["value"],
                bad_label,
                f"'{bad_label}' was incorrectly extracted as vessel name!"
            )
            self.assertEqual(
                res["value"],
                "",
                f"Expected empty vessel for non-vessel text, got '{res['value']}'"
            )
            self.assertEqual(res["confidence"], 0.0)

            # extract_vessel_name_from_text check
            ext_res = extract_vessel_name_from_text(
                text=text,
                filename="USER'S MANUAL_LEVEL WATCH.pdf",
                known_vessels=VESSEL_MASTER_LIST,
            )
            self.assertIsNone(
                ext_res,
                f"extract_vessel_name_from_text incorrectly extracted '{ext_res}' from text containing '{bad_label}'"
            )

    def test_live_sharepoint_files_snow_flower_folder_inference(self):
        """Confirm files uploaded into 'Snow Flower/Incidents/Level Gauge' folder path,
        with form field labels in OCR text and generic filenames, resolve vessel confidence
        to 0.45 (Tier 2 folder path inference). Under the 60% confidence floor rule, the final
        vessel value in suggested_tags/classification MUST BE BLANK ("") so it routes to
        Needs Review rather than displaying a low-confidence pre-fill.
        """
        live_files = [
            ("USER'S MANUAL_LEVEL AERAN X.pdf", "Ship's Name: Shipyard Name\nLEVEL AERAN X\nUSER MANUAL"),
            ("USER'S MANUAL_LEVEL WATCH TYPE 2 A.pdf", "Vessel: 2. Application\nLEVEL WATCH TYPE 2 A\nUSER MANUAL"),
            ("USER'S MANUAL_LOADING MONITOR.pdf", "Name of Ship: Owner\nLOADING MONITOR\nUSER MANUAL"),
            ("USER'S MANUAL_TEMPERATURE GAUGE.pdf", "Ship Name: S. The Temperature Sensor\nTEMPERATURE GAUGE"),
            ("LEVEL ECHO X.pdf", "LEVEL ECHO X\nSPECIFICATION SHEET"),
            ("LEVEL SWITCH ACE.pdf", "LEVEL SWITCH ACE\nOPERATING INSTRUCTIONS"),
        ]

        folder_path = "Snow Flower/Incidents/Level Gauge"

        for fn, text in live_files:
            # 1. Check raw tiered extraction resolves to Snow Flower at 0.45
            raw_vessel = _classify_vessel_tiered(
                text=text,
                filename=fn,
                known_vessels=VESSEL_MASTER_LIST,
                source_path=folder_path,
            )
            self.assertEqual(
                raw_vessel["value"],
                "Snow Flower",
                f"Raw tiered extraction for '{fn}' should find 'Snow Flower' in path"
            )
            self.assertEqual(raw_vessel["confidence"], 0.45)
            self.assertEqual(raw_vessel["tier"], 2)

            # 2. Check full classification enforces the 60% confidence floor (blank value)
            res = classify_all_fields_tiered(
                text=text,
                filename=fn,
                known_vessels=VESSEL_MASTER_LIST,
                source_path=folder_path,
            )

            vessel_res = res["vessel"]
            self.assertEqual(
                vessel_res["value"],
                "",
                f"File '{fn}' with 45% confidence must have blank vessel value after floor enforcement! Got '{vessel_res['value']}'"
            )
            self.assertEqual(
                vessel_res["tier"],
                2,
                f"Folder-inferred vessel for '{fn}' should be Tier 2"
            )
            self.assertEqual(
                vessel_res["confidence"],
                0.45,
                f"Folder-inferred vessel confidence for '{fn}' must be preserved at 0.45 (Needs Review)"
            )
            self.assertIn(
                "{vessel}",
                res["suggested_path"],
                f"Suggested path for '{fn}' must use '{{vessel}}' placeholder when vessel is blanked"
            )

    def test_genuine_vessel_in_ocr_text_gets_tier1(self):
        """Confirm genuine vessel names in OCR text still get Tier 1 high confidence."""
        genuine_text = "SHIP'S NAME: M/V SNOW FLOWER\nHULL NO: 1234\nLEVEL GAUGE MANUAL"
        res = classify_all_fields_tiered(
            text=genuine_text,
            filename="USER'S MANUAL_LEVEL GAUGE.pdf",
            known_vessels=VESSEL_MASTER_LIST,
            source_path="",
        )
        self.assertEqual(res["vessel"]["value"], "Snow Flower")
        self.assertEqual(res["vessel"]["tier"], 1)
        self.assertGreaterEqual(res["vessel"]["confidence"], 0.85)

    def test_low_confidence_vessel_floor_blanked(self):
        """Regression: folder-path vessel inference (conf=0.45) must be blanked in
        classify_all_fields_tiered() because 0.45 < VESSEL_CONFIDENCE_FLOOR (0.60).
        The confidence value should be PRESERVED for diagnostics but the value cleared.
        """
        # Generic tech manual with no vessel signal in text or filename
        text = "LEVEL AERAN X\nUSER MANUAL\nOPERATING INSTRUCTIONS"
        filename = "USER'S MANUAL_LEVEL AERAN X.pdf"
        source_path = "Snow Flower/Incidents/Level Gauge"

        res = classify_all_fields_tiered(
            text=text,
            filename=filename,
            known_vessels=VESSEL_MASTER_LIST,
            source_path=source_path,
        )

        vessel = res["vessel"]
        # Value MUST be blank after floor enforcement
        self.assertEqual(
            vessel["value"], "",
            f"Expected blank vessel after floor enforcement, got '{vessel['value']}'"
        )
        # Confidence is preserved for diagnostic purposes
        self.assertEqual(
            vessel["confidence"], 0.45,
            f"Expected confidence=0.45 (capped folder-path), got {vessel['confidence']}"
        )
        self.assertEqual(vessel["tier"], 2)
        # Suggested path must NOT contain Snow Flower (vessel blanked)
        self.assertIn("{vessel}", res["suggested_path"],
            "Suggested path should contain '{{vessel}}' placeholder when vessel is blanked")
        self.assertNotIn("Snow Flower", res["suggested_path"])

    def test_senegal_express_45pct_blanked(self):
        """Regression: live production case — 'Shop test report of Main engine.pdf'
        uploaded in a 'Senegal Express/...' folder path.

        Previously the 45%-confidence folder inference was flowing through into
        suggested_tags, causing the UI to pre-fill 'Senegal Express' at 45%.
        After this fix, vessel must be blank and the item must route to needs_review.
        """
        text = "MAIN ENGINE SHOP TEST REPORT\nCYLINDER PRESSURE MEASUREMENTS\nMAX FIRING PRESSURE"
        filename = "Shop test report of Main engine.pdf"
        source_path = "Senegal Express/Technical & Crewing/Drawings and Manuals"

        res = classify_all_fields_tiered(
            text=text,
            filename=filename,
            known_vessels=VESSEL_MASTER_LIST,
            source_path=source_path,
        )

        vessel = res["vessel"]
        # MUST be blank: folder-path confidence 0.45 is below VESSEL_CONFIDENCE_FLOOR
        self.assertEqual(
            vessel["value"], "",
            f"Senegal Express at 45% must be blanked by floor, got '{vessel['value']}'"
        )
        # Confidence must equal the floor-capped path inference value
        self.assertLess(
            vessel["confidence"], VESSEL_CONFIDENCE_FLOOR,
            f"Vessel confidence {vessel['confidence']} should be < floor ({VESSEL_CONFIDENCE_FLOOR})"
        )
        # Suggested path must use placeholder
        self.assertIn("{vessel}", res["suggested_path"])
        self.assertNotIn("Senegal Express", res["suggested_path"])


if __name__ == "__main__":
    unittest.main()
