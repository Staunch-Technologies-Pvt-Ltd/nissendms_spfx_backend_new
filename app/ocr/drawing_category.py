"""Comprehensive Drawing & Manual document classification and OCR metadata extraction.

Based on the official SharePoint Term Store taxonomy:
1. Drawing Category (Category = "Drawing"):
   - Basic: Basic Drawings, Capacity Plan & Dead Weight, Damage Control Plan, Docking Plan,
            EEDI Technical file, Emergency Towing Booklet, General Arrangement, Loading Manual,
            Ship Structure Access Manuals, Trim & Stability Information
   - Electrical: Electrical Drawings, Emergency Switchboard Arrangement, Main Switchboard Arrangement,
                 Power Distribution Diagram, Single Line Diagram
   - Hull: Bulkhead plans, Cargo Securing Manual, Container Stowage Plan, Hull Drawings,
           Makers List of Hull parts, Midship Section, Mooring Arrangement, Painting Schedule,
           Profile & Deck Plan, Results of Official Sea Trial, Rudder and Rudder stock,
           Shell Expansion, Superstructure
   - Machinery: Arrangement of Engine Room, Machinery Drawings, Machinery Makers List,
                Machinery Particulars, Pipeline Diagram, Shafting Arrangements, Stern Tube,
                Test Record of Official Sea Trial, Other Drawings
   - Safety: Fire Control Plan, Life Saving Appliances Plan, Safety Drawings
   - Archive: Archive

2. Manual Category (Category = "Manual"):
   - Automation: Alarm Monitoring System, Engine Control System
   - Auxiliary Engine: Operation & Maintenance Manual
   - Boiler: Operation & Maintenance Manual
   - Bridge Equipments: Bridge Equipments
   - Cargo: Ballast System Manual, Cargo Crane Manual, Cargo Pump Manual, COW Manual,
            Hatch Cover Manual, IG System Manual, ODME Manual
   - Deck Machinery: Mooring Winch Manual, Windlass Manual
   - Electrical: Main Switchboard Manual, Power Management System
   - Main Engine: Operation & Maintenance Manual, Other Manuals
   - Pollution: BWTS Manual, EGR System Manual, Exhaust Gas Scrubber Manual, Incinerator Manual,
                OWS Manual, SCR System Manual, Sewage Treatment Plant Manual
   - Propulsion: Shaft Generator Manual
   - Refrigeration: AC Plant Manual
   - Safety: CO2 System Manual, Emergency Generator Manual, Fire Alarm Manual, Fire Detection System Manual
   - Shafting: CPP Manual, Stern Tube Manual
   - Steering Gear: Maintenance Manual, Operation Manual
   - Thrusters: Operation & Maintenance Manual
   - To Be Classified: To Be Classified

3. 24 Production Vessel Names:
   Belle Lune, Bow Fighter, Bow Fraternity, Cameroun Express, Cecilie F, Cote D Ivoire Express,
   Duchess Emerald, Ghana Express, Lignum Grid, Lignum Mesh, Lignum Web, Maersk El Banco,
   Maersk El Palomar, Maersk Ferrato, Maersk Finisterre, Maersk Frio, Norse Evolution,
   Norse Ijmuiden, Norse New Haven, Peissy, Potiniere, Senegal Express, Snow Flake, Snow Flower
"""
from __future__ import annotations

import re
from typing import Any

# ── 0. CONFIDENCE FLOOR ──────────────────────────────────────────────────────
# Vessel matches below this threshold are treated as "not detected" — value is
# blanked so the staging router sends the file to Needs Review rather than
# pre-filling the vessel field with a low-confidence guess.
VESSEL_CONFIDENCE_FLOOR: float = 0.60


def is_non_document_filename(filename: str) -> bool:
    """Return true for operating-system files that should bypass OCR taxonomy."""
    return (filename or "").strip().lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1] in {
        "thumbs.db",
        "desktop.ini",
        ".ds_store",
    }

# ── 1. VESSEL MASTER LIST (24 Production Vessels) ─────────────────────────────
VESSEL_MASTER_LIST: list[str] = [
    "Belle Lune",
    "Bow Fighter",
    "Bow Fraternity",
    "Cameroun Express",
    "Cecilie F",
    "Cote D Ivoire Express",
    "Duchess Emerald",
    "Ghana Express",
    "Lignum Grid",
    "Lignum Mesh",
    "Lignum Web",
    "Maersk El Banco",
    "Maersk El Palomar",
    "Maersk Ferrato",
    "Maersk Finisterre",
    "Maersk Frio",
    "Norse Evolution",
    "Norse Ijmuiden",
    "Norse New Haven",
    "Peissy",
    "Potiniere",
    "Senegal Express",
    "Snow Flake",
    "Snow Flower",
]

# Vessel aliases and shipyard/hull number mapping to canonical master vessel names
VESSEL_ALIASES: dict[str, tuple[str, ...]] = {
    "Belle Lune": (
        "belle lune", "belle-lune", "belle_lune",
        "ss268", "ss 268", "ss-268", "ss_268", "ss.268", "hull ss268", "hull ss 268", "hull 268", "h268", "s-268",
        "sno.268", "sno 268", "s.no.268", "s.no 268", "sno-268", "s.no-268"
    ),
    "Bow Fighter": (
        "bow fighter", "bow-fighter", "bow_fighter",
        "n-2119", "n2119", "n 2119", "n.2119", "hull n-2119", "hull n2119", "hull n 2119",
        "1054292", "imo 1054292", "imo1054292", "imo: 1054292", "nk-23", "nk23", "gh-3030", "gh3030"
    ),
    "Bow Fraternity": ("bow fraternity", "bow-fraternity", "bow_fraternity"),
    "Cameroun Express": ("cameroun express", "cameroon express", "cameroun-express", "cameroun_express"),
    "Cecilie F": ("cecilie f", "cecilie-f", "cecilie_f", "cecilief"),
    "Cote D Ivoire Express": (
        "cote d ivoire express", "cote d'ivoire express", "cote d' ivoire express",
        "cote d’ivoire express", "côte d'ivoire express", "côte d’ivoire express",
        "cote divoire express", "cotedivoire express", "cote-d-ivoire-express"
    ),
    "Duchess Emerald": (
        "dutches emerald", "duchess emerald", "dutchess emerald",
        "dutches-emerald", "duchess-emerald"
    ),
    "Ghana Express": (
        "ghana express", "ghana-express", "ghana_express",
        "sno.721", "sno 721", "s.no.721", "s.no 721", "sno-721", "s.no-721",
        "hull 721", "hull-721", "h721", "h-721", "s721", "s-721", "ship 721", "ship no 721", "ship no. 721"
    ),
    "Lignum Grid": ("lignum grid", "lignum-grid", "lignum_grid"),
    "Lignum Mesh": ("lignum mesh", "lignum-mesh", "lignum_mesh"),
    "Lignum Web": ("lignum web", "lignum-web", "lignum_web"),
    "Maersk El Banco": (
        "maersk ei banco", "maersk el banco", "maersk-ei-banco", "maersk-el-banco",
        "maersk_ei_banco", "maersk_el_banco", "ei banco", "el banco",
        "9964493", "imo 9964493", "imo: 9964493", "imo:9964493", "imo no. 9964493", "imo number : 9964493"
    ),
    "Maersk El Palomar": (
        "maersk ei palomar", "maersk el palomar", "maersk-ei-palomar", "maersk-el-palomar",
        "maersk_ei_palomar", "maersk_el_palomar", "ei palomar", "el palomar",
        "9964481", "imo 9964481", "imo: 9964481", "imo:9964481", "imo no. 9964481", "imo number : 9964481",
        "5450", "s.no.5450", "s.no 5450", "sno.5450", "sno 5450", "f452301"
    ),
    "Maersk Ferrato": ("maersk ferrato", "maersk-ferrato", "maersk_ferrato"),
    "Maersk Finisterre": ("maersk finisterre", "maersk-finisterre", "maersk_finisterre"),
    "Maersk Frio": ("maersk frio", "maersk-frio", "maersk_frio"),
    "Norse Evolution": ("norse evolution", "norse-evolution", "norse_evolution"),
    "Norse Ijmuiden": (
        "norse ijmuiden", "norse-ijmuiden", "norse_ijmuiden", "norse ymuiden", "norse-ymuiden"
    ),
    "Norse New Haven": ("norse new haven", "norse-new-haven", "norse_new_haven", "norse newhaven"),
    "Peissy": (
        "peissy",
        "ss378", "ss 378", "ss-378", "ss_378", "ss.378", "hull ss378", "hull ss 378", "hull 378", "h378", "s.no.ss378", "s.no ss378",
        "sno.378", "sno 378", "s.no.378", "s.no 378", "sno-378", "s.no-378"
    ),
    "Potiniere": ("potiniere", "potinière", "potiniere-"),
    "Senegal Express": ("senegal express", "senegal-express", "senegal_express"),
    "Snow Flake": ("snow flake", "snowflake", "snow-flake", "snow_flake"),
    "Snow Flower": ("snow flower", "snowflower", "snow-flower", "snow_flower"),
}

