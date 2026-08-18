"""Classify a folder (by its full logical path from the Documents root) into
the semantic node type used by the UI, based on the declarative template.

`parts` is the list of folder names from the Documents root downward, e.g.
["Vessels", "Specific Vessels", "MV Horizon", "Technical & Crewing",
 "Month End Reports", "July 2026", "Main Engine"]
or
["Vessels", "Common for all ships", "Insurance", "Agreements"]
or
["Kaizen - Knowledge Bank", "Templates"]

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
from .. import template


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

    kaizen_name = template.FLAT_MAIN_FOLDERS[0]

    # --------------------------------------------------------------
    # Kaizen - Knowledge Bank sits directly at the Documents root.
    # --------------------------------------------------------------
    if parts[0] == kaizen_name:
        if len(parts) == 1:
            return {"kind": "main", "upload": False, "month_driven": False}
        return _descend(template.FLAT_TEMPLATE[kaizen_name], parts[1:])

    # --------------------------------------------------------------
    # Everything else lives under "Vessels".
    # --------------------------------------------------------------
    if parts[0] != template.VESSELS_ROOT:
        return {"kind": "folder", "upload": False, "month_driven": False}

    if len(parts) == 1:
        return {"kind": "root", "upload": False, "month_driven": False}  # Vessels itself

    root = parts[1]

    if root == template.SPECIFIC_VESSELS_ROOT:
        if len(parts) == 2:
            return {"kind": "root", "upload": False, "month_driven": False}  # Specific Vessels itself

        # parts[2] = ship name
        if len(parts) == 3:
            return {"kind": "ship", "upload": False, "month_driven": False}

        main = parts[3]
        if main not in template.MAIN_FOLDERS:
            return {"kind": "folder", "upload": False, "month_driven": False}
        if len(parts) == 4:
            return {"kind": "main", "upload": False, "month_driven": False}
        return _descend(template.SHIP_TEMPLATE[main], parts[4:])

    if root == template.COMMON_SHIPS_ROOT:
        if len(parts) == 2:
            return {"kind": "root", "upload": False, "month_driven": False}  # Common for all ships itself

        main = parts[2]
        if main not in template.MAIN_FOLDERS:
            return {"kind": "folder", "upload": False, "month_driven": False}
        if len(parts) == 3:
            return {"kind": "main", "upload": False, "month_driven": False}
        return _descend(template.COMMON_TEMPLATE[main], parts[3:])

    # Unrecognized folder directly under Vessels
    return {"kind": "folder", "upload": False, "month_driven": False}