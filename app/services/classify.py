"""Classify a folder (by its full logical path from the Documents root) into
the semantic node type used by the UI, based on the declarative template.

`parts` is the list of folder names from the Documents root downward, e.g.
["Technical & Crewing", "MV Horizon", "Month End Reports", "July 2026", "Main Engine"]
or
["Insurance", "Common for all ships", "Miscellaneous"]
or
["Kaizen - Knowledge Bank", "Templates"]

Expected folder layout:

Documents (root)
├── Technical & Crewing
│   ├── {Ship Name}
│   └── Common for all ships
├── Commercial & Chartering
│   ├── {Ship Name}
│   └── Common for all ships
├── Insurance
│   ├── {Ship Name}
│   └── Common for all ships
└── Kaizen - Knowledge Bank
"""
from .. import template


AUTO_APPLY_CONFIDENCE_THRESHOLD = 0.85


def evaluate_auto_apply(classification_result: dict) -> bool:
    """Strict gate used only by the tenant Sites scan flow."""
    return all(
        float((classification_result.get(field) or {}).get("confidence", 0)) > AUTO_APPLY_CONFIDENCE_THRESHOLD
        for field in ("vessel", "category", "sub_category")
    )


def _find(nodes, name):
    for n in nodes:
        if n["name"].lower() == name.lower():
            return n
    return None


def _flags(node):
    k = node["kind"]
    if k == "month_driven":
        return {
            "kind": "month_driven",
            "upload": True,
            "month_driven": True,
            "categories": [c["name"] for c in node.get("month_children", [])],
        }
    if k == "leaf":
        return {"kind": "leaf", "upload": True, "month_driven": False}
    if k == "drawing_classifier":
        return {
            "kind": "drawing_classifier",
            "upload": True,
            "month_driven": False,
            "categories": [c["name"] for c in node.get("children", [])],
        }
    return {"kind": "folder", "upload": False, "month_driven": False}


def _descend(nodes, rest):
    """Walk further down a SHIP_TEMPLATE / COMMON_TEMPLATE / FLAT_TEMPLATE
    node list using the remaining path segments."""
    node = _find(nodes, rest[0])
    if node is None:
        return {"kind": "folder", "upload": False, "month_driven": False}
    if len(rest) == 1:
        return _flags(node)
    if node["kind"] == "month_driven":
        # rest[1] is a "{Month YYYY}" folder; anything deeper is a category leaf.
        if len(rest) == 2:
            return {"kind": "month", "upload": True, "month_driven": False}
        return {"kind": "leaf", "upload": True, "month_driven": False}
    if node["kind"] in ("folder", "drawing_classifier"):
        return _descend(node.get("children", []), rest[1:])
    return {"kind": "folder", "upload": False, "month_driven": False}


def classify(parts: list[str]) -> dict:
    if not parts:
        return {"kind": "root", "upload": False, "month_driven": False}

    # --------------------------------------------------------------
    # Flat root folders (none configured by default — FLAT_MAIN_FOLDERS
    # is empty, so this block is skipped).
    # --------------------------------------------------------------
    for flat_name in template.FLAT_MAIN_FOLDERS:
        if parts[0].lower() == flat_name.lower():
            if len(parts) == 1:
                return {"kind": "main", "upload": False, "month_driven": False}
            return _descend(template.FLAT_TEMPLATE.get(flat_name, []), parts[1:])

    # --------------------------------------------------------------
    # Main Department Folders at Documents root
    # e.g. ["Technical & Crewing", "MV Horizon", "Month End Reports", ...]
    # or   ["Technical & Crewing", "Common for all ships", ...]
    # --------------------------------------------------------------
    main_match = None
    for m in template.MAIN_FOLDERS:
        if m.lower() == parts[0].lower():
            main_match = m
            break

    if main_match:
        if len(parts) == 1:
            return {"kind": "main", "upload": False, "month_driven": False}

        second = parts[1]
        if second.lower() in ("common for all ships", "common for all vessels", "common", "common (not ship specific)"):
            if len(parts) == 2:
                return {"kind": "common", "upload": False, "month_driven": False}
            return _descend(template.COMMON_TEMPLATE[main_match], parts[2:])
        else:
            # Second segment is the vessel name
            if len(parts) == 2:
                return {"kind": "ship", "upload": False, "month_driven": False}
            return _descend(template.SHIP_TEMPLATE[main_match], parts[2:])

    # --------------------------------------------------------------
    # Legacy fallback support for "Vessels/Specific Vessels/..."
    # --------------------------------------------------------------
    if parts[0].lower() == "vessels":
        if len(parts) == 1:
            return {"kind": "root", "upload": False, "month_driven": False}
        root = parts[1].lower()
        if root == "specific vessels":
            if len(parts) == 2:
                return {"kind": "root", "upload": False, "month_driven": False}
            if len(parts) == 3:
                return {"kind": "ship", "upload": False, "month_driven": False}
            legacy_main = None
            for m in template.MAIN_FOLDERS:
                if m.lower() == parts[3].lower():
                    legacy_main = m
                    break
            if not legacy_main:
                return {"kind": "folder", "upload": False, "month_driven": False}
            if len(parts) == 4:
                return {"kind": "main", "upload": False, "month_driven": False}
            return _descend(template.SHIP_TEMPLATE[legacy_main], parts[4:])
        elif root in ("common for all ships", "common for all vessels", "common"):
            if len(parts) == 2:
                return {"kind": "root", "upload": False, "month_driven": False}
            legacy_main = None
            for m in template.MAIN_FOLDERS:
                if m.lower() == parts[2].lower():
                    legacy_main = m
                    break
            if not legacy_main:
                return {"kind": "folder", "upload": False, "month_driven": False}
            if len(parts) == 3:
                return {"kind": "main", "upload": False, "month_driven": False}
            return _descend(template.COMMON_TEMPLATE[legacy_main], parts[3:])

    return {"kind": "folder", "upload": False, "month_driven": False}