# ── 2. DRAWING SPECIFICATION TAXONOMY (Term Store Exact) ──────────────────────
DRAWING_TAXONOMY: dict[str, dict[str, tuple[str, ...]]] = {
    "Basic": {
        "Basic Drawings": ("basic drawing", "basic drawings", "key plan", "contract plan", "inventory", "legal equipment", "list of inventory"),
        "Capacity Plan & Dead Weight": ("capacity plan", "deadweight scale", "dead weight", "tank capacity plan", "sounding table", "level gauge", "sounding table for level gauge"),
        "Damage Control Plan": ("damage control", "damage stability", "damage control plan", "damage control booklet"),
        "Docking Plan": ("docking plan", "docking drawing", "dry docking plan", "keel block plan"),
        "EEDI Technical file": ("eedi", "eedi technical file", "energy efficiency design index", "eeoi"),
        "Emergency Towing Booklet": ("emergency towing", "etb", "emergency towing arrangement", "emergency towing booklet", "eta drawing"),
        "General Arrangement": ("general arrangement", "ga drawing", "general arrangement plan", "ga plan"),
        "Loading Manual": ("loading manual", "loading computer", "longitudinal strength", "loading instrument"),
        "Ship Structure Access Manuals": ("ship structure access", "ssam", "access manual", "pma manual"),
        "Trim & Stability Information": ("trim & stability", "trim and stability", "stability booklet", "stability information", "stability calculation"),
    },
    "Electrical": {
        "Electrical Drawings": ("electrical drawing", "electrical drawings", "electrical schematic", "cable diagram", "electric equipment", "arrangement of electric equipment"),
        "Emergency Switchboard Arrangement": ("emergency switchboard arrangement", "esb arrangement", "emergency switchboard drawing", "esb layout"),
        "Main Switchboard Arrangement": ("main switchboard arrangement", "msb arrangement", "main switchboard drawing", "msb layout", "440v switchboard"),
        "Power Distribution Diagram": ("power distribution diagram", "lighting distribution", "feeder list", "power feeder diagram", "antenna", "transducer", "wheel house", "wheelhouse", "speed log", "electrical equipment", "electric equipment (wheelhouse)", "arrangement of electric equipment (wheelhouse)"),
        "Single Line Diagram": ("single line diagram", "sld", "one line diagram", "main power single line", "electrical distribution diagram"),
    },
    "Hull": {
        "Bulkhead plans": ("bulkhead plan", "watertight bulkhead", "collision bulkhead", "transverse bulkhead", "bulkhead construction", "bulkhead plans", "joiner bhd", "bhd", "bulkhead"),
        "Cargo Securing Manual": ("cargo securing manual", "csm", "cargo lashing manual", "cargo securing plan"),
        "Container Stowage Plan": ("container stowage", "container securing", "bay plan", "lashing plan", "container arrangement", "stowage plan"),
        "Hull Drawings": (
            "hull drawing", "hull drawings", "hull structure", "hull construction",
            "hull part", "(hull part)", "hull parts", "(hull parts)",
            "sounding table (hull part)", "sounding table(hull part)",
            "sounding table (for level gauge) (hull part)", "sounding table(for level gauge)(hull part)",
            "sounding table for level gauge (hull part)", "level gauge (hull part)", "level gauge(hull part)",
        ),
        "Makers List of Hull parts": ("makers list of hull", "hull maker list", "hull equipment list", "hull fittings maker"),
        "Midship Section": ("midship section", "typical midship", "transverse section", "midship construction"),
        "Mooring Arrangement": ("mooring arrangement", "mooring plan", "towing and mooring", "fairlead arrangement"),
        "Painting Schedule": ("painting schedule", "coating specification", "paint maker", "surface preparation", "paint scheme"),
        "Profile & Deck Plan": ("profile & deck", "profile and deck", "deck plan", "upper deck plan", "scantling profile"),
        "Results of Official Sea Trial": ("results of official sea trial", "sea trial results", "sea trial report", "speed trial results", "noise measurement", "test result of noise", "trial result"),
        "Rudder and Rudder stock": ("rudder", "rudder stock", "rudder horn", "rudder carrier", "rudder profile"),
        "Shell Expansion": ("shell expansion", "bottom plating", "side shell", "shell plating"),
        "Superstructure": ("superstructure", "deck house", "accommodation plan", "wheelhouse construction", "accommodation arrangement", "arr. of joiner", "arr. of insulation", "deck covering in accomm", "accomm", "accommodation"),
    },
    "Machinery": {
        "Arrangement of Engine Room": ("arrangement of engine room", "engine room arrangement", "er layout", "machinery arrangement", "er plan", "ecr", "engine control room"),
        "Machinery Drawings": ("machinery drawing", "machinery drawings", "engine drawings", "piping drawing"),
        "Machinery Makers List": ("machinery makers list", "machinery maker", "engine maker list", "equipment maker list", "machinery part", "spare parts", "spare part", "tool list", "spare parts and tool list"),
        "Machinery Particulars": ("machinery particulars", "machinery data", "principal machinery particulars", "engine specification"),
        "Pipeline Diagram": ("pipeline diagram", "piping diagram", "bilge piping", "ballast piping", "fuel oil piping", "lube oil piping", "cooling water piping"),
        "Shafting Arrangements": ("shafting arrangement", "shaft alignment", "intermediate shaft", "propeller shaft", "thrust shaft", "shafting arrangements"),
        "Stern Tube": ("stern tube", "stern tube sealing", "stern tube bearing", "aft stern tube"),
        "Test Record of Official Sea Trial": ("test record of official sea trial", "sea trial test record", "engine sea trial record", "torsional vibration test", "official shop test", "results of official shop test", "shop test record", "shop test"),
        "Other Drawings": ("other drawings", "miscellaneous drawing", "general drawing"),
    },
    "Safety": {
        "Fire Control Plan": ("fire control plan", "fire fighting plan", "fifi plan", "fire detection plan", "safety plan", "fire and safety", "fire protection", "sound insulation"),
        "Life Saving Appliances Plan": ("life saving appliances", "lsa plan", "lifeboat arrangement", "life raft arrangement", "davit winch", "boat and davit", "rescue boat plan", "lsa", "solas check list", "life saving equipment", "arr.of life saving"),
        "Safety Drawings": ("safety drawing", "safety drawings", "safety equipment arrangement", "lifesaving drawing", "solas"),
    },
    "Archive": {
        "Archive": ("archive", "superseded", "obsolete", "historical", "as-built", "as built", "void drawing"),
    },
}

# ── 3. MANUAL SPECIFICATION TAXONOMY (Term Store Exact) ───────────────────────
MANUAL_TAXONOMY: dict[str, dict[str, tuple[str, ...]]] = {
    "Automation": {
        "Alarm Monitoring System": (
            "alarm monitoring system", "alarm system", "alarm manual", "ams manual",
            "ias manual", "kongsberg ams", "valmet ams", "alarm monitoring", "alarm & monitoring"
        ),
        "Engine Control System": (
            "engine control system", "ecs manual", "nabtesco", "rcs manual", "remote control system",
            "control system operation manual", "control system manual", "bridge control system"
        ),
    },
    "Auxiliary Engine": {
        "Operation & Maintenance Manual": (
            "auxiliary generator diesel engine", "auxiliary generator", "auxiliary engine",
            "generator diesel engine", "auxiliary diesel engine", "diesel generator manual",
            "diesel generator", "aux engine manual", "aux engine", "dg manual",
            "yanmar generator", "daihatsu generator", "cummins dg", "auxiliary engine manual",
            "auxiliary generator engine", "auxiliary gen", "aux. generator", "aux. engine",
            "aux gen engine", "operation manual for auxiliary generator", "operation manual for auxiliary",
            "instruction manual for auxiliary generator"
        ),
    },
    "Boiler": {
        "Operation & Maintenance Manual": (
            "vertical composite boiler", "composite boiler", "auxiliary boiler", "exhaust gas boiler",
            "boiler manual", "boiler instruction", "miura boiler", "aalborg boiler", "kangrim boiler",
            "oil fired boiler", "steam boiler", "boiler operation"
        ),
    },
    "Bridge Equipments": {
        "Bridge Equipments": (
            "bridge equipment", "bridge equipments", "radar manual", "ecdis manual", "gyro compass",
            "autopilot manual", "ais manual", "gps navigator", "vdr manual", "navtex manual",
            "magnetic compass", "echo sounder", "speed log manual"
        ),
    },
    "Cargo": {
        "Ballast System Manual": ("ballast system manual", "ballast pump manual", "framo ballast", "taiko ballast"),
        "Cargo Crane Manual": ("cargo crane manual", "macgregor crane", "ihi crane", "mitsubishi crane", "deck crane manual", "hose crane"),
        "Cargo Pump Manual": ("cargo pump manual", "framo cargo pump", "shinko cargo pump", "submerged cargo pump", "cargo oil pump"),
        "COW Manual": ("crude oil washing manual", "cow manual", "crude oil wash"),
        "Hatch Cover Manual": ("hatch cover manual", "macgregor hatch", "tts hatch cover", "hydraulic hatch cover"),
        "IG System Manual": ("inert gas system manual", "ig system manual", "inert gas generator", "igs manual"),
        "ODME Manual": ("odme manual", "oil discharge monitoring equipment", "rivertrace odme", "o.d.m.", "odm manual", "r.o.b. manual", "rob manual", "oil discharge monitor", "odme"),
    },
    "Deck Machinery": {
        "Mooring Winch Manual": ("mooring winch manual", "hydraulic mooring winch", "electric mooring winch", "mooring winch"),
        "Windlass Manual": ("windlass manual", "anchor windlass manual", "hydraulic windlass", "fukushima windlass", "anchor windlass"),
    },
    "Electrical": {
        "Main Switchboard Manual": ("main switchboard manual", "msb instruction manual", "terasaki msb", "hyundai switchboard"),
        "Power Management System": ("power management system manual", "pms manual", "deif pms", "terasaki pms"),
    },
    "Main Engine": {
        "Operation & Maintenance Manual": (
            "main engine operation", "main engine manual", "me instruction manual", "main engine maintenance",
            "main engine", "man b&w", "win gd", "wartsila", "mitsubishi ue", "main diesel engine",
            "me-c", "me-b", "flex engine", "two stroke engine",
            "operation & data", "operation and data", "operation & maintenance", "operation", "maintenance",
            "component", "component no", "accessories with engine", "manoeuvering system", "maneuvering system",
            "manoeuvering", "maneuvering", "list of spare parts", "spare parts and tools", "spare parts", "tools (main engine)",
        ),
        "Other Manuals": (
            "other manuals", "engine maker manual", "special tool manual", "engine room control air dryer",
            "control air dryer", "air dryer", "engine room pumps", "engine room pump", "pumps manual",
            "jacket water pre heater", "jacket water heater", "pre heater", "lube oil purifier",
            "fuel oil purifier", "oil purifier", "fresh water generator", "fw generator",
            "heat exchanger", "plate cooler", "starting air compressor", "air compressor manual"
        ),
    },
    "Pollution": {
        "BWTS Manual": ("bwts manual", "ballast water treatment system manual", "pureballast", "optimarin", "erma first", "oceanmaster"),
        "EGR System Manual": ("egr system manual", "exhaust gas recirculation manual"),
        "Exhaust Gas Scrubber Manual": ("exhaust gas scrubber", "egcs manual", "scrubber manual", "alfa laval puresox", "wartsila scrubber"),
        "Incinerator Manual": ("incinerator manual", "teamtec incinerator", "miura incinerator", "maxi incinerator"),
        "OWS Manual": (
            "ows manual", "oily water separator manual", "bilge separator manual", "bilge separator",
            "oily bilge separator", "deckma ows", "boss ows", "oily water separator"
        ),
        "SCR System Manual": ("scr system manual", "selective catalytic reduction", "scr manual"),
        "Sewage Treatment Plant Manual": ("sewage treatment plant", "stp manual", "hamann stp", "taiko kikai stp", "jetted sewage"),
    },
    "Propulsion": {
        "Shaft Generator Manual": ("shaft generator", "shaft generator manual", "pto generator", "sg manual"),
    },
    "Refrigeration": {
        "AC Plant Manual": ("ac plant manual", "air conditioning manual", "provision plant manual", "daikin ac", "sabroe ac"),
    },
    "Safety": {
        "CO2 System Manual": ("co2 system manual", "co2 fire", "co2 system", "carbon dioxide system", "fixed co2"),
        "Emergency Generator Manual": ("emergency generator", "em gen", "emergency diesel generator", "edg manual"),
        "Fire Alarm Manual": ("fire alarm manual", "fire alarm system", "consilium fire", "tyco fire"),
        "Fire Detection System Manual": ("fire detection system", "fire detection manual", "smoke detection", "fire & smoke"),
    },
    "Shafting": {
        "CPP Manual": ("cpp manual", "controllable pitch propeller manual", "cpp instruction", "kamome cpp"),
        "Stern Tube Manual": ("stern tube manual", "stern tube seal manual", "kobe steel stern tube", "wartsila seal"),
    },
    "Steering Gear": {
        "Maintenance Manual": ("steering gear maintenance", "steering gear service manual"),
        "Operation Manual": ("steering gear operation", "steering gear manual", "steering gear", "hatlapa steering", "yokogawa steering"),
    },
    "Thrusters": {
        "Operation & Maintenance Manual": ("bow thruster manual", "stern thruster manual", "thruster operation", "nakashima thruster", "brunvoll thruster", "thruster manual", "thrusters manual"),
    },
    "To Be Classified": {
        "To Be Classified": ("to be classified", "unclassified manual", "general manual"),
    },
}

