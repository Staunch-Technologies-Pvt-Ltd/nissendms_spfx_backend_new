"""Which document-management group a file belongs to — Drawings, Manuals,
To Be Classified, or other — from the folders it sits in below its vessel.

Shared by the Vessels page summary (/api/dashboard/vessel-summary) and the
Documents Copilot, so both count a file the same way.
"""
from __future__ import annotations

GROUP_LABELS = {
    "drawings": "Drawings",
    "manuals": "Manuals",
    "to_be_classified": "To Be Classified",
    "other": "Other",
}


def group_of(doc: dict) -> str:
    """'drawings' | 'manuals' | 'to_be_classified' | 'other' for a dashboard doc."""
    vessel_key = (doc.get("vessel") or "").strip().casefold()
    parts = [" ".join(p.split()).casefold() for p in (doc.get("subFolderPath") or "").split(">") if p.strip()]
    # Only look below the vessel's own folder, so a "Drawings" folder
    # higher up the path doesn't decide it.
    if vessel_key in parts:
        parts = parts[parts.index(vessel_key) + 1:]
    # Folder names that merely contain the word count too — e.g. "Final
    # Drawings (Maker)" or "Machinery Manuals" in sites not yet organised
    # as Drawings and Manuals. A name with both words ("Drawings and
    # Manuals") is a container, so look further down.
    for part in parts:
        if "to be classif" in part:
            return "to_be_classified"
        has_drawing, has_manual = "drawing" in part, "manual" in part
        if has_drawing and not has_manual:
            return "drawings"
        if has_manual and not has_drawing:
            return "manuals"
    return "other"
