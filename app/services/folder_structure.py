"""Folder Structure Mode — Settings → Vessel Settings.

Four modes decide what, on top of the existing pooled vessel slot, a vessel
gets in the DMS (Folder rows) and in SharePoint Online:

  empty_pool      Mode 1. Existing behaviour only: the vessel's pooled slot
                  root folder (RealBackend._build_pool_slot / _link_claimed_slot
                  / _provision_vessel). No named structure. Applying it only
                  checks/links slot ↔ SharePoint sync; it never creates.
  full_template   Mode 2. Slot + the full standard template (per-ship folders
                  under <main>/<vessel>/, common folders once under <main>/).
  adopt_existing  Mode 3. Scan and adopt what exists (matched by normalised
                  name), keep custom folders, link unlinked SharePoint folders
                  to DMS rows. Creates nothing.
  adopt_create    Mode 4. Mode 3 + create missing main folders (with their
                  sub-tree) and missing standard sub-folders in existing mains.

No second provisioning path: SharePoint folders are created with the same
primitive every other provisioning path uses (graph.drive.ensure_folder —
create-or-fetch, idempotent) and DMS rows with RealBackend._upsert, under
RealBackend._semaphore() throttling. Nothing is ever renamed, moved or
deleted. A DMS row is written only after its SharePoint folder exists, so a
failure can never leave a DMS row without a SharePoint folder; a re-run
links any folder whose DB write failed.

The planner is pure (adapters injected) so dry-run and apply share one code
path and tests run without Graph or PostgreSQL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

MODES = ("empty_pool", "full_template", "adopt_existing", "adopt_create")
DEFAULT_MODE = "empty_pool"
MODE_LABELS = {
    "empty_pool": "Empty Pool (Empty Folder Slots)",
    "full_template": "New DMS Folder Structure Pool",
    "adopt_existing": "Existing Structure (Adopt, Don't Create Main Folders)",
    "adopt_create": "Existing Structure + Create Missing Main Folders",
}
DEFAULT_MODE_SETTING_KEY = "folder_structure_default_mode"

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "vessel_management.json"

_apply_lock = asyncio.Lock()


# ---------------------------------------------------------------- names
_FORBIDDEN = '*?"<>|'


def sanitize_name(name: str) -> str:
    """Same rules as real_backend.sanitize_folder_name (kept in sync; the
    real backend passes its own function in at runtime)."""
    name = name.replace("/", "-").replace("\\", "-").replace(":", "-")
    for c in _FORBIDDEN:
        name = name.replace(c, "_")
    return name.strip(" .")


def match_key(name: str | None) -> str:
    """Case-insensitive, trimmed, punctuation-insensitive, '&' == 'and'.

    "Commercial & Chartering" == "Commercial and Chartering" ==
    "commercial  chartering"; "Kaizen – Knowledge Bank" == "Kaizen - Knowledge
    Bank"; "Flag / MPA" == "Flag - MPA" == "Flag & MPA".
    """
    text = (name or "").lower().replace("&", " and ")
    tokens = [t for t in re.split(r"[^0-9a-z]+", text) if t and t != "and"]
    return " ".join(tokens)


# ------------------------------------------------------------- template
@dataclass
class TNode:
    name: str
    children: list["TNode"] = field(default_factory=list)
    month_driven: bool = False

    @property
    def kind(self) -> str:
        if self.month_driven:
            return "month_driven"
        return "folder" if self.children else "leaf"


@dataclass
class TMain:
    name: str
    per_ship: list[TNode]
    common: list[TNode]
    # Optional wrapper folder that holds the common folders, e.g.
    # "Common for all ships" (the name existing libraries already use).
    common_folder: str | None = None


@dataclass
class Template:
    template_id: str
    version: int
    root: str
    mains: list[TMain]
    raw: dict


def _parse_node(raw: Any) -> TNode:
    if isinstance(raw, str):
        return TNode(name=raw)
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        raise ValueError(f"Invalid template node: {raw!r}")
    return TNode(
        name=str(raw["name"]).strip(),
        children=[_parse_node(c) for c in raw.get("children") or []],
        month_driven=bool(raw.get("month_driven")),
    )


def parse_template(data: dict) -> Template:
    mains = []
    for m in data.get("main_folders") or []:
        if not str(m.get("name") or "").strip():
            raise ValueError("Every main folder needs a name")
        mains.append(TMain(
            name=str(m["name"]).strip(),
            per_ship=[_parse_node(n) for n in m.get("per_ship") or []],
            common=[_parse_node(n) for n in m.get("common") or []],
            common_folder=(str(m.get("common_folder")).strip() or None) if m.get("common_folder") else None,
        ))
    if not mains:
        raise ValueError("Template has no main_folders")
    return Template(
        template_id=str(data.get("template_id") or "vessel_management"),
        version=int(data.get("version") or 1),
        root=str(data.get("root") or "").strip().strip("/"),
        mains=mains,
        raw=data,
    )


def template_path() -> Path:
    override = os.environ.get("FOLDER_TEMPLATE_PATH", "").strip()
    return Path(override) if override else TEMPLATE_PATH


def load_template() -> Template:
    with open(template_path(), encoding="utf-8") as fh:
        return parse_template(json.load(fh))


def find_month_children(tpl: Template, folder_name: str) -> list[TNode] | None:
    """Children of the month_driven template node whose name matches."""
    key = match_key(folder_name)

    def walk(nodes):
        for n in nodes:
            if n.month_driven and match_key(n.name) == key:
                return n.children
            found = walk(n.children)
            if found is not None:
                return found
        return None

    for m in tpl.mains:
        found = walk(m.per_ship) or walk(m.common)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------- adapters
class SpoAdapter(Protocol):
    async def root_id(self) -> str: ...
    async def child_folders(self, item_id: str) -> list[dict]: ...
    async def item_exists(self, item_id: str) -> bool: ...
    async def ensure_folder(self, parent_id: str, name: str) -> dict: ...


class DmsAdapter(Protocol):
    def row_by_item(self, item_id: str) -> dict | None: ...
    def row_by_path(self, path: str) -> dict | None: ...
    def vessel_ship_rows(self, vessel_id: int) -> list[dict]: ...
    def upsert(self, *, path: str, name: str, kind: str, item_id: str,
               month_driven: bool, vessel_id: int | None) -> None: ...


@dataclass
class VesselRef:
    id: int
    name: str
    mode: str | None = None


# ----------------------------------------------------------------- plan
@dataclass
class PlanItem:
    path: str
    name: str
    level: str                 # root | main | ship | common | folder | slot
    scope: str                 # template | custom | slot
    template_name: str | None
    vessel: str | None
    sp: str                    # exists | create | skip
    dms: str                   # linked | link | create | skip
    outcome: str               # created | reused | custom | skipped | failed
    reason: str = ""
    kind: str = "folder"


@dataclass
class Summary:
    created_dms: int = 0
    created_sp: int = 0
    reused: int = 0
    custom_kept: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class Result:
    mode: str
    dry_run: bool
    vessels: list[str]
    site: dict
    items: list[PlanItem] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)

    def add(self, item: PlanItem) -> PlanItem:
        self.items.append(item)
        return item

    def finalize(self) -> "Result":
        s = Summary()
        for it in self.items:
            if it.outcome == "failed":
                s.failed += 1
                continue
            if it.sp == "create" and it.outcome == "created":
                s.created_sp += 1
            if it.dms in ("create", "link") and it.outcome in ("created", "reused", "custom"):
                s.created_dms += 1
            if it.outcome == "reused":
                s.reused += 1
            elif it.outcome == "custom":
                s.custom_kept += 1
            elif it.outcome == "skipped":
                s.skipped += 1
        self.summary = s
        return self

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, self.mode),
            "dry_run": self.dry_run,
            "vessels": self.vessels,
            "site": self.site,
            "summary": asdict(self.summary),
            "items": [asdict(i) for i in self.items],
        }


class _Planner:
    """Walks template × live SharePoint × DMS rows. With apply=False it only
    reads (dry-run); with apply=True it creates/links as it goes."""

    def __init__(self, tpl: Template, spo: SpoAdapter, dms: DmsAdapter, *, mode: str,
                 apply: bool, vessels: list[VesselRef], all_vessel_names: list[str],
                 result: Result, sanitize=sanitize_name):
        self.tpl, self.spo, self.dms = tpl, spo, dms
        self.mode, self.apply = mode, apply
        self.vessels = vessels
        self.vessel_keys = {match_key(n) for n in all_vessel_names}
        self.result = result
        self.sanitize = sanitize
        self.create_mains = mode in ("full_template", "adopt_create")
        self.create_children = mode in ("full_template", "adopt_create")
        self.adopt_customs = mode in ("adopt_existing", "adopt_create")

    # -- helpers -----------------------------------------------------
    @staticmethod
    def _join(parent: str, name: str) -> str:
        return f"{parent}/{name}" if parent else name

    async def _children(self, item_id: str | None) -> list[dict]:
        if not item_id:
            return []
        return [c for c in await self.spo.child_folders(item_id) if c.get("name")]

    def _match(self, wanted: str, existing: list[dict], used: set[str]) -> dict | None:
        safe = self.sanitize(wanted).lower()
        for c in existing:
            if c["id"] not in used and c["name"].lower() == safe:
                return c
        key = match_key(wanted)
        for c in existing:
            if c["id"] not in used and match_key(c["name"]) == key:
                return c
        return None

    def _dms_state(self, item_id: str) -> str:
        return "linked" if self.dms.row_by_item(item_id) else "link"

    async def _ensure(self, parent_id: str, name: str) -> dict:
        last: Exception | None = None
        for attempt in range(2):  # GraphClient already retries 429/503 x6
            try:
                return await self.spo.ensure_folder(parent_id, name)
            except Exception as exc:  # noqa: BLE001 - reported per item
                last = exc
                if getattr(exc, "status", None) in (400, 401, 403, 404, 409, 423):
                    break
                await asyncio.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    def _upsert(self, **kw) -> None:
        self.dms.upsert(**kw)

    # -- one folder --------------------------------------------------
    async def _node(self, *, parent_id: str | None, parent_path: str, existing: list[dict],
                    used: set[str], wanted: str, kind: str, month_driven: bool,
                    level: str, vessel: VesselRef | None, allow_create: bool,
                    parent_failed: bool = False) -> tuple[str | None, str, str]:
        """Resolve one template folder.

        Returns (item_id, path, state); state is "exists" (found or created),
        "planned" (dry-run: will be created), "skipped" or "failed"."""
        vname = vessel.name if vessel else None
        vid = vessel.id if vessel else None
        hit = self._match(wanted, existing, used)
        if hit is not None:
            used.add(hit["id"])
            path = self._join(parent_path, hit["name"])
            dms_state = self._dms_state(hit["id"])
            item = self.result.add(PlanItem(
                path=path, name=hit["name"], level=level, scope="template",
                template_name=wanted, vessel=vname, sp="exists", dms=dms_state,
                outcome="reused", kind=kind,
                reason="" if hit["name"] == self.sanitize(wanted) else "matched by name; existing name kept",
            ))
            if self.apply and dms_state == "link":
                try:
                    self._upsert(path=path, name=hit["name"], kind=kind, item_id=hit["id"],
                                 month_driven=month_driven, vessel_id=vid)
                except Exception as exc:  # noqa: BLE001
                    item.outcome, item.reason = "failed", f"DMS link failed: {exc}"
            return hit["id"], path, "exists"

        safe = self.sanitize(wanted)
        path = self._join(parent_path, safe)
        if not allow_create or parent_failed:
            self.result.add(PlanItem(
                path=path, name=safe, level=level, scope="template", template_name=wanted,
                vessel=vname, sp="skip", dms="skip", outcome="skipped", kind=kind,
                reason=("parent folder failed" if parent_failed else
                        "not created in this mode" if not allow_create else ""),
            ))
            return None, path, "failed" if parent_failed else "skipped"

        item = self.result.add(PlanItem(
            path=path, name=safe, level=level, scope="template", template_name=wanted,
            vessel=vname, sp="create", dms="create", outcome="created", kind=kind,
        ))
        if not self.apply:
            return None, path, "planned"
        if parent_id is None:
            item.outcome, item.reason = "failed", "parent folder missing"
            return None, path, "failed"
        try:
            created = await self._ensure(parent_id, safe)
        except Exception as exc:  # noqa: BLE001
            item.outcome = "failed"
            item.reason = f"SharePoint: {exc}"[:500]
            return None, path, "failed"
        try:
            self._upsert(path=path, name=created.get("name") or safe, kind=kind,
                         item_id=created["id"], month_driven=month_driven, vessel_id=vid)
        except Exception as exc:  # noqa: BLE001
            # SharePoint folder exists; a re-run links it (idempotent).
            item.outcome = "failed"
            item.reason = f"SharePoint folder created but DMS write failed (re-run to link): {exc}"[:500]
            return None, path, "failed"
        return created["id"], path, "exists"

    def _customs(self, *, parent_path: str, existing: list[dict], used: set[str],
                 vessel: VesselRef | None, level: str, skip_vessel_folders: bool = False) -> None:
        if not self.adopt_customs:
            return
        for c in existing:
            if c["id"] in used:
                continue
            if skip_vessel_folders and match_key(c["name"]) in self.vessel_keys:
                continue  # another vessel's ship folder, not a custom folder
            path = self._join(parent_path, c["name"])
            dms_state = self._dms_state(c["id"])
            item = self.result.add(PlanItem(
                path=path, name=c["name"], level=level, scope="custom", template_name=None,
                vessel=vessel.name if vessel else None, sp="exists", dms=dms_state,
                outcome="custom", kind="folder", reason="not in template — kept unchanged",
            ))
            if self.apply and dms_state == "link":
                try:
                    self._upsert(path=path, name=c["name"], kind="folder", item_id=c["id"],
                                 month_driven=False, vessel_id=vessel.id if vessel else None)
                except Exception as exc:  # noqa: BLE001
                    item.outcome, item.reason = "failed", f"DMS link failed: {exc}"

    async def _subtree(self, *, parent_id: str | None, parent_path: str, state: str,
                       nodes: list[TNode], vessel: VesselRef | None) -> None:
        """Children of a resolved folder. state is the parent's state."""
        if state == "skipped":
            return
        failed = state == "failed"
        existing = await self._children(parent_id) if state == "exists" else []
        used: set[str] = set()
        for n in nodes:
            nid, npath, nstate = await self._node(
                parent_id=parent_id, parent_path=parent_path, existing=existing, used=used,
                wanted=n.name, kind=n.kind, month_driven=n.month_driven, level="folder",
                vessel=vessel, allow_create=self.create_children, parent_failed=failed,
            )
            if n.children and not n.month_driven:
                await self._subtree(parent_id=nid, parent_path=npath, state=nstate,
                                    nodes=n.children, vessel=vessel)
        self._customs(parent_path=parent_path, existing=existing, used=used, vessel=vessel,
                      level="folder")

    # -- modes -------------------------------------------------------
    async def run(self) -> None:
        if self.mode == "empty_pool":
            await self._run_empty_pool()
            return
        lib_root = await self.spo.root_id()
        base_id, base_path, base_state = lib_root, "", "exists"
        if self.tpl.root:
            base_id, base_path, base_state = await self._node(
                parent_id=lib_root, parent_path="", existing=await self._children(lib_root),
                used=set(), wanted=self.tpl.root, kind="folder", month_driven=False,
                level="root", vessel=None, allow_create=self.create_mains,
            )
            if base_state == "skipped":
                return
        top = await self._children(base_id) if base_state == "exists" else []
        top_used: set[str] = set()
        for main in self.tpl.mains:
            mid, mpath, mstate = await self._node(
                parent_id=base_id, parent_path=base_path, existing=top, used=top_used,
                wanted=main.name, kind="main", month_driven=False, level="main",
                vessel=None, allow_create=self.create_mains,
                parent_failed=base_state == "failed",
            )
            if mstate == "skipped":
                continue  # Mode 3: absent main folder — nothing below it is created.
            main_children = await self._children(mid) if mstate == "exists" else []
            used: set[str] = set()
            main_failed = mstate == "failed"

            # Common folders — once per main, not per vessel.
            if main.common:
                if main.common_folder:
                    cid, cpath, cstate = await self._node(
                        parent_id=mid, parent_path=mpath, existing=main_children, used=used,
                        wanted=main.common_folder, kind="common", month_driven=False,
                        level="common", vessel=None, allow_create=self.create_children,
                        parent_failed=main_failed,
                    )
                    await self._subtree(parent_id=cid, parent_path=cpath, state=cstate,
                                        nodes=main.common, vessel=None)
                else:
                    for n in main.common:
                        nid, npath, nstate = await self._node(
                            parent_id=mid, parent_path=mpath, existing=main_children, used=used,
                            wanted=n.name, kind=n.kind, month_driven=n.month_driven,
                            level="common", vessel=None, allow_create=self.create_children,
                            parent_failed=main_failed,
                        )
                        if n.children and not n.month_driven:
                            await self._subtree(parent_id=nid, parent_path=npath, state=nstate,
                                                nodes=n.children, vessel=None)

            # Per-ship folders — one per vessel, named after the vessel.
            if main.per_ship:
                for v in self.vessels:
                    sid, spath, sstate = await self._node(
                        parent_id=mid, parent_path=mpath, existing=main_children, used=used,
                        wanted=v.name, kind="ship", month_driven=False, level="ship",
                        vessel=v, allow_create=self.create_children, parent_failed=main_failed,
                    )
                    await self._subtree(parent_id=sid, parent_path=spath, state=sstate,
                                        nodes=main.per_ship, vessel=v)
            self._customs(parent_path=mpath, existing=main_children, used=used, vessel=None,
                          level="folder", skip_vessel_folders=True)

    async def _run_empty_pool(self) -> None:
        """Mode 1: existing slot behaviour. Report/link only — never create."""
        lib_root = None
        for v in self.vessels:
            rows = self.dms.vessel_ship_rows(v.id)
            if rows:
                for row in rows:
                    ok = await self.spo.item_exists(row["drive_item_id"]) if row.get("drive_item_id") else False
                    self.result.add(PlanItem(
                        path=row["path"], name=row["name"], level="slot", scope="slot",
                        template_name=None, vessel=v.name, sp="exists" if ok else "skip",
                        dms="linked", outcome="reused" if ok else "failed", kind="ship",
                        reason="" if ok else "slot's SharePoint folder is missing — use Provision",
                    ))
                continue
            if lib_root is None:
                lib_root = await self.spo.root_id()
                root_children = await self._children(lib_root)
            hit = self._match(v.name, root_children, set())
            if hit is None:
                self.result.add(PlanItem(
                    path=self.sanitize(v.name), name=self.sanitize(v.name), level="slot",
                    scope="slot", template_name=None, vessel=v.name, sp="skip", dms="skip",
                    outcome="skipped", kind="ship",
                    reason="vessel has no slot folder yet — use the existing Provision action",
                ))
                continue
            item = self.result.add(PlanItem(
                path=hit["name"], name=hit["name"], level="slot", scope="slot",
                template_name=None, vessel=v.name, sp="exists", dms="link",
                outcome="reused", kind="ship", reason="SharePoint folder linked to DMS slot",
            ))
            if self.apply:
                try:
                    self._upsert(path=hit["name"], name=hit["name"], kind="ship",
                                 item_id=hit["id"], month_driven=False, vessel_id=v.id)
                except Exception as exc:  # noqa: BLE001
                    item.outcome, item.reason = "failed", f"DMS link failed: {exc}"