# Department mapping
DEPARTMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Technical & Crewing": ("technical", "crewing", "drawing", "manual", "maintenance", "engine", "hull", "electrical", "service agreement", "class", "statutory", "survey", "drydock"),
    "Commercial & Chartering": ("charter party", "fixture recap", "freight invoice", "hire statement", "laytime", "demurrage", "chartering", "commercial", "broker"),
    "Insurance": ("p&i", "h&m", "hull and machinery", "protection and indemnity", "war risk", "insurance claim", "surveyor report", "loss prevention", "cover note", "policy"),
    "Kaizen - Knowledge Bank": ("kaizen", "knowledge bank", "standard operating procedure", "best practice", "circular", "fleet standard", "guideline"),
}


def _norm_match_key(value: str) -> str:
    """Build a comparison key for vessel matching by removing separators."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _parse_known_vessels(known_vessels: list[Any] | None) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Extract vessel names, IMO map (7-digit IMO -> vessel name), and hull map (normalized hull -> vessel name) from known_vessels."""
    names: list[str] = []
    imo_map: dict[str, str] = {}
    hull_map: dict[str, str] = {}

    if known_vessels:
        for item in known_vessels:
            if not item:
                continue
            v_name = None
            v_imo = None
            v_hull = None

            if isinstance(item, str):
                v_name = item.strip()
            elif isinstance(item, dict):
                v_name = str(item.get("name") or "").strip()
                v_imo = str(item.get("imo") or "").strip()
                v_hull = str(item.get("hull_number") or "").strip()
            elif hasattr(item, "name"):
                v_name = str(getattr(item, "name", "") or "").strip()
                v_imo = str(getattr(item, "imo", "") or "").strip()
                v_hull = str(getattr(item, "hull_number", "") or "").strip()

            if v_name:
                names.append(v_name)
                if v_imo and v_imo.isdigit() and len(v_imo) == 7 and v_imo != "0000000":
                    imo_map[v_imo] = v_name
                if v_hull and v_hull.strip() and v_hull.strip().lower() not in ("—", "none", "null"):
                    hull_clean = v_hull.strip()
                    hull_key = _norm_match_key(hull_clean)
                    if hull_key:
                        hull_map[hull_key] = v_name
                        digits = re.sub(r"\D", "", hull_clean)
                        if digits and len(digits) >= 2:
                            hull_map[digits] = v_name

    return names, imo_map, hull_map


def _make_flexible_phrase_regex(phrase: str) -> str:
    """Build a regex pattern that matches phrase with flexible spaces, dashes, dots, or underscores between words."""
    cleaned = (phrase or "").strip()
    if not cleaned:
        return ""
    words = [re.escape(w) for w in re.split(r"[\s/\\_\-.,;:]+", cleaned) if w]
    if not words:
        return ""
    if len(words) == 1:
        w = words[0]
        if w.isdigit():
            return rf"(?:^|[\s/\\_\-.,;:()\[\]])(?:s\.?no\.?|hull|h|s|ship\s*no\.?|ship)?\s*{w}(?:$|[\s/\\_\-.,;:()\[\]])"
        return rf"(?:^|[\s/\\_\-.,;:()\[\]]){w}(?:$|[\s/\\_\-.,;:()\[\]])"
    inner = r"[\s/\\_\-.,;:]+".join(words)
    return rf"(?:^|[\s/\\_\-.,;:()\[\]]){inner}(?:$|[\s/\\_\-.,;:()\[\]])"


GENERIC_FOLDER_NAMES: set[str] = {
    # SharePoint roots & system folders
    "shared documents", "documents", "root", "forms", "site assets", "site pages",
    "all items", "style library", "lists",
    # Departments & organizational units
    "technical", "technical & crewing", "commercial", "commercial & chartering",
    "insurance", "knowledge bank", "kaizen - knowledge bank", "kaizen",
    "crewing", "hseq", "finance", "accounts", "operation", "operations", "marine",
    "it", "admin", "administration", "human resources", "hr",
    # Generic container & navigation folders
    "type of vessel", "vessel type", "vessel name", "vessel", "vessels",
    "all vessels", "general", "specific vessels", "vessel management",
    "our fleet", "fleet", "ships", "ship",
    # Taxonomy group folders
    "drawings and manuals", "drawings & manuals", "drawing and manual",
    "drawings", "drawing", "manuals", "manual", "plans", "plan",
    "specifications", "specification", "certificates", "certificate",
    # Common categories and equipment folders that should not be mistaken for vessel names
    "basic", "hull", "machinery", "electrical", "safety", "automation",
    "electric part", "piping", "navigation", "cargo", "pollution", "refrigeration",
    "deck machinery", "other manuals", "archive", "to be classified",
    "main engine", "mb main engine", "auxiliary engine", "diesel generator",
    "boiler", "shafting", "steering gear", "propulsion", "thrusters",
    # Month / period folders
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}


def _is_dm_marker(s: str) -> bool:
    sl = s.lower().strip()
    if sl in ("drawings", "drawing", "manuals", "manual"):
        return True
    return any(
        sl.startswith(stem) or stem in sl
        for stem in ("drawings and manuals", "drawings & manuals", "drawing and manual", "drawing & manual")
    )


def _is_generic_or_system_folder(s: str) -> bool:
    sl = s.lower().strip()
    if sl in GENERIC_FOLDER_NAMES:
        return True
    if _is_dm_marker(sl):
        return True
    # Equipment folder prefixes like MB-, ME-, AE-, DG-, AUX-
    if any(sl.startswith(p) for p in ("mb ", "me ", "ae ", "dg ", "aux ")):
        return True
    # Equipment / index codes like 001.900_..., 001.901_..., 01_...
    if re.match(r"^\d+([._]\d+)*[_\s-]", sl):
        return True
    return False


def _extract_vessel_from_folder_path(path: str | None) -> str | None:
    """Extract vessel name from SharePoint folder hierarchy when not found in known vessels.
    Handles structures like:
      - Technical / SC413 / Drawings and Manuals / ...
      - Technical / SC413 / type of vessel / Drawings and Manuals / ...
      - Technical / type of vessel / SC413 / Drawings and Manuals / ...
      - Vessels / Specific Vessels / SC413 / ...
      - SC413 / Drawings / ...
    """
    if not path:
        return None
    norm_path = str(path).replace("\\", "/").replace(">", "/")
    segments = [s.strip() for s in norm_path.split("/") if s.strip()]
    if not segments:
        return None

    marker_idx = next((i for i, s in enumerate(segments) if _is_dm_marker(s)), -1)

    if marker_idx > 0:
        # Scan backward from the marker towards the root: the first non-generic segment is the vessel!
        for s in reversed(segments[:marker_idx]):
            if not _is_generic_or_system_folder(s):
                return s

    # If marker found, also check segment immediately after marker if it's a valid vessel name (not generic/category)
    if marker_idx >= 0 and marker_idx + 1 < len(segments):
        after_seg = segments[marker_idx + 1]
        if not _is_generic_or_system_folder(after_seg) and after_seg.lower() not in ("drawings", "drawing", "manuals", "manual"):
            return after_seg

    # Fallback if no marker or all segments before marker were generic:
    for s in segments:
        if not _is_generic_or_system_folder(s):
            return s

    return None


