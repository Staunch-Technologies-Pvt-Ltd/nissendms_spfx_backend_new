"""Declarative folder template — the single source of truth for the DMS hierarchy.

Node kinds
----------
- "leaf":         a final folder that exposes an upload button.
- "folder":       an intermediate container of children (no direct upload).
- "month_driven": special folder whose upload button lives at its root; uploads
                  are routed into auto-created `{Month YYYY}` sub-folders, each of
                  which contains the `month_children` categories.

The same template is consumed by the stub here and (in Phase B) by the real
SharePoint Embedded provisioner.
"""


def leaf(name):
    return {"name": name, "kind": "leaf"}


def folder(name, children):
    return {"name": name, "kind": "folder", "children": children}


def month_driven(name, month_children):
    return {"name": name, "kind": "month_driven", "month_children": month_children}


def drawing_classifier(name, categories):
    """Like `leaf` — a single upload button, no dropdown — but the upload is
    auto-routed: document text is matched against `categories` and filed into
    the corresponding child leaf, falling back to "Other Drawings" (never
    "To be Classified") when nothing matches. See ocr/drawing_category.py."""
    return {"name": name, "kind": "drawing_classifier", "children": [leaf(c) for c in categories]}


# =============================================================================
# REPLACEMENT BLOCK for backend/app/template.py
#
# This replaces everything in the file FROM the line starting with
# "MAIN_FOLDERS = [" DOWN TO THE END OF THE FILE.
#
# Do NOT touch anything above that point — leaf(), folder(), month_driven(),
# drawing_classifier(), and FALLBACK_LEAF_NAMES stay exactly as they are
# today in your file. This block only redefines the folder structure itself.
# =============================================================================

# Fallback leaf names used when routing rejected/unclassified uploads.
FALLBACK_LEAF_NAMES = {"to be classified", "other drawings", "other manuals"}

# Top-level main folders located directly at the Documents root
MAIN_FOLDERS = [
    "Technical & Crewing",
    "Commercial & Chartering",
    "Insurance",
]

# Kaizen - Knowledge Bank is intentionally excluded from the auto-provisioned
# SharePoint hierarchy. It should not be created automatically in the app.
FLAT_MAIN_FOLDERS = []
ALL_MAIN_FOLDERS = MAIN_FOLDERS + FLAT_MAIN_FOLDERS

# Legacy root constants kept for backwards-compatibility
VESSELS_ROOT = ""
SPECIFIC_VESSELS_ROOT = ""
COMMON_SHIPS_ROOT = "Common for all ships"
# Insurance uses a different name for its common folder
INSURANCE_COMMON_FOLDER_NAME = "Common (Not Ship Specific)"

# ---------------------------------------------------------------------------
# Per-ship sub-tree for each main folder — created under {Main Folder}/{Ship Name}/...
# ---------------------------------------------------------------------------
SHIP_TEMPLATE = {
    "Technical & Crewing": [
        month_driven(
            "Month End Reports",
            [
                leaf("Main Engine"),
                leaf("Aux Engine"),
                leaf("Cooling Water"),
                leaf("Inspection Reports"),
                leaf("Defect Reports"),
                leaf("Guarantee Claims"),
                leaf("To be Classified"),
            ],
        ),
        folder(
            "Service Agreements",
            [
                leaf("Technical Management"),
                leaf("Crew Management"),
                leaf("Vendor & Service Provider"),
                leaf("To be Classified"),
            ],
        ),
        folder(
            "Registration",
            [
                leaf("Flag & MPA"),
                leaf("Ship Builder"),
                leaf("Radio & Telecom"),
                leaf("Crewing & SMOU"),
                leaf("Novation"),
                leaf("To be Classified"),
            ],
        ),
        folder(
            "Drawings and Manuals",
            [
                folder(
                    "Drawing",
                    [
                        leaf("Archive"),
                        leaf("Basic"),
                        leaf("Electrical"),
                        leaf("Hull"),
                        leaf("Machinery"),
                        leaf("Other Drawings"),
                        leaf("Safety"),
                    ],
                ),
                folder(
                    "Manual",
                    [
                        leaf("Automation"),
                        leaf("Auxiliary Engine"),
                        leaf("Boiler"),
                        leaf("Cargo"),
                        leaf("Deck Machinery"),
                        leaf("Electrical"),
                        leaf("Main Engine"),
                        leaf("Other Manuals"),
                        leaf("Pollution"),
                        leaf("Propulsion"),
                        leaf("Refrigeration"),
                        leaf("Safety"),
                        leaf("Shafting"),
                        leaf("Steering Gear"),
                        leaf("Thrusters"),
                    ],
                ),
                leaf("To be Classified"),
            ],
        ),
          folder(
            "PO & Invoice",
            [
                leaf("Purchase Order"),
                leaf("Vendor Invoice"),
            ],
        ),
        leaf("Incidents"),
        leaf("Crewing"),
        leaf("To be Classified"),
    ],
    "Commercial & Chartering": [
        folder(
            "Agreements",
            [
                leaf("Charter party"),
                leaf("Pool Agreement"),
                leaf("Commission Agreement"),
                leaf("To be Classified"),
            ],
        ),
        month_driven(
            "Invoices & Payments",
            [leaf("Invoice"), leaf("Payments"), leaf("To be Classified")],
        ),
        month_driven(
            "Claims & Disputes",
            [leaf("Disputes"), leaf("Claims"), leaf("To be Classified")],
        ),
        leaf("To be Classified"),
    ],
    "Insurance": [
        leaf("P&I"),
        leaf("H&M"),
        leaf("War Risk"),
        leaf("Flag & MPA"),
        leaf("USA Related"),
    ],
}

# ---------------------------------------------------------------------------
# Common tree for each main folder, now created directly under
# COMMON_SHIPS_ROOT/{Main Folder}/... — the old per-main wrapper folder
# ("Common for all ships", "Common Agreements (Not Ship Specific)", "Common
# (Not Ship Specific)") is dropped because COMMON_SHIPS_ROOT is now that
# same container, one level up, shared by all three main folders.
# ---------------------------------------------------------------------------
COMMON_TEMPLATE = {
    "Technical & Crewing": [
        folder(
            "Vendor & Service Agreements",
            [leaf("Vendor & Service Provider Agreement"), leaf("To be Classified")],
        ),
        leaf("Vendor Management"),
        leaf("To be Classified"),
    ],
    "Commercial & Chartering": [
        folder(
            "Agreements",
            [
                leaf("Charter party"),
                leaf("Pool Agreement"),
                leaf("Commission Agreement"),
                leaf("To be Classified"),
            ],
        ),
        leaf("To be Classified"),
    ],
    "Insurance": [
        leaf("Agreements"),
        leaf("Miscellaneous"),
    ],
}

# ---------------------------------------------------------------------------
# Flat (non-vessel) root folders. Intentionally empty: the app no longer
# auto-creates "Kaizen - Knowledge Bank" or any other root folder/leaf tree
# in SharePoint Online. SHIP_TEMPLATE / COMMON_TEMPLATE above are kept only
# so existing legacy folders can still be classified — nothing provisions
# them any more.
# ---------------------------------------------------------------------------
FLAT_TEMPLATE: dict = {}