async def plan(tpl: Template, spo: SpoAdapter, dms: DmsAdapter, *, mode: str, apply: bool,
               vessels: list[VesselRef], all_vessel_names: list[str], site: dict,
               sanitize=sanitize_name) -> Result:
    if mode not in MODES:
        raise ValueError(f"Unknown folder structure mode {mode!r}")
    result = Result(mode=mode, dry_run=not apply, vessels=[v.name for v in vessels], site=site)
    planner = _Planner(tpl, spo, dms, mode=mode, apply=apply, vessels=vessels,
                       all_vessel_names=all_vessel_names, result=result, sanitize=sanitize)
    await planner.run()
    return result.finalize()


# ======================================================================
# Runtime wiring (RealBackend + Graph + PostgreSQL)
# ======================================================================
class _GraphSpo:
    def __init__(self, backend, drive_id: str):
        from ..graph import drive as gd

        self._gd = gd
        self._backend = backend
        self._drive = drive_id
        self._cache: dict[str, list[dict]] = {}

    async def root_id(self) -> str:
        return await self._gd.get_root_item_id(self._drive)

    async def child_folders(self, item_id: str) -> list[dict]:
        if item_id not in self._cache:
            async with self._backend._semaphore():
                kids = await self._gd.list_children(self._drive, item_id)
            self._cache[item_id] = [
                {"id": k["id"], "name": k.get("name", "")} for k in kids if k.get("folder") is not None
            ]
        return self._cache[item_id]

    async def item_exists(self, item_id: str) -> bool:
        from ..graph.client import GraphError

        try:
            item = await self._gd.get_item(self._drive, item_id)
            return item.get("folder") is not None
        except GraphError as exc:
            if exc.status == 404:
                return False
            raise

    async def ensure_folder(self, parent_id: str, name: str) -> dict:
        async with self._backend._semaphore():
            item = await self._gd.ensure_folder(self._drive, parent_id, name)
        if item.get("folder") is None and "file" in item:
            raise RuntimeError(f"name conflict: a file named {name!r} already exists")
        self._cache.pop(parent_id, None)
        return item