def _match_known_vessel(
    cand: str,
    known_vessels: list[Any] | None = None,
) -> str | None:
    """Matches a candidate vessel string against VESSEL_ALIASES, VESSEL_MASTER_LIST,
    and any custom known_vessels list. Returns the canonical master vessel name or None.
    """
    if not cand:
        return None
    cand_clean = cand.strip()
    if not cand_clean or len(cand_clean) < 2:
        return None

    cand_lower = cand_clean.lower()
    # Strip common prefixes (M/V, MV, M.V., M/T, MT, M.T., SS, HULL, SNO, S.NO., S/NO, SHIP NO., SHIP)
    cand_core = re.sub(r"^(mv|m/v|m\.v\.|mt|m/t|m\.t\.|ss|hull|sno|s\.no\.|s\.no|s/no|ship\s+no\.|ship\s+no|ship)\s*", "", cand_lower).strip()
    cand_key = _norm_match_key(cand_core or cand_lower)

    # 1. Exact or alias match in VESSEL_ALIASES
    for canonical_vessel, aliases in VESSEL_ALIASES.items():
        canonical_lower = canonical_vessel.lower()
        canonical_key = _norm_match_key(canonical_lower)
        if (
            cand_lower == canonical_lower or
            cand_core == canonical_lower or
            (cand_key and cand_key == canonical_key)
        ):
            return canonical_vessel
        for alias in aliases:
            a_clean = alias.strip().lower()
            if not a_clean:
                continue
            if cand_lower == a_clean or cand_core == a_clean or (cand_key and cand_key == _norm_match_key(a_clean)):
                return canonical_vessel

    # 2. Check VESSEL_MASTER_LIST
    for v in VESSEL_MASTER_LIST:
        v_clean = v.strip()
        v_lower = v_clean.lower()
        v_core = re.sub(r"^(mv|m/v|m\.v\.|mt|m/t|m\.t\.|ss|hull)\s+", "", v_lower).strip()
        if (
            cand_lower == v_lower or
            cand_core == v_core or
            cand_core == v_lower or
            (cand_key and cand_key == _norm_match_key(v_core or v_lower))
        ):
            return v_clean

    # 3. Check custom known_vessels if provided
    if known_vessels:
        parsed_names, imo_map, hull_map = _parse_known_vessels(known_vessels)
        if cand in imo_map:
            return imo_map[cand]
        cand_h_key = _norm_match_key(cand)
        if cand_h_key in hull_map:
            return hull_map[cand_h_key]

        for v in parsed_names:
            if not v:
                continue
            v_clean = v.strip()
            v_lower = v_clean.lower()
            v_core = re.sub(r"^(mv|m/v|m\.v\.|mt|m/t|m\.t\.|ss|hull)\s+", "", v_lower).strip()
            if (
                cand_lower == v_lower or
                cand_core == v_core or
                cand_core == v_lower or
                (cand_key and cand_key == _norm_match_key(v_core or v_lower))
            ):
                return v_clean

    return None


def extract_vessel_name_from_text(text: str, filename: str = "", known_vessels: list[Any] | None = None) -> str | None:
    """Detect vessel name from text and filename strictly validated against master vessel list, aliases, or custom known vessels."""
    vessel_res = _classify_vessel_tiered(
        text,
        filename,
        known_vessels=known_vessels,
        text_is_usable=is_text_usable_for_classification(text),
    )
    return vessel_res.get("value") or None


def classify_document_content(
    text: str,
    filename: str = "",
    known_vessels: list[str] | None = None,
) -> dict[str, Any]:
    """Perform full hierarchical classification of a document based on text & filename.

    Returns the exact 4 SharePoint metadata fields:
        Category (Drawing / Manual)
        Group (e.g. Basic, Hull, Machinery, Cargo, Electrical, etc.)
        Sub-Category (e.g. Bulkhead plans, General Arrangement, Single Line Diagram, etc.)
        Vessel Name (e.g. Bow Fraternity)

    Files with unmatched or low-confidence specifications are routed to 'To Be Classified'
    under the same matched vessel.
    """
    content = f"{filename}\n{text}".lower()
    fn_lower = filename.lower()

    # 1. Detect Vessel Name independently
    detected_vessel = extract_vessel_name_from_text(text, filename, known_vessels)

    # 2. Check Drawing Taxonomy matches
    best_drawing_group: str | None = None
    best_drawing_subcat: str | None = None
    best_drawing_score = 0.0
    drawing_matches: list[str] = []

    for grp_name, subcats in DRAWING_TAXONOMY.items():
        # Check generic group name match (medium weight)
        grp_lower = grp_name.lower()
        if grp_lower in content:
            grp_weight = 3.0 if grp_lower in fn_lower else 1.5
            best_drawing_score += content.count(grp_lower) * grp_weight
            drawing_matches.append(grp_name)
            if not best_drawing_group:
                best_drawing_group = grp_name

        # Check specific subcategory term match (high weight)
        for subcat_name, keywords in subcats.items():
            score = 0.0
            for kw in keywords:
                if kw in content:
                    kw_weight = 8.0 if kw in fn_lower else 5.0
                    score += content.count(kw) * kw_weight
                    drawing_matches.append(subcat_name)
            if score > best_drawing_score:
                best_drawing_score = score
                best_drawing_group = grp_name
                best_drawing_subcat = subcat_name

    # 3. Check Manual Taxonomy matches
    best_manual_group: str | None = None
    best_manual_subcat: str | None = None
    best_manual_score = 0.0
    manual_matches: list[str] = []

    for grp_name, subcats in MANUAL_TAXONOMY.items():
        # Check generic group name match (medium weight)
        grp_lower = grp_name.lower()
        if grp_lower in content:
            grp_weight = 3.0 if grp_lower in fn_lower else 1.5
            best_manual_score += content.count(grp_lower) * grp_weight
            manual_matches.append(grp_name)
            if not best_manual_group:
                best_manual_group = grp_name

        # Check specific subcategory term match (high weight)
        for subcat_name, keywords in subcats.items():
            score = 0.0
            for kw in keywords:
                if kw in content:
                    kw_weight = 8.0 if kw in fn_lower else 5.0
                    score += content.count(kw) * kw_weight
                    manual_matches.append(subcat_name)
            if score > best_manual_score:
                best_manual_score = score
                best_manual_group = grp_name
                best_manual_subcat = subcat_name

    # Check filename clues
    if any(k in fn_lower for k in ("drawing", "dwg", "plan", "diagram", "schematic")):
        best_drawing_score += 4.0
    if any(k in fn_lower for k in ("manual", "instruction", "guide", "booklet", "operation")):
        best_manual_score += 4.0

    # 4. Decide Category, Group, Sub-Category
    # 4. Decide Group (Drawing/Manual), Category (Basic/Hull/Electrical/etc.), Sub-Category
    matched_kws: list[str] = []
    confidence = 0.35

    if best_drawing_score >= best_manual_score and best_drawing_score > 0:
        group = "Drawing"
        category = best_drawing_group or "Basic"
        sub_category = best_drawing_subcat or (list(DRAWING_TAXONOMY.get(category, {}).keys())[0] if DRAWING_TAXONOMY.get(category) else "Basic Drawings")
        matched_kws = list(dict.fromkeys(drawing_matches))[:6]
        # High confidence if specific subcategory was matched
        has_specific_subcat = best_drawing_subcat is not None
        base_conf = 0.75 if has_specific_subcat else 0.55
        confidence = min(0.98, base_conf + (best_drawing_score * 0.03))
    elif best_manual_score > 0:
        group = "Manual"
        category = best_manual_group or "To Be Classified"
        sub_category = best_manual_subcat or (list(MANUAL_TAXONOMY.get(category, {}).keys())[0] if MANUAL_TAXONOMY.get(category) else "To Be Classified")
        matched_kws = list(dict.fromkeys(manual_matches))[:6]
        has_specific_subcat = best_manual_subcat is not None
        base_conf = 0.75 if has_specific_subcat else 0.55
        confidence = min(0.98, base_conf + (best_manual_score * 0.03))
    else:
        # Fallback to "To Be Classified" under the vessel
        group = "Manual"
        category = "To Be Classified"
        sub_category = "To Be Classified"
        confidence = 0.35

    # 5. Department Detection
    detected_dept = "Technical & Crewing"
    best_dept_score = 0
    for dept, keywords in DEPARTMENT_KEYWORDS.items():
        score = sum(content.count(k) for k in keywords)
        if score > best_dept_score:
            best_dept_score = score
            detected_dept = dept

    vessel_display = detected_vessel or "{vessel}"
    
    if category == "To Be Classified" or sub_category == "To Be Classified":
        suggested_path_parts = [detected_dept, vessel_display, "Drawings and Manuals", "To be Classified"]
    else:
        suggested_path_parts = [detected_dept, vessel_display, "Drawings and Manuals", group, category]

    return {
        "vessel_name": detected_vessel or "",
        "department": detected_dept,
        "group": group,                     # SharePoint Group column ("Drawing" or "Manual")
        "category": category,               # SharePoint Category column ("Basic", "Electrical", "Hull", etc.)
        "sub_category": sub_category,       # SharePoint Sub-Category column ("Bulkhead plans", etc.)
        "sub_category_1": group,            # Backward compatibility
        "sub_category_2": category,         # Backward compatibility
        "leaf": sub_category,               # Backward compatibility
        "suggested_path": "/".join(suggested_path_parts),
    }


def normalize_ocr_text(text: str) -> str:
    """Normalize OCR text:
    - Replace smart quotes, unicode hyphens/dashes, symbols
    - Collapse single-character tracking artifacts for letters (e.g. 'G H A N A' -> 'GHANA', 'I N D E X' -> 'INDEX')
    - Collapse single-character tracking artifacts for isolated digits (e.g. '7 2 1' -> '721')
    - Normalize whitespace while preserving line structure
    """
    if not text:
        return ""

    # 1. Normalize unicode quotation marks, accents, dashes, dots
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("·", ".").replace("•", ".")

    # 2. Collapse runs of single letters separated by space
    letter_pattern = re.compile(r'(?<!\S)(?:[A-Za-z]\s+)+[A-Za-z](?!\S)')
    t = letter_pattern.sub(lambda m: re.sub(r'\s+', '', m.group(0)), t)

    # 3. Collapse runs of isolated single digits separated by space
    digit_pattern = re.compile(r'(?<!\S)(?:[0-9]\s+)+[0-9](?!\S)')
    t = digit_pattern.sub(lambda m: re.sub(r'\s+', '', m.group(0)), t)

    # 4. Collapse multiple horizontal spaces/tabs on each line
    lines = [re.sub(r'[ \t]+', ' ', line.strip()) for line in t.splitlines()]
    return "\n".join(lines).strip()


