"""Folder/File Placement Detection Service

Detects unclassified or misplaced folders/files at 3 structural levels:
1. main_folder_unmatched: Items directly under a root (Specific Vessels /
   Common for all ships) or under a ship folder that don't match an
   expected main-department name.
2. vessel_level_unmatched: Folders or files created directly under
   Specific Vessels (as siblings to vessels) that do not match any
   provisioned vessel name in DB/pool.
3. subfolder_unmatched: Items created inside a vessel's or common tree
   that are outside the expected template subfolder hierarchy.

Expected folder layout:

Documents (root)
├── Vessels
│   ├── Specific Vessels
│   │   └── {Ship Name}
│   │       ├── Technical & Crewing
│   │       ├── Commercial & Chartering
│   │       └── Insurance
│   └── Common for all ships
│       ├── Technical & Crewing
│       ├── Commercial & Chartering
│       └── Insurance
└── Kaizen - Knowledge Bank
"""
import logging
from sqlalchemy.orm import Session
from ..db import models
from ..template import (
    MAIN_FOLDERS, SHIP_TEMPLATE, COMMON_TEMPLATE,
    SPECIFIC_VESSELS_ROOT, COMMON_SHIPS_ROOT, VESSELS_ROOT,
)

logger = logging.getLogger(__name__)

# Known standard department folders/categories at main level
KNOWN_MAIN_CATEGORIES: dict[str, set[str]] = {
    "Technical & Crewing": {
        "Month End Reports", "Service Agreements", "Registration", "Drawings and Manuals",
        "PO & Invoice", "Incidents", "Crewing", "To be Classified"
    },
    "Commercial & Chartering": {
        "Agreements", "Invoices & Payments", "Claims & Disputes", "To be Classified"
    },
    "Insurance": {
        "P&I", "H&M", "War Risk", "Flag & MPA", "USA Related"
    },
    "Kaizen - Knowledge Bank": {
        "Templates", "Procedures and Work Instructions", "Lessons Learned", "Circulars and Guidance"
    },
}


def scan_and_record_anomalies(db: Session, tree_items: list[dict]):
    """
    Scans a flat or hierarchical list of drive items (from Graph API or folder cache)
    and records/updates anomalies in the folder_anomalies table.

    Each item dict is expected to have:
    - id (drive_item_id)
    - name
    - item_type ('folder' or 'file')
    - path (e.g. "Vessels/Specific Vessels/Snow Flower Test/Technical & Crewing/Drawings and Manuals"
      or "Kaizen - Knowledge Bank/Templates")
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
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if not parts:
            continue

        anomaly_type = None
        vessel_name = None
        dept_name = ""

        # ---------------------------------------------------------------
        # Branch 1: Kaizen - Knowledge Bank sits directly at Documents root
        # ---------------------------------------------------------------
        if parts[0] == "Kaizen - Knowledge Bank":
            rest = parts[1:]
            if len(rest) == 0:
                continue  # the root itself
            dept_name = "Kaizen - Knowledge Bank"
            sub_path_name = rest[0]
            expected_cats = set(KNOWN_MAIN_CATEGORIES.get(dept_name, set()))
            if (
                sub_path_name not in expected_cats
                and sub_path_name not in {"To be Classified", "Other Drawings", "Other Manuals"}
            ):
                anomaly_type = "subfolder_unmatched"

        # ---------------------------------------------------------------
        # Branch 2: anything outside "Vessels" and outside Kaizen is unknown
        # ---------------------------------------------------------------
        elif parts[0] != VESSELS_ROOT:
            continue  # outside all known roots

        # ---------------------------------------------------------------
        # Branch 3: everything under Vessels/...
        # ---------------------------------------------------------------
        else:
            inner = parts[1:]
            if not inner:
                continue  # the Vessels root itself
            root = inner[0]

            if root == SPECIFIC_VESSELS_ROOT:
                # Vessels/Specific Vessels/{Ship}/{Main}/...
                rest = inner[1:]
                if len(rest) == 0:
                    continue  # the root itself

                first = rest[0]
                first_lower = first.lower()

                if len(rest) == 1:
                    # Direct child of Specific Vessels — should be a ship folder
                    if first_lower in pool_slugs or first_lower.startswith("pool-"):
                        continue
                    if first_lower in known_vessels:
                        continue
                    anomaly_type = "vessel_level_unmatched" if item_type == "folder" else "main_folder_unmatched"
                    dept_name = "Technical & Crewing"

                else:
                    if first_lower in pool_slugs or first_lower.startswith("pool-"):
                        continue

                    if first_lower not in known_vessels:
                        if item_type == "folder" and len(rest) == 1:
                            anomaly_type = "vessel_level_unmatched"
                            dept_name = "Technical & Crewing"
                        # else: nested under an unknown vessel — skip, will
                        # surface once the vessel-level anomaly is resolved
                    else:
                        vessel_name = known_vessels[first_lower]
                        if len(rest) == 2:
                            continue  # the main-folder root itself
                        dept_name = rest[1]
                        sub_path_name = rest[2]
                        expected_cats = set(KNOWN_MAIN_CATEGORIES.get(dept_name, set()))
                        if dept_name in SHIP_TEMPLATE:
                            for node in SHIP_TEMPLATE[dept_name]:
                                expected_cats.add(node["name"])

                        if dept_name not in MAIN_FOLDERS:
                            anomaly_type = "main_folder_unmatched"
                        elif (
                            sub_path_name not in expected_cats
                            and not sub_path_name.startswith("Month ")
                            and sub_path_name not in {"To be Classified", "Other Drawings", "Other Manuals"}
                        ):
                            anomaly_type = "subfolder_unmatched"

            elif root == COMMON_SHIPS_ROOT:
                # Vessels/Common for all ships/{Main}/...
                rest = inner[1:]
                if len(rest) == 0:
                    continue  # the root itself

                dept_name = rest[0]
                if len(rest) == 1:
                    continue  # the main-folder root itself

                if dept_name not in MAIN_FOLDERS:
                    anomaly_type = "main_folder_unmatched"
                else:
                    sub_path_name = rest[1]
                    expected_cats = set(KNOWN_MAIN_CATEGORIES.get(dept_name, set()))
                    if dept_name in COMMON_TEMPLATE:
                        for node in COMMON_TEMPLATE[dept_name]:
                            expected_cats.add(node["name"])

                    if (
                        sub_path_name not in expected_cats
                        and not sub_path_name.startswith("Month ")
                        and sub_path_name not in {"To be Classified", "Other Drawings", "Other Manuals"}
                    ):
                        anomaly_type = "subfolder_unmatched"

            else:
                # Unrecognized folder directly under Vessels (not Specific
                # Vessels or Common for all ships)
                continue

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