class _DbDms:
    def __init__(self, backend):
        from ..db import models
        from ..db.base import SessionLocal

        self._backend, self._models, self._SessionLocal = backend, models, SessionLocal

    def _row(self, **filters) -> dict | None:
        with self._SessionLocal() as db:
            r = db.query(self._models.Folder).filter_by(**filters).first()
            return None if r is None else {"path": r.path, "name": r.name,
                                           "drive_item_id": r.drive_item_id, "kind": r.kind}

    def row_by_item(self, item_id: str) -> dict | None:
        return self._row(drive_item_id=item_id)

    def row_by_path(self, path: str) -> dict | None:
        return self._row(path=path)

    def vessel_ship_rows(self, vessel_id: int) -> list[dict]:
        """Slot root(s): flat ship rows (no '/' in path) for this vessel."""
        with self._SessionLocal() as db:
            rows = db.query(self._models.Folder).filter_by(vessel_id=vessel_id, kind="ship").all()
            return [{"path": r.path, "name": r.name, "drive_item_id": r.drive_item_id}
                    for r in rows if "/" not in (r.path or "")]

    def upsert(self, *, path, name, kind, item_id, month_driven, vessel_id) -> None:
        with self._SessionLocal() as db:
            self._backend._upsert(db, path, name, kind, item_id, month_driven, vessel_id)
            db.commit()