def is_text_usable_for_classification(text: str, min_words: int = 4) -> bool:
    """Returns False if extracted text is mostly noise/fragments and shouldn't be trusted for vessel/category matching."""
    if not text:
        return False
    norm = normalize_ocr_text(text)
    words = re.findall(r'\b[A-Za-z0-9]{3,}\b', norm)
    return len(words) >= min_words


def _classify_department_tiered(
    text: str,
    filename: str = "",
    source_path: str = "",
    text_is_usable: bool = True,
) -> dict[str, Any]:
    """Tier 1: Folder path context or explicit text cue (>= 85%).
    Tier 2: Fuzzy keyword matching across DEPARTMENT_KEYWORDS.
    """
    # Tier 1: Source folder breadcrumbs (highest authority)
    if source_path:
        norm_path = source_path.replace("\\", "/").replace(">", "/").lower()
        for dept in DEPARTMENT_KEYWORDS.keys():
            dept_clean = dept.lower()
            if dept_clean in norm_path or dept_clean.replace(" & ", " and ") in norm_path or dept_clean.replace(" & ", "_") in norm_path:
                return {"value": dept, "confidence": 0.98, "tier": 1}

    combined = f"{filename}\n{text}".lower()
    fn_lower = filename.lower()
    target_content = combined if text_is_usable else fn_lower

    # Tier 1: Explicit Department Header/Pattern in document text (only if text is usable)
    if text_is_usable:
        dept_header_match = re.search(
            r"(?:department|division|dept)\s*[:\-\.]?\s*([A-Za-z\s&]{4,40})",
            combined,
            re.IGNORECASE,
        )
        if dept_header_match:
            dept_str = dept_header_match.group(1).strip()
            for dept in DEPARTMENT_KEYWORDS.keys():
                if dept.lower() in dept_str.lower():
                    return {"value": dept, "confidence": 0.92, "tier": 1}

    # Tier 2: Keyword scoring across DEPARTMENT_KEYWORDS
    best_dept = "Technical & Crewing"
    best_score = 0
    for dept, keywords in DEPARTMENT_KEYWORDS.items():
        score = sum(target_content.count(k.lower()) * (4.0 if k.lower() in fn_lower else 1.0) for k in keywords)
        if score > best_score:
            best_score = score
            best_dept = dept

    if best_score >= 3:
        conf = min(0.92, 0.75 + (best_score * 0.03))
        tier = 1 if best_score >= 4 else 2
        return {"value": best_dept, "confidence": round(conf, 2), "tier": tier}
    elif best_score > 0:
        conf = 0.80 if (text_is_usable or any(k.lower() in fn_lower for k in DEPARTMENT_KEYWORDS.get(best_dept, ()))) else 0.40
        return {"value": best_dept, "confidence": conf, "tier": 2}
    
    # Default fallback
    return {"value": "Technical & Crewing", "confidence": 0.70 if text_is_usable else 0.50, "tier": 2}


