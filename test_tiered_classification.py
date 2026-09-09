import sys
sys.path.insert(0, 'c:/sharepoint spfx/backend')

from app.ocr.drawing_category import (
    classify_all_fields_tiered,
    VESSEL_MASTER_LIST,
)

def run_tests():
    print("=================================================================")
    print(" RUNNING TWO-TIER FIELD EXTRACTION & DEPARTMENT SCOPING TESTS")
    print(" (Path ends at Category; Sub-Category is metadata tag only)")
    print("=================================================================")

    tests = [
        {
            "name": "Bow Fighter ODM Control System Manual (Hull N-2119 & IMO 1054292 in text)",
            "fn": "NK-23_N-2119_GH-3030_O.D.M. CONTROL SYSTEM OPERATION MANUAL (R.O.B. MANUAL).pdf",
            "text": "PRINCIPAL PARTICULARS\nName of ship: BOW FIGHTER\nHull number: N-2119\nIMO No.: 1054292\nNISSEN KAIUN SINGAPORE PTE. LTD.",
            "path": "",
            "expected": {
                "vessel": "Bow Fighter",
                "department": "Technical & Crewing",
                "group": "Manual",
            },
            "expected_path": "Technical & Crewing/Bow Fighter/Drawings and Manuals/To be Classified",
            "min_conf": {
                "vessel": 0.95,
                "department": 0.70,
                "group": 0.85,
            }
        },
        {
            "name": "Exact IMO Number Match (IMO 9876543)",
            "fn": "Safety_Plan_9876543.pdf",
            "text": "FIRE CONTROL PLAN\nIMO 9876543\nSAFETY DRAWINGS",
            "path": "",
            "known_vessels_override": [
                {"name": "Norse Evolution", "imo": "9876543", "hull_number": "NE-501"}
            ],
            "expected": {
                "vessel": "Norse Evolution",
                "department": "Technical & Crewing",
                "group": "Drawing",
                "category": "Safety",
                "sub_category": "Fire Control Plan",
            },
            "expected_path": "Technical & Crewing/Norse Evolution/Drawings and Manuals/Drawing/Safety",
            "min_conf": {
                "vessel": 0.95,
                "department": 0.70,
                "group": 0.85,
                "category": 0.85,
                "sub_category": 0.85,
            }
        },
        {
            "name": "Exact Hull Number Match (Hull H-501)",
            "fn": "Midship_Section_H501.pdf",
            "text": "MIDSHIP SECTION\nHULL NO. H-501\nHULL DRAWINGS",
            "path": "",
            "known_vessels_override": [
                {"name": "Norse Evolution", "imo": "9876543", "hull_number": "H-501"}
            ],
            "expected": {
                "vessel": "Norse Evolution",
                "department": "Technical & Crewing",
                "group": "Drawing",
                "category": "Hull",
                "sub_category": "Midship Section",
            },
            "expected_path": "Technical & Crewing/Norse Evolution/Drawings and Manuals/Drawing/Hull",
            "min_conf": {
                "vessel": 0.95,
                "department": 0.70,
                "group": 0.85,
                "category": 0.85,
                "sub_category": 0.85,
            }
        },
        {
            "name": "Ghana Express Shell Expansion (SNo.721 HO-3 in filename & text)",
            "fn": "SNo.721 HO-3 SHELL EXPANSION.pdf",
            "text": "SHIP No. 721\nSHIP NAME GHANA EXPRESS\nSHELL EXPANSION\nFINISHED PLAN\nHO-3\nKITANIHON SHIPBUILDING",
            "path": "",
            "expected": {
                "vessel": "Ghana Express",
                "department": "Technical & Crewing",
                "group": "Drawing",
                "category": "Hull",
                "sub_category": "Shell Expansion",
            },
            "expected_path": "Technical & Crewing/Ghana Express/Drawings and Manuals/Drawing/Hull",
            "min_conf": {
                "vessel": 0.85,
                "department": 0.70,
                "group": 0.85,
                "category": 0.85,
                "sub_category": 0.85,
            }
        },
        {
            "name": "Peissy Wheelhouse Equipment (Hull SS378 in filename & text)",
            "fn": "SS378_FR-11_ARRANGEMENT OF ELECTRIC EQUIPMENT (WHEELHOUSE).pdf",
            "text": "PEISSY\nS.No.SS378\nARRANGEMENT OF ELECTRIC EQUIPMENT (WHEELHOUSE)\nTECHNICAL & CREWING",
            "path": "Technical & Crewing/Peissy/Drawings and Manuals",
            "expected": {
                "vessel": "Peissy",
                "department": "Technical & Crewing",
                "group": "Drawing",
                "category": "Electrical",
                "sub_category": "Power Distribution Diagram",
            },
            "expected_path": "Technical & Crewing/Peissy/Drawings and Manuals/Drawing/Electrical",
            "min_conf": {
                "vessel": 0.85,
                "department": 0.85,
                "group": 0.85,
                "category": 0.85,
                "sub_category": 0.85,
            }
        },
        {
            "name": "Belle Lune Bulkhead Drawing",
            "fn": "SS268 240200-ARR. OF JOINER BHD..pdf",
            "text": "BELLE LUNE\nHULL SS268\nBULKHEAD PLANS\nDWG NO: 240200",
            "path": "",
            "expected": {
                "vessel": "Belle Lune",
                "department": "Technical & Crewing",
                "group": "Drawing",
                "category": "Hull",
                "sub_category": "Bulkhead plans",
            },
            "expected_path": "Technical & Crewing/Belle Lune/Drawings and Manuals/Drawing/Hull",
            "min_conf": {
                "vessel": 0.85,
                "department": 0.70,
                "group": 0.85,
                "category": 0.85,
                "sub_category": 0.85,
            }
        },
        {
            "name": "Commercial & Chartering Department File Fallback (Sub-floor Vessel Blanked)",
            "fn": "Unclassified_Charter_Document.pdf",
            "text": "CHARTER PARTY AGREEMENT 2026",
            "path": "Commercial & Chartering/Belle Lune/Voyage Documents",
            "expected": {
                "vessel": "",
                "department": "Commercial & Chartering",
                "group": "Drawing",
                "category": "To Be Classified",
                "sub_category": "To Be Classified",
            },
            "expected_path": "Commercial & Chartering/{vessel}/Drawings and Manuals/To be Classified",
            "min_conf": {
                "department": 0.85,
            }
        },
        {
            "name": "Commercial & Chartering Department with Filename Vessel Match",
            "fn": "Belle Lune - Charter Party Agreement 2026.pdf",
            "text": "CHARTER PARTY AGREEMENT 2026",
            "path": "Commercial & Chartering/Belle Lune/Voyage Documents",
            "expected": {
                "vessel": "Belle Lune",
                "department": "Commercial & Chartering",
                "group": "Drawing",
                "category": "To Be Classified",
                "sub_category": "To Be Classified",
            },
            "expected_path": "Commercial & Chartering/Belle Lune/Drawings and Manuals/To be Classified",
            "min_conf": {
                "vessel": 0.85,
                "department": 0.85,
            }
        },
        {
            "name": "Bow Fraternity Single Line Diagram",
            "fn": "Bow Fraternity - Single Line Diagram.pdf",
            "text": "BOW FRATERNITY\nSINGLE LINE DIAGRAM\n440V MAIN POWER DISTRIBUTION",
            "path": "",
            "expected": {
                "vessel": "Bow Fraternity",
                "group": "Drawing",
                "category": "Electrical",
                "sub_category": "Single Line Diagram",
            },
            "expected_path": "Technical & Crewing/Bow Fraternity/Drawings and Manuals/Drawing/Electrical",
            "min_conf": {
                "vessel": 0.85,
                "group": 0.85,
                "category": 0.85,
                "sub_category": 0.85,
            }
        },
    ]

    passed = 0
    total = len(tests)

    for t in tests:
        res = classify_all_fields_tiered(
            t["text"],
            filename=t["fn"],
            known_vessels=t.get("known_vessels_override") or VESSEL_MASTER_LIST,
            source_path=t.get("path", ""),
        )

        print(f"\nTest: {t['name']}")
        print(f"  Vessel:       {res['vessel']['value']} (conf={res['vessel']['confidence']}, tier={res['vessel']['tier']})")
        print(f"  Department:   {res['department']['value']} (conf={res['department']['confidence']}, tier={res['department']['tier']})")
        print(f"  Group:        {res['group']['value']} (conf={res['group']['confidence']}, tier={res['group']['tier']})")
        print(f"  Category:     {res['category']['value']} (conf={res['category']['confidence']}, tier={res['category']['tier']})")
        print(f"  Sub-Category: {res['sub_category']['value']} (conf={res['sub_category']['confidence']}, tier={res['sub_category']['tier']})")
        print(f"  Path:         {res['suggested_path']}")

        mismatch = False
        for field, exp_val in t["expected"].items():
            act_val = res[field]["value"]
            if act_val != exp_val:
                print(f"  [FAIL] Mismatch in '{field}': expected '{exp_val}', got '{act_val}'")
                mismatch = True

        if "expected_path" in t:
            if res["suggested_path"] != t["expected_path"]:
                print(f"  [FAIL] Mismatch in 'suggested_path': expected '{t['expected_path']}', got '{res['suggested_path']}'")
                mismatch = True

        for field, min_c in t.get("min_conf", {}).items():
            act_c = res[field]["confidence"]
            if act_c < min_c:
                print(f"  [FAIL] Low confidence in '{field}': expected >= {min_c}, got {act_c}")
                mismatch = True

        if not mismatch:
            print("  [PASS]")
            passed += 1

    print(f"\n=================================================================")
    print(f" SUMMARY: {passed}/{total} tests passed.")
    print("=================================================================")
    if passed != total:
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
