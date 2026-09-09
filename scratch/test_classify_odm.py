import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ocr.drawing_category import classify_document_content, classify_all_fields_tiered, VESSEL_MASTER_LIST

filename = "NK-23_N-2119_GH-3030_O.D.M. CONTROL SYSTEM OPERATION MANUAL  (R.O.B. MANUAL).pdf"
res1 = classify_document_content("", filename=filename, known_vessels=VESSEL_MASTER_LIST)
print("classify_document_content:")
for k, v in res1.items():
    print(f"  {k}: {v}")

print("\nclassify_all_fields_tiered:")
res2 = classify_all_fields_tiered("", filename=filename, known_vessels=VESSEL_MASTER_LIST)
for k, v in res2.items():
    print(f"  {k}: {v}")
