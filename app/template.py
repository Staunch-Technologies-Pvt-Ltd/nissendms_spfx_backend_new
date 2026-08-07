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


# Recognised "not yet classified" fallback leaf names used across the tree.
# Most folders fall back to "To be Classified"; the Drawing and Manual
# subfolders (under "Drawings and Manuals") use "Other Drawings" / "Other
# Manuals" instead. Reject-upload routing reuses whichever one already
# exists in a given folder rather than always defaulting to the generic name.
FALLBACK_LEAF_NAMES = {"to be classified", "other drawings", "other manuals"}


# The top-level main folders.
MAIN_FOLDERS = [
    "Technical & Crewing",
    "Commercial & Chartering",
    "Insurance",
    "Kaizen - Knowledge Bank",
]

# Main folders that are flat and shared: same content for every user, no
# per-vessel ship folder and no "Common for all ships" split (unlike the
# other main folders, which are cloned per vessel via SHIP_TEMPLATE below).
FLAT_MAIN_FOLDERS = [
    "Kaizen - Knowledge Bank",
]

# ---------------------------------------------------------------------------
# Per-ship sub-tree for each main folder.
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
                    "Basic",
                    [
                        leaf("General Arrangement"),
                        leaf("Capacity Plan & Dead Weight"),
                        leaf("Trim & Stability Information"),
                        leaf("Loading Manual"),
                        leaf("Damage Control Plan"),
                        leaf("EEDI Technical File"),
                        leaf("Ship Structure Access Manuals"),
                        leaf("Docking Plan"),
                        leaf("Emergency Towing Booklet"),
                        leaf("To be Classified"),
                    ],
                ),
                folder(
                    "Hull",
                    [
                        leaf("Makers List of Hull Parts"),
                        leaf("Results of Official Sea Trial"),
                        leaf("Container Stowage Plan"),
                        leaf("Mooring Arrangement"),
                        leaf("Midship Section"),
                        leaf("Bulkhead Plans"),
                        leaf("Profile & Deck Plan"),
                        leaf("Superstructure"),
                        leaf("Shell Expansion"),
                        leaf("Rudder and Rudder Stock"),
                        leaf("Painting Schedule"),
                        leaf("Cargo Securing Manual"),
                        leaf("To be Classified"),
                    ],
                ),
                folder(
                    "Safety",
                    [
                        leaf("Life Saving Appliances Plan"),
                        leaf("Fire Control Plan"),
                        leaf("To be Classified"),
                    ],
                ),
                folder(
                    "Engine",
                    [
                        leaf("Arrangement of Engine Room"),
                        leaf("Machinery Makers List"),
                        leaf("Machinery Particulars"),
                        leaf("Test Record of Official Sea Trial"),
                        leaf("Shafting Arrangements"),
                        leaf("Stern Tube"),
                        leaf("Bilge System"),
                        leaf("Ballast System"),
                        leaf("Fuel Oil System"),
                        leaf("Lube Oil System"),
                        leaf("Cooling Water System"),
                        leaf("Air System"),
                        leaf("To be Classified"),
                    ],
                ),
                folder(
                    "Electrical",
                    [
                        leaf("Single Line Diagram"),
                        leaf("Main Switchboard Arrangement"),
                        leaf("Emergency Switchboard Arrangement"),
                        leaf("Power Distribution Diagram"),
                        leaf("To be Classified"),
                    ],
                ),
                folder(
                    "Manual",
                    [
                        folder(
                            "Main Engine",
                            [
                                leaf("Operation & Maintenance Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Auxiliary Engine",
                            [
                                leaf("Operation & Maintenance Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Boiler",
                            [
                                leaf("Operation & Maintenance Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Shafting",
                            [
                                leaf("Stern Tube Manual"),
                                leaf("CPP Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Steering Gear",
                            [
                                leaf("Operation Manual"),
                                leaf("Maintenance Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Propulsion",
                            [
                                leaf("Shaft Generator Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Thrusters",
                            [
                                leaf("Operation & Maintenance Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Electrical",
                            [
                                leaf("Power Management System"),
                                leaf("Main Switchboard Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Automation",
                            [
                                leaf("Alarm Monitoring System"),
                                leaf("Engine Control System"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Cargo",
                            [
                                leaf("Cargo Crane Manual"),
                                leaf("Hatch Cover Manual"),
                                leaf("Ballast System Manual"),
                                leaf("Cargo Pump Manual"),
                                leaf("IG System Manual"),
                                leaf("COW Manual"),
                                leaf("ODME Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Safety",
                            [
                                leaf("Fire Detection System Manual"),
                                leaf("Fire Alarm Manual"),
                                leaf("CO2 System Manual"),
                                leaf("Emergency Generator Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Pollution",
                            [
                                leaf("BWTS Manual"),
                                leaf("OWS Manual"),
                                leaf("Sewage Treatment Plant Manual"),
                                leaf("Incinerator Manual"),
                                leaf("Exhaust Gas Scrubber Manual"),
                                leaf("SCR System Manual"),
                                leaf("EGR System Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Refrigeration",
                            [
                                leaf("AC Plant Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                        folder(
                            "Deck Machinery",
                            [
                                leaf("Windlass Manual"),
                                leaf("Mooring Winch Manual"),
                                leaf("Hydraulic Manual"),
                                leaf("Other Manuals"),
                            ],
                        ),
                    ],
                ),
                leaf("To be Classified"),
            ],
        ),
        folder("PO & Invoice", [leaf("Purchase Order"), leaf("Vendor Invoice")]),
        leaf("Incidents"),
        leaf("Crewing"),
        leaf("To be Classified"),
    ],
    "Commercial & Chartering": [
        folder(
            "Agreements",
            [
                leaf("Charter Party"),
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
        leaf("Flag - MPA"),
        leaf("USA Related"),
    ],
}

# ---------------------------------------------------------------------------
# "Common for all ships" sub-tree for each main folder.
# ---------------------------------------------------------------------------
COMMON_TEMPLATE = {
    "Technical & Crewing": folder(
        "Common for all ships",
        [
            folder(
                "Vendor & Service Agreements",
                [leaf("Vendor & Service Provider Agreement"), leaf("To be Classified")],
            ),
            leaf("Vendor Management"),
            leaf("To be Classified"),
        ],
    ),
    "Commercial & Chartering": folder(
        "Common Agreements (Not Ship Specific)",
        [
            folder(
                "Agreements",
                [
                    leaf("Charter Party"),
                    leaf("Pool Agreement"),
                    leaf("Commission Agreement"),
                    leaf("To be Classified"),
                ],
            ),
            leaf("To be Classified"),
        ],
    ),
    "Insurance": folder(
        "Common (Not Ship Specific)",
        [leaf("Agreements"), leaf("Miscellaneous")],
    ),
}

# ---------------------------------------------------------------------------
# Flat, shared sub-tree for each entry in FLAT_MAIN_FOLDERS — placed directly
# under the main folder itself (no ship/common split).
# ---------------------------------------------------------------------------
FLAT_TEMPLATE = {
    "Kaizen - Knowledge Bank": [
        leaf("Templates"),
        leaf("Procedures and Work Instructions"),
        leaf("Lessons Learned"),
        folder(
            "Circulars and Guidance",
            [
                leaf("Equipment Maker"),
                leaf("Class"),
                # Uses U+2044 (fraction slash), not ASCII "/" — SharePoint
                # rejects "/" in folder names since it's the path separator.
                leaf("Flag ⁄ Port State"),
                leaf("SIRE⁄OCIMF⁄RightShip"),
                leaf("Shipyard"),
            ],
        ),
    ],
}
