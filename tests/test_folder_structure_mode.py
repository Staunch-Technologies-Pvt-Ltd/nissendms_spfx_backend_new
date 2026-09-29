"""Folder Structure Mode — planner + guard tests (no Graph, no DB).

Uses an in-memory fake SharePoint drive and fake DMS so every mode, the
idempotency rule, failure handling and the protected-site guard can be
verified offline. Test site names only — never the production site.
"""
import asyncio
import itertools

import pytest

from app.services import folder_structure as fs


# ------------------------------------------------------------------ fakes
class FakeSpo:
    def __init__(self):
        self._ids = itertools.count(1)
        self.items = {"root": {"id": "root", "name": "", "parent": None, "folder": True}}
        self.creates = 0
        self.fail_names: set[str] = set()

    def add(self, parent_id, name, folder=True):
        iid = f"i{next(self._ids)}"
        self.items[iid] = {"id": iid, "name": name, "parent": parent_id, "folder": folder}
        return iid

    def path_id(self, path):
        cur = "root"
        for part in path.split("/"):
            cur = next(i for i, it in self.items.items() if it["parent"] == cur and it["name"] == part)
        return cur

    def names(self, parent_id):
        return sorted(it["name"] for it in self.items.values() if it["parent"] == parent_id)

    async def root_id(self):
        return "root"

    async def child_folders(self, item_id):
        return [{"id": it["id"], "name": it["name"]} for it in self.items.values()
                if it["parent"] == item_id and it["folder"]]

    async def item_exists(self, item_id):
        return item_id in self.items

    async def ensure_folder(self, parent_id, name):
        if name in self.fail_names:
            err = RuntimeError("Graph 503: throttled")
            err.status = 503
            raise err
        for it in self.items.values():
            if it["parent"] == parent_id and it["name"].lower() == name.lower():
                if not it["folder"]:
                    raise RuntimeError("name conflict")
                return {"id": it["id"], "name": it["name"], "folder": {}}
        self.creates += 1
        return {"id": self.add(parent_id, name), "name": name, "folder": {}}


class FakeDms:
    def __init__(self):
        self.rows = {}  # path -> row

    def row_by_item(self, item_id):
        return next((r for r in self.rows.values() if r["drive_item_id"] == item_id), None)

    def row_by_path(self, path):
        return self.rows.get(path)

    def vessel_ship_rows(self, vessel_id):
        return [r for r in self.rows.values()
                if r["vessel_id"] == vessel_id and r["kind"] == "ship" and "/" not in r["path"]]

    def upsert(self, *, path, name, kind, item_id, month_driven, vessel_id):
        self.rows[path] = {"path": path, "name": name, "kind": kind, "drive_item_id": item_id,
                           "month_driven": month_driven, "vessel_id": vessel_id}


SITE = {"site_key": "testsite", "site_name": "Vessel DMS Test", "site_url": "https://t/sites/DMSTest", "drive_id": "drv-test"}


def run(spo, dms, mode, vessels, apply=True):
    tpl = fs.load_template()
    return asyncio.run(fs.plan(tpl, spo, dms, mode=mode, apply=apply, vessels=vessels,
                               all_vessel_names=[v.name for v in vessels], site=SITE))


V1 = fs.VesselRef(id=1, name="MV Test One")
V2 = fs.VesselRef(id=2, name="MV Test Two")


def slot(spo, dms, v):
    iid = spo.add("root", v.name)
    dms.upsert(path=v.name, name=v.name, kind="ship", item_id=iid, month_driven=False, vessel_id=v.id)


# ------------------------------------------------------------ name match
@pytest.mark.parametrize("a,b", [
    ("Commercial & Chartering", "Commercial and Chartering"),
    ("  commercial   &  chartering ", "COMMERCIAL AND CHARTERING"),
    ("Kaizen – Knowledge Bank", "Kaizen - Knowledge Bank"),
    ("Flag / MPA", "Flag - MPA"),
    ("SIRE/OCIMF/RightShip", "SIRE-OCIMF-RightShip"),
])
def test_match_key_equal(a, b):
    assert fs.match_key(a) == fs.match_key(b)


def test_match_key_different():
    assert fs.match_key("Insurance") != fs.match_key("Insurances")


def test_template_loads_and_has_four_mains():
    tpl = fs.load_template()
    assert [m.name for m in tpl.mains] == [
        "Technical & Crewing", "Commercial & Chartering", "Insurance", "Kaizen – Knowledge Bank"]
    tc = tpl.mains[0]
    mer = next(n for n in tc.per_ship if n.name == "Month End Reports")
    assert mer.month_driven and len(mer.children) == 7


# ----------------------------------------------------------------- Mode 1
def test_mode1_creates_nothing_and_keeps_slot_in_sync():
    spo, dms = FakeSpo(), FakeDms()
    slot(spo, dms, V1)
    before = dict(spo.items)
    r = run(spo, dms, "empty_pool", [V1])
    assert spo.items == before and spo.creates == 0
    assert r.summary.reused == 1 and r.summary.failed == 0
    assert all(i.level == "slot" for i in r.items)