def current_site_info() -> dict:
    from ..config import compute_sp_site_url, settings

    site_key = str(settings.active_site or "")
    name = str(getattr(settings, "sp_site_name", "") or "")
    return {
        "site_key": site_key,
        "site_name": name,
        "site_url": compute_sp_site_url(site_key, name, getattr(settings, "sharepoint_site_url", "") or ""),
        "drive_id": str(settings.drive_id or ""),
    }


def assert_site_allowed(site: dict, tpl: Template | None = None) -> None:
    from ..graph.guard import assert_allowed

    assert_allowed(site.get("drive_id"), site.get("site_url"), site.get("site_name"),
                   site.get("site_key"), tpl.root if tpl else None,
                   operation="folder-structure-mode", folder_mode=True)


def _site_vessels(db, models) -> list:
    from ..config import settings, site_alias_matches

    active = str(settings.active_site or "")
    out = []
    for v in db.query(models.Vessel).order_by(models.Vessel.name).all():
        sites = [str(s) for s in (v.provisioned_site_ids or []) if str(s).strip()]
        if v.provisioned_site_key:
            sites.append(str(v.provisioned_site_key))
        if not sites or any(site_alias_matches(s, active) for s in sites):
            out.append(v)
    return out


def list_site_vessels() -> list[dict]:
    from ..db import models
    from ..db.base import SessionLocal

    with SessionLocal() as db:
        return [{"id": str(v.id), "name": v.name,
                 "folder_structure_mode": v.folder_structure_mode or DEFAULT_MODE,
                 "is_provisioned": bool(v.is_provisioned)}
                for v in _site_vessels(db, models)]


