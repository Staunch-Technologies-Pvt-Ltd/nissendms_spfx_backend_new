import sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal
from app.db import models as db_models

with SessionLocal() as db:
    item = db.query(db_models.OcrStagingFile).filter_by(id=274).first()
    if item:
        print("Item 274 OCR Text Preview:")
        print(item.ocr_text_preview)
