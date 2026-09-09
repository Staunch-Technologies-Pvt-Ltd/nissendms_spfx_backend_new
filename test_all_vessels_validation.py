"""
test_all_vessels_validation.py
"""
import sys
sys.path.insert(0, r"c:\sharepoint spfx\backend")
from app.ocr.drawing_category import classify_document_content, VESSEL_MASTER_LIST

# Format: (filename, vessel, group, category, sub_category)
# Group = Drawing / Manual
# Category = Basic / Cargo / Electrical / Hull / Safety / etc.
# Sub-Category = specific term
test_cases = [
    ("Belle Lune - General Arrangement.pdf", "Belle Lune", "Drawing", "Basic", "General Arrangement"),
    ("Bow Fighter Cargo Pump Manual.pdf", "Bow Fighter", "Manual", "Cargo", "Cargo Pump Manual"),
    ("Bow Fraternity Single Line Diagram.pdf", "Bow Fraternity", "Drawing", "Electrical", "Single Line Diagram"),
    ("Cameroun Express BWTS Manual.pdf", "Cameroun Express", "Manual", "Pollution", "BWTS Manual"),
    ("Cecilie F Midship Section.pdf", "Cecilie F", "Drawing", "Hull", "Midship Section"),
    ("Cote D'Ivoire Express Emergency Generator.pdf", "Cote D Ivoire Express", "Manual", "Safety", "Emergency Generator Manual"),
    ("Duchess Emerald Docking Plan.pdf", "Dutches Emerald", "Drawing", "Basic", "Docking Plan"),
    ("Ghana Express Rudder.pdf", "Ghana Express", "Drawing", "Hull", "Rudder and Rudder stock"),
    ("Lignum Grid Fire Control Plan.pdf", "Lignum Grid", "Drawing", "Safety", "Fire Control Plan"),
    ("Lignum Mesh Main Switchboard Manual.pdf", "Lignum Mesh", "Manual", "Electrical", "Main Switchboard Manual"),
    ("Lignum Web Boiler Manual.pdf", "Lignum Web", "Manual", "Boiler", "Operation & Maintenance Manual"),
    ("Maersk El Banco Main Engine.pdf", "Maersk EI Banco", "Manual", "Main Engine", "Operation & Maintenance Manual"),
    ("Maersk El Palomar Alarm System.pdf", "Maersk EI Palomar", "Manual", "Automation", "Alarm Monitoring System"),
    ("Maersk Ferrato Superstructure.pdf", "Maersk Ferrato", "Drawing", "Hull", "Superstructure"),
    ("Maersk Finisterre Mooring Arrangement.pdf", "Maersk Finisterre", "Drawing", "Hull", "Mooring Arrangement"),
    ("Maersk Frio AC Plant Manual.pdf", "Maersk Frio", "Manual", "Refrigeration", "AC Plant Manual"),
    ("Norse Evolution Windlass Manual.pdf", "Norse Evolution", "Manual", "Deck Machinery", "Windlass Manual"),
    ("Norse Ijmuiden Shaft Generator.pdf", "Norse Ijmuiden", "Manual", "Propulsion", "Shaft Generator Manual"),
    ("Norse New Haven Steering Gear.pdf", "Norse New Haven", "Manual", "Steering Gear", "Operation Manual"),
    ("Peissy Thrusters Manual.pdf", "Peissy", "Manual", "Thrusters", "Operation & Maintenance Manual"),
    ("Potiniere Bulkhead plans.pdf", "Potiniere", "Drawing", "Hull", "Bulkhead plans"),
    ("Senegal Express Capacity Plan.pdf", "Senegal Express", "Drawing", "Basic", "Capacity Plan & Dead Weight"),
    ("Snowflake Life Saving Appliances.pdf", "Snow Flake", "Drawing", "Safety", "Life Saving Appliances Plan"),
    ("Snowflower Cargo Crane Manual.pdf", "Snow Flower", "Manual", "Cargo", "Cargo Crane Manual"),
    ("Belle Lune Random Unclassified Document.pdf", "Belle Lune", "Manual", "To Be Classified", "To Be Classified"),
]

passed = 0
for fn, exp_v, exp_grp, exp_cat, exp_sub in test_cases:
    res = classify_document_content("", fn, VESSEL_MASTER_LIST)
    v_ok = res.get("vessel_name") == exp_v
    grp_ok = res.get("group") == exp_grp
    cat_ok = res.get("category") == exp_cat
    sub_ok = res.get("sub_category") == exp_sub
    if v_ok and grp_ok and cat_ok and sub_ok:
        passed += 1
        print(f"[PASS] {fn} -> {res.get('vessel_name')} | Group: {res.get('group')} | Category: {res.get('category')} | SubCat: {res.get('sub_category')}")
    else:
        print(f"[FAIL] {fn}")
        print(f"  Got: vessel={res.get('vessel_name')}, group={res.get('group')}, category={res.get('category')}, sub_category={res.get('sub_category')}")
        print(f"  Exp: vessel={exp_v}, group={exp_grp}, category={exp_cat}, sub_category={exp_sub}")

print(f"\nTotal passed: {passed} / {len(test_cases)}")
if passed != len(test_cases):
    sys.exit(1)
