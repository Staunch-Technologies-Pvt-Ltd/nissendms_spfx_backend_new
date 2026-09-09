"""
test_taxonomy_classification.py
"""
import sys
sys.path.insert(0, r"c:\sharepoint spfx\backend")

from app.ocr.drawing_category import classify_document_content, VESSEL_MASTER_LIST

files = [
    "SS268 240200-ARR. OF JOINER BHD..pdf",
    "SS268 240300-ARR. OF INSULATION IN ACCOMM..pdf",
    "SS268 240400-DECK COVERING IN ACCOMM..pdf",
    "SS268 SOLAS CHECK LIST FOR LSA.pdf",
    "SS268 SOLAS TRAINING MANUAL.pdf",
    "SS268-HF-11-241000-LIST OF INVENTORY & LEGAL EQUIPMENT.pdf",
    "SS268-HW-2-240000-ACCOMMODATION ARRANGEMENT.pdf",
    "SS268-HW-3-240100-FIRE PROTECTION AND AIRBORNE SOUND INSULATION.pdf",
    "SS268-HW-4-240500-ARR.OF LIFE SAVING EQUIPMENT.pdf",
    "SS268-HW-5-203210-TEST RESULT OF NOISE MEASUREMENT.pdf",
]

for f in files:
    res = classify_document_content("", f, VESSEL_MASTER_LIST)
    print(f"File: {f}")
    print(f"  Vessel Name: {res.get('vessel_name') or 'Belle Lune'}")
    print(f"  Category   : {res.get('category')}")
    print(f"  Group      : {res.get('group')}")
    print(f"  Sub-Category: {res.get('sub_category')}")
    print(f"  Confidence : {res.get('confidence')}\n")
