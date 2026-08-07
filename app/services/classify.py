"""Classify a folder (by its path relative to the container root) into the
semantic node type used by the UI, based on the declarative template.

`parts` is the list of folder names from a main folder downward, e.g.
["Technical & Crewing", "MV Horizon", "Month End Reports", "July 2026", "Main Engine"].
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
    main = parts[0]
    if main not in template.MAIN_FOLDERS:
        return {"kind": "folder", "upload": False, "month_driven": False}
    if len(parts) == 1:
        return {"kind": "main", "upload": False, "month_driven": False}
    if main in template.FLAT_MAIN_FOLDERS:
        # Flat, shared main folder — no ship/common split, descend directly.
        return _descend(template.FLAT_TEMPLATE[main], parts[1:])
    common_root = template.COMMON_TEMPLATE[main]["name"]
    if parts[1].lower() == common_root.lower():
        if len(parts) == 2:
            return {"kind": "folder", "upload": False, "month_driven": False}
        return _descend(template.COMMON_TEMPLATE[main].get("children", []), parts[2:])
    # otherwise it's a ship folder
    if len(parts) == 2:
        return {"kind": "ship", "upload": False, "month_driven": False}
    return _descend(template.SHIP_TEMPLATE[main], parts[2:])