def get_default_mode() -> str:
    from ..db import models
    from ..db.base import SessionLocal

    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=DEFAULT_MODE_SETTING_KEY).one_or_none()
        value = (row.value if row else "") or DEFAULT_MODE
    return value if value in MODES else DEFAULT_MODE


def set_default_mode(mode: str, email: str) -> str:
    from datetime import datetime

    from ..db import models
    from ..db.base import SessionLocal

    if mode not in MODES:
        raise ValueError(f"Unknown folder structure mode {mode!r}")
    with SessionLocal() as db:
        row = db.query(models.AppSetting).filter_by(key=DEFAULT_MODE_SETTING_KEY).one_or_none()
        if row is None:
            row = models.AppSetting(key=DEFAULT_MODE_SETTING_KEY)
            db.add(row)
        row.value, row.updated_by, row.updated_at = mode, email, datetime.utcnow()
        db.commit()
    return mode


async def run(backend, *, mode: str, vessel_ids: list[str] | None, all_vessels: bool,
              apply: bool, requesting_email: str = "", requesting_name: str = "",
              trigger: str = "settings") -> dict:
    """Dry-run or apply `mode` to the selected vessels of the current site."""
    from ..db import models
    from ..db.base import SessionLocal
    from .real_backend import sanitize_folder_name

    if mode not in MODES:
        raise ValueError(f"Unknown folder structure mode {mode!r}")
    tpl = load_template()
    site = current_site_info()
    assert_site_allowed(site, tpl)

    with SessionLocal() as db:
        site_vessels = _site_vessels(db, models)
        all_names = [v.name for v in db.query(models.Vessel).all()]
        if all_vessels:
            chosen = site_vessels
        else:
            wanted = {int(x) for x in (vessel_ids or []) if str(x).strip().isdigit()}
            chosen = [v for v in site_vessels if v.id in wanted]
        refs = [VesselRef(id=v.id, name=v.name, mode=v.folder_structure_mode) for v in chosen]
    if not refs:
        raise ValueError("No vessels selected on the current site")

    drive_id = await backend._drive()
    spo, dms = _GraphSpo(backend, drive_id), _DbDms(backend)

    async def _go() -> Result:
        return await plan(tpl, spo, dms, mode=mode, apply=apply, vessels=refs,
                          all_vessel_names=all_names, site=site, sanitize=sanitize_folder_name)

    if not apply:
        return (await _go()).to_dict()

    if _apply_lock.locked():
        raise RuntimeError("Another folder-structure apply is running — try again when it finishes")
    async with _apply_lock:
        result = await _go()
        with SessionLocal() as db:
            for v in db.query(models.Vessel).filter(models.Vessel.id.in_([r.id for r in refs])).all():
                v.folder_structure_mode = mode
            db.commit()
    out = result.to_dict()
    s = out["summary"]
    message = (
        f"{requesting_email or 'system'} applied folder structure mode "
        f"'{MODE_LABELS[mode]}' ({trigger}) to {len(refs)} vessel(s) on site "
        f"'{site['site_name'] or site['site_key']}' (drive {site['drive_id']}): "
        f"created SPO {s['created_sp']} / DMS {s['created_dms']}, reused {s['reused']}, "
        f"custom kept {s['custom_kept']}, skipped {s['skipped']}, failed {s['failed']}."
    )
    try:
        await backend._create_activity(
            action_type="apply_folder_structure_mode",
            requesting_email=requesting_email or "system",
            requesting_name=requesting_name or None,
            department="All Departments",
            vessel_id=refs[0].id if len(refs) == 1 else None,
            vessel_name=refs[0].name if len(refs) == 1 else f"{len(refs)} vessels",
            target_description=f"Folder structure mode: {MODE_LABELS[mode]}",
            payload={"mode": mode, "vessels": [r.name for r in refs], "site": site,
                     "trigger": trigger, "template_version": tpl.version},
            changes=[asdict(result.summary)],
            message=message,
        )
    except Exception:  # audit must never fail the apply
        log.exception("[folder-structure] audit write failed")
    log.info("[folder-structure] %s", message)
    return out


