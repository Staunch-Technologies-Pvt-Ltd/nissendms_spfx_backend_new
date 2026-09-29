"""Tag Configuration — validation, hierarchy, seed, Add/Replace, and parity of
the tag automation with the previous hard-coded lists."""
import pytest

from app.services import tag_config as tc
from tests._legacy_derive import legacy_derive_dms_tags


# ------------------------------------------------------------ validation
def test_folder_name_mapping():
    assert tc.folder_name_for("Technical & Crewing") == "Technical and Crewing"
    assert tc.folder_name_for("A&B") == "A and B"
    assert tc.folder_name_for('Bad: "name"?') == "Bad name"
    assert len(tc.folder_name_for("x" * 300)) == tc.MAX_NAME_LEN


@pytest.mark.parametrize("name,ok", [
    ("Technical & Crewing", True), ("Kaizen - Knowledge Bank", True), ("", False), ("   ", False),
    ("a/b", False), ("50%", False), ("x#y", False), ("{x}", False), ("x" * 129, False), (".hidden", False),
])
def test_validate_name(name, ok):
    assert (tc.validate_name(name) == []) is ok


def test_parent_rules_optional_levels_no_orphans_no_cycles():
    assert tc.validate_parent("domain", None) == []
    assert tc.validate_parent("domain", "domain")                     # domains have no parent
    assert tc.validate_parent("main_folder", None)                    # orphan
    assert tc.validate_parent("category", "main_folder") == []        # skipped Group is allowed
    assert tc.validate_parent("group", "category")                    # lower level can't be parent
    assert tc.validate_parent("category", "category")                 # same level → no cycles


def test_name_key_unique_rule():
    assert tc.name_key("  Technical   &  Crewing ") == tc.name_key("technical & crewing")


# ------------------------------------------------------------------ seed
def test_seed_has_four_default_domains():
    view = tc.builtin_view()
    assert view.domain_names() == [
        "Technical & Crewing", "Commercial & Chartering", "Insurance", "Kaizen - Knowledge Bank"]
    assert all(r["is_default"] and r["source"] == "Default" for r in view.rows)
    assert view.default_domain() == "Technical & Crewing"
    kz = view.resolve("domain", "Kaizen & Knowledgebank")
    assert kz and kz["name"] == "Kaizen - Knowledge Bank"
    assert kz["attributes"]["path_mode"] == "legacy"


def test_seed_hierarchy_is_correctly_parented():
    view = tc.builtin_view()
    dm = view.resolve("main_folder", "Drawings and Manuals")
    assert view.by_id[dm["parent_id"]]["name"] == "Technical & Crewing"
    groups = [g["name"] for g in view.children(dm["id"])]
    assert groups == ["Drawings", "Manuals"]
    drawings = view.resolve("group", "drawing")
    assert drawings["folder_name"] == "Drawing"
    cats = [c["name"] for c in view.children(drawings["id"])]
    assert cats == ["Basic", "Electrical", "Hull", "Machinery", "Safety", "Archive"]
    hull = next(c for c in view.children(drawings["id"]) if c["name"] == "Hull")
    assert "Midship Section" in [s["name"] for s in view.children(hull["id"])]
    for r in view.rows:
        assert tc.validate_parent(r["level"], view.by_id[r["parent_id"]]["level"] if r["parent_id"] else None) == []


@pytest.mark.parametrize("seg,expected", [
    ("Technical & Crewing", "Technical & Crewing"), ("technical and crewing new", "Technical & Crewing"),
    ("technical", "Technical & Crewing"), ("knowledge bank", "Kaizen - Knowledge Bank"),
    ("Insurance", "Insurance"), ("Crewing", None), ("MV Alpha", None), ("Hull", None),
])
def test_match_domain_segments(seg, expected):
    """Every value the old literal tuples accepted still matches; a main folder
    named like a domain alias ("Crewing") does not."""
    assert tc.builtin_view().match_domain(seg) == expected


