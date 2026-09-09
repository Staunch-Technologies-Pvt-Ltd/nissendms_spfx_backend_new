import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

from app.ocr.drawing_category import classify_document_content, VESSEL_MASTER_LIST

filename = "NK-23_N-2119_GH-3030_O.D.M. CONTROL SYSTEM OPERATION MANUAL  (R.O.B. MANUAL).pdf"
classification = classify_document_content("", filename=filename, known_vessels=VESSEL_MASTER_LIST)

raw_group = classification.get("group") or ""
ai_group = (
    "Drawings" if raw_group.lower().startswith("draw")
    else "Manuals" if raw_group.lower().startswith("man")
    else ""
)
ai_category = classification.get("category") or ""
ai_sub = classification.get("sub_category") or ""
ai_vessel = classification.get("vessel_name") or ""

print(f"group: {ai_group!r}")
print(f"category: {ai_category!r}")
print(f"sub_category: {ai_sub!r}")
print(f"vessel: {ai_vessel!r}")
print(f"confidence: {classification.get('confidence')}")
print(f"raw group: {raw_group!r}")

# Now simulate _derive_dms_tags for the Crewing path
from app.services.real_backend import SharePointBackend
dest_path = "Technical & Crewing/Norse New Haven/Crewing"
fields = SharePointBackend._derive_dms_tags(dest_path, "Norse New Haven")
print(f"\n_derive_dms_tags for Crewing path: {fields}")

# Final merged result
if not fields.get("Group") and ai_group and ai_category:
    fields["Group"] = ai_group
    fields["Category"] = ai_category
    if ai_sub:
        fields["SubCategory"] = ai_sub
    if ai_vessel and not fields.get("VesselName"):
        fields["VesselName"] = ai_vessel

print(f"\nFinal merged tags: {fields}")