def classify_deletion(
    path_parts: list[str], is_folder: bool, vessel_names: "set[str] | None" = None
) -> dict:
    """Decide which Recycle Bin tab a deleted node belongs in, and tag it
    with vessel/category/sub-category context, at deletion-capture time —
    used by services.real_backend._record_deletion (and the native-SPO
    reconciliation job) so classification never depends on which client-side
    path-guessing heuristic happens to run later.

    `path_parts` is the deleted node's full original path split on "/", with
    the node's own name as the last element (same convention as classify()
    above). `vessel_names` is the authoritative Term Store vessel list
    (VESSEL_MASTER_LIST) unioned with any live `Vessel` DB rows — callers
    build this set once and pass it in so a bulk pass doesn't re-fetch it
    per item; defaults to VESSEL_MASTER_LIST alone when omitted.

    Returns {"classification": "vessel" | "normal_folder" | "file",
             "vessel_name": str, "category": str, "sub_category": str}.
    """
    if vessel_names is None:
        from ..ocr.drawing_category import VESSEL_MASTER_LIST
        vessel_names = {v.lower() for v in VESSEL_MASTER_LIST}
    else:
        vessel_names = {v.lower() for v in vessel_names}

    parts = [p for p in (path_parts or []) if p]
    name = parts[-1] if parts else ""
    ancestors = parts[:-1]

    # Which ancestor segment (if any) is this node's vessel context?
    vessel_name = ""
    vessel_idx = -1
    for i, seg in enumerate(ancestors):
        if seg.lower() in vessel_names:
            vessel_name = seg
            vessel_idx = i
            break

    if is_folder and name.lower() in vessel_names:
        # The deleted node's own name matches a Term Store vessel name —
        # it's a vessel folder itself, wherever in the tree it sat.
        return {"classification": "vessel", "vessel_name": name, "category": "", "sub_category": ""}

    category = ancestors[vessel_idx + 1] if vessel_idx >= 0 and len(ancestors) > vessel_idx + 1 else ""
    sub_category = ancestors[vessel_idx + 2] if vessel_idx >= 0 and len(ancestors) > vessel_idx + 2 else ""

    if not is_folder:
        return {
            "classification": "file",
            "vessel_name": vessel_name,
            "category": category,
            "sub_category": sub_category,
        }

    # A folder that is not itself a vessel name. "Part of a vessel's own
    # path" means it sits under a recognized vessel ancestor — it's still
    # tagged with that vessel (mirroring how files are tagged) but there is
    # no 4th tab for it, so it stays grouped with Normal Folders. A folder
    # with no vessel ancestor at all is a true general/admin folder.
    return {
        "classification": "normal_folder",
        "vessel_name": vessel_name,
        "category": category,
        "sub_category": sub_category,
    }