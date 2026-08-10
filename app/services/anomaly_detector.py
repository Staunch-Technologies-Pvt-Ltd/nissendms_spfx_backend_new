"""Folder/File Placement Detection Service

Detects unclassified or misplaced folders/files at 3 structural levels:
1. main_folder_unmatched: Items in main department folder line that don't match expected department categories.
2. vessel_level_unmatched: Folders or files created directly under Vessel Management / Technical & Crewing (as siblings to vessels) that do not match any provisioned vessel name in DB/pool.
3. subfolder_unmatched: Items created inside a vessel's tree that are outside the expected template subfolder hierarchy.
"""
import logging
from sqlalchemy.orm import Session
from ..db import models
from ..template import MAIN_FOLDERS, SHIP_TEMPLATE

logger = logging.getLogger(__name__)

# Known standard department folders/categories at main level
KNOWN_MAIN_CATEGORIES: dict[str, set[str]] = {
    "Technical & Crewing": {
        "Month End Reports", "Service Agreements", "Registration", "Drawings and Manuals", 
        "PO & Invoice", "Incidents", "Crewing", "To be Classified", "Common for all ships", 
        "Common Agreements (Not Ship Specific)", "Common (Not Ship Specific)"
    },
    "Commercial & Chartering": {
        "Agreements", "Invoices & Payments", "Claims & Disputes", "To be Classified"
    },
    "Insurance": {
        "P&I", "H&M", "War Risk", "Flag - MPA", "USA Related", "Flag and MPA"
    },
    "Kaizen - Knowledge Bank": {
        "Templates", "Procedures and Work Instructions", "Lessons Learned", "Circulars and Guidance"
    },
    "Knowledge Bank": {
        "Templates", "Procedures and Work Instructions", "Lessons Learned", "Circulars and Guidance"
    }
}


def scan_and_record_anomalies(db: Session, tree_items: list[dict]):
    """
    Scans a flat or hierarchical list of drive items (from Graph API or folder cache)
    and records/updates anomalies in the folder_anomalies table.
    
    Each item dict is expected to have:
    - id (drive_item_id)
    - name
    - item_type ('folder' or 'file')
    - path (e.g. "Vessel Management/Technical & Crewing/Snow Flower Test/Drawings and Manuals")
    - parent_id (optional)
    """
    if not tree_items:
        return []

    # Get known vessel names from DB
    known_vessels = {v.name.strip().lower(): v.name for v in db.query(models.Vessel).all()}
    # Pool slots (Pool-xxxxx) are excluded from anomaly reporting
    pool_slugs = {p.slug.strip().lower() for p in db.query(models.PoolSlot).all()}

    detected_anomalies = []

    for item in tree_items:
        item_id = item.get("id") or item.get("drive_item_id")
        if not item_id:
            continue

        name = item.get("name", "").strip()
        item_type = item.get("item_type", "folder")
        if item.get("folder") is not None:
            item_type = "folder"
        elif item.get("file") is not None:
            item_type = "file"

        path = item.get("path", "") or item.get("spo_path", "")
        # Clean path parts
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if not parts:
            continue

        # Determine department index
        dept_idx = -1
        dept_name = ""
        for idx, part in enumerate(parts):
            if part in KNOWN_MAIN_CATEGORIES or part in MAIN_FOLDERS or part == "Vessel Management":
                dept_idx = idx
                dept_name = part if part != "Vessel Management" else "Technical & Crewing"
                break

        if dept_idx == -1:
            continue

        rel_parts = parts[dept_idx + 1:]  # path after department root
        
        anomaly_type = None
        vessel_name = None

        if len(rel_parts) == 0:
            continue  # Department folder itself

        if len(rel_parts) == 1:
            child_name = rel_parts[0]
            child_lower = child_name.lower()

            if child_lower in pool_slugs or child_lower.startswith("pool-"):
                continue  # Pool slot folder, skip

            # If it matches a known vessel name or "common", skip
            if child_lower in known_vessels or "common" in child_lower:
                continue

            # If it matches expected department category, skip
            if dept_name in KNOWN_MAIN_CATEGORIES and child_name in KNOWN_MAIN_CATEGORIES[dept_name]:
                continue

            # Unmatched item directly under department root
            if item_type == "folder":
                anomaly_type = "vessel_level_unmatched"
            else:
                anomaly_type = "main_folder_unmatched"

        elif len(rel_parts) >= 2:
            first_child = rel_parts[0]
            first_lower = first_child.lower()

            if first_lower in known_vessels:
                vessel_name = known_vessels[first_lower]
                sub_path_name = rel_parts[1]
                expected_cats = set(KNOWN_MAIN_CATEGORIES.get(dept_name, set()))
                if dept_name in SHIP_TEMPLATE:
                    for node in SHIP_TEMPLATE[dept_name]:
                        expected_cats.add(node["name"])

                if sub_path_name not in expected_cats and not sub_path_name.startswith("Month ") and sub_path_name not in {"To be Classified", "Other Drawings", "Other Manuals"}:
                    anomaly_type = "subfolder_unmatched"

            elif dept_name in KNOWN_MAIN_CATEGORIES and first_child not in KNOWN_MAIN_CATEGORIES[dept_name]:
                anomaly_type = "main_folder_unmatched"

        if anomaly_type:
            existing = db.query(models.FolderAnomaly).filter_by(drive_item_id=item_id).one_or_none()
            if not existing:
                existing = models.FolderAnomaly(
                    drive_item_id=item_id,
                    parent_drive_item_id=item.get("parent_id"),
                    name=name,
                    item_type=item_type,
                    anomaly_type=anomaly_type,
                    department=dept_name or "Technical & Crewing",
                    vessel_name=vessel_name,
                    spo_path=path,
                    resolved=False
                )
                db.add(existing)
            else:
                existing.name = name
                existing.anomaly_type = anomaly_type
                existing.spo_path = path
                if vessel_name:
                    existing.vessel_name = vessel_name

            db.commit()
            db.refresh(existing)
            detected_anomalies.append(existing)

    return detected_anomalies