async def apply_on_vessel_create(backend, vessel_id: str, vessel_name: str,
                                 requesting_email: str = "", requesting_name: str = "") -> None:
    """Background hook from RealBackend.create_vessel: persist the default
    mode on the new vessel and, for Modes 2–4, build/adopt its structure.
    Mode 1 (default) does nothing beyond saving the mode, so vessel
    creation stays exactly as before."""
    from ..db import models
    from ..db.base import SessionLocal

    try:
        mode = get_default_mode()
        with SessionLocal() as db:
            v = db.query(models.Vessel).filter_by(id=int(vessel_id)).one_or_none()
            if v is None:
                return
            v.folder_structure_mode = mode
            db.commit()
        if mode == "empty_pool":
            return
        # Wait for the concurrent create/claim to settle before scanning.
        await asyncio.sleep(2)
        for attempt in range(3):
            try:
                await run(backend, mode=mode, vessel_ids=[str(vessel_id)], all_vessels=False,
                          apply=True, requesting_email=requesting_email,
                          requesting_name=requesting_name, trigger="vessel_create")
                return
            except RuntimeError as exc:
                if "Another folder-structure apply" in str(exc) and attempt < 2:
                    await asyncio.sleep(15)
                    continue
                raise
    except Exception:
        log.exception("[folder-structure] on-create apply failed for vessel %s (%s)",
                      vessel_id, vessel_name)


