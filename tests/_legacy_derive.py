"""Frozen copy of RealBackend._derive_dms_tags as of 2026-09-24 (before
Tag Configuration). Used only by tests to prove the refactor is
behaviour-identical with the default seed."""
from app import template


def legacy_derive_dms_tags(folder_path: str, vessel_name: str | None) -> dict:
    """Derive Group, Category, SubCategory column values from a folder path.

    The DMS hierarchy is:
      {Main Folder}/{Vessel Name}/{Category}/{SubCategory}      (vessel folders)
      Common for all ships/{Main Folder}/{Category}/{SubCategory} (common folders)
      {Main Folder}/{Category}/{SubCategory}                    (flat/Kaizen)

    Returns a dict with keys matching the SharePoint internal column names:
      DMS_Group, DMS_Category, DMS_SubCategory
    These must match the actual internal names of the site columns you create
    in the SharePoint communication site or SPE container library.
    """
    parts = [p.strip() for p in folder_path.split("/") if p.strip()]
    group = ""
    category = ""
    sub_category = ""
    inferred_vessel = ""

    if not parts:
        return {}

    main_folders_lower = {m.lower() for m in template.MAIN_FOLDERS}
    flat_main_folders_lower = {m.lower() for m in template.FLAT_MAIN_FOLDERS}

    common_roots = {
        "common for all ships",
        "common (not ship specific)",
        "common for all vessels",
    }

    drawing_categories = {
        "basic", "electrical", "hull", "machinery", "safety", "archive"
    }
    manual_categories = {
        "automation", "auxiliary engine", "boiler", "bridge equipments", "cargo",
        "deck machinery", "electrical", "main engine", "pollution",
        "propulsion", "refrigeration", "safety", "shafting", "steering gear", "thrusters",
        "to be classified",
    }

    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    def _normalize_group(label: str) -> str:
        low = _norm(label)
        if low in {"drawing", "drawings"}:
            return "Drawings"
        if low in {"manual", "manuals"}:
            return "Manuals"
        return label

    # Prefer taxonomy parse under the canonical segment: "Drawings and Manuals"
    dm_idx = next((i for i, p in enumerate(parts) if _norm(p) == "drawings and manuals"), -1)
    if dm_idx >= 0:
        t1 = parts[dm_idx + 1] if len(parts) > dm_idx + 1 else ""
        t2 = parts[dm_idx + 2] if len(parts) > dm_idx + 2 else ""

        # Pattern: .../Drawings and Manuals/{Group}/{Category}
        if _norm(t1) in {"drawing", "drawings", "manual", "manuals"}:
            group = _normalize_group(t1)
            category = t2
        # Pattern: .../Drawings and Manuals/To be Classified
        elif _norm(t1) == "to be classified":
            group = "Manuals"
            category = "To be Classified"
        # Pattern: .../Drawings and Manuals/{Category}/{Sub-Category}
        else:
            cat1 = _norm(t1)
            if cat1 in drawing_categories:
                group = "Drawings"
            elif cat1 in manual_categories:
                group = "Manuals"
            category = t1
            sub_category = t2

    # Legacy fallback parse if "Drawings and Manuals" is not present
    elif parts[0].lower() in common_roots and len(parts) >= 2:
        if len(parts) >= 3:
            category = parts[2]
        if len(parts) >= 4:
            sub_category = parts[3]
    elif _norm(parts[0]) in main_folders_lower and len(parts) >= 2:
        v_norm = (vessel_name or "").strip().lower()
        offset = 1
        if v_norm and len(parts) > 1 and parts[1].strip().lower() == v_norm:
            offset = 2
        # If vessel_name is unavailable, infer that the 2nd segment is vessel
        # when the 3rd segment looks like a known taxonomy/category branch.
        elif not v_norm and len(parts) > 2:
            seg3 = _norm(parts[2])
            if seg3 in drawing_categories or seg3 in manual_categories or seg3 == "drawings and manuals":
                offset = 2
        if len(parts) > offset:
            category = parts[offset]
        if len(parts) > offset + 1:
            sub_category = parts[offset + 1]
        if _norm(category) == "to be classified":
            group = "Manuals"
            sub_category = sub_category or "To be Classified"
        cat1 = _norm(category)
        if cat1 in drawing_categories:
            group = "Drawings"
        elif cat1 in manual_categories:
            group = "Manuals"
        if len(parts) > 1:
            inferred_vessel = parts[1]
    elif _norm(parts[0]) in flat_main_folders_lower:
        if len(parts) >= 2:
            category = parts[1]
        if len(parts) >= 3:
            sub_category = parts[2]
        cat1 = _norm(category)
        if cat1 in drawing_categories:
            group = "Drawings"
        elif cat1 in manual_categories:
            group = "Manuals"

    # Fallback: use whatever we have
    else:
        group = parts[0] if parts else ""
        category = parts[1] if len(parts) > 1 else ""
        sub_category = parts[2] if len(parts) > 2 else ""

    # Final normalization guardrails:
    # - Group must be Drawings/Manuals when inferable
    # - Category must not be a main folder or vessel name
    main_folder_values = {_norm(x) for x in (template.MAIN_FOLDERS + template.FLAT_MAIN_FOLDERS)}
    vessel_norm = _norm(vessel_name or inferred_vessel)
    if _norm(group) in main_folder_values or (vessel_norm and _norm(group) == vessel_norm):
        group = ""
    if _norm(category) in main_folder_values or (vessel_norm and _norm(category) == vessel_norm):
        category = ""

    cat1 = _norm(category)
    if not group:
        if cat1 in drawing_categories:
            group = "Drawings"
        elif cat1 in manual_categories or cat1 == "to be classified":
            group = "Manuals"
    if cat1 == "to be classified" and not sub_category:
        sub_category = "To be Classified"

    tags: dict = {}
    if group:
        # Use both the canonical Term Store column names AND the DMS_ aliases
        # so update_file_columns can resolve whichever column set is provisioned.
        tags["Group"] = group
        tags["DMS_Group"] = group
    if category:
        tags["Category"] = category
        tags["DMS_Category"] = category
    if sub_category:
        tags["SubCategory"] = sub_category
        tags["DMS_SubCategory"] = sub_category
    vname = (vessel_name or inferred_vessel or "").strip()
    if vname and vname.lower() not in {"common for all ships", "common for all vessels", "kaizen - knowledge bank"}:
        tags["VesselName"] = vname
        tags["vessel"] = vname
    return tags
