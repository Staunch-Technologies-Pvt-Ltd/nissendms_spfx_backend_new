import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal
from app.db import models as db_models

with SessionLocal() as db:
    items = db.query(db_models.OcrStagingFile).order_by(db_models.OcrStagingFile.id.desc()).limit(10).all()
    print(f"Found {len(items)} staging rows in DB:")
    for it in items:
        print(f"ID={it.id} | filename={it.filename!r} | status={it.status} | drive_item_id={it.drive_item_id}")
        print(f"  vessel_name={it.vessel_name!r}")
        print(f"  suggested_tags={it.suggested_tags_json}")
        print(f"  error={it.error!r}")
        print(f"  final_path={it.final_path!r}")
        print("-" * 60)