def test_mode1_links_unlinked_slot_folder():
    spo, dms = FakeSpo(), FakeDms()
    spo.add("root", V1.name)
    r = run(spo, dms, "empty_pool", [V1])
    assert spo.creates == 0
    assert dms.rows[V1.name]["vessel_id"] == 1 and r.summary.created_dms == 1


# ----------------------------------------------------------------- Mode 2
def test_mode2_full_tree_with_ship_folders_and_single_common():
    spo, dms = FakeSpo(), FakeDms()
    slot(spo, dms, V1)
    slot(spo, dms, V2)
    r = run(spo, dms, "full_template", [V1, V2])
    assert r.summary.failed == 0
    tc = spo.path_id("Technical & Crewing")
    assert set(spo.names(tc)) == {"Common for all ships", V1.name, V2.name}
    ship = spo.path_id(f"Technical & Crewing/{V1.name}")
    assert spo.names(ship) == sorted(["Month End Reports", "Service Agreements", "Registration",
                                      "Drawings and Manuals", "Incidents", "Crewing"])
    # month-driven: no month folders created in advance
    assert spo.names(spo.path_id(f"Technical & Crewing/{V1.name}/Month End Reports")) == []
    # sanitised names
    assert "Flag - MPA" in spo.names(spo.path_id(f"Insurance/{V1.name}"))
    # Kaizen: common only, no ship folder
    kz = spo.path_id("Kaizen – Knowledge Bank")
    assert V1.name not in spo.names(kz) and "Class" in spo.names(kz)
    # every created SPO folder has a DMS row, ship rows linked to vessel
    for it in spo.items.values():
        if it["id"] != "root":
            assert dms.row_by_item(it["id"]) is not None, it["name"]
    assert dms.rows[f"Technical & Crewing/{V2.name}"]["kind"] == "ship"
    assert dms.rows[f"Technical & Crewing/{V2.name}"]["vessel_id"] == 2
    assert dms.rows[f"Technical & Crewing/{V2.name}/Month End Reports"]["month_driven"] is True


def test_mode2_dry_run_writes_nothing():
    spo, dms = FakeSpo(), FakeDms()
    r = run(spo, dms, "full_template", [V1], apply=False)
    assert spo.creates == 0 and dms.rows == {}
    # the preview summary counts what *would* be created
    assert r.dry_run is True and r.summary.created_sp > 40
    assert r.summary.created_sp == sum(1 for i in r.items if i.sp == "create")


# ------------------------------------------------------------ Modes 3 / 4
def partial_project():
    """Folder 1 + Folder 3 only, legacy-style names, plus a custom folder."""
    spo, dms = FakeSpo(), FakeDms()
    slot(spo, dms, V1)
    tc = spo.add("root", "Technical and Crewing")  # '&' written as 'and'
    ship = spo.add(tc, V1.name)
    spo.add(ship, "month end reports")
    spo.add(ship, "Client Docs")                   # custom
    ins = spo.add("root", "Insurance")
    spo.add(ins, "Client Docs")                    # custom at main level
    spo.add("root", "Unrelated Root Folder")       # not ours, never touched
    return spo, dms


def test_mode3_adopts_keeps_names_creates_nothing():
    spo, dms = partial_project()
    before = {k: dict(v) for k, v in spo.items.items()}
    r = run(spo, dms, "adopt_existing", [V1])
    assert spo.creates == 0 and spo.items == before
    names = {i.path for i in r.items if i.outcome == "reused"}
    assert "Technical and Crewing" in names                       # name kept as-is
    assert f"Technical and Crewing/{V1.name}/month end reports" in names
    customs = {i.path for i in r.items if i.outcome == "custom"}
    assert f"Technical and Crewing/{V1.name}/Client Docs" in customs
    assert "Insurance/Client Docs" in customs
    # Folder 2 and Folder 4 not created, anywhere
    assert "Commercial & Chartering" not in spo.names("root")
    assert "Kaizen – Knowledge Bank" not in spo.names("root")
    # SharePoint folders linked into DMS
    assert dms.rows["Technical and Crewing"]["kind"] == "main"
    assert dms.rows[f"Technical and Crewing/{V1.name}/Client Docs"]["vessel_id"] == 1
    assert "Unrelated Root Folder" not in {r["name"] for r in dms.rows.values()}


def test_mode4_creates_missing_mains_and_subfolders_only():
    spo, dms = partial_project()
    r = run(spo, dms, "adopt_create", [V1])
    assert r.summary.failed == 0
    root = spo.names("root")
    assert "Commercial & Chartering" in root and "Kaizen – Knowledge Bank" in root
    assert "Technical & Crewing" not in root            # existing one adopted, not duplicated
    ship = spo.path_id(f"Technical and Crewing/{V1.name}")
    kids = spo.names(ship)
    assert "month end reports" in kids and "Month End Reports" not in kids
    assert "Registration" in kids and "Client Docs" in kids
    assert "Client Docs" in spo.names(spo.path_id("Insurance"))
    assert V1.name in spo.names(spo.path_id("Insurance"))