async def ensure_current_month_folders(backend) -> dict:
    """Daily job: create this month's folder (+ its template categories)
    under every month_driven folder of vessels in Modes 2 and 4. Idempotent;
    month names use the app's existing month_label so uploads reuse them."""
    from datetime import date

    from ..db import models
    from ..db.base import SessionLocal
    from ..ocr.dates import month_label
    from .real_backend import sanitize_folder_name

    tpl = load_template()
    site = current_site_info()
    try:
        assert_site_allowed(site, tpl)
    except Exception as exc:
        log.error("[folder-structure] month job blocked: %s", exc)
        return {"blocked": str(exc)}
    today = date.today()
    label = month_label(today.year, today.month)
    with SessionLocal() as db:
        vids = [v.id for v in db.query(models.Vessel).filter(
            models.Vessel.folder_structure_mode.in_(("full_template", "adopt_create"))).all()]
        rows = [] if not vids else [
            {"path": r.path, "name": r.name, "item": r.drive_item_id, "vid": r.vessel_id}
            for r in db.query(models.Folder).filter(
                models.Folder.kind == "month_driven", models.Folder.vessel_id.in_(vids)).all()
        ]
    if not rows:
        return {"month": label, "folders": 0}
    drive_id = await backend._drive()
    spo, dms = _GraphSpo(backend, drive_id), _DbDms(backend)
    done = failed = 0
    for r in rows:
        children = find_month_children(tpl, r["name"]) or []
        try:
            month = await spo.ensure_folder(r["item"], label)
            mpath = f"{r['path']}/{month.get('name') or label}"
            dms.upsert(path=mpath, name=month.get("name") or label, kind="month",
                       item_id=month["id"], month_driven=False, vessel_id=r["vid"])
            for c in children:
                safe = sanitize_folder_name(c.name)
                cat = await spo.ensure_folder(month["id"], safe)
                dms.upsert(path=f"{mpath}/{cat.get('name') or safe}", name=cat.get("name") or safe,
                           kind="leaf", item_id=cat["id"], month_driven=False, vessel_id=r["vid"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("[folder-structure] month folder %s under %s failed: %s", label, r["path"], exc)
    return {"month": label, "folders": done, "failed": failed}
