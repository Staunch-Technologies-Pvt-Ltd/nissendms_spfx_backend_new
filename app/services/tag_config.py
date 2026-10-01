"""Tag Configuration — configurable Domains, Main Folders, Groups, Categories
and Sub Categories per client (Settings → Tag Configuration).

The tag automation (RealBackend._derive_dms_tags, main.py path parsing,
get_document_aliases) reads its vocabulary from here instead of hard-coded
lists. Vessel Name is NOT part of this: it comes only from the Term Store.

Scope ("client") = the app's existing site scoping: settings.active_site
(session-aware). Resolution order for reads:
    rows for the site  →  rows for "__template__"  →  built-in seed (in memory)
so an existing client sees exactly the previous behaviour after deployment.
A client's own rows are created copy-on-write on its first change.

Hierarchy (confirmed 2026-09-24: optional levels): a child's parent must be
at a *higher* level; levels may be skipped (Main Folder → Category). This
also makes cycles impossible. Domains have no parent; everything else must
have one (no orphans).

Nothing is ever hard-deleted if it is a default item or in use: it is set
Inactive/Archived. Inactive items are hidden from new tagging but still
resolve for existing documents (resolve(..., include_inactive=True)).

Rename: the item's name changes; the old name is kept in aliases so values
already written on documents still resolve. Existing SharePoint tags are not
rewritten (no retro-migration) — see docs/tag-configuration.md.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

log = logging.getLogger(__name__)

LEVELS = ("domain", "main_folder", "group", "category", "sub_category")
LEVEL_LABELS = {
    "domain": "Domain", "main_folder": "Main Folder", "group": "Group",
    "category": "Category", "sub_category": "Sub Category",
}
LEVEL_INDEX = {lvl: i for i, lvl in enumerate(LEVELS)}
STATUSES = ("Active", "Inactive", "Archived")
SOURCES = ("Default", "Custom", "Imported")
TEMPLATE_SCOPE = "__template__"
MAX_NAME_LEN = 128
# Characters SharePoint rejects in folder/file names (plus '&' is mapped, not rejected).
INVALID_CHARS = set('~"#%*:<>?/\\{|}')
MODES = ("add", "replace")
_CACHE_TTL = 60.0


def name_key(name: str | None) -> str:
    """Uniqueness key: case-insensitive, trimmed, inner whitespace collapsed."""
    return " ".join((name or "").split()).lower()


def match_norm(value: str | None) -> str:
    """Loose match used when resolving values found in paths/metadata:
    alphanumerics only, '&' == 'and'."""
    text = (value or "").lower().replace("&", " and ")
    tokens = [t for t in re.split(r"[^0-9a-z]+", text) if t and t != "and"]
    return "".join(tokens)


def folder_name_for(name: str) -> str:
    """Safe SharePoint folder name for a display name.

    Mapping (documented in docs/tag-configuration.md):
      '&'  → 'and' (with single spaces around it)
      any of ~ " # % * : < > ? / \\ { | }  → removed
      leading/trailing spaces and dots   → trimmed; max 128 chars.
    """
    s = re.sub(r"\s*&\s*", " and ", name or "")
    s = "".join(c for c in s if c not in INVALID_CHARS)
    s = " ".join(s.split()).strip(" .")
    return s[:MAX_NAME_LEN]


def validate_name(name: str | None) -> list[str]:
    errors = []
    clean = " ".join((name or "").split())
    if not clean:
        errors.append("Name is required.")
        return errors
    if len(clean) > MAX_NAME_LEN:
        errors.append(f"Name must be at most {MAX_NAME_LEN} characters.")
    bad = sorted({c for c in clean if c in INVALID_CHARS})
    if bad:
        errors.append("Name contains characters SharePoint does not allow: " + " ".join(bad))
    if clean.startswith(".") or clean.endswith("."):
        errors.append("Name cannot start or end with a dot.")
    return errors


def validate_parent(level: str, parent_level: str | None) -> list[str]:
    if level not in LEVEL_INDEX:
        return [f"Unknown level {level!r}."]
    if level == "domain":
        return [] if parent_level is None else ["A Domain cannot have a parent."]
    if parent_level is None:
        return [f"A {LEVEL_LABELS[level]} must have a parent."]
    if LEVEL_INDEX.get(parent_level, 99) >= LEVEL_INDEX[level]:
        return [f"The parent of a {LEVEL_LABELS[level]} must be a higher level "
                f"(got {LEVEL_LABELS.get(parent_level, parent_level)})."]
    return []


# ======================================================================
# Default seed — the values that were hard-coded before 2026-09-24
# ======================================================================
def _legacy_domain_aliases() -> dict[str, list[str]]:
    # Previously main.py get_document_aliases (L2236-2250).
    return {
        "Technical & Crewing": [
            "technical and crewing new", "technical and crewing  new", "technical & crewing",
            "technical & crewing new", "technical", "crewing", "technical and crewing",
            "technical/crewing",
        ],
        "Commercial & Chartering": [
            "commercial and chartering", "commercial & chartering", "commercial", "chartering",
            "commercial & operations", "operations",
        ],
        "Insurance": ["insurance", "claims", "insurance & claims"],
        "Kaizen - Knowledge Bank": [
            "kaizen", "knowledge bank", "kaizen - knowledge bank", "kaizen-knowledge bank",
            "kaizen & knowledgebank", "kaizen and knowledgebank",
        ],
    }


def seed_tree() -> list[dict]:
    """Nested default tree built from the previous hard-coded sources:
    template.SHIP_TEMPLATE / COMMON_TEMPLATE (main folders),
    ocr.drawing_category DRAWING_TAXONOMY / MANUAL_TAXONOMY (groups,
    categories, sub-categories), main.py department aliases (domains).

    Domain path_mode keeps _derive_dms_tags' exact previous behaviour:
      "vessel" — {Domain}/{Vessel}/{Category}/...  (the 3 template.MAIN_FOLDERS)
      "legacy" — Kaizen: was in no list, so it fell through to the generic
                 fallback branch; kept identical on purpose.
    Only main folders that carry tagging meaning are seeded as parents of
    Groups ("Drawings and Manuals"); other main folders are seeded without
    children because the automation tags their sub-folders positionally
    (Category = main folder segment), not from a vocabulary.
    """
    from .. import template
    from ..ocr.drawing_category import DRAWING_TAXONOMY, MANUAL_TAXONOMY

    aliases = _legacy_domain_aliases()

    def main_folders_for(domain: str) -> list[str]:
        names: list[str] = []
        for spec in template.SHIP_TEMPLATE.get(domain, []) + template.COMMON_TEMPLATE.get(domain, []):
            if spec["name"] not in names:
                names.append(spec["name"])
        return names

    def taxonomy_group(display: str, folder: str, alias: list[str], tax: dict) -> dict:
        return {
            "level": "group", "name": display, "folder_name": folder, "aliases": alias,
            "children": [
                {"level": "category", "name": cat, "children": [
                    {"level": "sub_category", "name": sub} for sub in subs.keys()
                ]}
                for cat, subs in tax.items()
            ],
        }

    tree = []
    for dom in template.MAIN_FOLDERS:
        mains = []
        for mf in main_folders_for(dom):
            node: dict = {"level": "main_folder", "name": mf}
            if mf == "Drawings and Manuals":
                node["children"] = [
                    taxonomy_group("Drawings", "Drawing", ["drawing", "drawings"], DRAWING_TAXONOMY),
                    taxonomy_group("Manuals", "Manual", ["manual", "manuals"], MANUAL_TAXONOMY),
                ]
            mains.append(node)
        tree.append({"level": "domain", "name": dom, "aliases": aliases.get(dom, []),
                     "attributes": {"path_mode": "vessel"}, "children": mains})
    tree.append({
        "level": "domain", "name": "Kaizen - Knowledge Bank",
        "aliases": aliases["Kaizen - Knowledge Bank"], "attributes": {"path_mode": "legacy"},
        "children": [{"level": "main_folder", "name": n} for n in (
            "Templates", "Procedures and Work Instructions", "Lessons Learned", "Circulars and Guidance")],
    })
    return tree


def flatten_tree(tree: list[dict], *, source: str = "Default", is_default: bool = True) -> list[dict]:
    """Nested seed → flat rows with temporary ids (negative) and parent ids."""
    rows: list[dict] = []
    counter = [0]

    def walk(nodes, parent_id):
        for order, n in enumerate(nodes):
            counter[0] -= 1
            rid = counter[0]
            name = n["name"]
            rows.append({
                "id": rid, "level": n["level"], "name": name, "name_key": name_key(name),
                "display_name": n.get("display_name") or name,
                "folder_name": n.get("folder_name") or name,
                "code": n.get("code"), "description": n.get("description"),
                "parent_id": parent_id, "sort_order": (order + 1) * 10, "status": "Active",
                "is_default": is_default, "source": source, "replaced_by": None,
                "aliases": list(n.get("aliases") or []),
                "attributes": dict(n.get("attributes") or {}), "version": 1,
            })
            walk(n.get("children") or [], rid)

    walk(tree, None)
    return rows


# ======================================================================
# Read model used by the automation
# ======================================================================
@dataclass
class TagView:
    """Immutable snapshot of one client's configuration (dict rows)."""
    site_key: str
    origin: str               # site | template | builtin
    rows: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.by_id = {r["id"]: r for r in self.rows}
        self._children: dict[Any, list[dict]] = {}
        for r in sorted(self.rows, key=lambda x: (x["sort_order"], x["name_key"])):
            self._children.setdefault(r["parent_id"], []).append(r)

    # -- basic queries -------------------------------------------------
    def items(self, level: str | None = None, *, active_only: bool = False) -> list[dict]:
        out = [r for r in self.rows if (level is None or r["level"] == level)
               and (not active_only or r["status"] == "Active")]
        return sorted(out, key=lambda r: (self._path_sort(r), r["sort_order"], r["name_key"]))

    def children(self, parent_id, *, active_only: bool = False) -> list[dict]:
        return [r for r in self._children.get(parent_id, [])
                if not active_only or r["status"] == "Active"]

    def ancestors(self, row: dict) -> list[dict]:
        out, cur, seen = [], row, set()
        while cur and cur.get("parent_id") is not None and cur["parent_id"] not in seen:
            seen.add(cur["parent_id"])
            cur = self.by_id.get(cur["parent_id"])
            if cur:
                out.append(cur)
        return list(reversed(out))

    def path_of(self, row: dict) -> str:
        return " > ".join([a["display_name"] for a in self.ancestors(row)] + [row["display_name"]])

    def _path_sort(self, row: dict) -> tuple:
        return tuple((a["sort_order"], a["name_key"]) for a in self.ancestors(row))

    def domain_of(self, row: dict) -> dict | None:
        if row["level"] == "domain":
            return row
        anc = self.ancestors(row)
        return anc[0] if anc and anc[0]["level"] == "domain" else None

    # -- resolution ------------------------------------------------------
    def _names(self, row: dict) -> set[str]:
        vals = {row["name"], row["display_name"], row["folder_name"], *row.get("aliases", [])}
        return {match_norm(v) for v in vals if v}

    def resolve(self, level: str, value: str | None, *, include_inactive: bool = True,
                within: Iterable[dict] | None = None) -> dict | None:
        """Find the item a raw value (path segment / metadata) refers to.
        Active items win over inactive ones with the same name."""
        key = match_norm(value)
        if not key:
            return None
        pool = list(within) if within is not None else self.items(level)
        hits = [r for r in pool if r["level"] == level and key in self._names(r)
                and (include_inactive or r["status"] == "Active")]
        if not hits:
            return None
        hits.sort(key=lambda r: (r["status"] != "Active", r["sort_order"]))
        return hits[0]

    def descendants(self, row: dict, level: str | None = None) -> list[dict]:
        out: list[dict] = []
        stack = list(self._children.get(row["id"], []))
        while stack:
            r = stack.pop()
            if level is None or r["level"] == level:
                out.append(r)
            stack.extend(self._children.get(r["id"], []))
        return out

    # -- automation vocabulary ------------------------------------------
    def domain_names(self, *, active_only: bool = True) -> list[str]:
        return [r["display_name"] for r in self.items("domain", active_only=active_only)]

    def default_domain(self) -> str:
        """Fallback when a path has no domain (was hard-coded
        "Technical & Crewing"): first Active domain by sort order."""
        names = self.domain_names(active_only=True)
        return names[0] if names else "Technical & Crewing"

    def match_domain(self, value: str | None, *, include_inactive: bool = True) -> str | None:
        """Domain for a path segment. An alias that is also the name of a
        folder at another level (e.g. "crewing" vs the "Crewing" main folder)
        is ignored, so a sub-folder is never mistaken for a Domain."""
        key = match_norm(value)
        if not key:
            return None
        if key in self._other_level_names():
            hit = next((r for r in self.items("domain")
                        if key in {match_norm(r["name"]), match_norm(r["display_name"]), match_norm(r["folder_name"])}
                        and (include_inactive or r["status"] == "Active")), None)
        else:
            hit = self.resolve("domain", value, include_inactive=include_inactive)
        return hit["display_name"] if hit else None

    def _other_level_names(self) -> set[str]:
        if not hasattr(self, "_other_names"):
            self._other_names = {match_norm(v) for r in self.rows if r["level"] != "domain"
                                 for v in (r["name"], r["display_name"], r["folder_name"])}
        return self._other_names

    def domain_alias_map(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.items("domain", active_only=True):
            vals = [r["name"].lower(), r["display_name"].lower(), r["folder_name"].lower(),
                    *[a.lower() for a in r.get("aliases", [])]]
            out[r["display_name"]] = list(dict.fromkeys(v for v in vals if v))
        return out

    def vessel_domain_keys(self) -> set[str]:
        """lower-cased names/aliases of domains whose paths are {Domain}/{Vessel}/..."""
        return self._domain_keys("vessel")

    def flat_domain_keys(self) -> set[str]:
        return self._domain_keys("flat")

    def _domain_keys(self, mode: str) -> set[str]:
        keys: set[str] = set()
        for r in self.items("domain"):
            if (r.get("attributes") or {}).get("path_mode", "vessel") == mode:
                keys |= {r["name"].strip().lower(), r["display_name"].strip().lower(),
                         r["folder_name"].strip().lower()}
        return keys

    def group_markers(self) -> set[str]:
        """lower names of main folders that contain Groups ("drawings and manuals")."""
        out: set[str] = set()
        for r in self.items("main_folder"):
            if any(c["level"] == "group" for c in self.children(r["id"])):
                out |= {r["name"].lower(), r["display_name"].lower(), r["folder_name"].lower(),
                        *[a.lower() for a in r.get("aliases", [])]}
        return out

    def group_for_value(self, value: str | None) -> str:
        """Canonical Group display name for a raw value ("drawing" → "Drawings")."""
        hit = self.resolve("group", value)
        return hit["display_name"] if hit else (value or "")

    def group_value_keys(self) -> set[str]:
        keys: set[str] = set()
        for r in self.items("group"):
            keys |= {r["name"].lower(), r["display_name"].lower(), r["folder_name"].lower(),
                     *[a.lower() for a in r.get("aliases", [])]}
        return keys

    def categories_by_group(self) -> dict[str, set[str]]:
        """{group display name: lower category names} incl. inactive (resolve old docs)."""
        out: dict[str, set[str]] = {}
        for g in self.items("group"):
            cats = out.setdefault(g["display_name"], set())
            for c in self.descendants(g, "category"):
                cats |= {c["name"].lower(), c["display_name"].lower(), c["folder_name"].lower()}
        return out

    def to_public(self) -> dict:
        return {"site_key": self.site_key, "origin": self.origin,
                "items": [public_row(r, self) for r in self.items()]}


def public_row(r: dict, view: TagView | None = None) -> dict:
    out = {k: r.get(k) for k in (
        "id", "level", "name", "display_name", "folder_name", "code", "description",
        "parent_id", "sort_order", "status", "is_default", "source", "replaced_by", "version")}
    out["aliases"] = list(r.get("aliases") or [])
    out["attributes"] = dict(r.get("attributes") or {})
    if view is not None:
        out["path"] = view.path_of(r)
    return out


def builtin_view(site_key: str = TEMPLATE_SCOPE) -> TagView:
    return TagView(site_key=site_key, origin="builtin", rows=flatten_tree(seed_tree()))


# ======================================================================
# Add / Replace planning (pure)
# ======================================================================
@dataclass
class IncomingRow:
    level: str
    name: str
    parent_path: str = ""
    code: str | None = None
    description: str | None = None
    sort_order: int | None = None
    status: str = "Active"
    line: int = 0


@dataclass
class DiffEntry:
    action: str               # new | unchanged | reactivate | deactivate | error
    level: str
    name: str
    parent_path: str
    item_id: int | None = None
    message: str = ""
    in_use: int = 0
    line: int = 0


@dataclass
class Diff:
    mode: str
    entries: list[DiffEntry] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        c = {"new": 0, "unchanged": 0, "reactivate": 0, "deactivate": 0, "error": 0, "in_use": 0}
        for e in self.entries:
            c[e.action] += 1
            if e.action == "deactivate" and e.in_use:
                c["in_use"] += 1
        return c

    def to_dict(self) -> dict:
        c = self.counts
        return {
            "mode": self.mode, "counts": c,
            "confirmation": (
                f"{c['deactivate']} items will be deactivated, {c['new']} new items will be created, "
                f"{c['reactivate']} will be reactivated, {c['in_use']} of the deactivated items "
                "are in use by existing documents."
            ),
            "entries": [asdict(e) for e in self.entries],
        }


def _pkey(names: Iterable[str]) -> str:
    return " > ".join(match_norm(n) for n in names)


def parse_parent_path(path: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s*>\s*", path or "") if p.strip()]


def find_by_path(view: TagView, names: list[str]) -> dict | None:
    """Walk 'A > B > C' from the domains down (levels may be skipped)."""
    if not names:
        return None
    cur = None
    pool = view.items("domain")
    for i, nm in enumerate(names):
        key = match_norm(nm)
        cands = [r for r in pool if key in view._names(r)]
        if not cands:
            return None
        cands.sort(key=lambda r: (r["status"] != "Active", LEVEL_INDEX[r["level"]]))
        cur = cands[0]
        pool = view.children(cur["id"])
    return cur


def plan_changes(view: TagView, incoming: list[IncomingRow], mode: str,
                 usage: dict[int, int] | None = None) -> Diff:
    """Diff for Add or Replace. Replace scope = every item at a level present
    in `incoming`, under the same parent(s) referenced by the incoming rows
    (all domains for level 'domain')."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    usage = usage or {}
    diff = Diff(mode=mode)
    seen: set[tuple] = set()
    # parent id (or None for domains) → names kept, per level
    kept: dict[tuple[str, Any], set[str]] = {}
    staged_parents: dict[str, str] = {}  # rows created in this batch: path key → level
    new_active_levels: set[str] = set()

    ordered = sorted(incoming, key=lambda r: (LEVEL_INDEX.get(r.level, 99), r.line))
    for row in ordered:
        errs = []
        if row.level not in LEVEL_INDEX:
            errs.append(f"Unknown level {row.level!r}.")
        errs += validate_name(row.name)
        if row.status not in STATUSES:
            errs.append(f"Status must be one of {', '.join(STATUSES)}.")
        parent_names = parse_parent_path(row.parent_path)
        parent = None
        parent_pending = False
        if not errs:
            if row.level == "domain":
                if parent_names:
                    errs.append("A Domain cannot have a parent path.")
            else:
                if not parent_names:
                    errs.append(f"{LEVEL_LABELS[row.level]} needs a Parent Path.")
                else:
                    parent = find_by_path(view, parent_names)
                    if parent is None:
                        pkey = _pkey(parent_names)
                        if pkey in staged_parents:
                            parent_pending = True
                            plevel = staged_parents[pkey]
                            errs += validate_parent(row.level, plevel)
                        else:
                            errs.append(f"Parent '{row.parent_path}' was not found.")
                    else:
                        errs += validate_parent(row.level, parent["level"])
        dup_key = (row.level, _pkey(parent_names), name_key(row.name))
        if not errs and dup_key in seen:
            errs.append("Duplicate row in this import (same name under the same parent).")
        if errs:
            diff.entries.append(DiffEntry("error", row.level, row.name, row.parent_path,
                                          message=" ".join(errs), line=row.line))
            continue
        seen.add(dup_key)
        pid = parent["id"] if parent else None
        if not parent_pending:
            kept.setdefault((row.level, pid), set()).add(name_key(row.name))
        staged_parents[_pkey([*parent_names, row.name])] = row.level
        existing = None
        if not parent_pending:
            siblings = view.children(pid) if pid is not None else view.items("domain")
            existing = next((s for s in siblings if s["level"] == row.level
                             and s["name_key"] == name_key(row.name)), None)
        if existing is None:
            diff.entries.append(DiffEntry("new", row.level, row.name, row.parent_path, line=row.line))
            if row.status == "Active":
                new_active_levels.add(row.level)
        elif existing["status"] != "Active" and row.status == "Active":
            diff.entries.append(DiffEntry("reactivate", row.level, existing["name"], row.parent_path,
                                          item_id=existing["id"], line=row.line,
                                          message="exists (inactive) — will be reactivated, not duplicated"))
        else:
            diff.entries.append(DiffEntry("unchanged", row.level, existing["name"], row.parent_path,
                                          item_id=existing["id"], line=row.line))

    if mode == "replace":
        for (level, pid), names in kept.items():
            pool = view.children(pid) if pid is not None else view.items("domain")
            for s in pool:
                if s["level"] != level or s["status"] != "Active" or s["name_key"] in names:
                    continue
                diff.entries.append(DiffEntry(
                    "deactivate", level, s["name"], view.path_of(view.by_id[pid]) if pid else "",
                    item_id=s["id"], in_use=int(usage.get(s["id"], 0)),
                    message="replaced — kept for existing documents, hidden from new tagging",
                ))
        # A level may never end up with no Active item.
        for level in {lvl for (lvl, _p) in kept}:
            active_after = {r["id"] for r in view.items(level, active_only=True)}
            active_after -= {e.item_id for e in diff.entries if e.action == "deactivate" and e.level == level}
            active_after |= {e.item_id for e in diff.entries if e.action == "reactivate" and e.level == level}
            if not active_after and level not in new_active_levels:
                diff.entries.append(DiffEntry("error", level, "", "",
                                              message=f"{LEVEL_LABELS[level]}: the list must keep at least one Active item."))
    return diff


# ======================================================================
# Persistence (SQLAlchemy) — thin, so the logic above stays testable
# ======================================================================
_cache: dict[str, tuple[float, TagView]] = {}
_cache_lock = threading.Lock()


def _row_to_dict(r) -> dict:
    return {
        "id": r.id, "level": r.level, "name": r.name, "name_key": r.name_key,
        "display_name": r.display_name, "folder_name": r.folder_name, "code": r.code,
        "description": r.description, "parent_id": r.parent_id, "sort_order": r.sort_order,
        "status": r.status, "is_default": bool(r.is_default), "source": r.source,
        "replaced_by": r.replaced_by, "aliases": _json_list(r.aliases_json),
        "attributes": _json_dict(r.attributes_json), "version": r.version,
        "created_by": r.created_by, "modified_by": r.modified_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "modified_at": r.modified_at.isoformat() if r.modified_at else None,
    }


def _json_list(s) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _json_dict(s) -> dict:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def current_site_key() -> str:
    try:
        from ..config import settings
        return str(settings.active_site or "default").strip().lower() or "default"
    except Exception:
        return "default"


def invalidate(site_key: str | None = None) -> None:
    with _cache_lock:
        # Sites without their own rows read the template, so a template
        # change invalidates everything.
        if site_key is None or site_key == TEMPLATE_SCOPE:
            _cache.clear()
        else:
            _cache.pop(site_key, None)


def _db_ready() -> bool:
    try:
        from ..config import settings
        return bool(settings.db_configured)
    except Exception:
        return False


def _load_scope(db, scope: str) -> list[dict]:
    from ..db import models
    return [_row_to_dict(r) for r in db.query(models.TagConfigItem).filter_by(site_key=scope).all()]


def get_view(site_key: str | None = None, *, fresh: bool = False) -> TagView:
    """Configuration the automation should use for `site_key` (default:
    the request's active site). Never raises: falls back to the seed."""
    key = (site_key or current_site_key()).lower()
    now = time.monotonic()
    if not fresh:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL:
                return hit[1]
    view: TagView
    if not _db_ready():
        view = builtin_view(key)
    else:
        try:
            from ..db.base import SessionLocal
            with SessionLocal() as db:
                rows = _load_scope(db, key)
                origin = "site"
                if not rows:
                    rows, origin = _load_scope(db, TEMPLATE_SCOPE), "template"
            view = TagView(site_key=key, origin=origin, rows=rows) if rows else builtin_view(key)
        except Exception as exc:  # table missing before migration etc.
            log.warning("[tag-config] falling back to built-in defaults: %s", exc)
            view = builtin_view(key)
    with _cache_lock:
        _cache[key] = (now, view)
    return view


def _insert_rows(db, scope: str, rows: list[dict], *, email: str | None) -> dict[int, int]:
    """Insert flat rows (temp ids) preserving parent links. Returns id map."""
    from ..db import models
    idmap: dict[int, int] = {}
    pending = sorted(rows, key=lambda r: LEVEL_INDEX[r["level"]])
    for r in pending:
        obj = models.TagConfigItem(
            site_key=scope, level=r["level"], name=r["name"], name_key=r["name_key"],
            display_name=r["display_name"], folder_name=r["folder_name"], code=r.get("code"),
            description=r.get("description"),
            parent_id=idmap.get(r["parent_id"]) if r["parent_id"] is not None else None,
            sort_order=r["sort_order"], status=r["status"], is_default=r["is_default"],
            source=r["source"], replaced_by=None,
            aliases_json=json.dumps(r.get("aliases") or []),
            attributes_json=json.dumps(r.get("attributes") or {}),
            version=r.get("version") or 1, created_by=email, modified_by=email,
        )
        db.add(obj)
        db.flush()
        idmap[r["id"]] = obj.id
    return idmap


def seed_template(db) -> int:
    """Idempotent: seeds the "__template__" scope only when it is empty.
    Never touches a client's own rows. Returns rows inserted."""
    from ..db import models
    if db.query(models.TagConfigItem).filter_by(site_key=TEMPLATE_SCOPE).first() is not None:
        return 0
    rows = flatten_tree(seed_tree())
    _insert_rows(db, TEMPLATE_SCOPE, rows, email="system:seed")
    db.commit()
    invalidate()
    return len(rows)


def ensure_scope(db, scope: str, email: str | None) -> None:
    """Copy-on-write: give a client its own rows (copied from the template,
    or the built-in seed) before its first change."""
    from ..db import models
    if scope == TEMPLATE_SCOPE:
        if db.query(models.TagConfigItem).filter_by(site_key=TEMPLATE_SCOPE).first() is None:
            seed_template(db)
        return
    if db.query(models.TagConfigItem).filter_by(site_key=scope).first() is not None:
        return
    src = _load_scope(db, TEMPLATE_SCOPE) or flatten_tree(seed_tree())
    # remap real ids → temp negative ids so _insert_rows can relink parents
    remap = {r["id"]: -(i + 1) for i, r in enumerate(src)}
    rows = [dict(r, id=remap[r["id"]], parent_id=remap.get(r["parent_id"]) if r["parent_id"] else None)
            for r in src]
    _insert_rows(db, scope, rows, email=email)
    db.flush()


def snapshot(db, scope: str, mode: str, email: str | None, *, level: str | None = None,
             summary: dict | None = None) -> int:
    from ..db import models
    snap = models.TagConfigSnapshot(
        site_key=scope, changed_by=email, mode=mode, level=level,
        summary_json=json.dumps(summary or {}),
        snapshot_json=json.dumps(_load_scope(db, scope), default=str),
    )
    db.add(snap)
    db.flush()
    return snap.id


def restore_rows(db, scope: str, rows: list[dict], email: str | None) -> None:
    """Make the scope's rows equal to `rows` (a snapshot) without deleting
    anything referenced: existing ids are updated in place, rows not in the
    snapshot become Archived, missing ids are re-created."""
    from ..db import models
    current = {r.id: r for r in db.query(models.TagConfigItem).filter_by(site_key=scope).all()}
    wanted = {r["id"]: r for r in rows}
    missing = [r for rid, r in wanted.items() if rid not in current]
    for rid, obj in current.items():
        w = wanted.get(rid)
        if w is None:
            if obj.status == "Active":
                obj.status = "Archived"
                obj.modified_by = email
            continue
        for k in ("name", "name_key", "display_name", "folder_name", "code", "description",
                  "sort_order", "status", "is_default", "source", "replaced_by", "version"):
            setattr(obj, k, w.get(k))
        obj.parent_id = w.get("parent_id")
        obj.aliases_json = json.dumps(w.get("aliases") or [])
        obj.attributes_json = json.dumps(w.get("attributes") or {})
        obj.modified_by = email
    if missing:
        temp = {r["id"]: -(i + 1) for i, r in enumerate(missing)}
        rows2 = [dict(r, id=temp[r["id"]],
                      parent_id=temp.get(r["parent_id"], r["parent_id"]) if r["parent_id"] else None)
                 for r in missing]
        _insert_rows(db, scope, rows2, email=email)
    db.flush()


def usage_counts(db, view: TagView) -> dict[int, int]:
    """Approximate "in use" count per item: DMS folder rows (on the active
    drive) whose name matches the item. Domains count every folder under
    their path. Used to block hard deletes and to warn on Replace."""
    from ..db import models
    names: dict[str, int] = {}
    paths: list[tuple[str, int]] = []
    for r in view.rows:
        for v in {r["name"], r["display_name"], r["folder_name"]}:
            if r["level"] == "domain":
                paths.append((v.lower(), r["id"]))
            else:
                names.setdefault(v.lower(), r["id"])
    counts: dict[int, int] = {}
    try:
        for (name, path) in db.query(models.Folder.name, models.Folder.path).all():
            n = (name or "").lower()
            if n in names:
                counts[names[n]] = counts.get(names[n], 0) + 1
            first = (path or "").split("/", 1)[0].lower()
            for p, rid in paths:
                if first == p:
                    counts[rid] = counts.get(rid, 0) + 1
    except Exception as exc:
        log.debug("[tag-config] usage count unavailable: %s", exc)
    return counts


# ======================================================================
# Mutations (all take an open session; the caller commits)
# ======================================================================
class TagConfigError(ValueError):
    """User-facing validation error (→ HTTP 400/409)."""

    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


def _obj(db, scope: str, item_id: int):
    from ..db import models
    obj = db.query(models.TagConfigItem).filter_by(id=item_id, site_key=scope).one_or_none()
    if obj is None:
        raise TagConfigError(f"Item {item_id} not found in this configuration.", 404)
    return obj


def item_row(db, scope: str, item_id: int) -> dict:
    """Public read of one item's current row, e.g. to capture its name
    before an update_item() call renames it (see tag_config_api.py's
    /items/{id} PATCH, which needs the *old* display name to retag
    already-tagged documents afterwards)."""
    return _row_to_dict(_obj(db, scope, item_id))


def _siblings(db, scope: str, level: str, parent_id):
    from ..db import models
    return db.query(models.TagConfigItem).filter_by(site_key=scope, level=level, parent_id=parent_id).all()


def _check_unique(db, scope, level, parent_id, name, exclude_id=None):
    k = name_key(name)
    for s in _siblings(db, scope, level, parent_id):
        if s.id != exclude_id and s.name_key == k:
            where = "Domain" if level == "domain" else "parent"
            raise TagConfigError(
                f"'{s.name}' already exists under this {where}"
                + (" (inactive — reactivate it instead)." if s.status != "Active" else "."), 409)


def _view_for(db, scope: str) -> TagView:
    return TagView(site_key=scope, origin="site", rows=_load_scope(db, scope))


def create_item(db, scope: str, data: dict, email: str, *, source: str = "Custom") -> dict:
    ensure_scope(db, scope, email)
    level = data.get("level")
    name = " ".join((data.get("name") or "").split())
    errs = validate_name(name)
    parent = _obj(db, scope, int(data["parent_id"])) if data.get("parent_id") else None
    errs += validate_parent(level, parent.level if parent else None)
    if errs:
        raise TagConfigError(" ".join(errs))
    _check_unique(db, scope, level, parent.id if parent else None, name)
    from ..db import models
    sibs = _siblings(db, scope, level, parent.id if parent else None)
    sort = data.get("sort_order")
    if sort is None:
        sort = (max([s.sort_order for s in sibs], default=0) or 0) + 10
    status = data.get("status") or "Active"
    if status not in STATUSES:
        raise TagConfigError(f"Status must be one of {', '.join(STATUSES)}.")
    folder = (data.get("folder_name") or "").strip() or folder_name_for(name)
    ferrs = validate_name(folder)
    if ferrs:
        raise TagConfigError("Folder name: " + " ".join(ferrs))
    attrs = {"path_mode": "vessel"} if level == "domain" else {}
    attrs.update(data.get("attributes") or {})
    obj = models.TagConfigItem(
        site_key=scope, level=level, name=name, name_key=name_key(name),
        display_name=(data.get("display_name") or name).strip(), folder_name=folder,
        code=(data.get("code") or None), description=(data.get("description") or None),
        parent_id=parent.id if parent else None, sort_order=int(sort), status=status,
        is_default=False, source=source, aliases_json="[]", attributes_json=json.dumps(attrs),
        created_by=email, modified_by=email,
    )
    db.add(obj)
    db.flush()
    return _row_to_dict(obj)


def update_item(db, scope: str, item_id: int, data: dict, email: str) -> dict:
    """Edit / rename / re-parent. On rename the old name becomes an alias so
    existing tags keep resolving; SharePoint values are NOT rewritten."""
    ensure_scope(db, scope, email)
    obj = _obj(db, scope, item_id)
    if "name" in data and data["name"] is not None:
        new = " ".join(str(data["name"]).split())
        errs = validate_name(new)
        if errs:
            raise TagConfigError(" ".join(errs))
        if name_key(new) != obj.name_key:
            _check_unique(db, scope, obj.level, obj.parent_id, new, exclude_id=obj.id)
            aliases = _json_list(obj.aliases_json)
            for old in (obj.name, obj.display_name):
                if old and old not in aliases:
                    aliases.append(old)
            obj.aliases_json = json.dumps(aliases)
            obj.name, obj.name_key = new, name_key(new)
            obj.display_name = (data.get("display_name") or new).strip()
            if not data.get("folder_name") and obj.source != "Default":
                obj.folder_name = folder_name_for(new)
        elif data.get("display_name"):
            obj.display_name = data["display_name"].strip()
    if data.get("folder_name"):
        errs = validate_name(data["folder_name"])
        if errs:
            raise TagConfigError("Folder name: " + " ".join(errs))
        obj.folder_name = data["folder_name"].strip()
    if "parent_id" in data:
        new_parent = _obj(db, scope, int(data["parent_id"])) if data["parent_id"] else None
        errs = validate_parent(obj.level, new_parent.level if new_parent else None)
        if errs:
            raise TagConfigError(" ".join(errs))
        _check_unique(db, scope, obj.level, new_parent.id if new_parent else None, obj.name, exclude_id=obj.id)
        obj.parent_id = new_parent.id if new_parent else None
    for k in ("code", "description"):
        if k in data:
            setattr(obj, k, (data[k] or None))
    if "sort_order" in data and data["sort_order"] is not None:
        obj.sort_order = int(data["sort_order"])
    if "attributes" in data and isinstance(data["attributes"], dict):
        attrs = _json_dict(obj.attributes_json)
        attrs.update(data["attributes"])
        obj.attributes_json = json.dumps(attrs)
    if data.get("status"):
        set_status(db, scope, obj.id, data["status"], email)
    obj.version = (obj.version or 1) + 1
    obj.modified_by = email
    db.flush()
    return _row_to_dict(obj)


def set_status(db, scope: str, item_id: int, status: str, email: str) -> dict:
    if status not in STATUSES:
        raise TagConfigError(f"Status must be one of {', '.join(STATUSES)}.")
    ensure_scope(db, scope, email)
    obj = _obj(db, scope, item_id)
    if status != "Active" and obj.status == "Active":
        others = [s for s in _load_scope(db, scope)
                  if s["level"] == obj.level and s["status"] == "Active" and s["id"] != obj.id]
        if not others:
            raise TagConfigError(
                f"{LEVEL_LABELS[obj.level]}s must keep at least one Active item.", 409)
    if status == "Active" and obj.parent_id:
        parent = _obj(db, scope, obj.parent_id)
        if parent.status != "Active":
            raise TagConfigError("Activate the parent first.", 409)
    obj.status = status
    obj.modified_by = email
    obj.version = (obj.version or 1) + 1
    db.flush()
    return _row_to_dict(obj)


def delete_item(db, scope: str, item_id: int, email: str) -> dict:
    """Hard delete only for unused custom items without children."""
    ensure_scope(db, scope, email)
    obj = _obj(db, scope, item_id)
    view = _view_for(db, scope)
    if obj.is_default or obj.source == "Default":
        raise TagConfigError("Default items cannot be deleted — deactivate it instead.", 409)
    if view.children(obj.id):
        raise TagConfigError("This item has child items — deactivate it instead.", 409)
    used = usage_counts(db, view).get(obj.id, 0)
    if used:
        raise TagConfigError(
            f"'{obj.name}' is in use by {used} existing folder(s)/document path(s) — deactivate it instead.",
            409, {"usage": used})
    if obj.status == "Active":
        others = [s for s in view.items(obj.level, active_only=True) if s["id"] != obj.id]
        if not others:
            raise TagConfigError(f"{LEVEL_LABELS[obj.level]}s must keep at least one Active item.", 409)
    row = _row_to_dict(obj)
    db.delete(obj)
    db.flush()
    return row


def reorder(db, scope: str, level: str, parent_id, ordered_ids: list[int], email: str) -> list[dict]:
    ensure_scope(db, scope, email)
    sibs = {s.id: s for s in _siblings(db, scope, level, parent_id)}
    if set(ordered_ids) - set(sibs):
        raise TagConfigError("Reorder list contains items from another level/parent.")
    order = list(ordered_ids) + [i for i in sorted(sibs, key=lambda k: sibs[k].sort_order) if i not in ordered_ids]
    for pos, iid in enumerate(order):
        sibs[iid].sort_order = (pos + 1) * 10
        sibs[iid].modified_by = email
    db.flush()
    return [_row_to_dict(sibs[i]) for i in order]


def commit_changes(db, scope: str, incoming: list[IncomingRow], mode: str, email: str, *,
                   source: str = "Imported", remap: dict[int, int] | None = None,
                   snapshot_mode: str | None = None) -> dict:
    """Apply a previewed Add/Replace. Always snapshots first."""
    ensure_scope(db, scope, email)
    view = _view_for(db, scope)
    diff = plan_changes(view, incoming, mode, usage_counts(db, view))
    errors = [e for e in diff.entries if e.action == "error"]
    if errors:
        raise TagConfigError("Fix the errors shown in the preview before confirming.", 400, diff.to_dict())
    snap_id = snapshot(db, scope, snapshot_mode or ("Replace" if mode == "replace" else "Import"),
                       email, summary=diff.counts)
    by_line = {r.line: r for r in incoming}
    created = reactivated = deactivated = remapped = 0
    for e in sorted(diff.entries, key=lambda x: LEVEL_INDEX.get(x.level, 99)):
        if e.action == "new":
            src = by_line.get(e.line)
            v = _view_for(db, scope)
            parent = find_by_path(v, parse_parent_path(e.parent_path)) if e.parent_path else None
            create_item(db, scope, {
                "level": e.level, "name": e.name, "parent_id": parent["id"] if parent else None,
                "code": src.code if src else None, "description": src.description if src else None,
                "sort_order": src.sort_order if src else None, "status": src.status if src else "Active",
            }, email, source=source)
            created += 1
        elif e.action == "reactivate":
            obj = _obj(db, scope, e.item_id)
            obj.status, obj.modified_by = "Active", email
            reactivated += 1
        elif e.action == "deactivate":
            obj = _obj(db, scope, e.item_id)
            obj.status, obj.modified_by = "Inactive", email
            deactivated += 1
    # Optional: move children of replaced parents to their new parent.
    from ..db import models
    for old_pid, new_pid in (remap or {}).items():
        new_parent = _obj(db, scope, int(new_pid))
        for child in db.query(models.TagConfigItem).filter_by(site_key=scope, parent_id=int(old_pid)).all():
            if validate_parent(child.level, new_parent.level):
                continue
            if any(s.name_key == child.name_key for s in _siblings(db, scope, child.level, new_parent.id)):
                continue
            child.parent_id, child.modified_by = new_parent.id, email
            remapped += 1
    db.flush()
    return {"snapshot_id": snap_id, "created": created, "reactivated": reactivated,
            "deactivated": deactivated, "remapped": remapped, "diff": diff.to_dict()}


def reset_to_default(db, scope: str, email: str) -> dict:
    """Snapshot, then make every default item Active again with its seed
    name/order; custom items are set Inactive (never deleted)."""
    ensure_scope(db, scope, email)
    snap_id = snapshot(db, scope, "Reset", email)
    view = _view_for(db, scope)
    seed = flatten_tree(seed_tree())
    seed_by_path: dict[tuple, dict] = {}
    seed_view = TagView(site_key=scope, origin="builtin", rows=seed)
    for r in seed:
        seed_by_path[(r["level"], seed_view.path_of(r).lower())] = r
    matched: set[tuple] = set()
    reactivated = deactivated = created = 0
    for r in view.rows:
        key = (r["level"], view.path_of(r).lower())
        obj = _obj(db, scope, r["id"])
        if key in seed_by_path and (r["is_default"] or r["source"] == "Default"):
            s = seed_by_path[key]
            matched.add(key)
            if obj.status != "Active":
                reactivated += 1
            obj.status, obj.sort_order, obj.modified_by = "Active", s["sort_order"], email
        elif obj.status == "Active":
            obj.status, obj.modified_by = "Inactive", email
            deactivated += 1
    missing = [r for k, r in seed_by_path.items() if k not in matched]
    if missing:
        # parents that exist already are linked by path; others created in order
        for r in sorted(missing, key=lambda x: LEVEL_INDEX[x["level"]]):
            v = _view_for(db, scope)
            parent_path = seed_view.path_of(seed_view.by_id[r["parent_id"]]) if r["parent_id"] else ""
            parent = find_by_path(v, parse_parent_path(parent_path)) if parent_path else None
            from ..db import models
            db.add(models.TagConfigItem(
                site_key=scope, level=r["level"], name=r["name"], name_key=r["name_key"],
                display_name=r["display_name"], folder_name=r["folder_name"],
                parent_id=parent["id"] if parent else None, sort_order=r["sort_order"],
                status="Active", is_default=True, source="Default",
                aliases_json=json.dumps(r["aliases"]), attributes_json=json.dumps(r["attributes"]),
                created_by=email, modified_by=email))
            db.flush()
            created += 1
    db.flush()
    return {"snapshot_id": snap_id, "reactivated": reactivated, "deactivated": deactivated,
            "created": created}


def restore_snapshot(db, scope: str, snapshot_id: int | None, email: str) -> dict:
    """Roll back to a snapshot (latest when snapshot_id is None). The current
    state is snapshotted first, so a restore can itself be undone."""
    from ..db import models
    q = db.query(models.TagConfigSnapshot).filter_by(site_key=scope)
    snap = (q.filter_by(id=snapshot_id).one_or_none() if snapshot_id
            else q.filter(models.TagConfigSnapshot.mode != "Restore")
                  .order_by(models.TagConfigSnapshot.id.desc()).first())
    if snap is None:
        raise TagConfigError("No snapshot to restore.", 404)
    rows = json.loads(snap.snapshot_json or "[]")
    new_snap = snapshot(db, scope, "Restore", email, summary={"restored_snapshot": snap.id})
    restore_rows(db, scope, rows, email)
    return {"restored_snapshot_id": snap.id, "snapshot_id": new_snap, "items": len(rows)}


def export_rows(view: TagView, *, active_only: bool = False) -> list[dict]:
    """Rows in the import template shape (+ Source, Status) so an export can be re-imported.
    `active_only=True` drops Inactive/Archived rows — used by copy-from-site so a
    freshly-configured client doesn't inherit another site's disabled clutter."""
    out = []
    for r in view.items(active_only=active_only):
        parent = view.by_id.get(r["parent_id"]) if r["parent_id"] else None
        out.append({
            "Level": LEVEL_LABELS[r["level"]],
            "Parent Path": view.path_of(parent) if parent else "",
            "Name": r["name"], "Code": r.get("code") or "", "Description": r.get("description") or "",
            "Sort Order": r["sort_order"], "Status": r["status"], "Source": r["source"],
            "Folder Name": r["folder_name"],
        })
    return out


_LEVEL_FROM_LABEL = {**{v.lower(): k for k, v in LEVEL_LABELS.items()}, **{k: k for k in LEVELS},
                     "subcategory": "sub_category", "sub-category": "sub_category",
                     "mainfolder": "main_folder"}


def parse_import(records: list[dict]) -> list[IncomingRow]:
    """CSV/XLSX dict rows (template columns) → IncomingRow list."""
    out = []
    for i, rec in enumerate(records, start=2):  # line 1 = header
        norm = {str(k or "").strip().lower(): (v if v is not None else "") for k, v in rec.items()}
        if not any(str(v).strip() for v in norm.values()):
            continue
        lvl_raw = str(norm.get("level", "")).strip().lower()
        so = str(norm.get("sort order", "")).strip()
        out.append(IncomingRow(
            level=_LEVEL_FROM_LABEL.get(lvl_raw, lvl_raw),
            name=" ".join(str(norm.get("name", "")).split()),
            parent_path=str(norm.get("parent path", "")).strip(),
            code=str(norm.get("code", "")).strip() or None,
            description=str(norm.get("description", "")).strip() or None,
            sort_order=int(float(so)) if so.replace(".", "", 1).isdigit() else None,
            status=(str(norm.get("status", "")).strip().title() or "Active"),
            line=i,
        ))
    return out


# ======================================================================
# SharePoint Domain/Group/Category column sync + Term Store vessel names
# ======================================================================
DOMAIN_COLUMN_KEYS = ("domain", "department", "mainfolder", "dmsdepartment")

# Levels that have a real SharePoint column to keep in step with Active
# items. Main Folder has no column of its own (see DOMAIN_COLUMN_KEYS'
# "mainfolder" alias above — that's a legacy *name* for the Domain column on
# some sites, not the app's "Main Folder" tag level). Sub Category is never
# written to SharePoint (CLAUDE-CONTEXT.md §13), so it is deliberately absent
# here too — a site with Sub Category items configured in the app will never
# get a "no column found" sync error for it.
TAXONOMY_SYNC_LEVELS: dict[str, tuple[str, ...]] = {
    "domain": DOMAIN_COLUMN_KEYS,
    "group": ("group",),
    "category": ("category",),
}


def _col_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


async def _list_ref(drive_id: str) -> tuple[str, str]:
    from ..graph.client import graph
    lst = await graph().get(f"/drives/{drive_id}/list?$select=id,parentReference")
    site_id = (lst.get("parentReference") or {}).get("siteId") or ""
    if not site_id:
        d = await graph().get(f"/drives/{drive_id}?$select=id,sharePointIds")
        site_id = ((d.get("sharePointIds") or {}).get("siteId")) or ""
    return site_id, lst.get("id") or ""


_DOC_DRIVE_NAME_RE = re.compile(r"^(documents|shared documents)$", re.I)


async def _resolve_document_drive(site_key: str) -> tuple[str, str, str]:
    """Resolve the (drive_id, site_id, list_id) of the document library a
    given client/site actually browses and tags documents in.

    A SharePoint site can have several document libraries (drives). The
    `drive_id` configured for a site (site_configurations / .env DRIVE_ID)
    is not always the one the Documents module actually shows — the
    frontend's own site switch (VesselEmail.tsx `_switchDocumentSite`)
    prefers a drive literally named "Documents"/"Shared Documents" over the
    configured one, falling back to the configured drive, then the first
    drive on the site. Domain-column sync and the vessel Term Store lookup
    must resolve the drive the same way, per the `site_key` actually being
    worked on, or they can silently read/write the wrong library's columns
    on a site with more than one document library.
    """
    from ..config import settings
    from ..graph.client import graph
    from .site_provisioning import resolve_site_drive

    try:
        _, configured_drive_id, _ = await resolve_site_drive(site_key)
    except Exception:
        configured_drive_id = str(settings.drive_id)

    drive_id = configured_drive_id
    site_id, list_id = await _list_ref(drive_id)
    if site_id:
        try:
            page = await graph().get(f"/sites/{site_id}/drives")
            live = [d for d in page.get("value", []) if d.get("id")]
            preferred = next((d for d in live if _DOC_DRIVE_NAME_RE.match((d.get("name") or "").strip())), None)
            configured = next((d for d in live if d["id"] == configured_drive_id), None)
            chosen = preferred or configured or (live[0] if live else None)
            if chosen and chosen["id"] != drive_id:
                drive_id = chosen["id"]
                site_id, list_id = await _list_ref(drive_id)
        except Exception:
            pass  # fall back to the configured drive resolved above
    return drive_id, site_id, list_id


async def _sync_taxonomy_column(view: TagView, level: str, *, apply: bool) -> dict:
    """Keep one of the library's tag columns (Domain, Group or Category) in
    step with `view`'s Active items at that level.

    Type is detected at runtime from the column definition:
      choice → the column's choices become the Active item names
               (existing documents keep their value; SharePoint does not
               clear a value that is no longer a choice);
      term   → missing Active items are added as terms to the column's
               term set (nothing is deleted or deprecated);
      text   → nothing to sync.
    Requires the app to be allowed to edit list columns / the term set; a
    403 is reported, never raised. Guarded against NKSDocMan outside prod.

    This is a flat sync: Group and Category terms are pushed as a flat list
    at their own level, the same way Domain already was — it does not nest
    Category terms under their parent Group in the term set. A site whose
    real Term Store needs that nesting still needs it built by hand; this
    only makes sure the column has *some* matching, selectable terms instead
    of none, which is what "the tags are not shown" in SharePoint means for
    a site that was never seeded.
    """
    from ..graph.client import GraphError, graph

    if level not in TAXONOMY_SYNC_LEVELS:
        raise ValueError(f"level must be one of {tuple(TAXONOMY_SYNC_LEVELS)}")
    column_keys = TAXONOMY_SYNC_LEVELS[level]
    target = [r["display_name"] for r in view.items(level, active_only=True)]
    out: dict = {"level": level, "column": None, "type": None, "current": [], "target": target,
                 "add": [], "remove": [], "applied": False, "error": None}
    try:
        _drive_id, site_id, list_id = await _resolve_document_drive(view.site_key)
        cols = (await graph().get(f"/sites/{site_id}/lists/{list_id}/columns")).get("value", [])
        col = next((c for c in cols if _col_key(c.get("displayName")) in column_keys
                    or _col_key(c.get("name")) in column_keys), None)
        just_created = False
        if col is None:
            if not apply:
                out["error"] = f"No {LEVEL_LABELS[level]} column found in this library."
                out["add"] = target
                return out
            # The library has never had this column (e.g. a site that was set
            # up before this tag level existed, or that only got Vessel Name /
            # Group / Category wired up). Rather than erroring forever, create
            # it as a Choice column seeded with the current Active items —
            # the same column shape the "choice" branch below already knows
            # how to keep in sync, so every later add/remove of an Active
            # item for this level flows through unchanged from here on.
            try:
                display_name = LEVEL_LABELS[level]
                internal_name = re.sub(r"[^A-Za-z0-9]", "", display_name) or display_name
                col = await graph().post(
                    f"/sites/{site_id}/lists/{list_id}/columns",
                    json={
                        "name": internal_name,
                        "displayName": display_name,
                        "choice": {"choices": target, "displayAs": "dropDownMenu", "allowTextEntry": False},
                    },
                )
            except GraphError as exc:
                out["error"] = (
                    "The app is not allowed to create columns in this library (HTTP 403). "
                    f"An admin will need to add a '{LEVEL_LABELS[level]}' column by hand." if exc.status == 403
                    else f"Could not create the {LEVEL_LABELS[level]} column: {str(exc)[:300]}")
                out["add"] = target
                return out
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"Could not create the {LEVEL_LABELS[level]} column: {str(exc)[:300]}"
                out["add"] = target
                return out
            just_created = True
        out["column"] = {"id": col.get("id"), "name": col.get("name"), "displayName": col.get("displayName")}
        if "choice" in col:
            out["type"] = "choice"
            current = list((col.get("choice") or {}).get("choices") or [])
            out["current"] = current
            out["add"] = [t for t in target if t not in current]
            out["remove"] = [c for c in current if c not in target]
            if apply and (out["add"] or out["remove"]):
                choice = dict(col.get("choice") or {})
                choice["choices"] = target
                await graph().patch(f"/sites/{site_id}/lists/{list_id}/columns/{col['id']}",
                                    json={"choice": choice})
                out["applied"] = True
            if just_created:
                out["applied"] = True
        elif "term" in col:
            out["type"] = "term"
            set_id = ((col.get("term") or {}).get("termSet") or {}).get("id") or ""
            out["term_set_id"] = set_id
            if not set_id:
                out["error"] = "Term column without a term set id."
                return out
            terms = (await graph().get(f"/sites/{site_id}/termStore/sets/{set_id}/children")).get("value", [])
            current = [(t.get("labels") or [{}])[0].get("name", "") for t in terms]
            out["current"] = current
            cur_keys = {match_norm(c) for c in current}
            out["add"] = [t for t in target if match_norm(t) not in cur_keys]
            if apply and out["add"]:
                for name in out["add"]:
                    await graph().post(f"/sites/{site_id}/termStore/sets/{set_id}/children",
                                       json={"labels": [{"name": name, "languageTag": "en-US", "isDefault": True}]})
                out["applied"] = True
        else:
            out["type"] = "text"
    except GraphError as exc:
        out["error"] = (
            "The app is not allowed to change this column (HTTP 403). Nothing was changed; "
            f"an admin can update the {LEVEL_LABELS[level]} choices in the library settings, or "
            "grant the app permission to manage lists." if exc.status == 403 else str(exc)[:400])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:400]
    return out


async def taxonomy_column_sync(view: TagView, level: str, *, apply: bool) -> dict:
    """`_sync_taxonomy_column` plus, for Domains, the Term Store mirror.

    The library's Domain column is usually a Choice column, so the column
    sync never touches the Term Store. Each Domain is also its own term set in
    the "Vessel DMS" Term Store group (e.g. "Technical and Crewing"); create
    the ones that are missing. Independent of the column result (even when the
    column is missing or the update was refused) and never raises. With
    apply=False nothing is written and `term_store["add"]` lists what a sync
    would create."""
    out = await _sync_taxonomy_column(view, level, apply=apply)
    if level == "domain":
        try:
            out["term_store"] = await _ensure_domain_terms(view, out["target"], apply=apply)
        except Exception as exc:  # noqa: BLE001
            out["term_store"] = {"error": str(exc)[:300], "add": [], "added": []}
    return out


def _term_label(name: str) -> str:
    """Term Store labels cannot contain '&' (SharePoint rewrites it to a
    full-width character); the existing domains use 'and' ("Technical and
    Crewing"), so do the same."""
    return re.sub(r"\s+", " ", (name or "").replace("&", " and ")).strip()


_DMS_GROUP_NAME = "vessel dms"


def _set_names(term_set: dict) -> list[str]:
    return [n["name"] for n in (term_set.get("localizedNames") or []) if n.get("name")]


async def _ensure_domain_terms(view: TagView, target: list[str], *, apply: bool = True) -> dict:
    """Create every Active Domain that is missing as a term set of the
    "Vessel DMS" Term Store group. In that Term Store each Domain is its own
    term set ("Technical and Crewing", ...) whose terms are the Groups /
    Categories under it — Domains are NOT terms inside one shared set. Nothing
    is deleted or renamed, and names already present (loose match: '&' == 'and')
    are left alone.

    Returns {"group_id", "add", "added", "existing", "error"}: `add` is what is
    missing, `added` what was actually created (empty when apply=False)."""
    from ..graph import drive as gd
    from ..graph.client import graph

    res: dict = {"group_id": None, "add": [], "added": [], "existing": [], "error": None}
    if not target:
        return res
    _drive_id, site_id, _list_id = await _resolve_document_drive(view.site_key)
    if not site_id:
        res["error"] = "Could not resolve the SharePoint site for the Term Store."
        return res

    groups = (await graph().get(f"/sites/{site_id}/termStore/groups")).get("value", [])
    group = next((g for g in groups if (g.get("displayName") or "").strip().lower() == _DMS_GROUP_NAME), None)
    if group is None:
        res["error"] = ("No 'Vessel DMS' group was found in the Term Store. Create it in "
                        "SharePoint admin center → Content services → Term store, then sync again.")
        return res
    res["group_id"] = group["id"]

    sets = (await graph().get(f"/sites/{site_id}/termStore/groups/{group['id']}/sets")).get("value", [])
    have = {match_norm(n) for s in sets for n in _set_names(s)}
    for name in target:
        key = match_norm(name)
        if key in have:
            res["existing"].append(name)
            continue
        label = _term_label(name)
        res["add"].append(label)
        have.add(key)
        if not apply:
            continue
        try:
            await graph().post(f"/sites/{site_id}/termStore/groups/{group['id']}/sets",
                               json={"localizedNames": [{"name": label, "languageTag": "en-US"}]})
        except Exception as exc:  # noqa: BLE001 - keep what was already created
            res["error"] = f"Could not create the '{label}' term set: {str(exc)[:300]}"
            break
        res["added"].append(label)
    if res["added"]:
        gd._TERM_STORE_CACHE.pop(site_id, None)
    return res


def _sites_still_using_domain(db, deleted_scope: str, name: str) -> list[str]:
    """Other configured sites whose Tag Configuration still lists a Domain
    called `name` (any status, loose match). The Term Store is shared by the
    whole tenant, so a Domain's term set is only ours to remove when no other
    site still has that Domain — otherwise deleting it on one site would pull
    the term set out from under another."""
    from .site_provisioning import get_all_configured_sites
    key = match_norm(name)
    users: list[str] = []
    for site in get_all_configured_sites(db, include_hidden=True):
        sk = str(site["site_key"]).lower()
        if sk == deleted_scope:
            continue
        if any(match_norm(r["display_name"]) == key for r in get_view(sk, fresh=True).items("domain")):
            users.append(sk)
    return users


async def remove_domain_term_set(view: TagView, name: str, *, other_site_users: list[str]) -> dict:
    """Mirror a hard-deleted Domain by deleting its term set from the "Vessel
    DMS" Term Store group. Deliberately conservative — it only deletes when
    ALL of these hold, otherwise it leaves the set alone and says why:
      * no other configured site still has a Domain of that name (shared
        tenant-wide Term Store);
      * the set has no terms (its Groups/Categories) — a populated set may be
        tagged on documents, and deleting it would orphan those tags.
    Never raises. Returns {"deleted", "skipped", "reason", "error"}."""
    from ..graph.client import graph

    res: dict = {"deleted": None, "skipped": False, "reason": None, "error": None}
    try:
        if other_site_users:
            res.update(skipped=True, reason=f"Term set kept: still used by {', '.join(other_site_users)}.")
            return res
        _drive_id, site_id, _list_id = await _resolve_document_drive(view.site_key)
        if not site_id:
            res["error"] = "Could not resolve the SharePoint site for the Term Store."
            return res
        groups = (await graph().get(f"/sites/{site_id}/termStore/groups")).get("value", [])
        group = next((g for g in groups if (g.get("displayName") or "").strip().lower() == _DMS_GROUP_NAME), None)
        if group is None:
            return res
        sets = (await graph().get(f"/sites/{site_id}/termStore/groups/{group['id']}/sets")).get("value", [])
        key = match_norm(name)
        term_set = next((x for x in sets if any(match_norm(n) == key for n in _set_names(x))), None)
        if term_set is None:
            return res   # nothing to remove
        terms = (await graph().get(f"/sites/{site_id}/termStore/sets/{term_set['id']}/children")).get("value", [])
        if terms:
            res.update(skipped=True, reason=f"Term set kept: it still contains {len(terms)} term(s). "
                                            "Delete it in the Term Store if it is really unused.")
            return res
        await graph().delete(f"/sites/{site_id}/termStore/sets/{term_set['id']}")
        res["deleted"] = (_set_names(term_set) or [name])[0]
        from ..graph import drive as gd
        gd._TERM_STORE_CACHE.pop(site_id, None)
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)[:300]
    return res


async def domain_column_sync(view: TagView, *, apply: bool) -> dict:
    """Back-compat name for `taxonomy_column_sync(view, "domain", apply=...)`."""
    return await taxonomy_column_sync(view, "domain", apply=apply)


# ======================================================================
# Retag existing documents on rename / deactivate
# ======================================================================
# taxonomy_column_sync (above) only keeps the library COLUMN's choices/terms
# in step with the Active items — by design it never rewrites a value a
# document already has (see the module docstring: "Existing SharePoint tags
# are not rewritten"). That is the right default for a bulk import, but it
# means a straightforward rename ("Insurance244" -> "Insurance25") or a
# deactivate leaves every already-tagged file showing the old, no-longer-
# selectable value forever. retag_documents() is the explicit, heavier
# opposite: it walks every file in the site's document library and fixes up
# the ones that still hold `old_value` for this level.
#
# The internal (Graph) field name for a level's column does not always match
# its displayName (an older site can have the "Domain" column's SharePoint
# internal name as "Department"), so candidates are tried in order and the
# first one present on the item's listItem/fields wins.
FIELD_NAME_CANDIDATES: dict[str, tuple[str, ...]] = {
    "domain": ("Domain", "Department", "MainFolder", "DMS_Department"),
    "group": ("Group", "DMS_Group"),
    "category": ("Category", "DMS_Category"),
}


async def _walk_all_files(drive_id: str, root_item_id: str = "root") -> list[dict]:
    """Every file (not folder) under a drive, recursively. Best-effort: a
    folder Graph fails to list is logged and skipped, never fatal — one bad
    folder should not stop the whole retag pass."""
    from ..graph.client import graph
    out: list[dict] = []
    queue: list[str] = [root_item_id]
    seen: set[str] = set()
    while queue:
        item_id = queue.pop(0)
        if item_id in seen:
            continue
        seen.add(item_id)
        url = f"/drives/{drive_id}/items/{item_id}/children?$top=200&$select=id,name,file,folder"
        while url:
            try:
                page = await graph().get(url)
            except Exception as exc:  # noqa: BLE001
                log.warning("[tag-config] retag: could not list children of %s: %s", item_id, exc)
                break
            for it in page.get("value", []):
                if it.get("folder") is not None:
                    queue.append(it["id"])
                elif it.get("file") is not None:
                    out.append(it)
            url = page.get("@odata.nextLink")
    return out


async def retag_documents(view: TagView, level: str, old_value: str, new_value: str | None) -> dict:
    """After a Domain/Group/Category item is renamed (new_value = the new
    display name) or deactivated (new_value = None, clearing the field),
    find every file in the site's document library still tagged with
    `old_value` for this level and update it.

    Best-effort throughout: one file's Graph error is recorded in `errors`
    and does not stop the rest. Matching is by `name_key` (case/whitespace
    insensitive), same as the rest of tag_config's value resolution."""
    from ..graph.client import graph

    out: dict = {"level": level, "old_value": old_value, "new_value": new_value,
                 "checked": 0, "updated": 0, "errors": []}
    old_key = name_key(old_value)
    if not old_key:
        return out
    field_names = FIELD_NAME_CANDIDATES.get(level, (LEVEL_LABELS.get(level, level),))

    try:
        drive_id, _site_id, _list_id = await _resolve_document_drive(view.site_key)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"Could not resolve document library: {str(exc)[:200]}")
        return out

    try:
        files = await _walk_all_files(drive_id)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"Could not list documents: {str(exc)[:200]}")
        return out

    for it in files:
        item_id = it.get("id")
        if not item_id:
            continue
        out["checked"] += 1
        try:
            fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
        except Exception:  # noqa: BLE001
            continue  # not every file has a resolvable listItem (e.g. in a stale cache); skip it
        match_field = next((fn for fn in field_names if fn in fields), None)
        if not match_field or name_key(str(fields.get(match_field) or "")) != old_key:
            continue
        try:
            await graph().patch(
                f"/drives/{drive_id}/items/{item_id}/listItem/fields",
                json={match_field: new_value or ""},
            )
            out["updated"] += 1
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{it.get('name', item_id)}: {str(exc)[:150]}")
    return out


async def term_store_vessels(*, site_key: str | None = None, refresh: bool = False) -> dict:
    """Vessel Names — read-only, only from the Term Store."""
    from ..graph import drive as gd

    _drive_id, site_id, _ = await _resolve_document_drive(site_key or current_site_key())
    if refresh:
        gd._TERM_STORE_CACHE.pop(site_id, None)
    names = await gd.get_vessel_terms(site_id)
    return {"site_id": site_id, "vessel_names": names, "count": len(names),
            "manage_hint": "Vessel names are managed in the SharePoint Term Store "
                           "(Site settings → Term store management → Vessel Name)."}