def test_client_docs_survives_modes_3_and_4_unchanged():
    spo, dms = partial_project()
    cid = spo.path_id(f"Technical and Crewing/{V1.name}/Client Docs")
    run(spo, dms, "adopt_existing", [V1])
    run(spo, dms, "adopt_create", [V1])
    assert spo.items[cid]["name"] == "Client Docs"
    assert dms.row_by_item(cid)["name"] == "Client Docs"


@pytest.mark.parametrize("mode", fs.MODES)
def test_running_twice_creates_no_duplicates(mode):
    spo, dms = partial_project()
    run(spo, dms, mode, [V1, V2])
    n_items, n_rows = len(spo.items), len(dms.rows)
    r2 = run(spo, dms, mode, [V1, V2])
    assert len(spo.items) == n_items and len(dms.rows) == n_rows
    assert r2.summary.created_sp == 0 and r2.summary.created_dms == 0


def test_sharepoint_failure_is_reported_and_consistent():
    spo, dms = FakeSpo(), FakeDms()
    spo.fail_names = {"Registration"}
    r = run(spo, dms, "full_template", [V1])
    failed = [i for i in r.items if i.outcome == "failed"]
    assert failed and all("Registration" in i.path for i in failed)
    skipped_children = [i for i in r.items if i.reason == "parent folder failed"]
    assert {"Flag & MPA", "Novation"} <= {i.name for i in skipped_children}
    # No DMS row without a SharePoint folder
    for row in dms.rows.values():
        assert row["drive_item_id"] in spo.items
    assert not any("Registration" in p for p in dms.rows)
    # A re-run after the outage heals it
    spo.fail_names = set()
    r2 = run(spo, dms, "full_template", [V1])
    assert r2.summary.failed == 0
    assert any(p.endswith("/Registration/Novation") for p in dms.rows)


def test_existing_vessels_and_files_untouched():
    spo, dms = partial_project()
    file_id = spo.add(spo.path_id(f"Technical and Crewing/{V1.name}/Client Docs"), "report.pdf", folder=False)
    before_names = {k: (v["name"], v["parent"]) for k, v in spo.items.items()}
    for mode in fs.MODES:
        run(spo, dms, mode, [V1])
    for k, (name, parent) in before_names.items():
        assert spo.items[k]["name"] == name and spo.items[k]["parent"] == parent
    assert file_id in spo.items


# ------------------------------------------------------------------ guard
@pytest.fixture
def nonprod(monkeypatch):
    from app.graph import guard
    monkeypatch.setenv("DMS_ENVIRONMENT", "dev")
    monkeypatch.setenv("PROTECTED_DRIVE_IDS", "b!protected-drive")
    monkeypatch.delenv("PROTECTED_SITE_GUARD", raising=False)
    monkeypatch.setattr(guard, "_ids_cache", None)
    import app.config as cfg
    monkeypatch.setattr(cfg.Settings, "discover_available_sites", staticmethod(lambda: {
        "local": {"name": "local", "sp_site_name": "Vessel DMS (local)", "drive_id": "b!local-drive",
                  "web_url": "https://tenant.sharepoint.com/sites/NKSDocMan"},
        "testsite": {"name": "testsite", "sp_site_name": "DMS Test", "drive_id": "b!test-drive",
                     "web_url": "https://tenant.sharepoint.com/sites/DMSTest"},
    }))
    yield guard
    guard.reset_cache()


@pytest.mark.parametrize("target", [
    "https://tenant.sharepoint.com/sites/NKSDocMan/_api/web",
    "https://tenant.sharepoint.com/sites/nksdocsman/_api/web",
    "https://graph.microsoft.com/v1.0/drives/b!local-drive/items/root/children",
    "https://graph.microsoft.com/v1.0/drives/b!protected-drive/root",
    "NKS-Doc-Man",
])
def test_guard_blocks_production_site(nonprod, target):
    with pytest.raises(nonprod.ProtectedTargetError):
        nonprod.assert_allowed(target)


def test_guard_allows_test_site(nonprod):
    nonprod.assert_allowed("https://graph.microsoft.com/v1.0/drives/b!test-drive/root")


def test_guard_off_in_production(nonprod, monkeypatch):
    monkeypatch.setenv("DMS_ENVIRONMENT", "prod")
    nonprod.assert_allowed("https://tenant.sharepoint.com/sites/NKSDocMan/_api/web")


def test_guard_folder_mode_scope_still_blocks_folder_mode(nonprod, monkeypatch):
    monkeypatch.setenv("PROTECTED_SITE_GUARD", "folder_mode")
    nonprod.assert_allowed("https://tenant.sharepoint.com/sites/NKSDocMan/x")  # legacy calls pass
    with pytest.raises(nonprod.ProtectedTargetError):
        nonprod.assert_allowed("https://tenant.sharepoint.com/sites/NKSDocMan/x", folder_mode=True)


def test_folder_mode_site_check_blocks_protected_drive(nonprod):
    with pytest.raises(nonprod.ProtectedTargetError):
        fs.assert_site_allowed({"site_key": "local", "site_name": "Vessel DMS (local)",
                                "site_url": "https://tenant.sharepoint.com", "drive_id": "b!local-drive"})
