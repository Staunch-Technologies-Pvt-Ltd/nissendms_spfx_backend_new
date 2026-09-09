import sys, io, asyncio, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal
from app.db import models as db_models
from app.ocr.drawing_category import classify_all_fields_tiered, VESSEL_MASTER_LIST
from app.graph import drive as gd
from app.config import settings
from app.main import _build_sharepoint_metadata_payload, _normalize_metadata_group, _safe_tag_value, _extract_department_from_path

async def run():
    with SessionLocal() as db:
        vessel_names = [v.name for v in db.query(db_models.Vessel).all()] or list(VESSEL_MASTER_LIST)
        items = db.query(db_models.OcrStagingFile).filter(
            ~db_models.OcrStagingFile.status.in_(["moved", "dismissed"])
        ).all()

        print(f"Reclassifying {len(items)} active staging items...")
        for r in items:
            tiered = classify_all_fields_tiered(
                r.ocr_text_preview or "",
                filename=r.filename,
                known_vessels=vessel_names,
                source_path=r.source_subfolder_path or "",
            )
            source_department = _extract_department_from_path(r.source_subfolder_path)
            if source_department:
                tiered["department"] = {"value": source_department, "confidence": 0.98, "tier": 1}

            suggested_tags = {
                "vessel": tiered["vessel"],
                "department": tiered["department"],
                "group": tiered["group"],
                "category": tiered["category"],
                "sub_category": tiered["sub_category"],
            }
            r.suggested_tags_json = json.dumps(suggested_tags)
            r.vessel_name = tiered["vessel"]["value"] or ""
            r.confidence = tiered["overall_confidence"]
            r.final_path = tiered.get("suggested_path")
            r.matched_keywords_json = json.dumps(tiered.get("matched_keywords", [])[:10])

            vessel_val = tiered["vessel"]["value"]
            vessel_conf = tiered["vessel"]["confidence"]
            overall_conf = tiered["overall_confidence"]
            text_len = len((r.ocr_text_preview or "").strip())
            if not vessel_val or vessel_conf < 0.60 or overall_conf < 0.40 or text_len < 20:
                r.status = "needs_review"
            else:
                r.status = "tag_suggested"

            print(f"ID={r.id} | {r.filename}")
            print(f"  Vessel: {tiered['vessel']['value']} | Group: {tiered['group']['value']} | Category: {tiered['category']['value']} | SubCat: {tiered['sub_category']['value']}")
            print(f"  Status: {r.status} | Final Path: {r.final_path}")

            # Patch live in SharePoint Online if drive_item_id exists
            if r.drive_item_id and settings.sp_configured:
                dept_val = _safe_tag_value(tiered.get("department")) or "Technical & Crewing"
                v_val = _safe_tag_value(tiered.get("vessel")) or ""
                cat_val = _safe_tag_value(tiered.get("category")) or "To be Classified"
                sub_val = _safe_tag_value(tiered.get("sub_category")) or "To be Classified"
                grp_val = _normalize_metadata_group(_safe_tag_value(tiered.get("group")), cat_val) or "Manuals"

                payload = _build_sharepoint_metadata_payload(
                    department=dept_val,
                    vessel=v_val,
                    group=grp_val,
                    category=cat_val,
                    sub_category=sub_val,
                )
                try:
                    res = await gd.update_file_columns(settings.sp_drive_id, r.drive_item_id, payload)
                    print(f"  -> SP Patch OK: {res.get('patched_fields')}")
                except Exception as e:
                    print(f"  -> SP Patch ERR: {e}")

        db.commit()
        print("\nReclassification & patching complete!")

asyncio.run(run())