def _classify_vessel_tiered(
    text: str,
    filename: str = "",
    known_vessels: list[Any] | None = None,
    source_path: str = "",
    text_is_usable: bool = True,
) -> dict[str, Any]:
    """Tier 1: Direct exact match in text / filename / IMO number / hull number alias / strong vessel header (>= 85%).
    Tier 2: Source folder breadcrumbs fallback / fuzzy matching / custom known vessel list.
    """
    norm_text = normalize_ocr_text(text)
    fn_lower = (filename or "").lower()
    norm_fn_key = _norm_match_key(filename)
    has_text = bool(norm_text and len(norm_text.strip()) > 0)

    # Header / Page 1 text (first 3000 chars of text)
    header_text = norm_text[:3000].lower() if has_text else ""
    full_text_lower = norm_text.lower() if has_text else ""

    parsed_names, imo_map, hull_map = _parse_known_vessels(known_vessels)

    # ── Tier 1: Exact IMO Number match in document text or filename ──
    if imo_map:
        imo_matches = re.findall(r"\b(?:imo|imo\s*no\.?|imo\s*num\.?|imo\s*number|imo\s*#)\s*[:\-\.#]?\s*([0-9]{7})\b", f"{fn_lower}\n{full_text_lower}", re.IGNORECASE)
        for imo_num in imo_matches:
            if imo_num in imo_map:
                return {"value": imo_map[imo_num], "confidence": 0.99, "tier": 1}
        for num in re.findall(r"\b([0-9]{7})\b", fn_lower):
            if num in imo_map:
                return {"value": imo_map[num], "confidence": 0.99, "tier": 1}

    # ── Tier 1: Candidate vessel scoring across filename, header/page 1, full text, and DB hull map ──
    vessel_scores: dict[str, float] = {}
    all_vessels = list(dict.fromkeys(list(VESSEL_ALIASES.keys()) + parsed_names + list(VESSEL_MASTER_LIST)))

    for v in all_vessels:
        v_aliases = list(VESSEL_ALIASES.get(v, ()))
        phrases = [v] + v_aliases
        score = 0.0

        for phrase in phrases:
            if not phrase or not phrase.strip():
                continue
            clean_phrase = phrase.strip().lower()
            p_pat = _make_flexible_phrase_regex(clean_phrase)
            p_key = _norm_match_key(clean_phrase)

            # Match in filename (e.g. SS378 in filename -> Huge score!)
            if p_pat and re.search(p_pat, fn_lower, re.IGNORECASE):
                score += 150.0
            elif len(p_key) >= 4 and not p_key.isdigit() and p_key in norm_fn_key:
                score += 120.0

            # Match in header / Page 1 (first 3000 chars)
            if header_text:
                if p_pat and re.search(p_pat, header_text, re.IGNORECASE):
                    weight = 80.0 if len(clean_phrase) >= 5 or clean_phrase.startswith("ss") else 40.0
                    score += weight
                elif len(p_key) >= 5 and not p_key.isdigit() and p_key in _norm_match_key(header_text):
                    score += 60.0

            # Match in full text (only for distinctive phrases with length >= 6)
            if full_text_lower and len(clean_phrase) >= 6:
                if p_pat and re.search(p_pat, full_text_lower, re.IGNORECASE):
                    score += 20.0

        # Check DB hull map
        if hull_map:
            for h_key, vname in hull_map.items():
                if vname == v and len(h_key) >= 3:
                    if h_key in norm_fn_key:
                        score += 150.0
                    if header_text and h_key in _norm_match_key(header_text):
                        score += 70.0

        if score > 0:
            vessel_scores[v] = score

    if vessel_scores:
        best_vessel = max(vessel_scores, key=vessel_scores.get)
        best_score = vessel_scores[best_vessel]
        if best_score >= 100.0:
            return {"value": best_vessel, "confidence": 0.98, "tier": 1}
        elif best_score >= 40.0:
            return {"value": best_vessel, "confidence": 0.95, "tier": 1}
        elif best_score >= 15.0:
            return {"value": best_vessel, "confidence": 0.85, "tier": 1}

    # ── Tier 1: Direct document text regex patterns ("Name of Ship: XYZ", "M/V XYZ") ──
    if has_text:
        patterns = [
            r"(?:name\s+of\s+ship|ship(?:'s|\s+)?name|vessel\s*(?:name)?|name\s+of\s+vessel)\s*[:\-\.]?\s*([A-Z0-9][A-Za-z0-9\s\.\-]{2,30})",
            r"(?:kind\s+of\s+ship|type\s+of\s+ship|ship\s*no\.?|s\.?no\.?|hull\s*no\.?)\s*[:\-\.]?\s*([A-Z0-9][A-Za-z0-9\s\.\-]{2,30})",
            r"(?:m/?v|m\.v\.|m/t|mt|m\.t\.)\s+([A-Z0-9][A-Za-z0-9\s\-]{2,30})",
            r"(?:vessel\s*[:\-]\s*)([A-Z0-9][A-Za-z0-9\s\-]{2,30})",
        ]
        for p in patterns:
            m = re.search(p, header_text or full_text_lower, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                cand = re.split(r"[\r\n\t,;]", cand)[0].strip()
                matched_vessel = _match_known_vessel(cand, known_vessels)
                if matched_vessel:
                    return {"value": matched_vessel, "confidence": 0.94, "tier": 1}
                # Check if document cue matches the folder-derived vessel (e.g. S.NO. SC-413 matching folder SC413)
                if source_path:
                    folder_v = _extract_vessel_from_folder_path(source_path)
                    if folder_v and _norm_match_key(cand) == _norm_match_key(folder_v):
                        return {"value": folder_v, "confidence": 0.95, "tier": 1}

    # ── Tier 2: Source folder breadcrumbs fallback (used only when text & filename have no vessel) ──
    if source_path:
        folder_v = _extract_vessel_from_folder_path(source_path)
        if folder_v:
            matched_folder_vessel = _match_known_vessel(folder_v, known_vessels)
            if matched_folder_vessel:
                return {"value": matched_folder_vessel, "confidence": 0.85, "tier": 2}

        norm_path = source_path.replace("\\", "/").replace(">", "/")
        segments = [s.strip() for s in norm_path.split("/") if s.strip()]
        for seg in reversed(segments):
            matched_vessel = _match_known_vessel(seg, known_vessels)
            if matched_vessel:
                return {"value": matched_vessel, "confidence": 0.85, "tier": 2}

        if folder_v:
            return {"value": folder_v, "confidence": 0.50, "tier": 2}

    return {"value": "", "confidence": 0.0, "tier": 2}


def _classify_category_tiered(
    text: str,
    filename: str = "",
) -> dict[str, Any]:
    """Tier 1: Unambiguous document type cues in text / filename (>= 85%).
    Tier 2: Relative keyword frequency comparison between Drawing and Manual taxonomies.
    """
    combined = f"{filename}\n{text}".lower()
    fn_lower = filename.lower()

    # Tier 1: Check filename cues first (highest confidence)
    manual_tier1_patterns = [
        r"\binstruction\s+manual\b",
        r"\boperation\s+manual\b",
        r"\bmaintenance\s+manual\b",
        r"\btraining\s+manual\b",
        r"\boperation\s*&\s*maintenance\b",
        r"\bsolas\s+training\s+manual\b",
        r"\boperating\s+instructions\b",
        r"\bservice\s+manual\b",
        r"\btechnical\s+manual\b",
        r"\buser\s+guide\b",
        r"\bhandbook\b",
        r"\br\.?o\.?b\.?\s*manual\b",
        r"\bo\.?d\.?m\.?(?:\s+control)?(?:\s+system)?\s*manual\b",
        r"\bmanual\b",
    ]
    for pat in manual_tier1_patterns:
        if re.search(pat, fn_lower, re.IGNORECASE):
            return {"value": "Manual", "confidence": 0.96, "tier": 1}

    drawing_tier1_patterns = [
        r"\bdwg(?:\.|\s+)?no\b",
        r"\bdrawing\s+no\b",
        r"\bfinished\s+plan\b",
        r"\bgeneral\s+arrangement\s+plan\b",
        r"\bsingle\s+line\s+diagram\b",
        r"\bschematic\s+diagram\b",
        r"\bkey\s+plan\b",
        r"\bdocking\s+plan\b",
        r"\bga\s+drawing\b",
        r"\bmidship\s+section\b",
        r"\bcapacity\s+plan\b",
        r"\bwheelhouse\s+arrangement\b",
    ]
    for pat in drawing_tier1_patterns:
        if re.search(pat, fn_lower, re.IGNORECASE):
            return {"value": "Drawing", "confidence": 0.96, "tier": 1}

    # Check text for manual cues (manual cues always take precedence over drawing stamps like 'FINISHED PLAN')
    for pat in manual_tier1_patterns:
        if re.search(pat, combined, re.IGNORECASE):
            return {"value": "Manual", "confidence": 0.94, "tier": 1}

    for pat in drawing_tier1_patterns:
        if re.search(pat, combined, re.IGNORECASE):
            return {"value": "Drawing", "confidence": 0.94, "tier": 1}

    # Tier 2: Score taxonomy keywords
    drawing_score = 0.0
    manual_score = 0.0

    target_content = combined if text_is_usable else fn_lower

    for grp, subcats in DRAWING_TAXONOMY.items():
        if grp.lower() in target_content:
            drawing_score += 1.5
        for subcat, kws in subcats.items():
            for kw in kws:
                if kw in target_content:
                    drawing_score += 2.0

    for grp, subcats in MANUAL_TAXONOMY.items():
        if grp.lower() in target_content:
            manual_score += 1.5
        for subcat, kws in subcats.items():
            for kw in kws:
                if kw in target_content:
                    manual_score += 2.0

    if drawing_score >= manual_score and drawing_score > 0:
        conf = min(0.92, 0.70 + (drawing_score * 0.03))
        if not text_is_usable and not any(k in fn_lower for k in ("drawing", "dwg", "plan", "diagram", "schematic")):
            conf = min(conf, 0.40)
        return {"value": "Drawing", "confidence": round(conf, 2), "tier": 2}
    elif manual_score > 0:
        conf = min(0.92, 0.70 + (manual_score * 0.03))
        if not text_is_usable and not any(k in fn_lower for k in ("manual", "instruction", "guide", "booklet")):
            conf = min(conf, 0.40)
        return {"value": "Manual", "confidence": round(conf, 2), "tier": 2}

    return {"value": "Drawing", "confidence": 0.50, "tier": 2}


def _classify_group_tiered(
    text: str,
    filename: str = "",
    text_is_usable: bool = True,
    source_path: str = "",
) -> dict[str, Any]:
    """Tier 1: Unambiguous document group cues in text / filename / source_path (>= 85%).
    Tier 2: Relative keyword frequency comparison between Drawing and Manual taxonomies.
    """
    combined = f"{filename}\n{text}".lower()
    fn_lower = filename.lower()
    path_lower = (source_path or "").lower()

    # Parse path segments to avoid false manual trigger from parent 'Drawings and Manuals' folder
    path_segments = [s.strip().lower() for s in (source_path or "").replace("\\", "/").split("/") if s.strip()]
    clean_segments = [
        s for s in path_segments
        if not any(s.startswith(p) for p in ("drawings and manuals", "drawings & manuals", "drawing and manual"))
    ]
    clean_path = " / ".join(clean_segments)

    # Manual cues from clean path (excluding the parent "Drawings and Manuals" folder)
    path_has_manual = (
        "manuals" in clean_path or "manual" in clean_path or
        "main engine" in clean_path or "mb main engine" in clean_path or
        "auxiliary engine" in clean_path or "boiler" in clean_path or
        "steering gear" in clean_path or "deck machinery" in clean_path or
        "operation" in clean_path or "maintenance" in clean_path
    )
    path_has_drawing = (
        "drawings" in clean_path or "drawing" in clean_path or "dwg" in clean_path or
        any(s in ("hull", "electrical", "machinery", "basic", "safety", "other drawings") for s in clean_segments)
    )

    manual_tier1_patterns = [
        r"\binstruction\s+manual\b",
        r"\boperation\s+manual\b",
        r"\bmaintenance\s+manual\b",
        r"\btraining\s+manual\b",
        r"\boperation\s*&\s*maintenance\b",
        r"\bsolas\s+training\s+manual\b",
        r"\boperating\s+instructions\b",
        r"\bservice\s+manual\b",
        r"\btechnical\s+manual\b",
        r"\buser\s+guide\b",
        r"\bhandbook\b",
        r"\br\.?o\.?b\.?\s*manual\b",
        r"\bo\.?d\.?m\.?(?:\s+control)?(?:\s+system)?\s*manual\b",
        r"\bmanual\b",
    ]

    drawing_tier1_patterns = [
        r"\bdwg(?:\.|\s+)?no\b",
        r"\bdrawing\s+no\.?\b",
        r"\bfinished\s+plan\b",
        r"\blist\s+of\s+finished\s+drawings\b",
        r"\bfinished\s+drawings\b",
        r"\bgeneral\s+arrangement\s+plan\b",
        r"\bgeneral\s+arrangement\b",
        r"\bsingle\s+line\s+diagram\b",
        r"\bschematic\s+diagram\b",
        r"\bkey\s+plan\b",
        r"\bdocking\s+plan\b",
        r"\bga\s+drawing\b",
        r"\bga\s+plan\b",
        r"\bmidship\s+section\b",
        r"\bcapacity\s+plan\b",
        r"\bwheelhouse\s+arrangement\b",
        r"\belectrical\s+diagram\b",
        r"\bwiring\s+diagram\b",
        r"\bcable\s+plan\b",
        r"\bpenetration\s+(?:register|plan|drawing)\b",
        r"\bpipe\s+(?:plan|diagram|drawing)\b",
        r"\bmachinery\s+parts?\b",
        r"\bspare\s+parts?\s*(?:&|and)\s*tools?\s+list\b",
        r"\bspare\s+parts?\s+list\b",
        # ── Hull part / sounding table cues (always drawings) ──
        r"\bhull\s+part\b",
        r"\(hull\s+part\)",
        r"\bsounding\s+table\b",
        r"\blevel\s+gauge\b",
        # ── Deck plan / fire plan / condition plan cues ──
        # Ship drawings (deck plans, GA, fire plans) always have a symbol legend:
        r"\bcondition\s+plan\b",
        r"\bplan\s+on\s+deck\b",
        r"\bdeck\s+plan\b",
        r"\bprofile\s+(?:and|\&)\s+deck\b",
        r"\bfire\s+control\s+plan\b",
        r"\bfire\s+(?:and|\&)\s+safety\s+plan\b",
        r"\bfire\s+fighting\s+plan\b",
        r"\blife\s+saving\s+appliances\s+plan\b",
        r"\bdamage\s+control\s+plan\b",
        r"\bshell\s+expansion\b",
        r"\bmidship\s+construction\b",
        r"\bmooring\s+arrangement\b",
        # Legend/symbol tables are present on virtually ALL ship technical drawings:
        r"\bsymbol\b.{0,40}\bexplanation\b",
        r"\bexplanation\b.{0,40}\bremarks\b",
        r"\bsymbol\b.{0,40}\bremarks\b",
        r"\bship\s+no\.?\b",
        r"\bhull\s+no\.?\b",
    ]

    # Filename manual and drawing keyword checks
    _MANUAL_FN_KEYWORDS = (
        "operation", "maintenance", "maint", "overhaul", "instruction",
        "specification", "spare", "spares", "tool", "tools", "procedure",
        "service", "repair", "data", "component", "guide",
        "manual", "operator", "list of spare", "technical data",
        "manoeuvering", "maneuvering", "spare parts", "parts list", "accessories with engine",
        "parts book", "parts catalog", "parts catalogue", "catalog", "catalogue",
        "troubleshooting", "handbook", "booklet", "egr"
    )
    _DRAWING_FN_KEYWORDS = (
        "dwg", "drawing", "plan", "diagram", "layout", "arrangement",
        "elevation", "detail", "section", "register", "schematic", "ga drawing",
        "hull part", "hull parts", "(hull part)", "sounding table", "level gauge"
    )
    fn_has_manual = any(k in fn_lower for k in _MANUAL_FN_KEYWORDS)
    fn_has_drawing = any(k in fn_lower for k in _DRAWING_FN_KEYWORDS)
    fn_has_machinery_part_drawing = bool(re.search(r"\bmachinery\s+parts?\b", fn_lower, re.IGNORECASE))
    # Equipment section code e.g. MB-1, MB-2, ME-1, AE-2 (marine equipment manual volumes)
    is_equipment_code = bool(re.match(r"^[a-z]{2}-\d+", fn_lower))
    if is_equipment_code and not fn_has_drawing and not fn_has_machinery_part_drawing:
        fn_has_manual = True

    # ── Tier 1: Check filename cues FIRST (highest confidence) ──
    # 1. Clear drawing filename
    for pat in drawing_tier1_patterns:
        if re.search(pat, fn_lower, re.IGNORECASE):
            return {"value": "Drawing", "confidence": 0.96, "tier": 1}
    if fn_has_drawing and not fn_has_manual:
        return {"value": "Drawing", "confidence": 0.96, "tier": 1}

    # 2. Clear manual filename
    for pat in manual_tier1_patterns:
        if re.search(pat, fn_lower, re.IGNORECASE):
            return {"value": "Manual", "confidence": 0.96, "tier": 1}
    if fn_has_manual and not fn_has_drawing:
        return {"value": "Manual", "confidence": 0.96, "tier": 1}

    # 3. Clear path cues (if filename is ambiguous)
    if path_has_manual and not path_has_drawing and not fn_has_drawing:
        return {"value": "Manual", "confidence": 0.95, "tier": 1}
    if path_has_drawing and not path_has_manual and not fn_has_manual:
        return {"value": "Drawing", "confidence": 0.95, "tier": 1}

    # Check text for drawing cues FIRST.
    # Important: even if filename suggests manual, legend tables in drawings can contain
    # phrases like 'plan on deck', 'symbol / explanation / remarks', 'drawing no.' etc.
    # Engine manuals are excluded ONLY when path also confirms a manual sub-folder.
    _is_certain_manual_path = path_has_manual and not path_has_drawing
    if text_is_usable and not _is_certain_manual_path:
        for pat in drawing_tier1_patterns:
            if re.search(pat, combined, re.IGNORECASE | re.DOTALL):
                return {"value": "Drawing", "confidence": 0.95, "tier": 1}

    if text_is_usable:
        for pat in manual_tier1_patterns:
            if re.search(pat, combined, re.IGNORECASE):
                # Additional guard: if strong drawing visual structure cues are present
                # in the document, don't let a Manual pattern override it.
                # (e.g. 'operation manual' in the notes section of a fire control plan)
                _drawing_visual_cues = [
                    r"\bsymbol\b", r"\bexplanation\b", r"\bremarks\b",
                    r"\bplan\s+on\s+deck\b", r"\bcondition\s+plan\b",
                    r"\bship\s+no\.?\b", r"\bhull\s+no\.?\b",
                    r"\bdeck\s+plan\b", r"\bfire\s+control\s+plan\b",
                    r"\bgeneral\s+arrangement\b",
                ]
                _drawing_visual_count = sum(
                    1 for p in _drawing_visual_cues
                    if re.search(p, combined, re.IGNORECASE)
                )
                if _drawing_visual_count >= 2 and not _is_certain_manual_path:
                    # Document has drawing visual structure despite manual-sounding text
                    return {"value": "Drawing", "confidence": 0.93, "tier": 1}
                return {"value": "Manual", "confidence": 0.94, "tier": 1}

    # Tier 2: Keyword scoring
    drawing_score = 0.0
    manual_score = 0.0

    if any(k in fn_lower for k in ("drawing", "dwg", "plan", "diagram", "schematic", "arr.", "arrangement")):
        drawing_score += 4.0
    if any(k in fn_lower for k in ("manual", "instruction", "guide", "booklet", "operation", "procedure", "maintenance", "spare", "component")):
        manual_score += 4.0
    if path_has_manual:
        manual_score += 5.0
    if path_has_drawing:
        drawing_score += 5.0

    target_content = combined if text_is_usable else fn_lower

    for grp, subcats in DRAWING_TAXONOMY.items():
        if grp.lower() in target_content:
            drawing_score += 1.5
        for subcat, kws in subcats.items():
            for kw in kws:
                if kw in target_content:
                    drawing_score += 2.0

    for grp, subcats in MANUAL_TAXONOMY.items():
        if grp.lower() in target_content:
            manual_score += 1.5
        for subcat, kws in subcats.items():
            for kw in kws:
                if kw in target_content:
                    manual_score += 2.0

    if drawing_score >= manual_score and drawing_score > 0:
        conf = min(0.92, 0.70 + (drawing_score * 0.03))
        if not text_is_usable and not any(k in fn_lower for k in ("drawing", "dwg", "plan", "diagram", "schematic")):
            conf = min(conf, 0.40)
        return {"value": "Drawing", "confidence": round(conf, 2), "tier": 2}
    elif manual_score > 0:
        conf = min(0.92, 0.70 + (manual_score * 0.03))
        if not text_is_usable and not any(k in fn_lower for k in ("manual", "instruction", "guide", "booklet", "operation", "maintenance")):
            conf = min(conf, 0.40)
        return {"value": "Manual", "confidence": round(conf, 2), "tier": 2}

    return {"value": "Manual" if path_has_manual else "Drawing", "confidence": 0.50, "tier": 2}


def _classify_category_and_subcategory_tiered(
    text: str,
    filename: str = "",
    group_type: str = "Drawing",
    text_is_usable: bool = True,
    source_path: str = "",
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Tier 1: Exact / near-exact sub-category term match in document content or title (>= 85%).
    Tier 2: Taxonomy keyword overlap / category match; fallback to 'To Be Classified' (< 85%).
    """
    norm_text = normalize_ocr_text(text)
    fn_lower = (filename or "").lower()
    fn_clean = re.sub(r"[\s/\\_\-]+", " ", fn_lower)
    fn_norm = _norm_match_key(fn_lower)

    path_lower = (source_path or "").lower()
    path_clean = re.sub(r"[\s/\\_\-]+", " ", path_lower)

    # Header / Page 1 text (first 3000 chars)
    header_text = norm_text[:3000].lower() if norm_text else ""
    header_clean = re.sub(r"[\s/\\_\-]+", " ", header_text)
    header_norm = _norm_match_key(header_text)

    full_text_lower = norm_text.lower() if norm_text else ""
    full_text_clean = re.sub(r"[\s/\\_\-]+", " ", full_text_lower)
    full_text_norm = _norm_match_key(full_text_lower)

    taxonomy = MANUAL_TAXONOMY if group_type == "Manual" else DRAWING_TAXONOMY

    best_cat: str | None = None
    best_subcat: str | None = None
    best_subcat_score = 0.0
    matched_keywords: list[str] = []

    # Explicit Hull Part drawing cue in filename or header (takes absolute precedence for Drawing group)
    if group_type == "Drawing" and re.search(r"\(hull\s+part(?:s)?\)|hull\s+part(?:s)?", f"{fn_lower}\n{header_text}", re.IGNORECASE):
        best_cat = "Hull"
        best_subcat = "Hull Drawings"
        best_subcat_score = 999.0
        matched_keywords.append("Hull (hull part)")

    # Check specific sub-categories and their keywords
    for cat_name, subcats in taxonomy.items():
        for subcat_name, keywords in subcats.items():
            score = 0.0
            for kw in keywords:
                kw_clean = re.sub(r"[\s/\\_\-]+", " ", kw.lower().strip())
                kw_norm = _norm_match_key(kw_clean)
                kw_pat = _make_flexible_phrase_regex(kw_clean)

                # Weight: Folder / Source path match (Weight 60)
                if path_clean and (kw_clean in path_clean or (len(kw_norm) >= 4 and kw_norm in _norm_match_key(path_lower))):
                    score += 60.0
                    matched_keywords.append(f"{cat_name} (path: {kw})")

                # Weight: Filename match (Weight 50)
                if kw_clean in fn_clean or (kw_pat and re.search(kw_pat, fn_lower, re.IGNORECASE)) or (len(kw_norm) >= 4 and kw_norm in fn_norm):
                    score += 50.0 * (1.5 if len(kw_clean) > 10 else 1.0)
                    matched_keywords.append(f"{cat_name} ({kw})")

                # Weight: Page 1 / Header match (Weight 30)
                if header_text:
                    if kw_clean in header_clean or (kw_pat and re.search(kw_pat, header_text, re.IGNORECASE)) or (len(kw_norm) >= 5 and kw_norm in header_norm):
                        score += 30.0 * (1.5 if len(kw_clean) > 10 else 1.0)
                        matched_keywords.append(f"{cat_name} ({kw})")

                # Weight: Full body text match (Weight 4)
                if text_is_usable and full_text_lower:
                    if kw_clean in full_text_clean or (kw_pat and re.search(kw_pat, full_text_lower, re.IGNORECASE)):
                        score += 4.0
                        matched_keywords.append(f"{cat_name} ({kw})")

            if score > best_subcat_score:
                best_subcat_score = score
                best_cat = cat_name
                best_subcat = subcat_name

    # Check generic category names
    best_generic_cat: str | None = None
    best_cat_score = 0.0
    for cat_name in taxonomy.keys():
        cat_lower = cat_name.lower()
        cat_norm = _norm_match_key(cat_lower)
        c_score = 0.0

        # Folder path match (e.g. "MB MAIN ENGINE" or "Main Engine")
        if cat_lower in path_clean or (cat_lower == "main engine" and ("mb main engine" in path_lower or "main engine" in path_lower or "me engine" in path_lower)):
            c_score += 60.0
        if cat_lower in fn_clean or cat_norm in fn_norm:
            c_score += 40.0
        elif cat_lower == "main engine" and (re.match(r"^[a-z]{2}-\d+", fn_lower) or "engine" in fn_clean):
            c_score += 35.0
        if header_text and (cat_lower in header_clean or cat_norm in header_norm):
            c_score += 20.0
        if text_is_usable and (cat_lower in full_text_clean or (len(cat_norm) >= 4 and cat_norm in full_text_norm)):
            c_score += 5.0
        if c_score > best_cat_score:
            best_cat_score = c_score
            best_generic_cat = cat_name

    # Tier 1: Strong specific sub-category match found
    if best_subcat and best_subcat_score >= 10.0:
        subcat_conf = min(0.98, 0.88 + (best_subcat_score * 0.005))
        cat_conf = min(0.98, 0.90 + (best_subcat_score * 0.005))
        return (
            {"value": best_cat or "Basic", "confidence": round(cat_conf, 2), "tier": 1},
            {"value": best_subcat, "confidence": round(subcat_conf, 2), "tier": 1},
            list(dict.fromkeys(matched_keywords))[:8]
        )

    # Tier 2: Generic category match without strong specific subcat
    if best_generic_cat and best_cat_score >= 10.0:
        default_subcat = list(taxonomy[best_generic_cat].keys())[0] if taxonomy.get(best_generic_cat) else "Other Manuals"
        return (
            {"value": best_generic_cat, "confidence": 0.85, "tier": 2},
            {"value": default_subcat, "confidence": 0.80, "tier": 2},
            [best_generic_cat]
        )

    # Fallback to To Be Classified
    return (
        {"value": "To Be Classified", "confidence": 0.40, "tier": 2},
        {"value": "To Be Classified", "confidence": 0.40, "tier": 2},
        []
    )


def classify_all_fields_tiered(
    text: str,
    filename: str = "",
    known_vessels: list[str] | None = None,
    source_path: str = "",
) -> dict[str, Any]:
    """Perform full two-tier classification for all 4 SharePoint metadata fields:
    - vessel
    - department
    - group: "Drawing" or "Manual"
    - category: "Basic", "Electrical", "Hull", "Machinery", "Safety", "Automation", etc.
    - sub_category: "General Arrangement", "Bulkhead plans", etc.

    Returns per-field results with {value, confidence, tier}.
    """
    # 0. Text normalization & quality gate
    norm_text = normalize_ocr_text(text)
    text_is_usable = is_text_usable_for_classification(norm_text, min_words=4)

    # 1. Department (Locked in first!)
    dept_res = _classify_department_tiered(norm_text, filename, source_path, text_is_usable=text_is_usable)
    department = dept_res["value"]

    # 2. Vessel — apply confidence floor: any match below VESSEL_CONFIDENCE_FLOOR
    # is blanked so downstream routing (and the UI) never see a low-confidence name.
    vessel_res = _classify_vessel_tiered(norm_text, filename, known_vessels, source_path=source_path, text_is_usable=text_is_usable)
    if vessel_res["confidence"] < VESSEL_CONFIDENCE_FLOOR:
        vessel_res = {"value": "", "confidence": vessel_res["confidence"], "tier": vessel_res["tier"]}
    vessel_name = vessel_res["value"] or ""

    # Windows/macOS metadata files are not documents, even when they sit inside
    # a valid drawing folder and contain OCR-like binary noise.
    if is_non_document_filename(filename):
        path_vessel = vessel_name if vessel_name else "{vessel}"
        return {
            "vessel": vessel_res,
            "department": dept_res,
            "group": {"value": "Drawing", "confidence": 0.99, "tier": 1},
            "category": {"value": "To Be Classified", "confidence": 0.99, "tier": 1},
            "sub_category": {"value": "To Be Classified", "confidence": 0.99, "tier": 1},
            "matched_keywords": [],
            "overall_confidence": 0.99,
            "suggested_path": "/".join([department, path_vessel, "Drawings and Manuals", "To be Classified"]),
            "vessel_in_filename_only": False,
            "vessel_name": vessel_res["value"],
            "group_name": "Drawing",
            "category_name": "To Be Classified",
            "sub_category_name": "To Be Classified",
        }

    # Detect whether vessel was identified from filename alias only (not found in OCR body text).
    # This happens when e.g. N-2119 in filename → Bow Fighter, but "Bow Fighter" is absent from PDF content.
    vessel_in_filename_only = False
    if vessel_name:
        vessel_name_in_text = False
        if text_is_usable and norm_text:
            nt_lower = norm_text.lower()
            # Check canonical name, aliases, and normalized "ei" / "el" variants
            v_aliases = [vessel_name] + list(VESSEL_ALIASES.get(vessel_name, ()))
            expanded_aliases: set[str] = set()
            for a in v_aliases:
                a_clean = a.strip().lower()
                if a_clean:
                    expanded_aliases.add(a_clean)
                    if " ei " in a_clean:
                        expanded_aliases.add(a_clean.replace(" ei ", " el "))
                    if " el " in a_clean:
                        expanded_aliases.add(a_clean.replace(" el ", " ei "))
                    if a_clean.startswith("ei "):
                        expanded_aliases.add("el " + a_clean[3:])
                    if a_clean.startswith("el "):
                        expanded_aliases.add("ei " + a_clean[3:])

            for cand_alias in expanded_aliases:
                if len(cand_alias) >= 3:
                    pat = _make_flexible_phrase_regex(cand_alias)
                    if pat and re.search(pat, nt_lower, re.IGNORECASE):
                        vessel_name_in_text = True
                        break
                    elif cand_alias in nt_lower:
                        vessel_name_in_text = True
                        break

            # Also check if vessel's IMO number or hull number appears in text
            if not vessel_name_in_text:
                parsed_names, imo_map, hull_map = _parse_known_vessels(known_vessels)
                for imo_num, v_matched in imo_map.items():
                    if v_matched == vessel_name and imo_num in nt_lower:
                        vessel_name_in_text = True
                        break
                if not vessel_name_in_text:
                    for h_num, v_matched in hull_map.items():
                        if v_matched == vessel_name and len(h_num) >= 3 and h_num in _norm_match_key(nt_lower):
                            vessel_name_in_text = True
                            break

        if not vessel_name_in_text:
            vessel_in_filename_only = True

    # 3. Group (Drawing / Manual)
    group_res = _classify_group_tiered(norm_text, filename, text_is_usable=text_is_usable, source_path=source_path)
    group = group_res["value"]

    # 4. Category (Basic, Hull, etc.) and Sub-Category under Group
    cat_res, subcat_res, matched_kws = _classify_category_and_subcategory_tiered(
        norm_text, filename, group, text_is_usable=text_is_usable, source_path=source_path
    )
    category = cat_res["value"]
    sub_category = subcat_res["value"]

    # If Category is unambiguously a Manual category (e.g. Main Engine), ensure Group is Manual
    # (Excludes shared categories like Electrical or Safety which exist in both Drawing and Manual taxonomies)
    if category in MANUAL_TAXONOMY and category not in DRAWING_TAXONOMY and group != "Manual":
        group = "Manual"
        group_res = {"value": "Manual", "confidence": max(group_res.get("confidence", 0.90), 0.96), "tier": 1}
        cat_res, subcat_res, matched_kws = _classify_category_and_subcategory_tiered(
            norm_text, filename, "Manual", text_is_usable=text_is_usable, source_path=source_path
        )
        category = cat_res["value"]
        sub_category = subcat_res["value"]
    elif category in DRAWING_TAXONOMY and category not in MANUAL_TAXONOMY and group != "Drawing":
        group = "Drawing"
        group_res = {"value": "Drawing", "confidence": max(group_res.get("confidence", 0.90), 0.96), "tier": 1}
        cat_res, subcat_res, matched_kws = _classify_category_and_subcategory_tiered(
            norm_text, filename, "Drawing", text_is_usable=text_is_usable, source_path=source_path
        )
        category = cat_res["value"]
        sub_category = subcat_res["value"]

    # If specific sub-category matched with high confidence, reinforce group confidence
    if subcat_res["tier"] == 1 and subcat_res["confidence"] >= 0.85:
        group_res["confidence"] = max(group_res["confidence"], 0.94)
        group_res["tier"] = 1

    # Build suggested path: {department}/{vessel}/Drawings and Manuals/{group}/{category}
    path_vessel = vessel_name if vessel_name else "{vessel}"
    if category == "To Be Classified" or sub_category == "To Be Classified":
        suggested_path_parts = [department, path_vessel, "Drawings and Manuals", "To be Classified"]
    else:
        suggested_path_parts = [department, path_vessel, "Drawings and Manuals", group, category]

    # Calculate overall confidence (weighted average with vessel and subcategory weighted highest)
    overall_conf = round(
        (vessel_res["confidence"] * 0.35) +
        (group_res["confidence"] * 0.20) +
        (cat_res["confidence"] * 0.20) +
        (subcat_res["confidence"] * 0.25),
        2
    )

    return {
        "vessel": vessel_res,
        "department": dept_res,
        "group": group_res,
        "category": cat_res,
        "sub_category": subcat_res,
        "matched_keywords": matched_kws,
        "overall_confidence": overall_conf,
        "suggested_path": "/".join(suggested_path_parts),
        # Vessel provenance: True when vessel was detected from filename alias only,
        # not from the file's text content. UI should flag this as "vessel name not found in file".
        "vessel_in_filename_only": vessel_in_filename_only,
        # Flat legacy convenience aliases:
        "vessel_name": vessel_res["value"],
        "group_name": group_res["value"],
        "category_name": cat_res["value"],
        "sub_category_name": subcat_res["value"],
    }



def classify_drawing_category(text: str) -> str | None:
    """Legacy backward-compatibility function for existing drawing category calls."""
    res = classify_document_content(text)
    if res.get("category") == "Drawing":
        return res.get("group")
    return None


def classify_against_db_categories(
    text: str,
    filename: str = "",
    db_categories: list[Any] | None = None,
) -> tuple[Any | None, float, list[str]]:
    """Match document text against user-defined DocumentCategory models.

    Applies weighted scoring:
    - Specific Sub-Category term match = 5.0 weight
    - Generic Group / Category name match = 1.5 weight
    """
    if not db_categories:
        return None, 0.0, []

    content = f"{filename}\n{text}".lower()
    fn_lower = filename.lower()

    best_cat = None
    best_score = 0.0
    best_matches: list[str] = []

    for cat in db_categories:
        import json
        hints: list[str] = []
        if isinstance(getattr(cat, "ocr_hints_json", None), str):
            try:
                hints = json.loads(cat.ocr_hints_json)
            except Exception:
                hints = []
        elif isinstance(getattr(cat, "ocr_hints", None), list):
            hints = cat.ocr_hints

        cat_name = (getattr(cat, "name", "") or "").lower()
        if cat_name and cat_name not in hints:
            hints.append(cat_name)

        score = 0.0
        matches: list[str] = []
        for kw in hints:
            kw_clean = str(kw).strip().lower()
            if not kw_clean:
                continue
            if kw_clean in content:
                # Give higher weight to multi-word or specific terms (>10 chars or containing space)
                is_specific_term = " " in kw_clean or len(kw_clean) > 10
                base_weight = 5.0 if is_specific_term else 1.5
                fn_weight = 2.0 if kw_clean in fn_lower else 1.0
                cnt = content.count(kw_clean)
                score += cnt * base_weight * fn_weight
                matches.append(kw)

        if score > best_score:
            best_score = score
            best_cat = cat
            best_matches = matches

    if best_score > 0 and best_cat:
        confidence = min(0.98, 0.60 + (best_score * 0.04))
        return best_cat, round(confidence, 2), list(dict.fromkeys(best_matches))[:10]

    return None, 0.0, []