def test_alias_map_matches_previous_endpoint():
    amap = tc.builtin_view().domain_alias_map()
    assert set(amap) == {"Technical & Crewing", "Commercial & Chartering", "Insurance", "Kaizen - Knowledge Bank"}
    assert "technical" in amap["Technical & Crewing"]
    assert "claims" in amap["Insurance"]


# ------------------------------------------------------- automation parity
PATHS = [
    "Technical & Crewing/MV Alpha/Drawings and Manuals/Drawing/Hull/Midship Section",
    "Technical & Crewing/MV Alpha/Drawings and Manuals/Manual/Main Engine/Operation & Maintenance Manual",
    "Technical & Crewing/MV Alpha/Drawings and Manuals/Hull/Shell Expansion",
    "Technical & Crewing/MV Alpha/Drawings and Manuals/To be Classified",
    "Technical & Crewing/MV Alpha/Drawings and Manuals/Electrical/Single Line Diagram",
    "Technical & Crewing/MV Alpha/Service Agreements/Technical Management",
    "Technical & Crewing/MV Alpha/Registration/Flag & MPA",
    "Technical & Crewing/MV Alpha/Month End Reports/September 2026/Main Engine",
    "Commercial & Chartering/MV Alpha/Agreements/Charter party",
    "Insurance/MV Alpha/P&I",
    "Technical & Crewing/Hull/Profile & Deck Plan",
    "Technical & Crewing/To be Classified",
    "Common for all ships/Technical & Crewing/Vendor & Service Agreements/To be Classified",
    "Kaizen - Knowledge Bank/Templates",
    "Kaizen - Knowledge Bank/Circulars and Guidance/Class",
    "MV Alpha/Some Folder/Deeper",
    "Random/Path",
    "technical & crewing/mv beta/drawings and manuals/manuals/cargo/cow manual",
    "",
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("vessel", [None, "MV Alpha"])
def test_derive_tags_identical_to_previous_hard_coded_logic(path, vessel):
    from app.services.real_backend import RealBackend
    new = RealBackend._derive_dms_tags(path, vessel, view=tc.builtin_view())
    assert new == legacy_derive_dms_tags(path, vessel)


def test_derive_uses_configured_values():
    """A client adds Group 'Certificates' with Category 'Class' — automation picks it up."""
    rows = tc.flatten_tree(tc.seed_tree())
    view = tc.TagView("client", "site", rows)
    dm = view.resolve("main_folder", "Drawings and Manuals")
    rows.append({**rows[0], "id": 9001, "level": "group", "name": "Certificates", "name_key": "certificates",
                 "display_name": "Certificates", "folder_name": "Certificates", "parent_id": dm["id"],
                 "sort_order": 30, "aliases": [], "attributes": {}, "is_default": False, "source": "Custom"})
    rows.append({**rows[0], "id": 9002, "level": "category", "name": "Class Certificates",
                 "name_key": "class certificates", "display_name": "Class Certificates",
                 "folder_name": "Class Certificates", "parent_id": 9001, "sort_order": 10,
                 "aliases": [], "attributes": {}, "is_default": False, "source": "Custom"})
    view = tc.TagView("client", "site", rows)
    from app.services.real_backend import RealBackend
    tags = RealBackend._derive_dms_tags(
        "Technical & Crewing/MV A/Drawings and Manuals/Certificates/Class Certificates", "MV A", view=view)
    assert tags["Group"] == "Certificates" and tags["Category"] == "Class Certificates"


# ------------------------------------------------------------ Add/Replace
def _view():
    return tc.builtin_view("client")


def row(level, name, parent="", line=2, status="Active"):
    return tc.IncomingRow(level=level, name=name, parent_path=parent, line=line, status=status)


def test_add_mode_keeps_defaults_and_appends():
    d = tc.plan_changes(_view(), [row("domain", "Domain X"), row("domain", "insurance ", line=3)], "add")
    acts = {(e.name, e.action) for e in d.entries}
    assert ("Domain X", "new") in acts and ("Insurance", "unchanged") in acts
    assert d.counts["deactivate"] == 0


def test_full_chain_in_one_import_uses_staged_parents():
    d = tc.plan_changes(_view(), [
        row("domain", "Domain X", line=2),
        row("main_folder", "Main A", "Domain X", line=3),
        row("group", "Group A", "Domain X > Main A", line=4),
        row("category", "Cat A", "Domain X > Main A > Group A", line=5),
        row("sub_category", "Sub A", "Domain X > Main A > Group A > Cat A", line=6),
    ], "add")
    assert [e.action for e in d.entries] == ["new"] * 5


def test_replace_deactivates_others_and_reports_usage():
    view = _view()
    ins = view.resolve("domain", "Insurance")
    d = tc.plan_changes(view, [row("domain", "Insurance"), row("domain", "Domain X", line=3)],
                        "replace", usage={ins["id"]: 0, view.resolve("domain", "Technical & Crewing")["id"]: 12})
    deact = {e.name for e in d.entries if e.action == "deactivate"}
    assert deact == {"Technical & Crewing", "Commercial & Chartering", "Kaizen - Knowledge Bank"}
    assert d.counts["in_use"] == 1
    assert "3 items will be deactivated, 1 new items will be created" in d.to_dict()["confirmation"]


def test_replace_reactivates_matching_inactive_item_instead_of_duplicating():
    rows = tc.flatten_tree(tc.seed_tree())
    for r in rows:
        if r["name"] == "Insurance":
            r["status"] = "Inactive"
    d = tc.plan_changes(tc.TagView("c", "site", rows), [row("domain", "INSURANCE")], "replace")
    assert [e.action for e in d.entries if e.name == "Insurance"] == ["reactivate"]
    assert d.counts["new"] == 0


def test_replace_scope_is_limited_to_the_same_parent():
    view = _view()
    d = tc.plan_changes(view, [row("category", "Hull", "Technical & Crewing > Drawings and Manuals > Drawings")],
                        "replace")
    deact = {e.name for e in d.entries if e.action == "deactivate"}
    assert deact == {"Basic", "Electrical", "Machinery", "Safety", "Archive"}  # Manuals' categories untouched


def test_replace_can_never_empty_a_level():
    rows = [r for r in tc.flatten_tree(tc.seed_tree()) if r["level"] == "domain"]
    view = tc.TagView("c", "site", rows)
    d = tc.plan_changes(view, [row("domain", "Only", status="Inactive")], "replace")
    assert any(e.action == "error" and "at least one Active" in e.message for e in d.entries)


def test_validation_errors_in_preview():
    d = tc.plan_changes(_view(), [
        row("domain", "Bad/Name", line=2),
        row("domain", "Dup", line=3), row("domain", "dup ", line=4),
        row("category", "Orphan", "", line=5),
        row("category", "Lost", "No Such Domain", line=6),
        row("wrong", "x", line=7),
        row("group", "G", "Technical & Crewing > Drawings and Manuals > Drawings", line=8),
    ], "add")
    errs = {e.line: e.message for e in d.entries if e.action == "error"}
    assert set(errs) == {2, 4, 5, 6, 7, 8}
    assert "Duplicate" in errs[4] and "needs a Parent Path" in errs[5] and "not found" in errs[6]
    assert "higher level" in errs[8]


def test_export_then_import_roundtrip_is_unchanged():
    view = _view()
    exported = tc.export_rows(view)
    incoming = tc.parse_import(exported)
    d = tc.plan_changes(view, incoming, "add")
    assert d.counts["new"] == 0 and d.counts["error"] == 0
    assert d.counts["unchanged"] == len(view.rows)


def test_inactive_items_resolve_for_existing_docs_but_not_for_new_tagging():
    rows = tc.flatten_tree(tc.seed_tree())
    for r in rows:
        if r["name"] == "Archive":
            r["status"] = "Inactive"
    view = tc.TagView("c", "site", rows)
    assert view.resolve("category", "archive")["status"] == "Inactive"
    assert view.resolve("category", "archive", include_inactive=False) is None
