import sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal
from app.db import models as db_models
from app.ocr.drawing_category import classify_all_fields_tiered, VESSEL_MASTER_LIST

with SessionLocal() as db:
    item = db.query(db_models.OcrStagingFile).filter_by(id=274).first()
    text = item.ocr_text_preview or ""
    filename = item.filename

    res = classify_all_fields_tiered(text, filename=filename, known_vessels=VESSEL_MASTER_LIST)
    print("Tiered Classification for item 274:")
    for k, v in res.items():
        print(f"  {k}: {v}")
