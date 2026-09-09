import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal
from app.db import models as db_models

with SessionLocal() as db:
    item = db.query(db_models.OcrStagingFile).filter_by(id=274).first()
    if item:
        print("Item 274:")
        print("Filename:", item.filename)
        print("Status:", item.status)
        print("Matched Keywords:", item.matched_keywords_json)
        print("Suggested Tags:", item.suggested_tags_json)
        print("OCR Text Preview:")
        print(item.ocr_text_preview)
