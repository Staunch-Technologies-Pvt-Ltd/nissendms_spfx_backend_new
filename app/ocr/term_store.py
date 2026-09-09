"""SharePoint Term Store Taxonomy & Seed Management for OCR Document AI.

Structure is aligned with the production SharePoint Term Store taxonomy on
nissenkaiunsingapore.sharepoint.com/sites/NissenKaiuExternal.

This module provides the single source of truth for:
- 4 production metadata columns: Category, Group, Sub-Category, Vessel Name
- Built-in term store taxonomy definitions for Drawing and Manual categories
- 24 production vessel master names
- Functions to seed or sync DocumentCategory models in the database
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .drawing_category import DRAWING_TAXONOMY, MANUAL_TAXONOMY, VESSEL_MASTER_LIST

logger = logging.getLogger("vessel_dms.term_store")


def get_drawing_tag_fields() -> list[dict[str, Any]]:
    """Generate the 4 standard SharePoint metadata TagFieldDef objects for Drawing."""
    drawing_categories = list(DRAWING_TAXONOMY.keys())
    all_drawing_subcategories = []
    for subcats in DRAWING_TAXONOMY.values():
        all_drawing_subcategories.extend(subcats.keys())

    return [
        {
            "key": "group",
            "label": "Group",
            "type": "select",
            "required": True,
            "options": ["Drawing"],
        },
        {
            "key": "category",
            "label": "Category",
            "type": "select",
            "required": True,
            "options": drawing_categories,
        },
        {
            "key": "sub_category",
            "label": "Sub-Category",
            "type": "select",
            "required": True,
            "options": sorted(list(dict.fromkeys(all_drawing_subcategories))),
        },
        {
            "key": "vessel",
            "label": "Vessel Name",
            "type": "select_vessel",
            "required": True,
            "options": VESSEL_MASTER_LIST,
        },
    ]


def get_manual_tag_fields() -> list[dict[str, Any]]:
    """Generate the 4 standard SharePoint metadata TagFieldDef objects for Manual."""
    manual_categories = list(MANUAL_TAXONOMY.keys())
    all_manual_subcategories = []
    for subcats in MANUAL_TAXONOMY.values():
        all_manual_subcategories.extend(subcats.keys())

    return [
        {
            "key": "group",
            "label": "Group",
            "type": "select",
            "required": True,
            "options": ["Manual"],
        },
        {
            "key": "category",
            "label": "Category",
            "type": "select",
            "required": True,
            "options": manual_categories,
        },
        {
            "key": "sub_category",
            "label": "Sub-Category",
            "type": "select",
            "required": True,
            "options": sorted(list(dict.fromkeys(all_manual_subcategories))),
        },
        {
            "key": "vessel",
            "label": "Vessel Name",
            "type": "select_vessel",
            "required": True,
            "options": VESSEL_MASTER_LIST,
        },
    ]


def get_all_drawing_hints() -> list[str]:
    """Collect all keywords, group names, and specific term aliases for Drawing."""
    hints: list[str] = ["drawing", "drawings", "dwg", "plan", "diagram", "schematic"]
    for group_name, subcats in DRAWING_TAXONOMY.items():
        hints.append(group_name.lower())
        for subcat_name, aliases in subcats.items():
            hints.append(subcat_name.lower())
            for alias in aliases:
                if alias.lower() not in hints:
                    hints.append(alias.lower())
    return list(dict.fromkeys(hints))


def get_all_manual_hints() -> list[str]:
    """Collect all keywords, group names, and specific term aliases for Manual."""
    hints: list[str] = ["manual", "manuals", "instruction", "guide", "booklet", "operation", "maintenance"]
    for group_name, subcats in MANUAL_TAXONOMY.items():
        hints.append(group_name.lower())
        for subcat_name, aliases in subcats.items():
            hints.append(subcat_name.lower())
            for alias in aliases:
                if alias.lower() not in hints:
                    hints.append(alias.lower())
    return list(dict.fromkeys(hints))


def seed_term_store_categories(db_session: Any) -> None:
    """Seed or update DocumentCategory records for Drawing and Manual with Term Store taxonomy.

    Designed so it can be called on backend startup or later wired to a Graph Term Store API sync.
    """
    from ..db import models as db_models

    drawing_tag_fields = json.dumps(get_drawing_tag_fields())
    drawing_hints = json.dumps(get_all_drawing_hints())
    drawing_path_template = "Technical & Crewing/{vessel}/Drawings and Manuals/{group}/{category}/{sub_category}"

    manual_tag_fields = json.dumps(get_manual_tag_fields())
    manual_hints = json.dumps(get_all_manual_hints())
    manual_path_template = "Technical & Crewing/{vessel}/Drawings and Manuals/{group}/{category}/{sub_category}"

    # 1. Drawing
    drawing_cat = db_session.query(db_models.DocumentCategory).filter_by(name="Drawing").first()
    if not drawing_cat:
        drawing_cat = db_models.DocumentCategory(
            name="Drawing",
            department="Technical & Crewing",
            dms_path_template=drawing_path_template,
            tag_fields_json=drawing_tag_fields,
            ocr_hints_json=drawing_hints,
            is_active=True,
        )
        db_session.add(drawing_cat)
    else:
        drawing_cat.department = "Technical & Crewing"
        drawing_cat.dms_path_template = drawing_path_template
        drawing_cat.tag_fields_json = drawing_tag_fields
        drawing_cat.ocr_hints_json = drawing_hints
        drawing_cat.is_active = True

    # 2. Manual
    manual_cat = db_session.query(db_models.DocumentCategory).filter_by(name="Manual").first()
    if not manual_cat:
        manual_cat = db_models.DocumentCategory(
            name="Manual",
            department="Technical & Crewing",
            dms_path_template=manual_path_template,
            tag_fields_json=manual_tag_fields,
            ocr_hints_json=manual_hints,
            is_active=True,
        )
        db_session.add(manual_cat)
    else:
        manual_cat.department = "Technical & Crewing"
        manual_cat.dms_path_template = manual_path_template
        manual_cat.tag_fields_json = manual_tag_fields
        manual_cat.ocr_hints_json = manual_hints
        manual_cat.is_active = True

    # 3. Ensure all 24 production vessels exist in the Vessel database table
    from sqlalchemy import func
    for vessel_name in VESSEL_MASTER_LIST:
        existing_vessel = db_session.query(db_models.Vessel).filter(
            func.lower(db_models.Vessel.name) == vessel_name.lower()
        ).first()
        if not existing_vessel:
            db_session.add(db_models.Vessel(name=vessel_name, is_provisioned=True))

    try:
        db_session.commit()
        logger.info("Successfully seeded/updated DocumentCategory records and 24 vessels.")
    except Exception as exc:
        db_session.rollback()
        logger.warning("Could not commit term store categories seed: %s", exc)
