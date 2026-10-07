"""Live backend: SharePoint Embedded (Graph) + PostgreSQL + PaddleOCR.

- Provisioning walks the declarative template and creates folders via Graph
  (idempotent), caching each logical-path -> driveItem id in Postgres.
- Uploads go straight to Graph; month-driven uploads run OCR to pick the month,
  auto-create the `{Month YYYY}` folder (+ category sub-folders), and file the doc.
- Folder semantics (kind / upload / month_driven) are derived from the template
  via `classify`, so the UI renders identically to stub mode.
"""
import asyncio
import json
import random
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from sqlalchemy import func, or_ as sa_or

from .. import template
from ..config import settings, Settings, site_alias_matches
from ..db.base import SessionLocal
from ..db import models
from ..graph import drive as gd
from ..graph.guard import allow_protected_reads, protected_delete_operation
from ..graph.client import GraphError, graph
from ..ocr.dates import month_label
from ..ocr.drawing_category import classify_drawing_category
from ..ocr.extract import detect_document_month, extract_text
from .classify import classify, classify_deletion
from .errors import BadRequest, Conflict, NotFound, InternalServerError
from .normalize import normalize_vessel_name

import logging
log = logging.getLogger(__name__)

# Keyed by site_key ("all" for the merged/no-filter view) so stats for one
# SharePoint site are never served from another site's cached entry.
_DASHBOARD_STATS_CACHE: dict[str, dict] = {}
# Shared per-site scan (files + folders) behind both the dashboard counters
# and GET /api/dashboard/documents. Paging the whole library is far heavier
# than the old single 500-item page, so it is cached for 2 minutes (the Home
# page's site switcher and refresh pass force_refresh=true).
_DASHBOARD_SCAN_CACHE: dict[str, dict] = {}
_DASHBOARD_SCAN_INFLIGHT: dict[str, "asyncio.Future"] = {}
_DASHBOARD_CACHE_FILE = Path(__file__).resolve().parents[2] / ".dashboard_scan_cache.json"
# Bumped by invalidate_dashboard_stats_cache() (a site was added/hidden/
# removed). A scan that was already in flight when that happened was
# computed against the OLD site list — a generation mismatch when it
# finishes means "discard me, don't let me overwrite what invalidation
# just fixed" (see _dashboard_site_scan_uncached).
_DASHBOARD_CACHE_GENERATION = 0
# Live running totals of scans that are in progress, keyed by site_key:
# {"files": int, "folders": int}. Updated per item while a drive is being
# walked so the Home page can show a growing count IMMEDIATELY (e.g.
# "12,400+ files - counting") for a newly added 20k+ file site instead of a
# blank "Counting..." until the whole library has been read.
_DASHBOARD_LIVE_PROGRESS: dict[str, dict] = {}
# time.time() of the last completed scan in which any site was throttled
# (429/503). The scheduler's background refresh backs off for a few minutes
# after this instead of re-scanning into a still-exhausted Graph quota.
_DASHBOARD_LAST_THROTTLED_AT = 0.0
CACHE_TTL_DASHBOARD_STATS = 120  # seconds
# The dashboard must report complete library totals. Graph still paginates the
# search response, but this high guard only protects against an abnormal
# runaway response; normal libraries are scanned until Graph has no next page.
_DASHBOARD_MAX_ITEMS_PER_DRIVE = 100000


def _load_persisted_dashboard_scan() -> None:
    """Warm the in-memory dashboard cache from the last complete scan."""
    try:
        with _DASHBOARD_CACHE_FILE.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
        data = cached.get("data") if isinstance(cached, dict) else None
        timestamp = cached.get("timestamp") if isinstance(cached, dict) else None
        if isinstance(data, dict) and isinstance(data.get("sites"), list) and isinstance(data.get("docs"), list):
            for site in data["sites"]:
                site["site_name"] = _dashboard_site_label(site.get("site_key", ""), site.get("site_name"))
            _DASHBOARD_SCAN_CACHE["all"] = {"data": data, "timestamp": float(timestamp or 0)}
            log.info("dashboard cache: loaded persisted exact snapshot (%s files)", data.get("total_files", 0))
    except (OSError, ValueError, TypeError):
        pass


def _persist_dashboard_scan(data: dict, timestamp: float) -> None:
    """Persist the complete all-sites snapshot for fast post-restart loads."""
    temporary = _DASHBOARD_CACHE_FILE.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"data": data, "timestamp": timestamp}, handle, separators=(",", ":"))
        temporary.replace(_DASHBOARD_CACHE_FILE)
    except (OSError, TypeError, ValueError):
        log.warning("dashboard cache: could not persist exact snapshot", exc_info=True)


_DASHBOARD_FILE_TYPES: dict[str, tuple[str, ...]] = {
    "pdf": ("pdf",),
    "word": ("doc", "docx", "docm", "rtf", "odt"),
    "excel": ("xls", "xlsx", "xlsm", "xlsb", "csv", "ods"),
    "powerpoint": ("ppt", "pptx", "pptm", "odp"),
    "image": ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "heic", "webp"),
    "drawing": ("dwg", "dxf", "dwf", "dgn"),
    "email": ("msg", "eml"),
    "archive": ("zip", "rar", "7z"),
    "text": ("txt", "log", "xml", "json"),
}


def _dashboard_file_type(ext: str) -> str:
    """Coarse file-type group used by the Home page's Type filter."""
    low = (ext or "").lower()
    for group, exts in _DASHBOARD_FILE_TYPES.items():
        if low in exts:
            return group
    return "other"


def _normalize_term_label(value: str) -> str:
    """Case- and punctuation-insensitive key, matching the normalization
    already used when resolving Term Store labels in graph/drive.py."""
    import re
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


async def _term_store_vessel_folder_count(target: dict) -> tuple[int, list[str]]:
    """Count this SharePoint site's vessel folders: any folder whose name
    matches an entry in the site's Term Store vessel term set (managed
    metadata), searched at the drive root AND one level inside every
    non-matching root folder.

    Vessel folders don't all live in one place: "empty_pool" mode (the
    default since Part C) gives a vessel one flat folder directly under the
    drive root, while "full_template" / "adopt_*" modes and the legacy
    layout put it under <main>/<vessel name>/ (see
    templates/vessel_management.json) — e.g. "Technical & Crewing/Norse New
    Haven/". Scanning root-only (the previous behaviour) missed every
    nested vessel, which is why a fully templated site like NKSDocMan came
    back with 0 vessels. A vessel can also appear under more than one main
    folder (Technical & Crewing, Commercial & Chartering, Insurance all get
    their own "<vessel>" subfolder), so matches are deduped by the Term
    Store's official label, not counted per folder occurrence.

    Dashboard's "Total Vessels" tile must reflect real vessel folders in the
    selected site, not a static/DB number — a folder only counts as a vessel
    if its name matches the Term Store, exactly the same rule the upload/tag
    pipeline uses to recognize a vessel name (see graph/drive.py
    get_vessel_terms / _resolve_term_guid). Returns (count, matched_labels).
    """
    drive_id = target.get("drive_id")
    site_id = target.get("site_id")
    if not drive_id or not site_id:
        return 0, []

    # Admin-chosen vessel folders for this library (Site Management): every
    # sub-folder of them is a vessel; nothing else in the library is
    # considered. "none" = this site holds no vessels. Not configured = the
    # automatic Term Store match below.
    from . import vessel_roots
    roots_cfg = vessel_roots.get_for_drive(drive_id)
    if roots_cfg is not None:
        if roots_cfg["mode"] == "none":
            return 0, []
        try:
            terms = await gd.get_vessel_terms(site_id)
        except Exception:
            terms = []
        by_norm = {_normalize_term_label(t): t for t in terms}
        names: dict[str, str] = {}
        paths: dict[str, str] = {}
        for item, parent in await vessel_roots.child_folders_of_roots(graph(), drive_id, roots_cfg["paths"]):
            norm = _normalize_term_label(item["name"])
            names.setdefault(norm, by_norm.get(norm) or item["name"].strip())
            paths.setdefault(norm, f"{parent}/{item['name']}")
        vessel_roots.remember_paths(drive_id, paths)
        matched = sorted(names.values(), key=str.casefold)
        return len(matched), matched

    try:
        vessel_terms = await gd.get_vessel_terms(site_id)
    except Exception as e:
        log.debug("dashboard scan: get_vessel_terms failed for site=%s: %s", target.get("site_key"), e)
        vessel_terms = []
    if not vessel_terms:
        return 0, []
    # norm(name) -> official Term Store label, so occurrences of the same
    # vessel under different mains collapse to one label.
    term_set = {_normalize_term_label(t): t for t in vessel_terms}

    def _lookup_vessel_label(name: str) -> str | None:
        """Same-name match, plus the 'el' <-> 'ei' spelling alias that
        graph/drive.py._resolve_term_guid already applies when tagging
        uploads (e.g. folder "Maersk El Palomar" vs Term Store label
        "Maersk EI Palomar", or the reverse) — without it, a vessel whose
        folder spelling differs from its official term label never
        matches anywhere it appears, however many mains its folder is
        repeated under."""
        norm = _normalize_term_label(name)
        label = term_set.get(norm)
        if label:
            return label
        if "el" in norm:
            label = term_set.get(norm.replace("el", "ei"))
            if label:
                return label
        if "ei" in norm:
            label = term_set.get(norm.replace("ei", "el"))
            if label:
                return label
        return None

    async def _list_folders(item_id: str) -> list[dict]:
        g = graph()
        found: list[dict] = []
        url = f"/drives/{drive_id}/items/{item_id}/children?$select=id,name,folder&$top=999"
        while url:
            page = await g.get(url)
            for item in (page.get("value") or []) if isinstance(page, dict) else []:
                if isinstance(item, dict) and "folder" in item and item.get("name"):
                    found.append(item)
            url = page.get("@odata.nextLink") if isinstance(page, dict) else None
        return found

    try:
        root_folders = await _list_folders("root")
    except Exception as e:
        log.warning(
            "dashboard scan: could not list root folders for vessel count, site=%s: %s",
            target.get("site_key"), e,
        )
        return 0, []

    matched_labels: set[str] = set()
    non_vessel_roots: list[dict] = []
    for item in root_folders:
        label = _lookup_vessel_label(item["name"])
        if label:
            matched_labels.add(label)  # flat/pooled vessel folder at root
        else:
            non_vessel_roots.append(item)

    # One level under each root folder that isn't itself a vessel match —
    # covers <main>/<vessel>/ layouts. A matched root is a vessel's own
    # folder, not a "main", so its children aren't vessel folders and are
    # skipped.
    for item in non_vessel_roots:
        try:
            children = await _list_folders(item["id"])
        except Exception as e:
            log.debug(
                "dashboard scan: could not list children of '%s' for vessel count, site=%s: %s",
                item.get("name"), target.get("site_key"), e,
            )
            continue
        for child in children:
            label = _lookup_vessel_label(child["name"])
            if label:
                matched_labels.add(label)

    matched = sorted(matched_labels, key=str.casefold)
    return len(matched), matched


def _dashboard_site_label(site_key: str, site_name: str | None) -> str:
    """Use the tenant-facing name for the communication-site aliases."""
    key = (site_key or "").strip().lower()
    name = (site_name or "").strip()
    normalized = name.lower()
    if key in {"dev", "communication", "root"} and (
        not name or normalized in {"dev", "root", "communication", "communication site"}
        or "vessel dms" in normalized
    ):
        return "Communication Site"
    return name or site_key


_load_persisted_dashboard_scan()


def invalidate_dashboard_stats_cache():
    """Called whenever a site is added, hidden/unhidden, or removed in Site
    Management, so the change shows up on the Home dashboard right away.

    This used to just clear() both dashboard caches outright. That was
    correct but slow: clearing _DASHBOARD_SCAN_CACHE left nothing for the
    stale-while-revalidate path in _dashboard_site_scan to serve, so the
    *next* dashboard load had no choice but to block on a full live scan
    again (20-30s+ with a few thousand-item libraries) just to show a
    one-line site-list change — which is exactly what made a freshly added
    site look "stuck" for 20-30 seconds, and tempted hiding it while that
    scan was still running.

    Instead, reconcile the cached "all sites" site list immediately in
    place: this only needs _all_site_infos() (a DB read, no Graph calls), so
    it's instant. Sites that didn't change keep their last-known file/folder
    counts; a newly visible site is added with placeholder counts
    (stats_pending=True) the UI can render as "Counting…"; a hidden/removed
    site's row is dropped right away. The reconciled snapshot is stored as
    already-stale (timestamp 0) so the very next read still triggers a real
    background rescan to fill in the new site's actual numbers, without
    making that request wait for it.

    _DASHBOARD_CACHE_GENERATION is bumped so a scan that was already running
    when this fires (computed against the pre-change site list) knows, when
    it finishes, not to overwrite this reconciliation with its now-outdated
    result (see _dashboard_site_scan_uncached).
    """
    global _DASHBOARD_CACHE_GENERATION
    _DASHBOARD_CACHE_GENERATION += 1
    _DASHBOARD_STATS_CACHE.clear()

    cached = _DASHBOARD_SCAN_CACHE.get("all")
    if cached is not None:
        try:
            current_infos = {info["site_key"]: info for info in _all_site_infos(include_protected=True)}
            old_sites_by_key = {s["site_key"]: s for s in (cached["data"].get("sites") or [])}
            new_sites = []
            for key, info in current_infos.items():
                if key in old_sites_by_key:
                    existing = old_sites_by_key[key]
                    new_sites.append({
                        **existing,
                        "site_name": _dashboard_site_label(
                            key, info.get("sp_site_name") or existing.get("site_name")
                        ),
                        "drive_id": info.get("drive_id") or existing.get("drive_id") or "",
                        "web_url": info.get("web_url") or existing.get("web_url") or "",
                    })
                else:
                    new_sites.append({
                        "site_key": key,
                        "site_name": _dashboard_site_label(key, info.get("sp_site_name")),
                        "drive_id": info.get("drive_id") or "",
                        "web_url": info.get("web_url") or "",
                        "files": 0,
                        "folders": 0,
                        "last_modified_epoch": None,
                        "truncated": False,
                        "error": None,
                        "stats_pending": True,
                    })
            new_sites.sort(key=lambda s: (s.get("site_name") or "").lower())
            reconciled = {
                **cached["data"],
                "sites": new_sites,
                "total_files": sum(s.get("files", 0) for s in new_sites),
                "total_folders": sum(s.get("folders", 0) for s in new_sites),
            }
            _DASHBOARD_SCAN_CACHE["all"] = {"data": reconciled, "timestamp": 0.0}
            _persist_dashboard_scan(reconciled, 0.0)
        except Exception:
            log.warning("dashboard cache: reconciling site list on invalidate failed", exc_info=True)
            _DASHBOARD_SCAN_CACHE.pop("all", None)

    # Drop the in-flight reference (not the task itself — let it finish; the
    # generation check above keeps it from clobbering what we just fixed)
    # so the next request starts its own scan instead of awaiting a task
    # that no longer reflects the current site list.
    _DASHBOARD_SCAN_INFLIGHT.pop("all", None)


def mark_dashboard_stats_stale() -> None:
    """Lightweight invalidation for changes that DON'T alter the site list
    (e.g. vessel auto-sync creating Term Store entries): just mark every
    cached scan as stale so the next read triggers a background rescan.

    Unlike invalidate_dashboard_stats_cache(), this does NOT bump
    _DASHBOARD_CACHE_GENERATION and does NOT rebuild/persist the site rows.
    The Home page fires POST /api/vessels/sync-from-sharepoint on load,
    and that endpoint used to call invalidate_dashboard_stats_cache() —
    so every page load discarded the in-flight background scan (generation
    mismatch) and re-persisted the previous snapshot's rows verbatim,
    including an old "SharePoint is throttling requests" error. The error
    could never be cleared by a successful scan, because no scan result
    was ever allowed to be saved.
    """
    _DASHBOARD_STATS_CACHE.clear()
    for entry in _DASHBOARD_SCAN_CACHE.values():
        entry["timestamp"] = 0.0


def _all_site_infos(include_protected: bool = False, allow_protected: bool = False) -> list[dict]:
    """Like _all_site_drives(), plus each site's display name and URL, for
    the dashboard's per-site counts.

    Uses discover_visible_sites() (not discover_available_sites()) so a
    site an admin removed in Site Management drops out of the dashboard
    too, instead of lingering there forever (it's .env-backed, so the raw
    discover call re-derives it on every request and never "forgets" it)."""
    from ..graph import guard as _site_guard

    out: list[dict] = []
    try:
        for key, info in Settings.discover_visible_sites().items():
            drive_id = info.get("drive_id")
            if not drive_id or str(drive_id).startswith("<"):
                continue
            protected_reason = None
            if not _site_guard.is_production():
                protected_reason = _site_guard.is_protected(
                key, info.get("sp_site_name"), info.get("display_name"),
                info.get("site_name"), info.get("web_url"), drive_id, info.get("site_id"),
                )
            # Protected sites remain visible in the dashboard as metadata, but
            # are never included in the Graph scan outside production.
            if protected_reason and not include_protected:
                continue
            site_info = {
                "site_key": key.strip().lower(),
                "site_name": _dashboard_site_label(key, info.get("sp_site_name")),
                "drive_id": drive_id,
                "web_url": info.get("web_url") or "",
                "site_id": info.get("site_id") or "",
            }
            if protected_reason and not allow_protected:
                site_info["scan_blocked"] = protected_reason
            out.append(site_info)
    except Exception:
        pass
    return out


def _all_site_drives() -> list[tuple[str, str]]:
    """(site_key, drive_id) for every SharePoint site the DMS knows about —
    from .env-discovered sites and the site_configurations table, minus any
    the admin removed or hid. Used to scope dashboard stats to one site, or
    to search every site's drive when "All SharePoint Sites" is selected."""
    from ..graph import guard as _site_guard

    pairs: dict[str, str] = {}
    try:
        for key, info in Settings.discover_visible_sites().items():
            drive_id = info.get("drive_id")
            if not drive_id or str(drive_id).startswith("<"):
                continue
            if not _site_guard.is_production() and _site_guard.is_protected(
                key, info.get("sp_site_name"), info.get("display_name"),
                info.get("site_name"), info.get("web_url"), drive_id, info.get("site_id"),
            ):
                continue
            pairs[key.strip().lower()] = drive_id
    except Exception:
        pass
    return list(pairs.items())



def _get_template_node_for_path(main_folder: str, rel_path: list[str]) -> dict | None:
    """Find a node inside the SHIP_TEMPLATE hierarchy (or FLAT_TEMPLATE, for a
    flat shared main folder) matching the given relative path."""
    if main_folder in template.FLAT_MAIN_FOLDERS:
        nodes = template.FLAT_TEMPLATE[main_folder]
    elif main_folder in template.SHIP_TEMPLATE:
        nodes = template.SHIP_TEMPLATE[main_folder]
    else:
        return None

    current_node = {"kind": "folder", "children": nodes}
    
    for segment in rel_path:
        found = None
        children = current_node.get("children", [])
        for child in children:
            if child.get("name", "").lower() == segment.lower():
                found = child
                break
        if not found:
            return None
        current_node = found
        
    return current_node


def _tag_domain_match(segment: str) -> bool:
    """Is `segment` a Domain of Settings → Tag Configuration (exact name,
    any status — same rule as the previous template.ALL_MAIN_FOLDERS check)?"""
    from . import tag_config
    seg = (segment or "").strip().lower()
    return any(seg == d.lower() for d in tag_config.get_view().domain_names(active_only=False))


def sanitize_folder_name(name: str) -> str:
    # Replace slashes and backslashes with hyphens
    name = name.replace("/", "-").replace("\\", "-")
    # Replace colons with hyphens
    name = name.replace(":", "-")
    # Remove/replace other forbidden SharePoint characters
    for c in '*?"<>|':
        name = name.replace(c, "_")
    # Strip any leading/trailing spaces or dots
    name = name.strip(" .")
    return name


def _next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


class RealBackend:
    def __init__(self):
        self._drive_id = None
        self._base_ready = False
        self._sem = None
        # Prevent a click on Provision from starting a second Graph job while
        # the automatic post-create job is still running.
        self._provisioning_vessel_ids: set[int] = set()

    def _semaphore(self):
        # Bound concurrent Graph folder creation to speed up provisioning
        # without tripping SharePoint throttling.
        if self._sem is None:
            self._sem = asyncio.Semaphore(2)
        return self._sem

    # ------------------------------------------------------------- infra
    async def _drive(self) -> str:
        # SPO-only mode: always use the configured SharePoint document-library drive.
        if settings.drive_id:
            return settings.drive_id
        raise BadRequest("DRIVE_ID is required for SharePoint Online mode.")

    async def _resolve_drawing_target(self, drive_id, folder_id, path, filename, content, content_type):
        """OCR the document and match it against the Drawings sub-categories;
        fall back to "Other Drawings" (never "To be Classified") when nothing
        matches. See ocr/drawing_category.py."""
        text = await asyncio.to_thread(extract_text, content, filename, content_type or "")
        category = classify_drawing_category(text)
        target_name = category or "Other Drawings"
        target = await gd.ensure_folder(drive_id, folder_id, target_name)
        target_path = f"{path}/{target_name}"
        with SessionLocal() as db:
            self._upsert(db, target_path, target_name, "leaf", target["id"], False, None)
            db.commit()
        return target["id"], target_path

    def _upsert(self, db, path, name, kind, item_id, month_driven, vessel_id):
        row = db.query(models.Folder).filter_by(path=path).one_or_none()
        if row is None:
            # Also check by drive_item_id to avoid duplicates after renames
            row = db.query(models.Folder).filter_by(drive_item_id=item_id).one_or_none()
            if row is not None:
                # Update path to the new one if it changed
                old_path_row = db.query(models.Folder).filter_by(path=path).one_or_none()
                if old_path_row and old_path_row.id != row.id:
                    db.delete(old_path_row)
                row.path = path
            else:
                row = models.Folder(path=path)
                db.add(row)
        row.name = name
        row.kind = kind
        row.drive_item_id = item_id
        row.month_driven = month_driven
        if vessel_id is not None:
            row.vessel_id = vessel_id
        elif row.vessel_id is None and kind == "ship":
            # Auto-link: try to find a vessel whose name matches this ship folder
            vessel = db.query(models.Vessel).filter(
                func.lower(models.Vessel.name) == func.lower(name)
            ).one_or_none()
            if vessel:
                row.vessel_id = vessel.id
        return row

    def _folder_by_item(self, db, item_id):
        return db.query(models.Folder).filter_by(drive_item_id=item_id).one_or_none()

    def _classify_with_vessel_upgrade(self, db, parts: list[str], flags: dict) -> dict:
        """classify() (classify.py) only recognizes the old department
        template, so a flat (Part C) vessel root -- or anything created
        under it -- always falls through to its generic
        {"kind": "folder", "upload": False, "month_driven": False} case,
        regardless of what's actually stored for it in the DB.

        This adds two things the frontend needs, matching the guard in
        create_subfolder() exactly (month_driven OR belongs to a vessel):
        - "can_create_subfolder": true whenever "Add Folder" would actually
          be accepted here -- either the existing month_driven leaves, or
          any folder that belongs to a vessel, at any depth.
        - "upload": upgraded to true for the generic, vessel-owned case
          classify() doesn't recognize, so the upload button also appears
          for a flat vessel root and anything created under it.

        "Belongs to a vessel" is checked via Folder.vessel_id on the path's
        root segment alone, not also Folder.kind == "ship": browsing the
        Documents root re-upserts every top-level item (including vessel
        roots) via classify()'s generic "folder" kind, since classify()
        itself has no concept of a bare vessel-name root -- that re-upsert
        would flip a previously-correct kind="ship" row back to "folder"
        (see children() below), but _upsert() only ever touches vessel_id
        when it's given a non-None value or the row is kind=="ship", so an
        existing vessel_id survives that regardless of what kind ends up
        stored.
        """
        can_create_subfolder = bool(flags.get("month_driven"))
        if parts:
            root = db.query(models.Folder).filter_by(path=parts[0]).one_or_none()
            if root is not None and root.vessel_id is not None:
                can_create_subfolder = True
                if flags.get("kind") == "folder" and not flags.get("upload"):
                    flags = {**flags, "upload": True}
        return {**flags, "can_create_subfolder": can_create_subfolder}

    def _emit_folder_alert(self, db, *, drive_item_id: str | None, folder_name: str, folder_path: str,
                           parent_folder_id: str | None, vessel_name: str | None, department: str,
                           created_by_email: str, created_by_name: str, alert_type: str = "folder_created"):
        """Emit a folder creation alert for the top-header alert bell."""
        alert = models.FolderAlert(
            drive_item_id=drive_item_id,
            folder_name=folder_name,
            folder_path=folder_path,
            parent_folder_id=parent_folder_id,
            vessel_name=vessel_name,
            department=department,
            created_by_email=created_by_email,
            created_by_name=created_by_name,
            alert_type=alert_type,
        )
        db.add(alert)
        db.commit()

    async def _folder_path(self, drive_id, folder_id) -> str:
        with SessionLocal() as db:
            row = self._folder_by_item(db, folder_id)
            if row:
                return row.path
        # Fallback: derive from Graph parentReference (fetch only needed fields).
        item = await gd.get_item(drive_id, folder_id, select="id,name,parentReference")
        ref = (item.get("parentReference") or {}).get("path", "")
        rel = ref.split("root:", 1)[1].lstrip("/") if "root:" in ref else ""
        return f"{rel}/{item['name']}".strip("/") if rel else item["name"]

    # ---------------------------------------------------------- admin/activity
    def _is_admin(self, email: str | None) -> bool:
        clean = (email or "").strip().lower()
        if not clean:
            return False
        return clean in settings.admin_email_set

    def _display(self, email: str | None, name: str | None) -> str:
        if name:
            return name
        if email:
            return email.split("@")[0]
        return "A user"

    async def _resolve_department_vessel(self, folder_id: str):
        """(department, vessel_id, vessel_name, folder_name) for a folder in
        the template hierarchy, derived from its cached path + vessel_id."""
        with SessionLocal() as db:
            row = self._folder_by_item(db, folder_id)
            if row is not None:
                parts = row.path.split("/") if row.path else []
                if parts and _tag_domain_match(parts[0]):
                    department = parts[0]
                elif len(parts) >= 4 and parts[0] == template.VESSELS_ROOT and parts[1] == template.SPECIFIC_VESSELS_ROOT:
                    department = parts[3]
                elif len(parts) >= 3 and parts[0] == template.VESSELS_ROOT and parts[1] == template.COMMON_SHIPS_ROOT:
                    department = parts[2]
                else:
                    department = parts[0] if parts else "All Departments"
                vessel_name = None
                if row.vessel_id:
                    v = db.query(models.Vessel).filter_by(id=row.vessel_id).one_or_none()
                    vessel_name = v.name if v else None
                return department, row.vessel_id, vessel_name, row.name
        drive_id = await self._drive()
        path = await self._folder_path(drive_id, folder_id)
        parts = path.split("/") if path else []
        if parts and _tag_domain_match(parts[0]):
            department = parts[0]
        elif len(parts) >= 4 and parts[0] == template.VESSELS_ROOT and parts[1] == template.SPECIFIC_VESSELS_ROOT:
            department = parts[3]
        elif len(parts) >= 3 and parts[0] == template.VESSELS_ROOT and parts[1] == template.COMMON_SHIPS_ROOT:
            department = parts[2]
        else:
            department = parts[0] if parts else "All Departments"
        name = parts[-1] if parts else folder_id
        return department, None, None, name

    @staticmethod
    def _derive_dms_tags(folder_path: str, vessel_name: str | None, view=None) -> dict:
        """Derive Group, Category, SubCategory column values from a folder path.

        The DMS hierarchy is:
          {Main Folder}/{Vessel Name}/{Category}/{SubCategory}      (vessel folders)
          Common for all ships/{Main Folder}/{Category}/{SubCategory} (common folders)
          {Main Folder}/{Category}/{SubCategory}                    (flat/Kaizen)

        Returns a dict with keys matching the SharePoint internal column names:
          DMS_Group, DMS_Category, DMS_SubCategory
        These must match the actual internal names of the site columns you create
        in the SharePoint communication site or SPE container library.

        Vocabulary (domains, the Group marker folder, group names, the
        categories of each group) comes from Settings → Tag Configuration
        (services/tag_config.py). With the default seed the result is
        identical to the previous hard-coded lists (tests/test_tag_config.py
        parity test).
        """
        from . import tag_config as _tc
        view = view or _tc.get_view()
        parts = [p.strip() for p in folder_path.split("/") if p.strip()]
        group = ""
        category = ""
        sub_category = ""
        inferred_vessel = ""

        if not parts:
            return {}

        main_folders_lower = view.vessel_domain_keys()
        flat_main_folders_lower = view.flat_domain_keys()

        common_roots = {
            "common for all ships",
            "common (not ship specific)",
            "common for all vessels",
        }

        # {group display name: lower category names}, in group sort order
        # (seed: Drawings before Manuals, so shared names like "Electrical"
        # resolve to Drawings exactly as before).
        cats_by_group = view.categories_by_group()
        group_values = view.group_value_keys()
        group_markers = view.group_markers()

        def _norm(s: str) -> str:
            return (s or "").strip().lower()

        def _group_of(cat_lower: str) -> str:
            for g, cats in cats_by_group.items():
                if cat_lower in cats:
                    return g
            return ""

        def _normalize_group(label: str) -> str:
            return view.group_for_value(label) if _norm(label) in group_values else label

        # Prefer taxonomy parse under the Group marker folder ("Drawings and Manuals")
        dm_idx = next((i for i, p in enumerate(parts) if _norm(p) in group_markers), -1)
        if dm_idx >= 0:
            t1 = parts[dm_idx + 1] if len(parts) > dm_idx + 1 else ""
            t2 = parts[dm_idx + 2] if len(parts) > dm_idx + 2 else ""

            # Pattern: .../Drawings and Manuals/{Group}/{Category}
            if _norm(t1) in group_values:
                group = _normalize_group(t1)
                category = t2
            # Pattern: .../Drawings and Manuals/To be Classified
            elif _norm(t1) == "to be classified":
                group = _group_of("to be classified")
                category = "To be Classified"
            # Pattern: .../Drawings and Manuals/{Category}/{Sub-Category}
            else:
                group = _group_of(_norm(t1))
                category = t1
                sub_category = t2

        # Legacy fallback parse if "Drawings and Manuals" is not present
        elif parts[0].lower() in common_roots and len(parts) >= 2:
            if len(parts) >= 3:
                category = parts[2]
            if len(parts) >= 4:
                sub_category = parts[3]
        elif _norm(parts[0]) in main_folders_lower and len(parts) >= 2:
            v_norm = (vessel_name or "").strip().lower()
            offset = 1
            if v_norm and len(parts) > 1 and parts[1].strip().lower() == v_norm:
                offset = 2
            # If vessel_name is unavailable, infer that the 2nd segment is vessel
            # when the 3rd segment looks like a known taxonomy/category branch.
            elif not v_norm and len(parts) > 2:
                seg3 = _norm(parts[2])
                if _group_of(seg3) or seg3 in group_markers:
                    offset = 2
            if len(parts) > offset:
                category = parts[offset]
            if len(parts) > offset + 1:
                sub_category = parts[offset + 1]
            if _norm(category) == "to be classified":
                group = _group_of("to be classified")
                sub_category = sub_category or "To be Classified"
            cat_group = _group_of(_norm(category))
            if cat_group:
                group = cat_group
            if len(parts) > 1:
                inferred_vessel = parts[1]
        elif _norm(parts[0]) in flat_main_folders_lower:
            if len(parts) >= 2:
                category = parts[1]
            if len(parts) >= 3:
                sub_category = parts[2]
            cat_group = _group_of(_norm(category))
            if cat_group:
                group = cat_group

        # Fallback: use whatever we have
        else:
            group = parts[0] if parts else ""
            category = parts[1] if len(parts) > 1 else ""
            sub_category = parts[2] if len(parts) > 2 else ""

        # Final normalization guardrails:
        # - Group must be Drawings/Manuals when inferable
        # - Category must not be a main folder or vessel name
        main_folder_values = main_folders_lower | flat_main_folders_lower
        vessel_norm = _norm(vessel_name or inferred_vessel)
        if _norm(group) in main_folder_values or (vessel_norm and _norm(group) == vessel_norm):
            group = ""
        if _norm(category) in main_folder_values or (vessel_norm and _norm(category) == vessel_norm):
            category = ""

        cat1 = _norm(category)
        if not group:
            group = _group_of(cat1) or (_group_of("to be classified") if cat1 == "to be classified" else "")
        if cat1 == "to be classified" and not sub_category:
            sub_category = "To be Classified"

        tags: dict = {}
        if group:
            # Use both the canonical Term Store column names AND the DMS_ aliases
            # so update_file_columns can resolve whichever column set is provisioned.
            tags["Group"] = group
            tags["DMS_Group"] = group
        if category:
            tags["Category"] = category
            tags["DMS_Category"] = category
        if sub_category:
            tags["SubCategory"] = sub_category
            tags["DMS_SubCategory"] = sub_category
        vname = (vessel_name or inferred_vessel or "").strip()
        non_vessel = {"common for all ships", "common for all vessels"} | view._domain_keys("legacy")
        if vname and vname.lower() not in non_vessel:
            tags["VesselName"] = vname
            tags["vessel"] = vname
        return tags

    async def _tag_file_columns(
        self,
        drive_id: str,
        item_id: str,
        dest_path: str,
        vessel_name: str | None,
        filename: str | None = None,
        access_token: str | None = None,
        sp_access_token: str | None = None,
    ) -> None:
        """Derive DMS metadata tags from folder path and apply them to the uploaded
        file's SharePoint list-item column fields asynchronously.

        Falls back to AI classification (classify_document_content) when folder-path
        parsing cannot resolve a valid Group (e.g. files in Crewing, Registration, etc.)

        This is non-blocking and non-fatal — a failure here never aborts the upload.
        """
        try:
            fields = self._derive_dms_tags(dest_path, vessel_name)

            # If folder-path parsing yielded no Group, fall back to AI classifier
            # using the filename + folder path as context.
            if not fields.get("Group") and filename:
                try:
                    from .ocr.drawing_category import classify_document_content, VESSEL_MASTER_LIST
                    folder_context = dest_path.replace("/", " ")
                    classification = classify_document_content(
                        "", filename=filename, known_vessels=VESSEL_MASTER_LIST
                    )
                    raw_group = classification.get("group") or ""
                    from . import tag_config as _tc
                    _gview = _tc.get_view()
                    ai_group = (
                        _gview.group_for_value("drawings") if raw_group.lower().startswith("draw")
                        else _gview.group_for_value("manuals") if raw_group.lower().startswith("man")
                        else ""
                    )
                    # Only Active groups/categories are used for NEW tagging.
                    if ai_group and not _gview.resolve("group", ai_group, include_inactive=False):
                        ai_group = ""
                    ai_category = classification.get("category") or ""
                    ai_sub = classification.get("sub_category") or ""
                    ai_vessel = classification.get("vessel_name") or ""
                    if ai_group and ai_category:
                        fields["Group"] = ai_group
                        fields["DMS_Group"] = ai_group
                        fields["Category"] = ai_category
                        fields["DMS_Category"] = ai_category
                        if ai_sub:
                            fields["SubCategory"] = ai_sub
                            fields["DMS_SubCategory"] = ai_sub
                        if ai_vessel and not fields.get("VesselName"):
                            fields["VesselName"] = ai_vessel
                            fields["vessel"] = ai_vessel
                        log.info(
                            "_tag_file_columns: used AI classifier fallback for item_id=%s "
                            "filename=%s group=%s category=%s sub=%s vessel=%s",
                            item_id, filename, ai_group, ai_category, ai_sub, ai_vessel,
                        )
                except Exception as ai_exc:
                    log.debug(
                        "_tag_file_columns: AI classifier fallback failed for item_id=%s: %s",
                        item_id, ai_exc,
                    )

            if fields:
                await gd.update_file_columns(drive_id, item_id, fields, access_token=access_token, sp_access_token=sp_access_token)
                log.info(
                    "_tag_file_columns: tagged item_id=%s path=%s tags=%s",
                    item_id, dest_path, fields,
                )
            else:
                log.debug(
                    "_tag_file_columns: no tags derived for item_id=%s path=%s filename=%s",
                    item_id, dest_path, filename,
                )
        except Exception as exc:
            log.warning(
                "_tag_file_columns: non-fatal error tagging item_id=%s: %s",
                item_id, exc,
            )

    async def _admin_or_pending(
        self,
        *,
        action_type: str,
        requesting_email: str | None,
        requesting_name: str | None,
        department: str | None,
        vessel_id=None,
        vessel_name: str | None = None,
        target_id: str | None = None,
        target_description: str | None = None,
        payload: dict,
        changes: list[dict] | None = None,
        pending_message: str,
        activity_message: str,
        execute,
    ) -> dict:
        """Run a mutating action immediately and record it as a completed
        activity notification.

        Approvals have been removed: every user (not just SPE Admins) now
        gets `execute()` run immediately, matching how uploads and vessel
        creation already worked. `pending_message` is kept in the signature
        only so the 9 existing call sites don't need to change; it is no
        longer used.
        """
        result = await execute()
        # A completed vessel deletion has already removed the referenced
        # Vessel row. Keep the audit entry (including vessel_name), but do
        # not write its vessel_id FK after the deletion.
        activity_vessel_id = None if action_type == "delete_vessel" else vessel_id
        await self._create_activity(
            action_type=action_type,
            requesting_email=requesting_email or "",
            requesting_name=requesting_name,
            department=department,
            vessel_id=activity_vessel_id,
            vessel_name=vessel_name,
            target_id=target_id,
            target_description=target_description,
            payload=payload,
            changes=changes,
            message=activity_message,
        )
        return {"status": "completed", "message": activity_message, "result": result}

    async def _create_activity(
        self, *, action_type, requesting_email, requesting_name=None,
        department=None, vessel_id=None, vessel_name=None, target_id=None,
        target_description=None, payload=None, changes=None, message=None,
        filename=None, content_type=None, destination_folder_id=None,
        destination_path=None, is_month_upload=False, category=None,
        detected_month=None, final_path=None, size=0,
    ):
        with SessionLocal() as db:
            row = models.ApprovalRequest(
                filename=filename,
                content_type=content_type,
                size=size,
                uploaded_by_email=requesting_email,
                uploaded_by_name=requesting_name or "",
                destination_folder_id=destination_folder_id,
                destination_path=destination_path,
                is_month_upload=is_month_upload,
                category=category,
                detected_month=detected_month,
                status="completed",
                entry_kind="activity",
                action_type=action_type,
                department=department,
                vessel_id=int(vessel_id) if vessel_id else None,
                vessel_name=vessel_name,
                target_id=target_id,
                target_description=target_description,
                payload_json=json.dumps(payload or {}),
                changes_json=json.dumps(changes or []),
                message=message,
                decided_by_email=requesting_email,
                decided_at=datetime.utcnow(),
                final_path=final_path,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._approval_public(row)

    # ----------------------------------------------------------- deletion log
    def _resolve_current_site(self, db) -> tuple[str | None, str | None]:
        """(site_name, site_key) for the currently-active site — used to tag
        a deletion_log row when the delete path has no per-item site context
        of its own (folder/file deletes, unlike vessel deletes, don't carry
        provisioned_site_ids)."""
        site_key = settings.active_site or "default"
        site_cfg = db.query(models.SiteConfiguration).filter_by(site_key=site_key).one_or_none()
        site_name = (site_cfg.display_name or site_cfg.site_name) if site_cfg else (settings.active_site or "Primary Site")
        return site_name, site_key

    def _vessel_name_set(self, db) -> set[str]:
        """Term Store vessel names (VESSEL_MASTER_LIST) unioned with any live
        `Vessel` DB rows, lower-cased. Single source used by classify_deletion
        so 'vessel' classification always matches the authoritative list
        rather than a path-depth guess."""
        from ..ocr.drawing_category import VESSEL_MASTER_LIST
        names = {v.lower() for v in VESSEL_MASTER_LIST}
        names.update(v.name.lower() for v in db.query(models.Vessel.name).all())
        return names

    async def _record_deletion(
        self,
        *,
        item_type: str,  # "vessel" | "folder" | "file"
        drive_item_id: str | None,
        name: str,
        original_path: str | None,
        site_name: str | None,
        site_key: str | None,
        requesting_email: str | None,
        requesting_name: str | None,
        reason: str | None = None,
        source: str = "app",
        deleted_at: datetime | None = None,
    ) -> dict:
        """Single write path for the deletion_log table — the one source of
        truth for 'who/what/where/when/why' a deletion, read by both the
        enhanced Recycle Bin (Deleted By / Reason columns) and the live
        deletion popup (top-header alert bell).

        Called right after a successful SharePoint delete from every
        capture point: RealBackend's own delete_vessel/delete_folder/
        delete_file execution paths, the POST /api/recycle-bin/log-deletion
        endpoint (for the common client-side Graph-delete path from the
        SPFx web part), and the reconcile_native_deletions scheduler job
        (source="native_spo", for deletions made directly in SharePoint).
        """
        path_parts = [p for p in (original_path or "").replace("\\", "/").split("/") if p]
        if not path_parts:
            path_parts = [name]
        elif path_parts[-1] != name:
            path_parts.append(name)

        with SessionLocal() as db:
            vessel_names = self._vessel_name_set(db)
            info = classify_deletion(path_parts, item_type != "file", vessel_names)
            if item_type == "vessel":
                info = {"classification": "vessel", "vessel_name": name, "category": "", "sub_category": ""}

            display_name = requesting_name or (
                requesting_email.split("@")[0] if requesting_email else None
            )
            clean_reason = (reason or "").strip() or None

            existing = None
            if drive_item_id:
                existing = db.query(models.DeletionLog).filter_by(
                    drive_item_id=drive_item_id, site_key=site_key
                ).one_or_none()

            row = existing or models.DeletionLog()
            row.drive_item_id = drive_item_id
            row.item_name = name
            row.item_type = item_type
            row.classification = info["classification"]
            row.original_path = "/".join(path_parts)
            row.vessel_name = info["vessel_name"] or None
            row.category = info["category"] or None
            row.sub_category = info["sub_category"] or None
            row.site_name = site_name
            row.site_key = site_key
            row.deleted_by_email = requesting_email or row.deleted_by_email
            row.deleted_by_name = display_name or row.deleted_by_name
            row.reason = clean_reason or row.reason
            row.source = source
            if not existing:
                row.deleted_at = deleted_at or datetime.utcnow()
                db.add(row)
            db.commit()
            db.refresh(row)
            return self._deletion_log_public(row)

    @staticmethod
    def _deletion_log_public(row: "models.DeletionLog") -> dict:
        return {
            "id": row.id,
            "drive_item_id": row.drive_item_id,
            "item_name": row.item_name,
            "item_type": row.item_type,
            "classification": row.classification,
            "original_path": row.original_path,
            "vessel_name": row.vessel_name,
            "category": row.category,
            "sub_category": row.sub_category,
            "site_name": row.site_name,
            "site_key": row.site_key,
            "deleted_by_email": row.deleted_by_email,
            "deleted_by_name": row.deleted_by_name,
            "reason": row.reason,
            "source": row.source,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            "read": row.read,
        }

    async def log_deletion(
        self,
        *,
        item_type: str,
        drive_item_id: str | None,
        name: str,
        original_path: str | None,
        site_name: str | None,
        site_key: str | None,
        requesting_email: str | None,
        requesting_name: str | None,
        reason: str | None = None,
    ) -> dict:
        """Public wrapper for POST /api/recycle-bin/log-deletion — records a
        deletion that already happened client-side (e.g. a folder/file
        deleted directly against Graph from the SPFx web part rather than
        through this backend's own delete_folder/delete_file)."""
        return await self._record_deletion(
            item_type=item_type,
            drive_item_id=drive_item_id,
            name=name,
            original_path=original_path,
            site_name=site_name,
            site_key=site_key,
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            reason=reason,
            source="app",
        )

    # --------------------------------------------------------- provisioning
    async def _ensure_node(self, drive_id, parent_id, parent_path, spec, vessel_id):
        """Create a folder + its subtree via Graph. Siblings are created
        concurrently (bounded by the semaphore); each task uses its own DB
        session so concurrency is safe.
        Skips the Graph call entirely when the folder is already cached in DB."""
        name = sanitize_folder_name(spec["name"])
        path = f"{parent_path}/{name}" if parent_path else name
        kind = spec["kind"]

        # Fast path: folder already exists in DB cache — skip Graph round-trip.
        with SessionLocal() as db:
            cached = db.query(models.Folder).filter_by(path=path).one_or_none()
            cached_id = cached.drive_item_id if cached else None

        if cached_id:
            item_id = cached_id
        else:
            async with self._semaphore():
                item = await gd.ensure_folder(drive_id, parent_id, name)
            item_id = item["id"]
            with SessionLocal() as db:
                self._upsert(db, path, name, kind, item_id, kind == "month_driven", vessel_id)
                db.commit()

        # Month folders are created on upload + by the scheduler, not here.
        if kind != "month_driven":
            children = spec.get("children", [])
            await asyncio.gather(
                *(
                    self._ensure_node(drive_id, item_id, path, child, vessel_id)
                    for child in children
                )
            )

    async def _provision_subtree_batched(
        self,
        drive_id: str,
        root_id: str,
        root_path: str,
        specs: list,
        vessel_id: int,
    ) -> None:
        """Provision a vessel subtree level-by-level using Graph JSON $batch.

        Instead of one HTTP round-trip per folder (~150 calls for a full vessel),
        all sibling folders at the same depth are created in a single batch
        request (up to 20 per call).  The critical path reduces from
        depth × per-call-latency to depth × per-batch-latency — roughly 5 batch
        calls versus 150 individual calls.
        """
        # Each entry: (parent_id, parent_path, child_spec_list)
        queue: list[tuple[str, str, list]] = [(root_id, root_path, specs)]

        while queue:
            # Flatten everything at the current tree depth into a single list.
            pending: list[tuple[str, str, dict]] = []  # (parent_id, parent_path, spec)
            for parent_id, parent_path, spec_list in queue:
                for spec in spec_list:
                    pending.append((parent_id, parent_path, spec))

            # Bulk DB cache check — avoids redundant Graph calls for re-provision.
            all_paths = [f"{pp}/{sanitize_folder_name(s['name'])}" for _, pp, s in pending]
            with SessionLocal() as db:
                cached_map: dict[str, str] = {
                    row.path: row.drive_item_id
                    for row in db.query(models.Folder).filter(
                        models.Folder.path.in_(all_paths)
                    ).all()
                }

            item_id_map: dict[str, str] = dict(cached_map)
            uncached = [
                (pid, pp, spec)
                for pid, pp, spec in pending
                if f"{pp}/{sanitize_folder_name(spec['name'])}" not in cached_map
            ]

            # Batch-create all uncached folders at this level in one HTTP call.
            if uncached:
                created = await gd.batch_create_folders(
                    drive_id, [(pid, sanitize_folder_name(spec["name"])) for pid, pp, spec in uncached]
                )
                rows: list[tuple[str, str, str, str, bool]] = []
                for parent_id, parent_path, spec in uncached:
                    safe_name = sanitize_folder_name(spec["name"])
                    path = f"{parent_path}/{safe_name}"
                    item = created.get((parent_id, safe_name))
                    if item:
                        item_id_map[path] = item["id"]
                        rows.append((
                            path, safe_name, spec["kind"],
                            item["id"], spec["kind"] == "month_driven",
                        ))
                # Single DB write for the whole level.
                with SessionLocal() as db:
                    for path, name, kind, item_id, is_md in rows:
                        self._upsert(db, path, name, kind, item_id, is_md, vessel_id)
                    db.commit()

            # Queue the next depth level (month_driven children are created on upload).
            next_queue: list[tuple[str, str, list]] = []
            for parent_id, parent_path, spec in pending:
                path = f"{parent_path}/{sanitize_folder_name(spec['name'])}"
                item_id = item_id_map.get(path)
                if item_id and spec["kind"] != "month_driven":
                    children = spec.get("children", [])
                    if children:
                        next_queue.append((item_id, path, children))

            queue = next_queue
            # Brief pause between depth levels to avoid bursting the
            # container's request-unit quota (raaSContainerRU throttle).
            if queue:
                await asyncio.sleep(0.5)


    async def _ensure_month(self, db, drive_id, md_id, md_path, md_spec, year, month, vessel_id):
        """Create (or fetch) only the `{Month YYYY}` folder.

        The month's category leaves (md_spec["month_children"]) are no longer
        pre-created as a template; the caller creates just the one category
        folder the uploaded file actually goes into.
        """
        label = month_label(year, month)
        month_item = await gd.ensure_folder(drive_id, md_id, label)
        mpath = f"{md_path}/{label}"
        self._upsert(db, mpath, label, "month", month_item["id"], False, vessel_id)
        return month_item

    async def ensure_base_structure(self):
        """No-op. The app no longer auto-creates any root folders in SharePoint
        Online: no "Kaizen - Knowledge Bank", no "Technical & Crewing" /
        "Commercial & Chartering" / "Insurance" department folders, and no
        COMMON_TEMPLATE / FLAT_TEMPLATE leaf trees. Kept so existing callers
        (pool slot build, vessel create, reprovision, mains) need no changes.
        """
        self._base_ready = True

    # -------------------------------------------------------------- vessels
    async def list_vessels(self, site_key: str | None = None):
        with SessionLocal() as db:
            requested_site = (site_key or "").strip().lower() if site_key else None
            if requested_site and requested_site != "all":
                rows = db.query(models.Vessel).order_by(models.Vessel.created_at.desc().nulls_last(), models.Vessel.id.desc()).all()
                # NOTE (2026-09-25): this used to also match a vessel whose
                # provisioned_site_ids AND provisioned_site_key were BOTH
                # empty — i.e. any orphaned/legacy row with no site assigned
                # matched every single site query, so it appeared to exist
                # on every site in Vessel Management (e.g. a leftover row
                # like "Dutches Emerald" with no site ever set). Every
                # current vessel-creation path (create_vessel, reprovision,
                # pool-slot claim — see provisioned_site_ids= / _key=
                # assignments elsewhere in this file) always sets one of
                # these, so a genuinely unscoped row is stale data, not a
                # normal state. Such rows still surface under the unscoped
                # "all" view below so they aren't lost — an admin can find
                # and fix/delete them there — they just no longer leak into
                # every individual site's list.
                rows = [
                    vessel for vessel in rows
                    if (
                        any(
                            site_alias_matches(str(site).strip(), requested_site)
                            for site in (vessel.provisioned_site_ids or [])
                        )
                    ) or (
                        vessel.provisioned_site_key and
                        site_alias_matches(str(vessel.provisioned_site_key).strip(), requested_site)
                    )
                ]
            else:
                rows = db.query(models.Vessel).order_by(models.Vessel.created_at.desc().nulls_last(), models.Vessel.id.desc()).all()

            vessels = [
                {
                    "id": str(v.id),
                    "name": v.name,
                    "imo": v.imo,
                    "shipyard": v.shipyard,
                    "hull_number": v.hull_number,
                    "vessel_type": v.vessel_type,
                    "is_provisioned": v.is_provisioned,
                    "provisioned_site_ids": v.provisioned_site_ids or [],
                    "provisioned_site_key": v.provisioned_site_key,
                    "vessel_folder_path": v.vessel_folder_path,
                    "restored_at": v.restored_at.isoformat() if v.restored_at else None,
                    "status": "Active",
                    "source": "dms",
                }
                for v in rows
            ]

            # Backfill "Created Path" for vessels whose vessel_folder_path was
            # never set at creation time (e.g. vessels created via the
            # pool-slot fast path before this fix — see _link_claimed_slot).
            # models.Folder (kind="ship") is the authoritative path source;
            # this read-time backfill fixes already-existing vessels without
            # a migration or DB write.
            missing_ids = [int(v["id"]) for v in vessels if not v["vessel_folder_path"]]
            if missing_ids:
                folder_rows = (
                    db.query(models.Folder)
                    .filter(models.Folder.kind == "ship", models.Folder.vessel_id.in_(missing_ids))
                    .all()
                )
                # A vessel could (rarely) have ship-folder rows on more than
                # one site; prefer the row on the site the caller asked for.
                target_drive_id = None
                if requested_site:
                    try:
                        target_drive_id = Settings.load_site_config(requested_site).drive_id
                    except Exception:
                        target_drive_id = None
                paths_by_vessel: dict[int, str] = {}
                for f in folder_rows:
                    if f.vessel_id is None or not f.path:
                        continue
                    if f.vessel_id not in paths_by_vessel or (target_drive_id and f.site_id == target_drive_id):
                        paths_by_vessel[f.vessel_id] = f.path
                if paths_by_vessel:
                    for v in vessels:
                        path = paths_by_vessel.get(int(v["id"]))
                        if path:
                            v["vessel_folder_path"] = path

        # Merge in vessels that exist as real folders in SharePoint (their
        # folder name matches the site's Term Store vessel set — the same
        # rule the dashboard's "Total Vessels" tile uses, see
        # _term_store_vessel_folder_count) but have no Vessel DB row yet,
        # e.g. a vessel folder created outside this app or bulk-imported.
        # Scoped to the same site_key the caller asked for, so "Manage
        # vessels" for a given SharePoint site reflects what's actually
        # there, not only what was provisioned through the app.
        known_names = {_normalize_term_label(v["name"]) for v in vessels}
        from . import vessel_roots as _vessel_roots

        def _sharepoint_row(site_key_for_row, name: str, norm: str, path: str) -> dict:
            # View-only card for a vessel folder that isn't registered for
            # this site (the Vessels page only views its documents).
            return {
                "id": f"sp:{site_key_for_row}:{norm}",
                "name": name,
                "imo": None, "shipyard": None, "hull_number": None, "vessel_type": None,
                "is_provisioned": False,
                "provisioned_site_ids": [],
                "provisioned_site_key": site_key_for_row,
                "vessel_folder_path": path,
                "restored_at": None,
                "status": "Found in SharePoint",
                "source": "sharepoint",
            }

        # A single site with admin-chosen vessel folders: list them live
        # (a few Graph calls), so a new choice shows straight away instead of
        # after the next full site scan.
        if site_key and site_key != "all":
            try:
                _drive = Settings.load_site_config(site_key).drive_id
                _roots = _vessel_roots.get_for_drive(_drive)
            except Exception:
                _roots = None
            if _roots is not None:
                added = 0
                if _roots["mode"] == "folders":
                    for item, parent in await _vessel_roots.child_folders_of_roots(graph(), _drive, _roots["paths"]):
                        norm = _normalize_term_label(item["name"])
                        if norm in known_names:
                            continue
                        known_names.add(norm)
                        added += 1
                        vessels.append(_sharepoint_row(site_key, item["name"], norm, f"{parent}/{item['name']}"))
                log.info("list_vessels: site_key=%r -> %d vessel folder(s) from the chosen folders", site_key, added)
                return vessels
        try:
            dash = await self.get_dashboard_stats(site_key=site_key)
        except Exception as e:
            log.warning("list_vessels: get_dashboard_stats(site_key=%r) raised — SharePoint vessel merge skipped: %s", site_key, e, exc_info=True)
            dash = None
        added = 0
        if dash:
            sites_in_dash = dash.get("sites") or []
            log.info(
                "list_vessels: site_key=%r -> %d DB vessel(s), dashboard scan returned %d site(s): %s",
                site_key, len(vessels), len(sites_in_dash),
                [(s.get("site_key"), s.get("vessels"), len(s.get("vessel_names") or []), s.get("error")) for s in sites_in_dash],
            )
            for site in sites_in_dash:
                site_key_for_row = site.get("site_key")
                site_drive = site.get("drive_id")
                for name in site.get("vessel_names") or []:
                    norm = _normalize_term_label(name)
                    if norm in known_names:
                        continue
                    known_names.add(norm)
                    added += 1
                    # The real folder path when it came from the site's chosen
                    # vessel folders; else just the name (the frontend then
                    # finds it at the root or one level down).
                    vessels.append(_sharepoint_row(
                        site_key_for_row, name, norm, _vessel_roots.discovered_path(site_drive, norm) or name,
                    ))
            log.info("list_vessels: site_key=%r -> added %d SharePoint-only vessel(s), %d total returned", site_key, added, len(vessels))
        else:
            log.info("list_vessels: site_key=%r -> dashboard scan unavailable, returning %d DB vessel(s) only", site_key, len(vessels))

        return vessels

    # Characters that SharePoint / OneDrive forbid in folder names.
    _ILLEGAL_NAME_CHARS = set('/\\:*?"<>|')

    def _validate_vessel_input(self, name, imo, exclude_vessel_id=None):
        name = (name or "").strip()
        name = sanitize_folder_name(name)
        imo = (imo or "").strip()
        if not name:
            raise BadRequest("Vessel name is required")
        if not imo or imo in ("—", "None", "null", "auto", "0000000"):
            import random as _rand
            imo = str(_rand.randint(1000000, 9999999))
        elif not imo.isdigit() or len(imo) != 7:
            raise BadRequest("IMO number must be exactly 7 digits")

        normalized_name = normalize_vessel_name(name)
        with SessionLocal() as db:
            q = db.query(models.Vessel).filter(
                func.lower(
                    func.replace(
                        func.replace(
                            func.replace(
                                func.replace(models.Vessel.name, ' ', ''),
                                '_', ''
                            ),
                            "'", ''
                        ),
                        '"', ''
                    )
                ) == normalized_name
            )
            if exclude_vessel_id:
                q = q.filter(models.Vessel.id != int(exclude_vessel_id))
            if q.first():
                raise Conflict("Vessel name already exists.")
            if imo and imo not in ("0000000", "—", ""):
                imo_q = db.query(models.Vessel).filter_by(imo=imo)
                if exclude_vessel_id:
                    imo_q = imo_q.filter(models.Vessel.id != int(exclude_vessel_id))
                if imo_q.first():
                    raise Conflict("A vessel with that IMO number already exists")

        return name, imo

    async def create_vessel(
        self, name, imo, shipyard=None, hull_number=None, vessel_type=None,
        requesting_email=None, requesting_name=None,
        provisioned_site_ids: list[str] | None = None,
        site_key: str | None = None,
        parent_folder_path: str | None = None,
        subfolders: list[str] | None = None,
    ):
        """Creating a vessel never requires approval — for anyone, admin or
        not. It always executes immediately and is always recorded as a
        completed activity entry for audit purposes.

        Uses Strategy A Truth Table:
        1. No selection -> pool claim on default/active drive (Fast Path)
        2. 1 site == default/active site -> pool claim on default drive (Fast Path)
        3. 1 site != default/active site -> bypass pool (fast_db_async + background provisioning)
        4. 2+ sites -> bypass pool (fast_db_async + background provisioning across drives)
        """
        import time as _time
        _t_total = _time.monotonic()

        clean_name, clean_imo = self._validate_vessel_input(name, imo)
        payload = {
            "name": clean_name, "imo": clean_imo, "shipyard": shipyard,
            "hull_number": hull_number, "vessel_type": vessel_type,
        }
        display = self._display(requesting_email, requesting_name)
        creation_method = "unknown"

        active_site = (settings.active_site or "dev").lower()
        selected_site = (site_key or "").strip().lower()
        custom_location = bool(selected_site)
        if custom_location and parent_folder_path is None:
            raise BadRequest("parent_folder_path is required when site_key is provided")
        if parent_folder_path is not None:
            parent_folder_path = (parent_folder_path or "").strip()

        # A vessel's folder must never be created inside another vessel's
        # folder (e.g. picking an existing vessel while browsing the parent-
        # folder picker) -- this is what produced "Peissy2/Peissy3": a real
        # vessel folder nested one level under another vessel's folder. That
        # breaks the flat one-root-folder-per-vessel invariant (CLAUDE-CONTEXT
        # §2) and makes the nested vessel invisible to vessel_sync.py's
        # root-folder scan (list_root_vessel_candidates only walks one level
        # into a fixed non-vessel-name allowlist, never into another vessel's
        # folder) -- so it would also never get pushed to the Term Store by
        # sync_vessels_from_sharepoint or the on-create push below. Reject it
        # up front instead: no DB row, no SharePoint folder, no Term Store
        # entry gets created for an invalid nested vessel.
        if custom_location and parent_folder_path:
            norm_parent = parent_folder_path.strip("/").lower()
            with SessionLocal() as db:
                other_vessels = (
                    db.query(models.Vessel)
                    .filter(models.Vessel.vessel_folder_path.isnot(None))
                    .all()
                )
                other_vessels = [(v.name, v.vessel_folder_path, list(v.provisioned_site_ids or [])) for v in other_vessels]
            for other_name, other_folder_path, other_sites in other_vessels:
                if selected_site not in [s.lower() for s in other_sites]:
                    continue
                other_norm = (other_folder_path or "").strip("/").lower()
                if not other_norm:
                    continue
                if norm_parent == other_norm or norm_parent.startswith(other_norm + "/"):
                    raise Conflict(
                        f"Cannot create vessel '{clean_name}' inside '{other_name}' folder "
                        f"('{parent_folder_path}'). A vessel's folder may not be nested "
                        "inside another vessel's folder — pick a different parent folder."
                    )

        target_sites = [s.strip().lower() for s in (provisioned_site_ids or []) if s.strip()]
        if custom_location:
            target_sites = [selected_site]

        # Strategy A Decision:
        should_claim_pool = not custom_location and (
            len(target_sites) == 0 or
            (len(target_sites) == 1 and target_sites[0] == active_site)
        )

        slot = self._claim_pool_slot() if should_claim_pool else None
        vessel = None

        if slot is not None:
            _t0 = _time.monotonic()
            log.info(
                "[create_vessel] Pool slot %d claimed for '%s' (slug=%s) — linking now",
                slot["slot_id"], clean_name, slot.get("slug", "?"),
            )
            try:
                vessel = await asyncio.wait_for(self._link_claimed_slot(slot, payload), timeout=1.5)
                _link_elapsed = _time.monotonic() - _t0
                log.info(
                    "[create_vessel] _link_claimed_slot succeeded in %.2fs for '%s'",
                    _link_elapsed, clean_name,
                )
                creation_method = "pool"
            except (Exception, asyncio.TimeoutError, BaseException) as link_err:
                _link_elapsed = _time.monotonic() - _t0
                log.warning(
                    "[create_vessel] _link_claimed_slot FAILED/TIMED OUT after %.2fs for '%s': %s — "
                    "releasing slot %d and falling back to fast DB creation",
                    _link_elapsed, clean_name, link_err, slot["slot_id"],
                )
                self._release_pool_slot(slot["slot_id"])
                vessel = None

        if vessel is None:
            log.info("[create_vessel] Creating vessel DB record immediately for '%s' (Strategy A)", clean_name)
            creation_method = "fast_db_async"
            final_target_sites = target_sites if target_sites else [active_site]
            with SessionLocal() as db:
                v_db = models.Vessel(
                    name=clean_name,
                    imo=clean_imo,
                    shipyard=shipyard,
                    hull_number=hull_number,
                    vessel_type=vessel_type,
                    is_provisioned=False,
                    provisioned_site_ids=final_target_sites,
                    provisioned_site_key=selected_site or None,
                    vessel_folder_path=None,
                )
                db.add(v_db)
                db.commit()
                db.refresh(v_db)
                vessel_id_num = v_db.id

            vessel = {
                "id": str(vessel_id_num),
                "name": clean_name,
                "imo": clean_imo,
                "shipyard": shipyard,
                "hull_number": hull_number,
                "vessel_type": vessel_type,
                "is_provisioned": False,
                "provisioned_site_ids": final_target_sites,
            }

            valid_custom_site = bool(selected_site and parent_folder_path is not None)
            if valid_custom_site:
                try:
                    Settings.load_site_config(selected_site)
                except Exception:
                    valid_custom_site = False

            if valid_custom_site:
                from . import site_provisioning
                try:
                    folder_result = await site_provisioning.create_vessel_at_path(
                        site_key=selected_site,
                        parent_path=parent_folder_path,
                        vessel_name=clean_name,
                        subfolders=subfolders,
                    )
                    if not isinstance(folder_result, dict):
                        raise TypeError("SharePoint folder creation returned an invalid result")
                    vessel_folder_path = folder_result.get("vessel_folder_path")
                    with SessionLocal() as db:
                        v_db = db.query(models.Vessel).filter_by(id=vessel_id_num).one_or_none()
                        if v_db is not None:
                            v_db.vessel_folder_path = vessel_folder_path
                            v_db.is_provisioned = True
                            if selected_site:
                                existing_sites = list(v_db.provisioned_site_ids or [])
                                if selected_site not in existing_sites:
                                    existing_sites.append(selected_site)
                                v_db.provisioned_site_ids = existing_sites
                                v_db.provisioned_site_key = selected_site
                            db.commit()
                    vessel.update({
                        "is_provisioned": True,
                        "provisioned_site_key": selected_site or None,
                        "vessel_folder_path": vessel_folder_path,
                        "folder_template": folder_result.get("template"),
                        "provisioned_site_ids": list(dict.fromkeys(final_target_sites + ([selected_site] if selected_site else []))),
                    })
                    # The vessel folder now exists in SharePoint, but the
                    # Documents module's live folder tree for this site
                    # (GET /api/sites/.../folders/root/recursive) caches the
                    # whole drive for 10 minutes — drop that site's cached
                    # entries now so the new vessel folder shows up on the
                    # very next Documents load instead of after the TTL.
                    try:
                        from ..main import invalidate_folder_caches
                        invalidate_folder_caches(drive_id=folder_result.get("drive_id"))
                    except Exception:
                        log.debug("Could not invalidate recursive-tree cache after creating vessel '%s'", clean_name, exc_info=True)
                except Exception:
                    log.exception("[create_vessel] Custom-site folder creation failed for '%s' on site '%s' parent='%s'", clean_name, selected_site, parent_folder_path)
                    raise
            else:
                vessel.update({
                    "is_provisioned": False,
                    "provisioned_site_key": selected_site or None,
                    "vessel_folder_path": None,
                    "provisioned_site_ids": final_target_sites,
                })

        # Settings → Vessel Settings → Folder Structure Mode. Records the
        # default mode on the new vessel; Mode 1 (default, "empty_pool") does
        # nothing else, so creation behaves exactly as before. Modes 2-4 run
        # in the background on the active site's drive and never delay this
        # response. Vessels created at a custom site/path are left alone.
        if creation_method == "pool" and vessel.get("id"):
            # A claimed pool slot is just a renamed empty folder — add the
            # vessel folder template's sub-folders in the background.
            from . import vessel_folder_template
            asyncio.create_task(
                vessel_folder_template.ensure_for_vessel(int(vessel["id"])),
                name=f"vessel_folder_template_{clean_name}",
            )

        if not custom_location and vessel.get("id"):
            from . import folder_structure
            asyncio.create_task(
                folder_structure.apply_on_vessel_create(
                    self, str(vessel["id"]), clean_name,
                    requesting_email or "", requesting_name or "",
                ),
                name=f"folder_structure_on_create_{clean_name}",
            )

        # Push this vessel's name into the SharePoint Term Store now, instead
        # of relying solely on the lazy create-on-first-tagged-upload path or
        # the throttled auto-sync (_maybeAutoSyncVesselsFromSharePoint ->
        # POST /api/vessels/sync-from-sharepoint), which only rediscovers a
        # vessel whose SharePoint folder sits at the drive root (or one level
        # inside a known non-vessel wrapper) -- see vessel_sync.py. Mirrors
        # the same best-effort call confirm_discovered_vessel() already makes.
        if vessel.get("id"):
            site_key_for_term = selected_site or active_site

            async def _push_vessel_term(vessel_name: str, site_key: str) -> None:
                try:
                    with allow_protected_reads():
                        site_info = next(
                            (i for i in _all_site_infos() if i["site_key"] == site_key), None
                        )
                    if site_info and site_info.get("site_id"):
                        await gd.ensure_vessel_term(site_info["site_id"], vessel_name)
                    else:
                        log.warning(
                            "[create_vessel] No site_id resolved for site_key='%s'; "
                            "Term Store entry for '%s' not created",
                            site_key, vessel_name,
                        )
                except Exception:
                    log.warning(
                        "[create_vessel] Term Store sync failed for '%s'", vessel_name, exc_info=True,
                    )

            asyncio.create_task(
                _push_vessel_term(clean_name, site_key_for_term),
                name=f"vessel_term_on_create_{clean_name}",
            )

        activity_message = (
            f"{display} ({requesting_email}) created vessel '{clean_name}'. No approval was required."
        )

        total_elapsed = _time.monotonic() - _t_total
        vessel_id = vessel.get("id", "?")
        with SessionLocal() as _snap_db:
            _available = _snap_db.query(models.PoolSlot).filter_by(status="available").count()
            _building  = _snap_db.query(models.PoolSlot).filter_by(status="building").count()
            _claimed   = _snap_db.query(models.PoolSlot).filter_by(status="claimed").count()
            _failed    = _snap_db.query(models.PoolSlot).filter_by(status="failed").count()
            _total     = _snap_db.query(models.PoolSlot).count()
        log.info(
            "[create_vessel] ✓ Vessel created: id=%s name='%s' imo=%s method=%s "
            "elapsed=%.2fs | pool_snapshot available=%d building=%d claimed=%d failed=%d total=%d",
            vessel_id, clean_name, clean_imo, creation_method, total_elapsed,
            _available, _building, _claimed, _failed, _total,
        )

        # Fire-and-forget activity log — never block the HTTP response
        asyncio.create_task(
            self._create_activity(
                action_type="create_vessel",
                requesting_email=requesting_email or "",
                requesting_name=requesting_name,
                department="All Departments",
                target_description=clean_name,
                payload=payload,
                message=activity_message,
            ),
            name=f"activity_create_vessel_{clean_name}"
        )
        return {"status": "completed", "message": activity_message, "result": vessel,
                "id": vessel.get("id"), "name": vessel.get("name"),
                "imo": vessel.get("imo"), "shipyard": vessel.get("shipyard"),
                "hull_number": vessel.get("hull_number"), "vessel_type": vessel.get("vessel_type")}

    def _claim_pool_slot(self) -> dict | None:
        """Atomically claim one available pool slot, or None if the pool is
        empty. Locks the PoolSlot row itself via SELECT...FOR UPDATE SKIP
        LOCKED — NOT a plain SELECT followed by an UPDATE — so two
        concurrent claims can never grab the same slot: the second
        claimer's query simply skips a slot row already locked by the
        first, instead of blocking or racing on a separate read-then-write.
        Locking the single PoolSlot row (rather than its several Folder
        rows individually) is also what guarantees a slot's whole set of
        ship folders — one per main department — moves as one atomic unit.
        """
        with SessionLocal() as db:
            pool_slot = (
                db.query(models.PoolSlot)
                .filter_by(status="available")
                .order_by(models.PoolSlot.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if pool_slot is None:
                total_available = db.query(models.PoolSlot).filter_by(status="available").count()
                log.warning(
                    "[pool] Claim attempted but POOL IS EMPTY (available=%d) — "
                    "falling back to full provisioning.",
                    total_available,
                )
                return None
            pool_slot.status = "claimed"
            db.commit()
            # Full pool state snapshot after the claim is committed
            remaining   = db.query(models.PoolSlot).filter_by(status="available").count()
            building    = db.query(models.PoolSlot).filter_by(status="building").count()
            still_claimed = db.query(models.PoolSlot).filter_by(status="claimed").count()
            failed      = db.query(models.PoolSlot).filter_by(status="failed").count()
            total       = db.query(models.PoolSlot).count()
            log.info(
                "[pool] Slot claimed: slot_id=%d slug=%s | "
                "pool_snapshot available=%d building=%d claimed=%d failed=%d total=%d",
                pool_slot.id, pool_slot.slug,
                remaining, building, still_claimed, failed, total,
            )
            return {"slot_id": pool_slot.id, "slug": pool_slot.slug}

    def _release_pool_slot(self, slot_id: int) -> None:
        """Put a slot back to 'available' after a failed claim-and-rename
        attempt, so it isn't stranded in 'claimed' with nothing linked."""
        with SessionLocal() as db:
            pool_slot = db.query(models.PoolSlot).filter_by(id=slot_id).one_or_none()
            if pool_slot is not None:
                pool_slot.status = "available"
                db.commit()
                available_now = db.query(models.PoolSlot).filter_by(status="available").count()
                log.info(
                    "[pool] Slot released back to available: slot_id=%d slug=%s — "
                    "%d slot(s) now available (released after link failure)",
                    slot_id, pool_slot.slug, available_now,
                )
            else:
                log.warning("[pool] _release_pool_slot: slot_id=%d not found in DB — nothing to release", slot_id)

    async def _link_claimed_slot(self, slot: dict, payload: dict) -> dict:
        """Rename a claimed pool slot's single root folder to the real
        vessel name, create the Vessel row, and point that one folder at
        the vessel — the fast path. Raises on any failure so create_vessel
        can release the slot and fall back to full provisioning rather
        than leave a half-linked vessel behind.

        Part C (2026-09-21): a pool slot is now exactly one flat root
        folder (see _build_pool_slot) — there is no subtree under it to
        rewrite, so this only ever touches the single Folder row for that
        root.
        """
        name, imo = payload["name"], payload["imo"]
        drive_id = await self._drive()
        import time as _time

        # Extract all needed data as plain Python objects BEFORE the session
        # closes and expires the ORM instances.  _rename_ship_folders accesses
        # folder.drive_item_id and folder.path — both would raise
        # DetachedInstanceError on expired objects if read after session exit.
        _t1 = _time.monotonic()
        with SessionLocal() as db:
            ship_rows = (
                db.query(models.Folder)
                .filter_by(pool_slot_id=slot["slot_id"], kind="ship")
                .all()
            )
            if not ship_rows:
                raise BadRequest(f"Pool slot {slot['slot_id']} has no ship folders")
            placeholder_name = ship_rows[0].name
            ship_data = [
                {"drive_item_id": f.drive_item_id, "path": f.path, "name": f.name}
                for f in ship_rows
            ]
        log.info("[_link_claimed_slot] DB read ship rows: %.3fs", _time.monotonic() - _t1)

        # Build lightweight proxy objects with only the attributes
        # _rename_ship_folders reads (.drive_item_id, .path, .name).
        class _FolderProxy:
            __slots__ = ("drive_item_id", "path", "name")
            def __init__(self, d):
                self.drive_item_id = d["drive_item_id"]
                self.path = d["path"]
                self.name = d["name"]

        ship_proxies = [_FolderProxy(d) for d in ship_data]

        _t2 = _time.monotonic()
        rename_results = await self._rename_ship_folders(
            drive_id, [(f, name) for f in ship_proxies]
        )
        log.info("[_link_claimed_slot] rename_ship_folders: %.3fs", _time.monotonic() - _t2)
        failed = [r for r in rename_results if not r[1]]
        if failed:
            raise BadRequest(
                f"Failed to rename {len(failed)} pool folder(s) for '{name}': {failed[0][2]}"
            )

        _t3 = _time.monotonic()
        active_site = (settings.active_site or "dev").lower()
        with SessionLocal() as db:
            vessel = models.Vessel(
                name=name, imo=imo, shipyard=payload.get("shipyard"),
                hull_number=payload.get("hull_number"), vessel_type=payload.get("vessel_type"),
                is_provisioned=True,
                provisioned_site_ids=[active_site],
                # Mirror the Folder row's own .path (set to `name` below, since
                # this is the flat pool-slot layout — no "Technical & Crewing/"
                # prefix here) so "Created Path" isn't blank on the vessel card.
                vessel_folder_path=name,
            )
            db.add(vessel)
            db.flush()
            vessel_id, vname, vimo = vessel.id, vessel.name, vessel.imo
            vshipyard, vhull, vtype = vessel.shipyard, vessel.hull_number, vessel.vessel_type

            # Single flat root folder — no subtree under it, so just this
            # one row's path/name/vessel_id needs updating (was previously
            # a loop rewriting an entire subtree per main folder).
            for ship_info in ship_data:
                row = db.query(models.Folder).filter_by(path=ship_info["path"]).one_or_none()
                if row is not None:
                    row.vessel_id = vessel_id
                    row.name = name
                    row.path = name
            db.commit()
        log.info("[_link_claimed_slot] DB vessel+path rewrite: %.3fs", _time.monotonic() - _t3)

        return {
            "id": str(vessel_id), "name": vname, "imo": vimo,
            "shipyard": vshipyard, "hull_number": vhull, "vessel_type": vtype,
            "is_provisioned": True,
            "provisioned_site_ids": [active_site],
        }


    async def _build_pool_slot(self) -> int:
        """Build one new pool slot from scratch: a single empty placeholder
        root folder at the drive root (never linked to a vessel until
        claimed).

        Part C (2026-09-21): vessel creation no longer auto-provisions a
        MAIN_FOLDERS/department subtree, and a vessel is no longer nested
        under any main department -- it is one flat root folder with no
        automatic internal structure at all. Any subfolder structure a
        vessel needs is created manually afterward (Phase 3's folder
        creation flow), not auto-built here. This replaces the previous
        per-main-folder ship-root + full SHIP_TEMPLATE subtree build.
        Returns the new PoolSlot's id.
        """
        slug = f"Pool-{uuid.uuid4().hex[:12]}"
        with SessionLocal() as db:
            pool_slot = models.PoolSlot(slug=slug, status="building")
            db.add(pool_slot)
            db.commit()
            db.refresh(pool_slot)
            slot_id = pool_slot.id

        await self.ensure_base_structure()
        drive_id = await self._drive()
        created_root_id: str | None = None

        try:
            root = await gd.get_root_item_id(drive_id)
            ship = await gd.ensure_folder(drive_id, root, slug)
            created_root_id = ship["id"]
            with SessionLocal() as db:
                row = self._upsert(db, slug, slug, "ship", ship["id"], False, None)
                row.pool_slot_id = slot_id
                db.commit()
        except Exception:
            # A completed build failure is no longer an active build. Mark it
            # failed immediately so reconcile_pool does not count it as capacity.
            with SessionLocal() as db:
                failed_slot = db.query(models.PoolSlot).filter_by(id=slot_id).one_or_none()
                if failed_slot is not None and failed_slot.status == "building":
                    failed_slot.status = "failed"
                    db.commit()
            # Best-effort cleanup of a partially built slot. Keep the PoolSlot
            # row as failed rather than deleting it so the attempt remains
            # auditable and the partially created Folder row can be cleaned
            # up safely.
            if created_root_id is not None:
                try:
                    await gd.delete_item(drive_id, created_root_id)
                except Exception:
                    pass
                with SessionLocal() as db:
                    rows = db.query(models.Folder).filter_by(path=slug).all()
                    for row in rows:
                        db.delete(row)
                    db.commit()
            raise

        with SessionLocal() as db:
            pool_slot = db.query(models.PoolSlot).filter_by(id=slot_id).one()
            pool_slot.status = "available"
            db.commit()
            # Full pool state snapshot now that this slot is marked available
            available_now = db.query(models.PoolSlot).filter_by(status="available").count()
            building_now  = db.query(models.PoolSlot).filter_by(status="building").count()
            claimed_now   = db.query(models.PoolSlot).filter_by(status="claimed").count()
            failed_now    = db.query(models.PoolSlot).filter_by(status="failed").count()
            total_now     = db.query(models.PoolSlot).count()
        log.info(
            "[pool] ✓ Slot filled: slot_id=%d slug=%s marked available | "
            "pool_snapshot available=%d building=%d claimed=%d failed=%d total=%d",
            slot_id, slug,
            available_now, building_now, claimed_now, failed_now, total_now,
        )
        return slot_id

    async def _replenish_one_slot(self, triggering_slot_id: int) -> None:
        """Fire-and-forget: build exactly one replacement pool slot after
        `triggering_slot_id` was claimed. Never awaited by create_vessel —
        must not add to that request's response time. Tracked via a
        ReplenishJob row (written before the build starts) so a process
        restart mid-build leaves a visible 'pending' row the scheduler's
        reconciliation check can find and retry, instead of the work
        silently vanishing with the in-memory asyncio task.
        """
        # Import the same timeout + target constants used by reconcile_pool and
        # fill_pool_on_startup so the three callers stay in sync.
        from ..scheduler import POOL_TARGET_SIZE, SLOT_BUILD_TIMEOUT_SECONDS

        log.info(
            "[pool] Replenishment task started: triggering_slot_id=%d "
            "(timeout=%ds target=%d)",
            triggering_slot_id, SLOT_BUILD_TIMEOUT_SECONDS, POOL_TARGET_SIZE,
        )

        # ── Guard: don't start a concurrent build if the pool is already ──────
        # being topped up (building > 0 counts toward the effective pool size,
        # exactly the same way reconcile_pool's deficit calculation works:
        #   deficit = max(0, POOL_TARGET_SIZE - available - building)
        # Launching a second _build_pool_slot() while one is already running
        # bursts Graph API requests, triggers 429 throttling, and causes BOTH
        # builds to slow down or fail — which is why available never recovered.
        with SessionLocal() as db:
            _cur_available = db.query(models.PoolSlot).filter_by(status="available").count()
            _cur_building  = db.query(models.PoolSlot).filter_by(status="building").count()
            _cur_claimed   = db.query(models.PoolSlot).filter_by(status="claimed").count()
            _cur_failed    = db.query(models.PoolSlot).filter_by(status="failed").count()
            _cur_total     = db.query(models.PoolSlot).count()

        effective_pool = _cur_available + _cur_building
        log.info(
            "[pool] Pre-build pool check: available=%d building=%d claimed=%d "
            "failed=%d total=%d → effective=%d (target=%d)",
            _cur_available, _cur_building, _cur_claimed, _cur_failed, _cur_total,
            effective_pool, POOL_TARGET_SIZE,
        )

        if effective_pool >= POOL_TARGET_SIZE:
            log.info(
                "[pool] Replenishment skipped (triggering_slot=%d): "
                "effective pool size %d already meets target %d "
                "(available=%d + building=%d). "
                "reconcile_pool will verify on its next tick.",
                triggering_slot_id, effective_pool, POOL_TARGET_SIZE,
                _cur_available, _cur_building,
            )
            return

        # Pool genuinely needs a new slot — proceed with the build.
        with SessionLocal() as db:
            job = models.ReplenishJob(triggering_slot_id=triggering_slot_id, status="pending")
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
        log.info(
            "[pool] ReplenishJob created: job_id=%d triggering_slot_id=%d "
            "(deficit=%d, building new slot now)",
            job_id, triggering_slot_id, POOL_TARGET_SIZE - effective_pool,
        )

        try:
            new_slot_id = await asyncio.wait_for(
                self._build_pool_slot(),
                timeout=SLOT_BUILD_TIMEOUT_SECONDS,
            )
            with SessionLocal() as db:
                job = db.query(models.ReplenishJob).filter_by(id=job_id).one()
                job.status = "done"
                job.new_slot_id = new_slot_id
                db.commit()
                # Final pool state after replenishment job completes
                available_now = db.query(models.PoolSlot).filter_by(status="available").count()
                building_now  = db.query(models.PoolSlot).filter_by(status="building").count()
                claimed_now   = db.query(models.PoolSlot).filter_by(status="claimed").count()
                failed_now    = db.query(models.PoolSlot).filter_by(status="failed").count()
                total_now     = db.query(models.PoolSlot).count()
            log.info(
                "[pool] ✓ Replenishment complete: new_slot_id=%d job_id=%d "
                "(triggered by slot %d) | pool_snapshot available=%d building=%d "
                "claimed=%d failed=%d total=%d",
                new_slot_id, job_id, triggering_slot_id,
                available_now, building_now, claimed_now, failed_now, total_now,
            )
        except asyncio.TimeoutError:
            # Build hung for longer than SLOT_BUILD_TIMEOUT_SECONDS.
            # Mark the job failed so reconcile_pool can retry it on the next
            # 5-minute tick. The PoolSlot itself stays in 'building' state and
            # will be marked 'failed' by reconcile_pool's stuck-slot cleanup.
            with SessionLocal() as db:
                job = db.query(models.ReplenishJob).filter_by(id=job_id).one_or_none()
                if job is not None:
                    job.status = "failed"
                    db.commit()
            log.warning(
                "[pool] ✗ Replenishment TIMED OUT after %ds (job_id=%d triggered by slot %d) — "
                "ReplenishJob marked failed; reconcile_pool will retry on next tick",
                SLOT_BUILD_TIMEOUT_SECONDS, job_id, triggering_slot_id,
            )
        except Exception as e:
            with SessionLocal() as db:
                job = db.query(models.ReplenishJob).filter_by(id=job_id).one_or_none()
                if job is not None:
                    job.status = "failed"
                    db.commit()
            log.warning(
                "[pool] ✗ Replenishment FAILED (job_id=%d triggered by slot %d): %s",
                job_id, triggering_slot_id, e,
            )


    async def start_vessel_provisioning(self, vessel_id: str) -> dict:
        """Start (or observe) the idempotent server-side provisioning job."""
        try:
            vessel_id_num = int(vessel_id)
        except (TypeError, ValueError):
            raise NotFound(f"Vessel {vessel_id!r} not found")

        with SessionLocal() as db:
            vessel = db.query(models.Vessel).filter_by(id=vessel_id_num).one_or_none()
            if vessel is None:
                raise NotFound(f"Vessel {vessel_id!r} not found")
            if vessel.is_provisioned:
                return {"status": "completed", "is_provisioned": True}

            # A retry must stay within the sites selected when the vessel was
            # created. The legacy single-site provisioner defaults to the
            # backend's active site, which can be a different SharePoint site.
            target_sites = [
                str(site).strip().lower()
                for site in (vessel.provisioned_site_ids or [])
                if str(site).strip()
            ]
            vessel_name = vessel.name

        if target_sites:
            from .site_provisioning import provision_vessel_multi_site
            result = await provision_vessel_multi_site(
                vessel_id_num,
                vessel_name,
                target_sites,
            )
            return {
                "status": "completed" if all(
                    item.get("status") == "success"
                    for item in result.get("results", {}).values()
                ) else "failed",
                "is_provisioned": bool(result.get("provisioned_sites")),
                **result,
            }

        if vessel_id_num not in self._provisioning_vessel_ids:
            self._provisioning_vessel_ids.add(vessel_id_num)

            async def run() -> None:
                try:
                    await self._provision_vessel({}, vessel_id_num)
                except Exception:
                    # The vessel remains available with is_provisioned=False,
                    # so a user can safely use Provision to retry it.
                    log.exception("Provisioning failed for vessel %s", vessel_id_num)
                finally:
                    self._provisioning_vessel_ids.discard(vessel_id_num)

            asyncio.create_task(run(), name=f"provision_vessel_{vessel_id_num}")
        return {"status": "provisioning", "is_provisioned": False}

    async def _provision_vessel(self, payload, existing_vessel_id: int | None = None):
        # Re-validate at execution time — covers the approve-time path, where
        # the name/IMO may have been taken by someone else since the request
        # was filed.
        if existing_vessel_id is not None:
            with SessionLocal() as db:
                existing = db.query(models.Vessel).filter_by(id=existing_vessel_id).one_or_none()
                if existing is None:
                    raise NotFound(f"Vessel {existing_vessel_id!r} not found")
                name, imo = existing.name, existing.imo
                shipyard, hull_number, vessel_type = existing.shipyard, existing.hull_number, existing.vessel_type
                existing.is_provisioned = False
                db.commit()
        else:
            name, imo = self._validate_vessel_input(payload["name"], payload["imo"])
            shipyard = payload.get("shipyard")
            hull_number = payload.get("hull_number")
            vessel_type = payload.get("vessel_type")

        await self.ensure_base_structure()
        drive_id = await self._drive()
        # Capture the existing vessel row + the Specific Vessels root id, then release the session.
        with SessionLocal() as db:
            if existing_vessel_id is None:
                vessel = models.Vessel(
                    name=name,
                    imo=imo,
                    shipyard=shipyard,
                    hull_number=hull_number,
                    vessel_type=vessel_type,
                )
                db.add(vessel)
                db.flush()
            else:
                vessel = db.query(models.Vessel).filter_by(id=existing_vessel_id).one()
            vessel_id, vname, vimo = vessel.id, vessel.name, vessel.imo
            vshipyard, vhull, vtype = vessel.shipyard, vessel.hull_number, vessel.vessel_type
            db.commit()

        created_ship_roots: list[tuple[str, str]] = []
        requesting_email = (payload.get("requesting_email") or "").strip()
        requesting_name = payload.get("requesting_name") or ""

        # Part C (2026-09-21): a vessel is a single flat root folder at the
        # drive root, with no automatic MAIN_FOLDERS/department nesting and
        # no SHIP_TEMPLATE subtree. This replaces the previous
        # gather-across-mains build (one ship root + subtree per main
        # department). Any subfolder structure is created manually
        # afterward via Phase 3's folder creation flow.
        try:
            root = await gd.get_root_item_id(drive_id)
            ship = await gd.ensure_folder(drive_id, root, name)
            ship_root_path = name
            created_ship_roots.append((ship["id"], ship_root_path))
            with SessionLocal() as db:
                self._upsert(db, ship_root_path, name, "ship", ship["id"], False, vessel_id)
                db.commit()
                # Emit a vessel-provisioned alert for the top-header alert bell.
                self._emit_folder_alert(
                    db,
                    drive_item_id=ship["id"],
                    folder_name=f"Vessel: {name}",
                    folder_path=ship_root_path,
                    parent_folder_id=root,
                    vessel_name=name,
                    department="All Departments",
                    created_by_email=requesting_email,
                    created_by_name=requesting_name,
                    alert_type="vessel_provisioned",
                )
        except Exception as provision_err:
            # Remove incomplete folder cache rows. A newly-created vessel is
            # retained so its Provision button can retry the server job.
            with SessionLocal() as db:
                for _, ship_path in created_ship_roots:
                    rows = db.query(models.Folder).filter(
                        sa_or(
                            models.Folder.path == ship_path,
                            models.Folder.path.like(f"{ship_path}/%")
                        )
                    ).all()
                    for row in rows:
                        db.delete(row)
                vessel = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
                if vessel:
                    vessel.is_provisioned = False
                db.commit()

            raise BadRequest(
                f"Could not provision SharePoint folders for vessel '{name}'. "
                f"Please try again. ({type(provision_err).__name__}: {provision_err})"
            ) from provision_err

        with SessionLocal() as db:
            vessel = db.query(models.Vessel).filter_by(id=vessel_id).one()
            vessel.is_provisioned = True
            db.commit()

        # Same reasoning as create_vessel's custom-site branch: drop this
        # drive's cached Documents live-tree entries so the new vessel
        # folder is visible immediately rather than after the 10-minute
        # recursive-tree cache TTL.
        try:
            from ..main import invalidate_folder_caches
            invalidate_folder_caches(drive_id=drive_id)
        except Exception:
            log.debug("Could not invalidate recursive-tree cache after provisioning vessel '%s'", name, exc_info=True)

        return {
            "id": str(vessel_id),
            "name": vname,
            "imo": vimo,
            "shipyard": vshipyard,
            "hull_number": vhull,
            "vessel_type": vtype,
            "is_provisioned": True,
        }

    def _validate_vessel_update(self, vessel_id, name, imo, shipyard, hull_number, vessel_type):
        with SessionLocal() as db:
            vessel = db.query(models.Vessel).filter_by(id=int(vessel_id)).first()
            if not vessel:
                raise NotFound("Vessel not found")
            old_values = {
                "name": vessel.name, "imo": vessel.imo, "shipyard": vessel.shipyard,
                "hull_number": vessel.hull_number, "vessel_type": vessel.vessel_type,
            }
        old_name, old_imo = old_values["name"], old_values["imo"]

        new_name = name.strip() if name is not None else None
        if new_name is not None:
            new_name = sanitize_folder_name(new_name)
        new_imo = imo.strip() if imo is not None else None

        if new_name is not None and new_name == "":
            raise BadRequest("Vessel name cannot be empty")
        if new_imo is not None and new_imo == "":
            raise BadRequest("IMO number cannot be empty")
        if new_imo and (not new_imo.isdigit() or len(new_imo) != 7):
            raise BadRequest("IMO number must be exactly 7 digits")

        if new_name and new_name.lower() != old_name.lower():
            normalized_name = normalize_vessel_name(new_name)
            with SessionLocal() as db:
                existing = db.query(models.Vessel).filter(
                    func.lower(
                        func.replace(
                            func.replace(
                                func.replace(
                                    func.replace(models.Vessel.name, ' ', ''),
                                    '_', ''
                                ),
                                "'", ''
                            ),
                            '"', ''
                        )
                    ) == normalized_name,
                    models.Vessel.id != int(vessel_id),
                ).first()
                if existing:
                    raise Conflict("Vessel name already exists.")

        if new_imo and new_imo != old_imo:
            with SessionLocal() as db:
                if db.query(models.Vessel).filter(
                    models.Vessel.imo == new_imo, models.Vessel.id != int(vessel_id)
                ).first():
                    raise Conflict("A vessel with that IMO number already exists")

        return old_values, new_name, new_imo

    async def update_vessel(
        self, vessel_id: str, name: str | None = None, imo: str | None = None,
        shipyard: str | None = None, hull_number: str | None = None, vessel_type: str | None = None,
        requesting_email=None, requesting_name=None,
        provisioned_site_ids: list[str] | None = None,
    ):
        old_values, new_name, new_imo = self._validate_vessel_update(
            vessel_id, name, imo, shipyard, hull_number, vessel_type
        )
        changes = []
        if new_name and new_name != old_values["name"]:
            changes.append({"field": "Name", "old": old_values["name"], "new": new_name})
        if new_imo and new_imo != old_values["imo"]:
            changes.append({"field": "IMO", "old": old_values["imo"], "new": new_imo})
        if shipyard is not None and (shipyard.strip() or None) != old_values["shipyard"]:
            changes.append({"field": "Shipyard", "old": old_values["shipyard"], "new": shipyard.strip() or None})
        if hull_number is not None and (hull_number.strip() or None) != old_values["hull_number"]:
            changes.append({"field": "Hull Number", "old": old_values["hull_number"], "new": hull_number.strip() or None})
        if vessel_type is not None and (vessel_type.strip() or None) != old_values["vessel_type"]:
            changes.append({"field": "Vessel Type", "old": old_values["vessel_type"], "new": vessel_type.strip() or None})

        payload = {
            "vessel_id": vessel_id, "name": new_name, "imo": new_imo,
            "shipyard": shipyard, "hull_number": hull_number, "vessel_type": vessel_type,
            "provisioned_site_ids": provisioned_site_ids,
        }
        display = self._display(requesting_email, requesting_name)
        change_summary = (
            ", ".join(f"{c['field']} ('{c['old']}' → '{c['new']}')" for c in changes)
            or "no field changes"
        )
        return await self._admin_or_pending(
            action_type="update_vessel",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department="All Departments",
            vessel_id=vessel_id,
            vessel_name=old_values["name"],
            target_id=vessel_id,
            target_description=old_values["name"],
            payload=payload,
            changes=changes,
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to update the "
                f"vessel details for {old_values['name']} ({change_summary})."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) updated the vessel details for "
                f"{old_values['name']}. No approval was required."
            ),
            execute=lambda: self._execute_update_vessel(payload),
        )

    async def _rename_ship_folders(self, drive_id, folders):
        """PATCH each (folder_row, new_name) pair's SharePoint name.

        Shared by update_vessel's rename path AND the pool-slot claim path
        (create_vessel), so the two callers can never drift on how a
        ship-folder rename is actually performed. Never raises — returns a
        per-folder (folder, ok, error) result list so callers can decide
        what to do with each outcome individually (e.g. only link an
        orphan's vessel_id if its own rename actually succeeded).

        Renames run concurrently (bounded by the same semaphore used for
        folder creation) rather than one Graph round-trip at a time — for
        the pool-slot claim path this is the difference between ~3 sequential
        PATCH latencies (~2.3s for 3 main folders) and ~1 (the slowest one).
        """
        from ..graph import drive as _gd

        async def _rename_one(folder, new_name):
            try:
                import time as _time
                _rt = _time.monotonic()
                async with self._semaphore():
                    await asyncio.wait_for(
                        _gd.graph().patch(
                            f"/drives/{drive_id}/items/{folder.drive_item_id}",
                            json={"name": new_name},
                        ),
                        timeout=2.0
                    )
                log.info("[_rename_one] %s -> %s: %.3fs", folder.name, new_name, _time.monotonic() - _rt)
                return (folder, True, None)
            except Exception as e:
                print(f"Error renaming folder {folder.path} in SharePoint: {e}")
                return (folder, False, str(e))

        return list(await asyncio.gather(*(_rename_one(f, n) for f, n in folders)))

    async def _sync_vessel_file_metadata(self, drive_id, folder_ids, vessel_name):
        """Update VesselName metadata for files below renamed ship folders."""
        file_ids = set()
        errors = []

        async def collect(item_id):
            for child in await gd.list_children(drive_id, item_id):
                child_id = child.get("id")
                if not child_id:
                    continue
                if child.get("folder") is not None:
                    await collect(child_id)
                elif child.get("file") is not None:
                    file_ids.add(child_id)

        for folder_id in folder_ids:
            try:
                await collect(folder_id)
            except Exception as exc:
                log.warning(
                    "Could not enumerate files below renamed vessel folder %s: %s",
                    folder_id,
                    exc,
                )
                errors.append((folder_id, False, f"could not enumerate folder: {exc}"))

        async def patch_one(item_id):
            try:
                async with self._semaphore():
                    result = await gd.update_file_columns(
                        drive_id,
                        item_id,
                        {"VesselName": vessel_name},
                    )
                if not result.get("ok"):
                    return item_id, False, result.get("error") or result.get("reason") or "metadata patch failed"
                return item_id, True, None
            except Exception as exc:
                return item_id, False, str(exc)

        results = await asyncio.gather(*(patch_one(item_id) for item_id in file_ids))
        return errors + [result for result in results if not result[1]]

    async def _execute_update_vessel(self, payload):
        vessel_id = payload["vessel_id"]
        old_values, new_name, new_imo = self._validate_vessel_update(
            vessel_id, payload["name"], payload["imo"],
            payload["shipyard"], payload["hull_number"], payload["vessel_type"],
        )
        old_name = old_values["name"]
        shipyard, hull_number, vessel_type = (
            payload["shipyard"], payload["hull_number"], payload["vessel_type"]
        )

        sp_success = True
        sp_errors = []
        if new_name and new_name != old_name:
            drive_id = await self._drive()
            renamed_folder_ids = []
            with SessionLocal() as db:
                # Rename all ship folders linked to this vessel in SharePoint
                vessel_folders = db.query(models.Folder).filter_by(vessel_id=int(vessel_id), kind="ship").all()
                for folder, ok, err in await self._rename_ship_folders(
                    drive_id, [(f, new_name) for f in vessel_folders]
                ):
                    if not ok:
                        sp_success = False
                        sp_errors.append(f"Folder '{folder.name}': {err}")
                    else:
                        renamed_folder_ids.append(folder.drive_item_id)

                # Also find orphaned ship folders (vessel_id=None) with the old name
                # and rename + link them to this vessel
                orphaned = db.query(models.Folder).filter(
                    models.Folder.kind == "ship",
                    models.Folder.vessel_id == None,  # noqa: E711
                    func.lower(models.Folder.name) == func.lower(old_name)
                ).all()
                for folder, ok, err in await self._rename_ship_folders(
                    drive_id, [(f, new_name) for f in orphaned]
                ):
                    if ok:
                        folder.vessel_id = int(vessel_id)
                    else:
                        sp_success = False
                        sp_errors.append(f"Orphaned folder '{folder.name}': {err}")
                db.commit()

            metadata_errors = await self._sync_vessel_file_metadata(
                drive_id, renamed_folder_ids, new_name
            )
            if metadata_errors:
                sp_success = False
                sp_errors.extend(
                    f"File '{item_id}' vessel metadata: {error}"
                    for item_id, _ok, error in metadata_errors
                )

        with SessionLocal() as db:
            v = db.query(models.Vessel).filter_by(id=int(vessel_id)).one()
            if new_name:
                v.name = new_name
            if new_imo:
                v.imo = new_imo
            if shipyard is not None:
                v.shipyard = shipyard.strip() or None
            if hull_number is not None:
                v.hull_number = hull_number.strip() or None
            if vessel_type is not None:
                v.vessel_type = vessel_type.strip() or None
            db.commit()

            if new_name and new_name != old_name:
                folders = db.query(models.Folder).filter_by(vessel_id=v.id).all()
                old_prefix = f"{template.VESSELS_ROOT}/{template.SPECIFIC_VESSELS_ROOT}/{old_name}"
                new_prefix = f"{template.VESSELS_ROOT}/{template.SPECIFIC_VESSELS_ROOT}/{new_name}"
                for folder in folders:
                    if folder.kind == "ship" and folder.name == old_name:
                        folder.name = new_name
                    if folder.path == old_prefix:
                        folder.path = new_prefix
                    elif folder.path.startswith(f"{old_prefix}/"):
                        folder.path = new_prefix + folder.path[len(old_prefix):]
                    db.commit()

            v_updated = db.query(models.Vessel).filter_by(id=int(vessel_id)).one()
            
            # Handle multi-site provisioning updates if provisioned_site_ids was passed
            requested_sites = payload.get("provisioned_site_ids")
            if requested_sites is not None:
                from .site_provisioning import provision_vessel_multi_site
                asyncio.create_task(
                    provision_vessel_multi_site(int(vessel_id), v_updated.name, requested_sites),
                    name=f"provision_vessel_update_{vessel_id}"
                )

            return {
                "id": str(v_updated.id),
                "name": v_updated.name,
                "imo": v_updated.imo,
                "shipyard": v_updated.shipyard,
                "hull_number": v_updated.hull_number,
                "vessel_type": v_updated.vessel_type,
                "is_provisioned": v_updated.is_provisioned,
                "provisioned_site_ids": v_updated.provisioned_site_ids or [],
                "sp_success": sp_success,
                "sp_errors": sp_errors,
            }
    
    async def delete_vessel(
        self, vessel_id: str, requesting_email=None, requesting_name=None, reason=None,
    ) -> dict:
        with SessionLocal() as db:
            try:
                vid = int(vessel_id)
            except ValueError:
                raise NotFound("Vessel not found")
            vessel = db.query(models.Vessel).filter_by(id=vid).one_or_none()
            if not vessel:
                raise NotFound("Vessel not found")
            vname = vessel.name

        display = self._display(requesting_email, requesting_name)
        return await self._admin_or_pending(
            action_type="delete_vessel",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department="All Departments",
            vessel_id=vessel_id,
            vessel_name=vname,
            target_id=vessel_id,
            target_description=f"Vessel: {vname}",
            payload={"vessel_id": vessel_id, "vessel_name": vname},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to delete vessel '{vname}'."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) deleted vessel '{vname}'. No approval was required."
            ),
            execute=lambda: self._execute_delete_vessel(
                vessel_id, requesting_email=requesting_email,
                requesting_name=requesting_name, reason=reason,
            ),
        )

    @protected_delete_operation
    async def _execute_delete_vessel(
        self, vessel_id: str, requesting_email=None, requesting_name=None, reason=None,
        source: str = "app",
    ) -> dict:
        """Delete a vessel: delete its root ship folders via Graph API (moving them to
        SharePoint Recycle Bin) and delete the vessel + folder rows from SQLite DB.

        A vessel can have folders on more than one SharePoint site (multi-site
        provisioning, or a folder discovered/tagged on a secondary site such as
        a Communication Site). Deletion used to only look at ``self._drive()``
        (the single currently-active site), so a folder living on any other
        site was never found and was left behind after the vessel row was
        removed from the DB. We now resolve every site the vessel is known to
        touch -- its own cached Folder.site_id values plus its
        provisioned_site_ids/provisioned_site_key -- and search/delete across
        all of their drives."""
        active_drive_id = await self._drive()
        from ..graph import drive as _gd
        from .site_provisioning import resolve_site_drive

        with SessionLocal() as db:
            try:
                vid = int(vessel_id)
            except ValueError:
                raise NotFound("Vessel not found")
            vessel = db.query(models.Vessel).filter_by(id=vid).one_or_none()
            if not vessel:
                return {"deleted": False, "message": "Vessel not found"}
            vname = vessel.name
            vimo = vessel.imo
            vtype = vessel.vessel_type
            v_is_provisioned = bool(vessel.is_provisioned)
            provisioned_refs = list(vessel.provisioned_site_ids or [])
            if vessel.provisioned_site_key:
                provisioned_refs.append(vessel.provisioned_site_key)

            # Find ship root folders for this vessel
            ship_folders = (
                db.query(models.Folder)
                .filter(models.Folder.vessel_id == vid, models.Folder.kind == "ship")
                .all()
            )
            ship_folder_refs = [
                (f.drive_item_id, f.path, f.site_id)
                for f in ship_folders
                if f.path
            ]

            # Resolve every site this vessel is known to touch to a drive_id.
            search_drive_ids: list[str] = []
            seen_drives: set[str] = set()

            def _add_drive(d: str | None) -> None:
                if d and d not in seen_drives:
                    seen_drives.add(d)
                    search_drive_ids.append(d)

            _add_drive(active_drive_id)
            for _, _, ref_site_id in ship_folder_refs:
                _add_drive(ref_site_id)
            for ref in provisioned_refs:
                try:
                    _, resolved_drive, _ = await resolve_site_drive(ref, db=db)
                    _add_drive(resolved_drive)
                except Exception as exc:
                    log.warning(
                        "[_execute_delete_vessel] could not resolve drive for site '%s': %s",
                        ref, exc,
                    )

        # Build candidate root paths for vessel folders across both current and
        # legacy structures.
        candidate_paths: list[str] = []
        seen_paths: set[str] = set()

        for _, p, _ in ship_folder_refs:
            cp = (p or "").strip("/")
            if cp and cp not in seen_paths:
                candidate_paths.append(cp)
                seen_paths.add(cp)

        for main in template.MAIN_FOLDERS:
            if main in template.FLAT_MAIN_FOLDERS:
                continue
            for cp in (
                f"{main}/{vname}",
                f"{main}/{template.VESSELS_ROOT}/{template.SPECIFIC_VESSELS_ROOT}/{vname}",
                f"{main}/{template.SPECIFIC_VESSELS_ROOT}/{vname}",
            ):
                cp = cp.strip("/")
                if cp and cp not in seen_paths:
                    candidate_paths.append(cp)
                    seen_paths.add(cp)

        # Legacy global vessel root variants.
        for cp in (
            f"{template.VESSELS_ROOT}/{template.SPECIFIC_VESSELS_ROOT}/{vname}",
            f"{template.SPECIFIC_VESSELS_ROOT}/{vname}",
            f"Vessels/Specific Vessels/{vname}",
        ):
            cp = cp.strip("/")
            if cp and cp not in seen_paths:
                candidate_paths.append(cp)
                seen_paths.add(cp)

        # Resolve live delete targets from path first, then name search as a
        # fallback -- across every site drive this vessel might have folders on.
        live_targets: dict[str, tuple[str, str]] = {}  # item_id -> (drive_id, best-known path)
        for drive_id in search_drive_ids:
            for cp in candidate_paths:
                try:
                    item = await asyncio.wait_for(
                        _gd.get_item_by_path(drive_id, cp, select="id,name,folder,parentReference"),
                        timeout=10,
                    )
                    if item and item.get("id") and item.get("folder") is not None:
                        live_targets[item["id"]] = (drive_id, cp)
                except GraphError as exc:
                    if exc.status != 404:
                        log.warning("[_execute_delete_vessel] path lookup failed for %s on drive %s: %s", cp, drive_id, exc)
                except Exception as exc:
                    log.warning("[_execute_delete_vessel] path lookup error for %s on drive %s: %s", cp, drive_id, exc)

        if not live_targets:
            valid_prefixes = [
                f"root:/{main}/".lower()
                for main in template.MAIN_FOLDERS
                if main not in template.FLAT_MAIN_FOLDERS
            ] + [
                "root:/vessels/specific vessels/",
                "root:/specific vessels/",
            ]
            for drive_id in search_drive_ids:
                try:
                    hits = await asyncio.wait_for(_gd.search_items(drive_id, vname), timeout=12)
                    for hit in hits:
                        if hit.get("folder") is None:
                            continue
                        if (hit.get("name") or "").strip().lower() != vname.strip().lower():
                            continue
                        parent_path = ((hit.get("parentReference") or {}).get("path") or "").lower()
                        if any(parent_path.startswith(pfx) for pfx in valid_prefixes):
                            live_targets[hit["id"]] = (drive_id, parent_path)
                except Exception as exc:
                    log.warning("[_execute_delete_vessel] search fallback failed for '%s' on drive %s: %s", vname, drive_id, exc)

        if not live_targets:
            log.info(
                "[_execute_delete_vessel] No active ship folders found in SharePoint for '%s' across %d known site drive(s) (already moved to Recycle Bin or absent). Proceeding with DB cleanup.",
                vname, len(search_drive_ids),
            )

        # Delete ship folders via Graph API -> automatically moved to SharePoint
        # Recycle Bin. Use bounded per-call timeouts and a path-based fallback
        # so stale cache IDs (wrong drive/item) do not silently remove the
        # vessel from DB while folders still exist in SharePoint.
        async def _delete_ship_folder(drive_id: str, item_id: str, ship_path: str) -> tuple[str, bool, str | None]:
            async def _delete_by_id(target_id: str) -> tuple[bool, str | None]:
                try:
                    await asyncio.wait_for(_gd.delete_item(drive_id, target_id), timeout=25)
                    return (True, None)
                except asyncio.TimeoutError:
                    return (False, "timeout")
                except GraphError as exc:
                    return (False, f"graph_{exc.status}: {exc}")
                except Exception as exc:
                    return (False, str(exc))

            try:
                ok, err = await _delete_by_id(item_id)
                if ok:
                    return (ship_path, True, None)

                # Fallback for stale/wrong-drive IDs: resolve by known path then delete.
                try:
                    live = await asyncio.wait_for(
                        _gd.get_item_by_path(drive_id, ship_path, select="id"),
                        timeout=12,
                    )
                except GraphError as exc:
                    if exc.status == 404:
                        return (ship_path, False, "path_lookup_404")
                    return (ship_path, False, f"path_lookup_graph_{exc.status}: {exc}")
                except asyncio.TimeoutError:
                    return (ship_path, False, "path_lookup_timeout")
                except Exception as exc:
                    return (ship_path, False, f"path_lookup_error: {exc}")

                live_id = (live or {}).get("id")
                if not live_id:
                    return (ship_path, False, "path_lookup_missing_id")

                ok2, err2 = await _delete_by_id(live_id)
                return (ship_path, ok2, err2)
            except Exception as exc:
                return (ship_path, False, str(exc))

        delete_results = await asyncio.gather(
            *(
                _delete_ship_folder(drive_id, item_id, ship_path)
                for item_id, (drive_id, ship_path) in live_targets.items()
            )
        )
        failed = []
        for ship_path, ok, err in delete_results:
            if not ok and err == "path_lookup_404":
                # The folder is already absent at the expected path, which
                # indicates it was moved/deleted in SharePoint. Treat as a
                # successful delete equivalent for this flow.
                ok = True
            if not ok:
                failed.append((ship_path, err or "unknown_error"))
                log.warning(
                    "[_execute_delete_vessel] Failed to delete ship folder %s: %s",
                    ship_path,
                    err,
                )

        if failed:
            return {
                "deleted": False,
                "vessel_name": vname,
                "message": (
                    "Could not move all vessel folders to Recycle Bin. "
                    "Please retry; vessel was not removed from the app."
                ),
                "failed_paths": [p for p, _ in failed],
            }

        ship_folder_ids = list(live_targets.keys())
        ship_paths = [p for _, p in live_targets.values()]

        # Record deletion in DB after successful SPO deletion so app state
        # matches SharePoint state.
        original_path = ship_paths[0] if ship_paths else f"Vessels/Specific Vessels/{vname}"
        primary_drive_item_id = ship_folder_ids[0] if ship_folder_ids else None
        site_name_val = None
        site_key_val = None
        with SessionLocal() as db:
            v_orig = db.query(models.Vessel).filter_by(id=vid).one_or_none()
            if v_orig and v_orig.provisioned_site_ids:
                site_key_val = v_orig.provisioned_site_ids[0]
                site_cfg = db.query(models.SiteConfiguration).filter_by(site_key=site_key_val).one_or_none()
                if site_cfg:
                    site_name_val = site_cfg.display_name or site_cfg.site_name
            if not site_name_val:
                site_name_val = settings.active_site or "Primary Site"
                site_key_val = settings.active_site or "default"

            existing = db.query(models.DeletedVessel).filter_by(vessel_name=vname).one_or_none()
            if existing:
                existing.vessel_imo = vimo
                existing.vessel_type = vtype
                existing.drive_item_id = primary_drive_item_id
                existing.original_path = original_path
                existing.site_name = site_name_val
                existing.site_key = site_key_val
                existing.deleted_at = datetime.utcnow()
            else:
                db.add(models.DeletedVessel(
                    vessel_name=vname,
                    vessel_imo=vimo,
                    vessel_type=vtype,
                    drive_item_id=primary_drive_item_id,
                    original_path=original_path,
                    site_name=site_name_val,
                    site_key=site_key_val,
                ))
            db.commit()

        await self._record_deletion(
            item_type="vessel",
            drive_item_id=primary_drive_item_id,
            name=vname,
            original_path=original_path,
            site_name=site_name_val,
            site_key=site_key_val,
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            reason=reason,
            source=source,
        )

        # Delete vessel & associated folder rows from DB, and cancel any pending approvals
        with SessionLocal() as db:
            vessel = db.query(models.Vessel).filter_by(id=vid).one_or_none()
            if vessel:
                vname_clean = vessel.name.strip().lower()
                # Cancel all pending approval requests for this vessel so the approval
                # queue does not show stale entries after the vessel is deleted.
                db.query(models.ApprovalRequest).filter(
                    models.ApprovalRequest.status == "pending",
                    sa_or(
                        models.ApprovalRequest.vessel_id == vid,
                        func.lower(models.ApprovalRequest.vessel_name) == vname_clean,
                    )
                ).update(
                    {models.ApprovalRequest.status: "cancelled"},
                    synchronize_session=False,
                )
                # Delete any folder rows belonging to or named after this vessel
                db.query(models.Folder).filter(
                    sa_or(
                        models.Folder.vessel_id == vid,
                        func.lower(models.Folder.name) == vname_clean,
                        models.Folder.path.ilike(f"%/{vname_clean}/%"),
                        models.Folder.path.ilike(f"%/{vname_clean}"),
                    )
                ).delete(synchronize_session=False)
                db.delete(vessel)
                db.commit()

        # Invalidate folder & tree caches so deleted vessel folders disappear immediately
        from ..main import invalidate_folder_caches
        invalidate_folder_caches()

        return {"deleted": True, "vessel_name": vname, "message": f"Moved vessel '{vname}' to Recycle Bin."}

    async def reconcile_vessel_folders(self, force: bool = True) -> dict:
        """Remove vessels whose SharePoint ship folder was deleted directly in
        SharePoint (soft delete into the app's Recycle Bin). See services/folder_sync.py."""
        from . import folder_sync
        return await folder_sync.reconcile_vessel_folders(self, force=force)

    async def sync_folder_table(self) -> dict:
        """Apply SharePoint renames/moves/deletes to the folders table (Graph delta)."""
        from . import folder_sync
        return await folder_sync.sync_folder_table(self)

    async def repair_vessel_links(self) -> dict:
        """Scan all ship-kind folders with vessel_id=None and try to link them
        to a vessel row by matching the folder name (case-insensitive).
        Returns a summary of how many were fixed."""
        with SessionLocal() as db:
            # Build name -> vessel_id map
            vessels = db.query(models.Vessel).all()
            name_to_id: dict[str, int] = {v.name.lower(): v.id for v in vessels}

            # Find orphaned ship folders
            orphans = (
                db.query(models.Folder)
                .filter(models.Folder.kind == "ship", models.Folder.vessel_id == None)  # noqa: E711
                .all()
            )
            fixed = 0
            unmatched = []
            for folder in orphans:
                vid = name_to_id.get(folder.name.lower())
                if vid is not None:
                    folder.vessel_id = vid
                    fixed += 1
                else:
                    unmatched.append(folder.name)
            db.commit()
        return {"fixed": fixed, "unmatched": unmatched}

    # ------------------------------------------------- vessel auto-discovery
    async def sync_vessels_from_sharepoint(self, site_key: str | None = None, actor_email: str | None = None) -> dict:
        """On-demand (or scheduled) reconciliation: cross-match every
        connected site's root folders against the DB vessel registry and
        the site's Term Store vessel term set. See services/vessel_sync.py
        for the read-only Graph walk this uses.

        For each root-level candidate folder:
        - in DB and in Term Store already -> nothing to do.
        - in DB, Term Store term missing -> create the term now (the same
          call the tag-write path makes lazily on first upload), so the
          dashboard's Total Vessels count and tag dropdowns catch up
          immediately instead of waiting for someone to tag a file.
        - in Term Store, no DB row -> already surfaced elsewhere as a
          "Found in SharePoint" row by list_vessels(); nothing to add here.
        - in neither -> upsert a FolderAnomaly(anomaly_type=
          "root_vessel_unmatched") row so it appears in the existing
          Alerts / classify-dialog flow for an admin to confirm with
          IMO/Hull Number via confirm_discovered_vessel(), below.

        Also flags same-site name collisions among the candidates
        themselves (case/punctuation-insensitive) as conflicts, per the
        "prevent duplicate vessel names" validation rule.

        Never creates, renames or moves a SharePoint folder — read-only
        Graph reads plus Term Store / FolderAnomaly / ActivityLog writes.
        """
        from . import vessel_sync

        requested_site = (site_key or "").strip().lower()
        with allow_protected_reads():
            infos = _all_site_infos(include_protected=False)
        if requested_site and requested_site != "all":
            infos = [i for i in infos if site_alias_matches(i["site_key"], requested_site)]

        with SessionLocal() as db:
            known_vessels = {_normalize_term_label(v.name) for v in db.query(models.Vessel).all()}
            pool_slugs = {p.slug.strip().lower() for p in db.query(models.PoolSlot).all()}

        summary: dict = {"new": 0, "updated": 0, "conflicts": 0, "sites": [], "conflict_details": []}
        for info in infos:
            drive_id, site_id = info.get("drive_id"), info.get("site_id")
            if not drive_id or not site_id:
                continue
            try:
                term_set = {_normalize_term_label(t) for t in await gd.get_vessel_terms(site_id)}
            except Exception as e:
                log.warning("vessel_sync: get_vessel_terms failed for site=%s: %s", info["site_key"], e)
                term_set = set()
            try:
                candidates = await vessel_sync.list_root_vessel_candidates(drive_id, pool_slugs)
            except Exception as e:
                log.warning("vessel_sync: candidate scan failed for site=%s: %s", info["site_key"], e)
                continue

            site_new = 0
            site_updated = 0
            seen_names: dict[str, str] = {}
            for cand in candidates:
                norm = vessel_sync.normalize_label(cand.name)
                prior_name = seen_names.get(norm)
                if prior_name is not None and prior_name != cand.name:
                    summary["conflicts"] += 1
                    summary["conflict_details"].append({
                        "site_key": info["site_key"], "names": [prior_name, cand.name], "path": cand.path,
                    })
                else:
                    seen_names[norm] = cand.name

                in_db = norm in known_vessels
                in_terms = norm in term_set
                if in_db and in_terms:
                    continue
                if in_db and not in_terms:
                    try:
                        created = await gd.ensure_vessel_term(site_id, cand.name)
                    except Exception as e:
                        log.warning("vessel_sync: term creation failed for '%s' site=%s: %s", cand.name, info["site_key"], e)
                        created = None
                    if created:
                        site_updated += 1
                    continue
                if in_terms and not in_db:
                    continue

                with SessionLocal() as db:
                    existing = db.query(models.FolderAnomaly).filter_by(drive_item_id=cand.drive_item_id).one_or_none()
                    if existing:
                        existing.name = cand.name
                        existing.spo_path = cand.path
                        existing.anomaly_type = "root_vessel_unmatched"
                    else:
                        db.add(models.FolderAnomaly(
                            drive_item_id=cand.drive_item_id,
                            name=cand.name,
                            item_type="folder",
                            anomaly_type="root_vessel_unmatched",
                            department="All Departments",
                            vessel_name=None,
                            spo_path=cand.path,
                            resolved=False,
                        ))
                        site_new += 1
                    db.commit()

            summary["new"] += site_new
            summary["updated"] += site_updated
            summary["sites"].append({
                "site_key": info["site_key"], "site_name": info.get("site_name"),
                "scanned": len(candidates), "new": site_new, "updated": site_updated,
            })

        actor = (actor_email or "").strip().lower() or "system"
        detail = (
            f"Vessel sync: {summary['new']} new folder(s) flagged for review, "
            f"{summary['updated']} Term Store entr{'y' if summary['updated'] == 1 else 'ies'} created, "
            f"{summary['conflicts']} duplicate-name warning(s)."
        )
        try:
            with SessionLocal() as db:
                db.add(models.ActivityLog(user_email=actor, action="vessel_sync", detail=detail))
                db.commit()
        except Exception:
            log.warning("vessel_sync: failed to write ActivityLog", exc_info=True)

        summary["message"] = detail
        return summary

    async def confirm_discovered_vessel(
        self, name: str, imo: str, hull_number: str | None,
        site_key: str, original_path: str,
        requesting_email: str | None = None, requesting_name: str | None = None,
    ) -> dict:
        """Promote a SharePoint-discovered root folder — surfaced either via
        list_vessels()'s 'source: sharepoint' merge or a resolved
        'root_vessel_unmatched' FolderAnomaly — into a real Vessel row,
        WITHOUT provisioning a new folder: the folder at original_path is
        already the vessel's folder (spec requirement: no duplicate folder
        creation). Also ensures the name exists as a Term Store term.

        Raises Conflict if the name or IMO collides with an existing
        vessel (same validation create_vessel uses), BadRequest if
        site_key/original_path are missing.
        """
        clean_name, clean_imo = self._validate_vessel_input(name, imo)
        hull_clean = (hull_number or "").strip() or None
        clean_site = (site_key or "").strip().lower()
        clean_path = (original_path or "").strip()
        if not clean_site or not clean_path:
            raise BadRequest("site_key and original_path are required to confirm a discovered vessel")

        with SessionLocal() as db:
            v_db = models.Vessel(
                name=clean_name,
                imo=clean_imo,
                hull_number=hull_clean,
                is_provisioned=True,
                provisioned_site_ids=[clean_site],
                provisioned_site_key=clean_site,
                vessel_folder_path=clean_path,
            )
            db.add(v_db)
            db.commit()
            db.refresh(v_db)
            vessel_id = v_db.id

        # Best-effort: resolve the matching FolderAnomaly row (if this came
        # from the Alerts/classify flow) so it drops out of the queue.
        try:
            with SessionLocal() as db:
                anomaly = (
                    db.query(models.FolderAnomaly)
                    .filter_by(anomaly_type="root_vessel_unmatched", spo_path=clean_path)
                    .one_or_none()
                )
                if anomaly:
                    anomaly.resolved = True
                    anomaly.vessel_name = clean_name
                    db.commit()
        except Exception:
            log.warning("confirm_discovered_vessel: failed to resolve matching anomaly", exc_info=True)

        # Best-effort: push the Term Store term now rather than waiting for
        # the next tagged upload, so the dashboard's Total Vessels count
        # reflects this vessel right away.
        with allow_protected_reads():
            site_info = next((i for i in _all_site_infos() if i["site_key"] == clean_site), None)
        if site_info and site_info.get("site_id"):
            try:
                await gd.ensure_vessel_term(site_info["site_id"], clean_name)
            except Exception:
                log.warning("confirm_discovered_vessel: Term Store sync failed for '%s'", clean_name, exc_info=True)

        display = self._display(requesting_email, requesting_name)
        message = (
            f"{display} ({requesting_email or 'system'}) confirmed SharePoint folder "
            f"'{clean_name}' as a registered vessel (IMO {clean_imo}). No new folder was created."
        )
        asyncio.create_task(
            self._create_activity(
                action_type="confirm_discovered_vessel",
                requesting_email=requesting_email or "",
                requesting_name=requesting_name,
                department="All Departments",
                vessel_id=vessel_id,
                vessel_name=clean_name,
                target_description=clean_path,
                message=message,
            ),
            name=f"activity_confirm_vessel_{clean_name}",
        )
        return {
            "id": str(vessel_id), "name": clean_name, "imo": clean_imo,
            "hull_number": hull_clean, "provisioned_site_key": clean_site,
            "vessel_folder_path": clean_path, "is_provisioned": True,
            "status": "completed", "message": message,
        }

    async def reprovision_vessel(self, vessel_id: str) -> dict:
        """Report a vessel's existing folders without creating anything.

        The app no longer auto-creates any department or template structure
        (Technical & Crewing / Commercial & Chartering / Insurance ship roots,
        SHIP_TEMPLATE leaves, or Kaizen). Reprovision therefore never writes
        to SharePoint; it only returns the vessel's cached ship-root paths so
        the existing response shape the frontend expects is preserved.
        """
        with SessionLocal() as db:
            vessel = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
            if vessel is None:
                raise NotFound(f"Vessel {vessel_id!r} not found")
            name = vessel.name
            existing_ship_paths = [
                row.path
                for row in db.query(models.Folder).filter_by(vessel_id=vessel.id, kind="ship").all()
            ]

        return {
            "ok": True,
            "vessel_id": vessel_id,
            "name": name,
            "is_provisioned": True,
            "created": [],
            "existed": existing_ship_paths,
            "failed": [],
            "summary": {
                "created_count": 0,
                "existed_count": len(existing_ship_paths),
                "failed_count": 0,
            },
            "message": (
                f"'{name}': no folders were created — the app does not "
                "auto-create template folder structures."
            ),
        }

    # ----------------------------------------------------------- navigation
    async def mains(self):
        # ensure_base_structure hits Graph API; if SharePoint is temporarily
        # unavailable we still want to serve whatever is cached in the DB.
        try:
            await self.ensure_base_structure()
        except Exception as exc:
            log.warning(
                "mains(): ensure_base_structure failed (%s) — serving DB cache", exc
            )
        with SessionLocal() as db:
            out = []
            for root_name in template.ALL_MAIN_FOLDERS:
                row = db.query(models.Folder).filter_by(path=root_name).one_or_none()
                if row:
                    out.append(
                        {
                            "id": row.drive_item_id,
                            "name": row.name,
                            "kind": "main",
                            "upload": False,
                            "month_driven": False,
                            "has_children": True,
                        }
                    )
            return out

    async def get_folder(self, folder_id):
        drive_id = await self._drive()
        path = await self._folder_path(drive_id, folder_id)
        parts = path.split("/")
        flags = classify(parts)
        with SessionLocal() as db:
            flags = self._classify_with_vessel_upgrade(db, parts, flags)
        return {"id": folder_id, "name": parts[-1], "has_children": True, **flags}

    async def resolve_path(self, path: str) -> str:
        """Resolve a logical folder path (e.g. 'Folder-3 Insurance/Bow Fighter' or
        'Technical & Crewing/Bow Fighter/Registration/Flag & MPA' or
        'Bow Fighter > Technical & Crewing > Registration > Flag & MPA') to its drive_item_id.
        """
        raw = (path or "").strip()
        # Handle '>' breadcrumb separator if present
        raw = raw.replace(" > ", "/").replace(">", "/").strip("/")
        
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        if parts and parts[0].lower() in ("vessel management", "shared documents"):
            parts.pop(0)

        main_map = {
            "folder-1 technical & crewing": "Technical & Crewing",
            "folder-1 technical and crewing": "Technical & Crewing",
            "technical & crewing": "Technical & Crewing",
            "technical and crewing": "Technical & Crewing",
            "folder-2 commercial & chartering": "Commercial & Chartering",
            "folder-2 commercial and chartering": "Commercial & Chartering",
            "commercial & chartering": "Commercial & Chartering",
            "commercial and chartering": "Commercial & Chartering",
            "folder-3 insurance": "Insurance",
            "insurance": "Insurance",
            "folder-4 kaizen - knowledge bank": "Kaizen - Knowledge Bank",
            "kaizen - knowledge bank": "Kaizen - Knowledge Bank",
            "knowledge bank": "Kaizen - Knowledge Bank",
        }

        # Identify components from parts
        main_folder = None
        vessel_name = None
        rest_parts = []

        if parts and parts[0].lower() in main_map:
            main_folder = main_map[parts[0].lower()]
            if len(parts) >= 2:
                vessel_name = parts[1]
                rest_parts = parts[2:]
        elif len(parts) >= 2 and parts[1].lower() in main_map:
            main_folder = main_map[parts[1].lower()]
            vessel_name = parts[0]
            rest_parts = parts[2:]
        elif parts:
            vessel_name = parts[0]
            rest_parts = parts[1:]

        canonical_candidates = []
        p0_lower = parts[0].lower() if parts else ""
        if p0_lower in ("kaizen - knowledge bank", "kaizen", "knowledge bank") or main_folder == "Kaizen - Knowledge Bank":
            canonical_candidates.append("/".join(["Kaizen - Knowledge Bank", *rest_parts]))
        elif vessel_name and vessel_name.lower() in ("common for all ships", "common for all vessels", "common"):
            if main_folder:
                canonical_candidates.append(f"{main_folder}/Common for all ships/{'/'.join(rest_parts)}".rstrip("/"))
                canonical_candidates.append(f"{main_folder}/Common (Not Ship Specific)/{'/'.join(rest_parts)}".rstrip("/"))
            canonical_candidates.append(f"Common for all ships/{'/'.join(rest_parts)}".rstrip("/"))
            canonical_candidates.append(f"Vessels/Common for all ships/{'/'.join(rest_parts)}".rstrip("/"))
        elif vessel_name:
            mains_to_try = [main_folder] if main_folder else template.MAIN_FOLDERS
            for m in mains_to_try:
                # 1. New primary structure: {MainFolder}/{VesselName}/{rest}
                canonical_candidates.append(f"{m}/{vessel_name}/{'/'.join(rest_parts)}".rstrip("/"))
                if len(rest_parts) > 1:
                    canonical_candidates.append(f"{m}/{vessel_name}/{'/'.join(rest_parts[:-1])}".rstrip("/"))
                # 2. Legacy Specific Vessels: Vessels/Specific Vessels/{VesselName}/{MainFolder}/{rest}
                canonical_candidates.append(f"Vessels/Specific Vessels/{vessel_name}/{m}/{'/'.join(rest_parts)}".rstrip("/"))
                if len(rest_parts) > 1:
                    canonical_candidates.append(f"Vessels/Specific Vessels/{vessel_name}/{m}/{'/'.join(rest_parts[:-1])}".rstrip("/"))
                # 3. Legacy Vessels root: Vessels/{VesselName}/{m}/{rest}
                canonical_candidates.append(f"Vessels/{vessel_name}/{m}/{'/'.join(rest_parts)}".rstrip("/"))
                # 4. Direct Vessel/Main/rest
                canonical_candidates.append(f"{vessel_name}/{m}/{'/'.join(rest_parts)}".rstrip("/"))

        canonical_candidates.append("/".join(parts))
        canonical_candidates = list(dict.fromkeys(c for c in canonical_candidates if c))
        normalized = canonical_candidates[0]


        drive_id = await self._drive()

        async def valid_cached_folder(folder_id: str, expected_path: str) -> bool:
            try:
                actual_path = await self._folder_path(drive_id, folder_id)
                actual = actual_path.strip("/").lower()
                expected = expected_path.strip("/").lower()
                return actual == expected or actual.endswith(f"/{expected}")
            except GraphError as e:
                # If Graph read validation is temporarily denied, do not reject
                # known DB mappings. This avoids false "path not found" errors
                # during OCR move operations.
                if e.status in (401, 403):
                    log.warning(
                        "resolve_path: Graph validation unavailable (%s) for cached id=%s path=%s; trusting cache",
                        e.status,
                        folder_id,
                        expected_path,
                    )
                    return True
                log.warning("resolve_path: stale folder cache entry id=%s path=%s", folder_id, expected_path)
                return False
            except Exception:
                log.warning("resolve_path: stale folder cache entry id=%s path=%s", folder_id, expected_path)
                return False

        with SessionLocal() as db:
            # 1. Exact canonical path match (case insensitive).
            for candidate in canonical_candidates:
                row = (
                    db.query(models.Folder)
                    .filter(func.lower(models.Folder.path) == candidate.lower())
                    .first()
                )
                if row and row.drive_item_id and await valid_cached_folder(row.drive_item_id, candidate):
                    return row.drive_item_id

            # Support legacy punctuation variants without losing vessel scope.
            scoped_candidates = []
            for candidate in canonical_candidates:
                scoped_candidates.extend([
                    candidate.replace("Flag & MPA", "Flag - MPA"),
                    candidate.replace("Flag & MPA", "Flag / MPA"),
                    candidate.replace("/", "-"),
                ])
            for candidate in scoped_candidates:
                row = (
                    db.query(models.Folder)
                    .filter(func.lower(models.Folder.path) == candidate.lower())
                    .first()
                )
                if row and row.drive_item_id and await valid_cached_folder(row.drive_item_id, candidate):
                    return row.drive_item_id

            # 2. Prefix / subfolder match
            leaf_row = (
                db.query(models.Folder)
                .filter(
                    (func.lower(models.Folder.path) == normalized.lower()) |
                    (func.lower(models.Folder.path).like(f"{normalized.lower()}/%")),
                    models.Folder.kind.in_(["leaf", "month_driven", "drawing_classifier", "month"])
                )
                .order_by(
                    models.Folder.name.in_(["To be Classified", "Other Drawings", "Other Manuals"]).desc(),
                    models.Folder.id.asc()
                )
                .first()
            )
            if leaf_row and leaf_row.drive_item_id and await valid_cached_folder(leaf_row.drive_item_id, normalized):
                return leaf_row.drive_item_id

            # 3. Match by leaf folder name only inside the requested vessel.
            # Never use a global leaf lookup: pool slots share the same leaf
            # names and may otherwise receive the upload.
            if len(parts) >= 2:
                leaf_name = parts[-1].lower()
                # New structure: {MainFolder}/{VesselName}/...
                # parts[0] = main folder, parts[1] = vessel name
                if parts[0].lower() in {m.lower() for m in template.MAIN_FOLDERS}:
                    vessel_prefix = f"{parts[0].lower()}/{parts[1].lower()}/"
                else:
                    # Fallback for legacy paths
                    vessel_prefix = f"{template.VESSELS_ROOT.lower()}/{template.SPECIFIC_VESSELS_ROOT.lower()}/{parts[0].lower()}/"
                fuzzy_leaf = (
                    db.query(models.Folder)
                    .filter(
                        func.lower(models.Folder.name).in_([leaf_name, "flag - mpa", "flag / mpa"]),
                        func.lower(models.Folder.path).like(f"{vessel_prefix}%"),
                        models.Folder.drive_item_id.isnot(None)
                    )
                    .order_by(models.Folder.id.desc())
                    .first()
                )
                if fuzzy_leaf and fuzzy_leaf.drive_item_id and await valid_cached_folder(fuzzy_leaf.drive_item_id, fuzzy_leaf.path):
                    return fuzzy_leaf.drive_item_id

            # 4. On-demand auto-reprovision if vessel folder structure is missing
            for part in parts:
                vessel = db.query(models.Vessel).filter(func.lower(models.Vessel.name) == part.lower()).first()
                if vessel:
                    try:
                        active_site = (settings.active_site or "dev").lower()
                        from .site_provisioning import get_available_provisioning_sites
                        avail = get_available_provisioning_sites(db)
                        if active_site not in avail:
                            log.warning("resolve_path: Active site '%s' is disabled for provisioning; skipping auto-reprovision for vessel %s", active_site, vessel.name)
                            break

                        log.info("resolve_path: Folder missing for path %r — auto-reprovisioning vessel %s (%s)", path, vessel.id, vessel.name)
                        await self.reprovision_vessel(str(vessel.id))
                        with SessionLocal() as db2:
                            row = db2.query(models.Folder).filter(func.lower(models.Folder.path) == normalized.lower()).first()
                            if row and row.drive_item_id:
                                return row.drive_item_id
                            leaf_row = (
                                db2.query(models.Folder)
                                .filter(
                                    (func.lower(models.Folder.path) == normalized.lower()) |
                                    (func.lower(models.Folder.path).like(f"{normalized.lower()}/%")),
                                    models.Folder.kind.in_(["leaf", "month_driven", "drawing_classifier", "month"])
                                )
                                .first()
                            )
                            if leaf_row and leaf_row.drive_item_id:
                                return leaf_row.drive_item_id
                    except Exception as reprov_err:
                        log.warning("resolve_path: Auto-reprovision failed for vessel %s: %s", vessel.id, reprov_err)
                    break

        # 5. Live Graph walking lookup in SharePoint if not found in database cache
        try:
            for candidate in canonical_candidates:
                candidate_segments = [s.strip() for s in candidate.split("/") if s.strip()]
                curr_item_id = await gd.get_root_item_id(drive_id)
                match_failed = False
                for seg in candidate_segments:
                    child = await gd.find_child(drive_id, curr_item_id, seg)
                    if not child:
                        norm_seg = seg.lower().replace("-", "").replace("/", "").replace(" ", "").replace("&", "and")
                        children = await gd.list_children(drive_id, curr_item_id)
                        found = None
                        for c in children:
                            if c.get("folder"):
                                c_norm = c.get("name", "").lower().replace("-", "").replace("/", "").replace(" ", "").replace("&", "and")
                                if c_norm == norm_seg or norm_seg in c_norm or c_norm in norm_seg:
                                    found = c
                                    break
                        if found:
                            child = found
                        else:
                            match_failed = True
                            break
                    curr_item_id = child["id"]
                if not match_failed and curr_item_id:
                    return curr_item_id
        except Exception as graph_lookup_err:
            log.warning("resolve_path: Live Graph walking failed for %r: %s", path, graph_lookup_err)

        raise NotFound(
            f"No folder found for path '{path}'. It may not have been "
            f"provisioned yet, or the path is incorrect."
        )

    async def children(self, folder_id):
        drive_id = await self._drive()
        resolved_folder_id = folder_id
        try:
            parent_path = await self._folder_path(drive_id, resolved_folder_id)
            items = await gd.list_children(drive_id, resolved_folder_id)
        except GraphError as first_error:
            # Folder IDs cached before a SharePoint move/site switch become
            # invalid. Recover through the stable logical DB path once before
            # surfacing a 404 to the Documents module.
            if first_error.status != 404 or not settings.db_configured:
                raise
            try:
                with SessionLocal() as db:
                    stale = db.query(models.Folder).filter_by(drive_item_id=folder_id).one_or_none()
                    stale_path = stale.path if stale else None
                if not stale_path:
                    raise first_error
                resolved_folder_id = await self.resolve_path(stale_path)
                parent_path = await self._folder_path(drive_id, resolved_folder_id)
                items = await gd.list_children(drive_id, resolved_folder_id)
                with SessionLocal() as db:
                    current = db.query(models.Folder).filter_by(drive_item_id=folder_id).one_or_none()
                    if current:
                        current.drive_item_id = resolved_folder_id
                        db.commit()
                log.info("children: recovered stale folder id %s as %s via path %s", folder_id, resolved_folder_id, stale_path)
            except Exception:
                raise first_error

        try:
            async def with_tags(item):
                try:
                    fields = await graph().get(
                        f"/drives/{drive_id}/items/{item['id']}/listItem/fields"
                    )
                except GraphError:
                    fields = {}
                normalized = {
                    str(k).strip().lower().replace("_", "").replace(" ", ""): str(v or "").strip()
                    for k, v in fields.items()
                }
                def field(*names):
                    return next((normalized.get(n.lower().replace("_", "").replace(" ", ""), "") for n in names if normalized.get(n.lower().replace("_", "").replace(" ", ""), "")), "")
                return {**item, "tags": {
                    "department": field("Department", "DMS_Department"),
                    "vessel": field("VesselName", "Vessel Name", "vessel"),
                    "group": field("Group", "DMS_Group"),
                    "category": field("Category", "DMS_Category"),
                    "sub_category": field("SubCategory", "DMS_SubCategory", "sub_category"),
                }}
            items = await asyncio.gather(*(with_tags(item) for item in items))
        except GraphError as e:
            if e.status in (404, 400):
                with SessionLocal() as db:
                    stale = db.query(models.Folder).filter_by(drive_item_id=folder_id).one_or_none()
                    if stale:
                        db.delete(stale)
                        db.commit()
                raise NotFound(
                    f"Folder '{folder_id}' could not be found in SharePoint. It may have been deleted or moved. Please navigate back and refresh."
                )
            raise

        parts = parent_path.split("/") if parent_path else []

        parent_parts = parts
        out = []

        # Ship folders are direct children of Main Folders (depth 1)
        # Legacy support: also children of "Vessels/Specific Vessels" (depth 2)
        is_main_level = (
            (len(parts) == 1 and parts[0] in template.MAIN_FOLDERS)
            or (len(parts) == 2 and parts[0] == "Vessels" and parts[1] == "Specific Vessels")
        )

        with SessionLocal() as db:
            parent_row = self._folder_by_item(db, folder_id)
            parent_vessel_id = parent_row.vessel_id if parent_row else None

            # Only load ship->vessel name map when listing children under Main Folders / Specific Vessels
            ship_id_to_name: dict[str, str] = {}
            if is_main_level:
                ship_rows = (
                    db.query(models.Folder.drive_item_id, models.Vessel.name)
                    .join(models.Vessel, models.Folder.vessel_id == models.Vessel.id)
                    .filter(models.Folder.kind == "ship")
                    .all()
                )
                ship_id_to_name = {
                    row.drive_item_id: row.name for row in ship_rows if row.drive_item_id
                }

            for it in items:
                sharepoint_name = it["name"]
                if "folder" in it:
                    child_parts = parent_parts + [sharepoint_name]
                    flags = self._classify_with_vessel_upgrade(db, child_parts, classify(child_parts))
                    vessel_id = parent_vessel_id

                    if flags["kind"] == "ship":
                        existing = (
                            db.query(models.Folder)
                            .filter_by(drive_item_id=it["id"])
                            .one_or_none()
                        )
                        if existing and existing.vessel_id:
                            vessel_id = existing.vessel_id

                    self._upsert(
                        db, "/".join(child_parts), sharepoint_name, flags["kind"], it["id"],
                        flags["month_driven"], vessel_id,
                    )

                    display_name = sharepoint_name
                    if flags["kind"] == "ship" and it["id"] in ship_id_to_name:
                        display_name = ship_id_to_name[it["id"]]

                    node = {
                        "id": it["id"],
                        "name": display_name,
                        "tags": it.get("tags", {}),
                        **flags,
                        "has_children": (it.get("folder") or {}).get("childCount", 0) > 0,
                    }
                else:
                    ext = sharepoint_name.rsplit(".", 1)[-1].lower() if "." in sharepoint_name else ""
                    node = {
                        "id": it["id"],
                        "name": sharepoint_name,
                        "tags": it.get("tags", {}),
                        "kind": "file",
                        "upload": False,
                        "month_driven": False,
                        "has_children": False,
                        "ext": ext,
                        "size": it.get("size"),
                        "modified": it.get("lastModifiedDateTime"),
                    }
                out.append(node)
            db.commit()
        return out

    def _derive_single_site_scan(self, site_cache_key: str) -> dict | None:
        """Slice one site's rows out of the cached "all sites" dashboard
        scan, for _dashboard_site_scan's cold-start fast path when the Home
        page's site filter switches to a site that has no scan cached under
        its own key yet. Returns None if there's no "all" data to slice
        from, or it doesn't (yet) contain a matching site — the caller falls
        back to a real scan in that case."""
        all_cached = _DASHBOARD_SCAN_CACHE.get("all")
        if all_cached is None:
            return None
        data = all_cached["data"]
        matching_sites = [
            s for s in (data.get("sites") or [])
            if site_alias_matches(s.get("site_key") or "", site_cache_key)
        ]
        if not matching_sites:
            return None
        matching_keys = {s.get("site_key") for s in matching_sites}
        docs = [d for d in (data.get("docs") or []) if d.get("site") in matching_keys]
        return {
            "docs": docs,
            "sites": matching_sites,
            "total_files": sum(s.get("files", 0) for s in matching_sites),
            "total_folders": sum(s.get("folders", 0) for s in matching_sites),
            "total_vessels": sum(s.get("vessels", 0) for s in matching_sites),
            "truncated": any(s.get("truncated") for s in matching_sites),
        }

    def _dashboard_pending_scan(self, site_key: str | None) -> dict:
        """Return site metadata while a cold Graph scan runs in the background."""
        requested = (site_key or "").strip().lower()
        infos = _all_site_infos(include_protected=True, allow_protected=True)
        if requested and requested != "all":
            infos = [
                info for info in infos
                if site_alias_matches(info.get("site_key"), requested)
            ]
        sites = [{
            "site_key": info["site_key"],
            "site_name": info.get("site_name") or info["site_key"],
            "drive_id": info.get("drive_id") or "",
            "web_url": info.get("web_url") or "",
            "files": 0,
            "folders": 0,
            "vessels": 0,
            "last_modified_epoch": None,
            "truncated": False,
            "error": None,
            "stats_pending": True,
        } for info in infos]
        return {"docs": [], "sites": sites, "total_files": 0, "total_folders": 0, "total_vessels": 0, "truncated": False}

    async def _dashboard_site_scan(self, force_refresh: bool = False, site_key: str | None = None) -> dict:
        """Scan the SharePoint drive(s) behind the Home dashboard, ONE shared
        pass for both the counters and the paged document browser.

        Per drive it pages through Graph's drive-wide search (the old code read
        only the first page of 500 items and ignored folders, so "Total
        Documents" was capped and there was no folder count). Bounded by
        _DASHBOARD_MAX_ITEMS_PER_DRIVE so one huge library can't stall the
        dashboard; `truncated` tells the UI when that bound was hit.

        Returns {"docs": [...every file, newest first...], "sites": [...per
        site counts...], "total_files", "total_folders", "truncated"}.
        Cached per site_key for CACHE_TTL_DASHBOARD_STATS seconds.
        """
        cache_key = (site_key or "").strip().lower() or "all"
        now_ts = time.time()
        cached = _DASHBOARD_SCAN_CACHE.get(cache_key)
        fresh = cached is not None and (now_ts - cached["timestamp"]) < CACHE_TTL_DASHBOARD_STATS
        if not force_refresh and fresh:
            return cached["data"]
        # The Home page asks for stats and for the documents page at the same
        # moment; let the second caller share the scan already running
        # instead of walking the whole library twice.
        inflight = _DASHBOARD_SCAN_INFLIGHT.get(cache_key)

        if not force_refresh and cached is not None:
            # Stale-while-revalidate: a full scan across every configured
            # drive can take 30+ seconds once a library has thousands of
            # items, and used to block every Home-page load the instant the
            # 120s cache expired — even a plain "open the dashboard" request
            # was paying for a live SharePoint walk. The background job
            # (scheduler.refresh_dashboard_stats_cache, every 100s) already
            # keeps this cache warm in normal operation; this path exists so
            # a slow/late refresh (or a cold start right after a restart, on
            # the second request onward) never makes the *user* wait — stale
            # counts are served immediately and a refresh is kicked off
            # behind the scenes, without blocking this request on it.
            if inflight is None or inflight.done():
                refresh_task = asyncio.ensure_future(
                    self._dashboard_site_scan_uncached(cache_key, site_key)
                )
                _DASHBOARD_SCAN_INFLIGHT[cache_key] = refresh_task

                def _log_refresh_result(t: "asyncio.Future", key: str = cache_key) -> None:
                    if _DASHBOARD_SCAN_INFLIGHT.get(key) is t:
                        _DASHBOARD_SCAN_INFLIGHT.pop(key, None)
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        log.warning(
                            "dashboard scan: background refresh for site=%s failed: %s",
                            key, exc,
                        )

                refresh_task.add_done_callback(_log_refresh_result)
            return cached["data"]

        # Switching the Home page's site filter to one that has never been
        # scanned under its own cache key used to always fall through to a
        # full live scan of just that drive — even though the "All
        # SharePoint Sites" view had usually already walked this exact
        # drive moments earlier as part of its own aggregate scan. Slice
        # this site's rows out of that "all" data instead: the filter
        # switch becomes instant, and a real per-site scan still happens
        # in the background (via the normal stale-while-revalidate path,
        # triggered the next time this cache_key is read) to correct it if
        # the aggregate was itself stale.
        if not force_refresh and cache_key != "all" and cached is None:
            derived = self._derive_single_site_scan(cache_key)
            if derived is not None:
                all_cached = _DASHBOARD_SCAN_CACHE.get("all")
                derived_ts = all_cached["timestamp"] if all_cached is not None else 0.0
                _DASHBOARD_SCAN_CACHE[cache_key] = {"data": derived, "timestamp": derived_ts}
                return derived

        # A cold scan can take longer than the browser request timeout for a
        # large SharePoint library. Return the configured site rows now and
        # let the shared scan fill the cache for the next request.
        if not force_refresh and cached is None:
            if inflight is None or inflight.done():
                task = asyncio.ensure_future(self._dashboard_site_scan_uncached(cache_key, site_key))
                _DASHBOARD_SCAN_INFLIGHT[cache_key] = task

                def _log_cold_result(t: "asyncio.Future", key: str = cache_key) -> None:
                    if _DASHBOARD_SCAN_INFLIGHT.get(key) is t:
                        _DASHBOARD_SCAN_INFLIGHT.pop(key, None)
                    if not t.cancelled() and t.exception() is not None:
                        log.warning("dashboard scan: cold background refresh for site=%s failed: %s", key, t.exception())

                task.add_done_callback(_log_cold_result)
            return self._dashboard_pending_scan(site_key)

        # No usable cache yet (first call ever) or the caller explicitly
        # asked for fresh data (force_refresh=true, e.g. the Refresh
        # button) — there's nothing to serve immediately, so wait for a
        # real scan.
        if inflight is not None and not inflight.done():
            return await asyncio.shield(inflight)
        task = asyncio.ensure_future(self._dashboard_site_scan_uncached(cache_key, site_key))
        _DASHBOARD_SCAN_INFLIGHT[cache_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if _DASHBOARD_SCAN_INFLIGHT.get(cache_key) is task and task.done():
                _DASHBOARD_SCAN_INFLIGHT.pop(cache_key, None)

    async def _dashboard_site_scan_uncached(self, cache_key: str, site_key: str | None) -> dict:
        gen_at_start = _DASHBOARD_CACHE_GENERATION
        with SessionLocal() as db:
            vessel_objs = db.query(models.Vessel).all()
            known_vessels = {v.name.strip().lower(): v.name for v in vessel_objs}

        now_dt = datetime.now(timezone.utc)

        # (site_key, site display name, drive_id, web_url) for every drive to
        # scan — one site's drive, or every configured site ("All Sites").
        # Several site keys can alias the same drive; each drive is scanned
        # and counted once.
        requested_site = (site_key or "").strip().lower()
        targets: list[dict] = []
        blocked_sites: list[dict] = []
        if settings.graph_configured:
            seen_drives: set[str] = set()
            with allow_protected_reads():
                display_infos = _all_site_infos(include_protected=True, allow_protected=True)
            for info in display_infos:
                if info.get("scan_blocked"):
                    if (
                        not requested_site or requested_site == "all"
                        or site_alias_matches(info["site_key"], requested_site)
                    ):
                        blocked_sites.append(info)
                    continue
                if not info["drive_id"] or info["drive_id"] in seen_drives:
                    continue
                if requested_site and requested_site != "all" and not site_alias_matches(info["site_key"], requested_site):
                    continue
                seen_drives.add(info["drive_id"])
                targets.append(info)
            if not targets and not blocked_sites:
                # No registered site matched (or none are registered yet) —
                # fall back to the default configured drive rather than
                # silently showing zero documents.
                targets = [{
                    "site_key": requested_site if requested_site and requested_site != "all" else (settings.active_site or "default"),
                    "site_name": requested_site or "SharePoint",
                    "drive_id": await self._drive(),
                    "web_url": "",
                }]

        # Scan sites that have no real counts yet (just added / never scanned)
        # BEFORE already-known sites: their numbers are what the user is
        # waiting on, and the known sites already show cached counts.
        _known_all = _DASHBOARD_SCAN_CACHE.get("all")
        _known_keys = {
            s.get("site_key") for s in ((_known_all or {}).get("data", {}).get("sites") or [])
            if not s.get("stats_pending") and not s.get("error")
        }
        targets.sort(key=lambda tg: tg.get("site_key") in _known_keys)

        # Bounds concurrent Graph requests across ALL sites scanned in this
        # call, not just within one site's tree-walk. Scanning "All Sites"
        # fires up to 4 tree-walk workers PER site via asyncio.gather, so 3
        # sites could burst 12+ simultaneous Graph requests — that burst is
        # what trips SharePoint's 429 throttle, which then marks the shared
        # GraphClient as quota-exhausted and makes every other concurrently-
        # scanning site fail instantly too (see GraphClient._throttled_until
        # in graph/client.py). Sharing one semaphore across every site's
        # search + tree-walk calls keeps total in-flight requests bounded
        # regardless of how many sites are being scanned at once.
        _graph_call_sem = asyncio.Semaphore(4)

        def _item_folder_parts(f: dict) -> list[str]:
            """Folder segments (library-relative) an item sits in."""
            parent_path = ((f.get("parentReference") or {}).get("path") or "")
            if "root:" in parent_path:
                rel = unquote(parent_path.split("root:", 1)[1]).strip("/")
                return [p.strip() for p in rel.split("/") if p.strip()]
            # Search results often omit parentReference.path; fall back to
            # the item's own URL (…/sites/<site>/<library>/<folders…>/<name>).
            # Office files' webUrl points at _layouts/Doc.aspx and carries no
            # folder path, so those come back without one.
            url = f.get("webUrl") or ""
            if not url or "/_layouts/" in url:
                return []
            path = unquote(urlparse(url).path)
            segs = [s for s in path.split("/") if s]
            if len(segs) >= 3 and segs[0].lower() in ("sites", "teams"):
                rest = segs[3:]
            else:
                rest = segs[1:]
            return rest[:-1]

        def _user_name(identity: dict | None) -> str:
            user = (identity or {}).get("user") or {}
            return str(user.get("displayName") or user.get("email") or "").strip()

        async def _scan_drive(target: dict) -> dict:
            drive_id = target["drive_id"]
            docs: list[dict] = []
            folders = 0
            items_seen = 0
            truncated = False
            last_epoch = 0
            scan_error: str | None = None

            # Admin-chosen vessel folders for this library: a file below
            # "<chosen folder>/<vessel>/…" belongs to that vessel even when
            # the vessel has no DB row yet (vessels found in SharePoint).
            def _norm_seg(s: str) -> str:
                return " ".join(s.split()).casefold()

            from . import vessel_roots as _vr
            _roots_cfg = _vr.get_for_drive(drive_id)
            root_prefixes = [
                [_norm_seg(p) for p in path.split("/") if p.strip()]
                for path in ((_roots_cfg or {}).get("paths") or [])
            ] if (_roots_cfg or {}).get("mode") == "folders" else []

            def _vessel_from_roots(parts: list[str]) -> str:
                normed = [_norm_seg(p) for p in parts]
                for prefix in root_prefixes:
                    if prefix and len(normed) > len(prefix) and normed[:len(prefix)] == prefix:
                        return parts[len(prefix)]
                return ""

            def _process_item(f: dict) -> None:
                nonlocal folders, last_epoch
                name = f.get("name")
                if not name:
                    return
                if "folder" in f:
                    folders += 1
                    _DASHBOARD_LIVE_PROGRESS[target["site_key"]] = {"files": len(docs), "folders": folders}
                    return
                if "file" not in f:
                    return
                if len(docs) % 50 == 0:
                    _DASHBOARD_LIVE_PROGRESS[target["site_key"]] = {"files": len(docs), "folders": folders}
                parts = _item_folder_parts(f)
                group = parts[0] if parts else (target.get("site_name") or "Shared Documents")

                vessel = ""
                for part in parts:
                    if part.lower() in known_vessels:
                        vessel = known_vessels[part.lower()]
                        break
                if not vessel and root_prefixes:
                    vessel = _vessel_from_roots(parts)

                doc_type = parts[-1] if parts else "Document"
                sub_folder_path = " > ".join(parts) if parts else group

                def _parse(value: str | None) -> datetime:
                    try:
                        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else now_dt
                    except Exception:
                        return now_dt

                mod_dt = _parse(f.get("lastModifiedDateTime") or f.get("createdDateTime"))
                created_dt = _parse(f.get("createdDateTime") or f.get("lastModifiedDateTime"))
                mod_epoch = int(mod_dt.timestamp() * 1000)
                last_epoch = max(last_epoch, mod_epoch)

                size_b = f.get("size", 0) or 0
                if size_b < 1024 * 1024:
                    size_str = f"{size_b / 1024:.1f} KB"
                else:
                    size_str = f"{size_b / (1024 * 1024):.1f} MB"
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

                docs.append({
                    "id": f.get("id") or name,
                    "name": name,
                    "ext": ext,
                    "fileType": _dashboard_file_type(ext),
                    "vessel": vessel or "Not Listed",
                    "type": doc_type,
                    "site": target["site_key"],
                    "siteName": target.get("site_name") or target["site_key"],
                    "modified": mod_dt.strftime("%b %d, %Y"),
                    "modifiedEpoch": mod_epoch,
                    "createdEpoch": int(created_dt.timestamp() * 1000),
                    "modifiedBy": _user_name(f.get("lastModifiedBy")),
                    "createdBy": _user_name(f.get("createdBy")),
                    "fileSize": size_str,
                    "sizeBytes": size_b,
                    "subFolderPath": sub_folder_path,
                    "webUrl": f.get("webUrl", ""),
                })

            async def _scan_via_search() -> int:
                """Fast path: Graph's drive-wide search in one flat pass.
                Returns how many items it saw (0 doesn't necessarily mean the
                drive is empty — see the fallback below)."""
                nonlocal truncated
                seen = 0
                g = graph()
                url = (
                    f"/drives/{drive_id}/root/search(q='')"
                    "?$select=id,name,size,file,folder,lastModifiedDateTime,createdDateTime,webUrl,parentReference,createdBy,lastModifiedBy"
                    "&$top=999"
                )
                while url:
                    async with _graph_call_sem:
                        res = await g.get(url)
                    items = res.get("value", []) if isinstance(res, dict) else []
                    for f in items:
                        seen += 1
                        _process_item(f)
                    if seen >= _DASHBOARD_MAX_ITEMS_PER_DRIVE:
                        truncated = bool(res.get("@odata.nextLink")) if isinstance(res, dict) else False
                        break
                    url = res.get("@odata.nextLink") if isinstance(res, dict) else None
                return seen

            async def _scan_via_tree_walk() -> int:
                """Fallback: walk /children recursively from the drive root.

                search(q='') depends on the site's SharePoint search index,
                which can lag behind for a newly connected or lightly-used
                site — the drive comes back with zero hits even though it
                visibly has files in SharePoint (this is what was happening
                to NissenKaiunExternal: 0/0 counted while its document
                library was full). Walking /children instead reads the
                drive's actual folder tree, so it's slower but always
                correct — used only when the search path found nothing.

                Folders are walked with a small bounded worker pool instead
                of one folder at a time — the previous serial
                `while queue: ... await g.get(...)` loop issued exactly one
                Graph request at a time, so a site with a few hundred
                folders (or one hit by search-index lag right after a site
                switch) could take minutes to finish, during which the
                dashboard/search endpoint kept returning scan_pending=true
                and the frontend kept polling. 4 concurrent workers mirrors
                the frontend's own _prefetchSiteSubtree concurrency cap
                (VesselEmail.tsx) instead of adding more parallel load than
                folder browsing already does against the same drive.
                """
                nonlocal folders, truncated
                folders = 0
                docs.clear()
                seen = 0
                g = graph()
                queue: asyncio.Queue = asyncio.Queue()
                queue.put_nowait("root")

                async def _worker() -> None:
                    nonlocal seen, truncated
                    while True:
                        folder_id = await queue.get()
                        try:
                            if seen >= _DASHBOARD_MAX_ITEMS_PER_DRIVE:
                                truncated = True
                                continue
                            url = (
                                f"/drives/{drive_id}/items/{folder_id}/children"
                                "?$select=id,name,size,file,folder,lastModifiedDateTime,createdDateTime,webUrl,parentReference,createdBy,lastModifiedBy"
                                "&$top=999"
                            )
                            while url:
                                async with _graph_call_sem:
                                    res = await g.get(url)
                                items = res.get("value", []) if isinstance(res, dict) else []
                                for f in items:
                                    seen += 1
                                    _process_item(f)
                                    if "folder" in f and f.get("id"):
                                        await queue.put(f["id"])
                                if seen >= _DASHBOARD_MAX_ITEMS_PER_DRIVE:
                                    truncated = True
                                    break
                                url = res.get("@odata.nextLink") if isinstance(res, dict) else None
                        finally:
                            queue.task_done()

                workers = [asyncio.create_task(_worker()) for _ in range(4)]
                try:
                    await queue.join()
                finally:
                    for w in workers:
                        w.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                return seen

            async def _try_reresolve_stale_drive() -> bool:
                """Both the search and the tree-walk agreed: this drive_id
                has nothing in it. That can mean the drive really is empty,
                or it can mean site_configurations.drive_id is stale (e.g.
                pointing at a document library that was recreated, or the
                wrong one entirely — NissenKaiunExternal's audit report
                showed a drive_id here that didn't match the one Graph
                itself resolves for the site). Cross-check against Graph's
                own /sites/{site_id}/drives, and if it names a *different*
                drive, rescan that one. If it has content, self-heal by
                writing the corrected drive_id back to site_configurations
                so future scans don't need to re-resolve."""
                nonlocal drive_id, truncated
                site_id = (target.get("site_id") or "").strip()
                if not site_id or "," not in site_id:
                    return False
                try:
                    g = graph()
                    page = await g.get(f"/sites/{quote(site_id, safe='')}/drives")
                    candidates = [d for d in (page.get("value") or []) if d.get("id")]
                except Exception as e:
                    log.info(
                        "dashboard scan: drive re-resolution failed for site=%s: %s",
                        target.get("site_key"), e,
                    )
                    return False
                real_drive = next(
                    (d for d in candidates if (d.get("name") or "").strip().lower() == "documents"),
                    None,
                ) or next((d for d in candidates if d.get("id") != drive_id), None)
                if not real_drive or real_drive["id"] == drive_id:
                    return False
                new_drive_id = real_drive["id"]
                log.warning(
                    "dashboard scan: site_configurations.drive_id looks stale for site=%s "
                    "(configured=%s, actual=%s) — rescanning the correct drive",
                    target.get("site_key"), drive_id, new_drive_id,
                )
                drive_id = new_drive_id
                seen = await _scan_via_search()
                if seen == 0:
                    seen = await _scan_via_tree_walk()
                if seen == 0:
                    return False
                try:
                    with SessionLocal() as db:
                        rec = db.query(models.SiteConfiguration).filter_by(
                            site_key=target["site_key"]
                        ).one_or_none()
                        if rec:
                            rec.drive_id = new_drive_id
                            db.commit()
                            log.warning(
                                "dashboard scan: updated site_configurations.drive_id for site=%s to %s",
                                target.get("site_key"), new_drive_id,
                            )
                except Exception as e:
                    log.warning(
                        "dashboard scan: could not persist corrected drive_id for site=%s: %s",
                        target.get("site_key"), e,
                    )
                return True

            async def _scan_once() -> None:
                nonlocal items_seen
                _DASHBOARD_LIVE_PROGRESS[target["site_key"]] = {"files": 0, "folders": 0}
                if not drive_id:
                    raise ValueError("no drive_id configured for this site")
                items_seen = await _scan_via_search()
                if items_seen == 0:
                    log.info(
                        "dashboard scan: search returned 0 items for site=%s drive=%s — "
                        "falling back to a recursive folder walk",
                        target.get("site_key"), drive_id,
                    )
                    items_seen = await _scan_via_tree_walk()
                if items_seen == 0:
                    await _try_reresolve_stale_drive()

            try:
                try:
                    await _scan_once()
                except GraphError as first_exc:
                    # Throttled: wait out Graph's own Retry-After (bounded)
                    # and try this site once more instead of immediately
                    # reporting "Unable to load". Sites are scanned one at a
                    # time in the background, so waiting here doesn't block
                    # the Home page (it's serving the cached snapshot).
                    if getattr(first_exc, "status", None) not in (429, 503):
                        raise
                    wait = getattr(first_exc, "retry_after", None) or 30.0
                    wait = min(max(float(wait), 5.0), 90.0)
                    log.info(
                        "dashboard scan: site=%s throttled — retrying in %.0fs",
                        target.get("site_key"), wait,
                    )
                    await asyncio.sleep(wait + random.random())
                    docs.clear()
                    folders = 0
                    last_epoch = 0
                    truncated = False
                    items_seen = 0
                    await _scan_once()
            except Exception as e:
                # Previously this was swallowed and the site came back with
                # files=0/folders=0 — indistinguishable from a genuinely
                # empty site (e.g. NissenKaiunExternal showing 0/0 with no
                # way to tell that from a permission/config problem). Now the
                # failure is logged AND carried in the result so the Home
                # page can render "Error" / "Unable to load" instead of 0.
                status = getattr(e, "status", None)
                healed = False
                if isinstance(e, GraphError) and status == 404:
                    # A hard 404 on the drive itself (not "0 items") usually
                    # means site_configurations.drive_id is pointing at a
                    # document library that no longer exists — recreated,
                    # renamed, or the site provisioned again under a new
                    # library. _try_reresolve_stale_drive() already heals
                    # exactly this for the "0 items, no error" case; a 404
                    # never reached that branch because the exception short-
                    # circuited the try block before items_seen==0 was ever
                    # checked. Attempt the same self-heal here before giving
                    # up, so a stale drive_id doesn't permanently show
                    # "Unable to load" until someone manually fixes the DB row.
                    try:
                        healed = await _try_reresolve_stale_drive()
                    except Exception as heal_exc:
                        log.warning(
                            "dashboard scan: re-resolution after 404 also failed for site=%s: %s",
                            target.get("site_key"), heal_exc,
                        )
                if not healed:
                    if isinstance(e, GraphError) and status in (401, 403):
                        scan_error = "Permission denied — check the app's Graph access to this site's drive"
                    elif isinstance(e, GraphError) and status == 404:
                        scan_error = (
                            "Configured document library not found — its drive_id looks stale "
                            "(recreated or renamed library). Re-link this site in Site Management."
                        )
                    elif isinstance(e, GraphError) and status in (429, 503):
                        scan_error = "SharePoint is throttling requests — try refreshing again shortly"
                    elif isinstance(e, GraphError):
                        scan_error = f"SharePoint request failed (status {status})"
                    elif isinstance(e, ValueError):
                        scan_error = str(e)
                    else:
                        scan_error = "Could not reach this site's document library"
                    log.warning(
                        "dashboard scan: failed for site=%s drive=%s: %s",
                        target.get("site_key"), drive_id, e,
                    )
            vessel_count = 0
            vessel_names: list[str] = []
            if not scan_error:
                try:
                    vessel_count, vessel_names = await _term_store_vessel_folder_count(
                        {**target, "drive_id": drive_id}
                    )
                except Exception as e:
                    log.warning(
                        "dashboard scan: vessel term-store cross-match failed for site=%s: %s",
                        target.get("site_key"), e,
                    )
            _DASHBOARD_LIVE_PROGRESS.pop(target["site_key"], None)
            return {
                "site_key": target["site_key"],
                "site_name": _dashboard_site_label(target["site_key"], target.get("site_name")),
                "drive_id": drive_id,
                "web_url": target.get("web_url") or "",
                "files": len(docs),
                "folders": folders,
                "vessels": vessel_count,
                # Official Term Store labels behind that count, so callers
                # like list_vessels() can merge real SharePoint vessels
                # (folders matching the term store) into the Vessel
                # Management list without a second scan.
                "vessel_names": vessel_names,
                "last_modified_epoch": last_epoch or None,
                "truncated": truncated,
                "error": scan_error,
                "_docs": docs,
            }

        # Sequential, not asyncio.gather: all sites share ONE GraphClient
        # (graph() is called without a site name), so that client's quota-
        # exhausted flag (GraphClient._throttled_until) is process-wide, not
        # per-site. Scanning sites concurrently meant one site tripping the
        # quota killed every OTHER site's in-flight request too — including
        # ones that were seconds from finishing with real data. That's why
        # NissenKaiunExternal was showing "Unable to load" with a throttling
        # error even though it had already fetched its real 339 files / 1160
        # folders: a later page of its own pagination (or NKSDocMan's
        # concurrently-running scan) tripped the shared flag and killed the
        # remaining in-flight call, discarding an otherwise-successful scan.
        # Scanning one site at a time means a trip only affects the site
        # that's actually running when it happens, and by the time the next
        # site's scan starts, some of the cooldown has already elapsed. This
        # is a background scan (the caller already has stale data to show
        # meanwhile via stale-while-revalidate), so the slower wall-clock
        # time here is a good trade for not corrupting unrelated sites.
        with allow_protected_reads():
            per_site = [await _scan_drive(t) for t in targets] if targets else []
        all_docs: list[dict] = []
        sites: list[dict] = []
        for s in per_site:
            all_docs.extend(s.pop("_docs"))
            sites.append(s)
        for info in blocked_sites:
            sites.append({
                "site_key": info["site_key"],
                "site_name": info["site_name"],
                "drive_id": info.get("drive_id") or "",
                "web_url": info.get("web_url") or "",
                "files": 0,
                "folders": 0,
                "vessels": 0,
                "vessel_names": [],
                "last_modified_epoch": None,
                "truncated": False,
                "error": "Protected site — dashboard scanning is available only in production",
                "_docs": [],
            })
        all_docs.sort(key=lambda d: d["modifiedEpoch"], reverse=True)
        sites.sort(key=lambda s: (s["site_name"] or "").lower())

        data = {
            "docs": all_docs,
            "sites": sites,
            "total_files": len(all_docs),
            "total_folders": sum(s["folders"] for s in sites),
            "total_vessels": sum(s.get("vessels", 0) for s in sites),
            "truncated": any(s["truncated"] for s in sites),
        }
        # Only cache this if nothing invalidated the site list while this
        # scan was running (a site added/hidden/removed mid-scan). Otherwise
        # this result was computed against a site list that's already out of
        # date, and caching it would silently undo the fix invalidation just
        # made (e.g. a site hidden mid-scan would reappear once this old
        # scan finishes and overwrites the cache). The caller who explicitly
        # awaited this (e.g. a force_refresh) still gets the data back —
        # just not cached — so it isn't wasted, only not trusted for later
        # reads.
        if any("throttl" in (s.get("error") or "").lower() for s in sites):
            global _DASHBOARD_LAST_THROTTLED_AT
            _DASHBOARD_LAST_THROTTLED_AT = time.time()
        if _DASHBOARD_CACHE_GENERATION == gen_at_start:
            completed_at = time.time()
            _DASHBOARD_SCAN_CACHE[cache_key] = {"data": data, "timestamp": completed_at}
            if cache_key == "all" and not data.get("truncated"):
                _persist_dashboard_scan(data, completed_at)
        return data

    async def get_dashboard_documents(self, force_refresh: bool = False, site_key: str | None = None) -> dict:
        """Every file behind the dashboard (newest first), for GET
        /api/dashboard/documents to filter and page server-side.

        Returns {"docs": [...], "pending": bool}. `pending` is true when
        this site_key has never completed a real scan yet and
        _dashboard_site_scan (see _dashboard_pending_scan) handed back an
        empty placeholder while the first scan runs in the background —
        NOT a genuine "this site has zero documents" result. Switching the
        "SharePoint site" dropdown to a site with no scan cached yet (its
        own key, and no "all" aggregate to slice a fast copy from either —
        see _derive_single_site_scan) hits exactly this path, so a search
        run immediately after switching used to come back empty even
        though the site genuinely has matching documents; giving the
        frontend a way to tell "still scanning" apart from "no matches"
        lets it retry once the real scan lands instead of treating the
        placeholder as a final, permanently-cached empty result (see
        _scheduleGlobalSearch/_triggerGlobalSearch in VesselEmail.tsx).
        """
        scan = await self._dashboard_site_scan(force_refresh=force_refresh, site_key=site_key)
        pending = any(s.get("stats_pending") for s in (scan.get("sites") or []))
        return {"docs": scan["docs"], "pending": pending}

    async def get_dashboard_stats(self, force_refresh: bool = False, site_key: str | None = None) -> dict:
        """Fast aggregated statistics for the Home/Dashboard module.

        When `site_key` is omitted (or "all"), figures are merged across
        every configured SharePoint site. When a specific site_key is given,
        the vessel count, file/folder counts, sites list and recent
        documents are all scoped to that one site — same site_key convention
        as GET /api/vessels?site_key=...

        The full document list is no longer embedded here (it could be
        thousands of rows); the Home page pages through it with
        GET /api/dashboard/documents instead.
        """
        cache_key = (site_key or "").strip().lower() or "all"
        now_ts = time.time()
        cached = _DASHBOARD_STATS_CACHE.get(cache_key)
        if not force_refresh and cached is not None and (now_ts - cached["timestamp"]) < CACHE_TTL_DASHBOARD_STATS:
            return cached["data"]

        scan = await self._dashboard_site_scan(force_refresh=force_refresh, site_key=site_key)
        all_docs = scan["docs"]

        # Overlay live running totals onto sites still being counted, so a
        # big new site shows "N+ files" right away instead of 0/blank.
        if any(s.get("stats_pending") for s in scan["sites"]):
            live_sites = []
            for s in scan["sites"]:
                prog = _DASHBOARD_LIVE_PROGRESS.get(s.get("site_key")) if s.get("stats_pending") else None
                live_sites.append({**s, "files": prog["files"], "folders": prog["folders"], "counting": True} if prog else s)
            scan = {
                **scan,
                "sites": live_sites,
                "total_files": sum(s.get("files", 0) for s in live_sites),
                "total_folders": sum(s.get("folders", 0) for s in live_sites),
            }

        # Vessel count scoped to the requested site: the number of the
        # site's ROOT-LEVEL folders whose name matches a Term Store vessel
        # term (managed metadata) — computed live as part of the same
        # per-site scan above (see _term_store_vessel_folder_count), not
        # read from the vessels table, so it reflects what's actually in
        # SharePoint right now and updates on site switch/refresh.
        total_vessels = scan.get("total_vessels", 0)

        # Active vessels = vessel folders (in the scanned site(s)) that hold at
        # least one document; the rest are vessels with no documents yet.
        docs_per_vessel: dict[str, int] = {}
        for d in all_docs:
            v = _normalize_term_label(d.get("vessel") or "")
            if v and v != "notlisted":
                docs_per_vessel[v] = docs_per_vessel.get(v, 0) + 1
        vessel_names_in_scope = {
            _normalize_term_label(str(n)) for s in scan["sites"] for n in (s.get("vessel_names") or []) if str(n).strip()
        }
        active_vessels = sum(1 for n in vessel_names_in_scope if docs_per_vessel.get(n))

        result = {
            # total_documents kept for older clients; same value as total_files.
            "total_documents": scan["total_files"],
            "total_files": scan["total_files"],
            "total_folders": scan["total_folders"],
            "total_sites": len(scan["sites"]),
            "total_vessels": total_vessels,
            "active_vessels": active_vessels,
            "vessels_without_documents": max(len(vessel_names_in_scope) - active_vessels, 0),
            "sites": scan["sites"],
            "truncated": scan["truncated"],
            "recent_documents": all_docs[:10],
            # So the Home page can show "Last refreshed at …" instead of
            # implying these numbers were just computed on this request —
            # most requests are served from cache (see CACHE_TTL_DASHBOARD_STATS).
            "last_refreshed_epoch": int(now_ts * 1000),
        }
        # A site added/hidden/removed moments ago can still be carrying
        # placeholder counts here (stats_pending — see
        # invalidate_dashboard_stats_cache) while its real scan runs in the
        # background. Caching that placeholder for the full TTL would pin
        # "Counting…" on screen for up to 2 minutes even after the real
        # numbers are ready underneath it — so skip the top-level cache
        # while anything is still pending; the scan-level cache underneath
        # already keeps repeat requests fast in the meantime.
        if not any(s.get("stats_pending") for s in scan["sites"]):
            _DASHBOARD_STATS_CACHE[cache_key] = {"data": result, "timestamp": now_ts}
        return result

    async def stats(self):
        dash = await self.get_dashboard_stats()
        with SessionLocal() as db:
            month_driven = db.query(models.Folder).filter_by(month_driven=True).count()
            months = db.query(models.Folder).filter_by(kind="month").count()
        return {
            "vessels": dash["total_vessels"],
            "main_folders": len(template.MAIN_FOLDERS),
            "month_driven": month_driven,
            "months": months,
            "documents": dash["total_documents"],
            **dash,
        }


    # -------------------------------------------------------------- uploads
    async def _check_global_duplicate(
        self,
        drive_id: str,
        filename: str,
        target_folder_id: str,
        target_folder_path: str | None = None,
        vessel_id: int | None = None,
    ):
        """Check for a file with the same name elsewhere in the same vessel's
        folder tree.

        Previously this listed EVERY item in the entire container
        (/drives/{id}/list/items, paginated) on every single upload — a cost
        that scales with total documents across ALL vessels, not just this
        one, so every upload got progressively slower as the DMS grew.

        Now it uses Graph's server-side recursive search
        (gd.search_items_in) scoped to just this vessel's "ship" folders, so
        the cost scales with one vessel's document count instead of the
        whole container's. Falls back to the old full-container scan only
        when vessel_id is unknown (should be rare).

        Raises Conflict with a clear message when a duplicate is found.
        Any DB or Graph error is swallowed so infrastructure issues never
        block an upload.
        """
        from ..graph.client import graph
        from urllib.parse import quote

        try:
            existing = await gd.find_child(drive_id, target_folder_id, filename)
            if existing and "file" in existing:
                parts_folder = [p.strip() for p in (target_folder_path or "").split("/") if p.strip()]
                # Path: Vessels/Specific Vessels/{Ship}/{Main}/... → ship at index 2
                if len(parts_folder) >= 3:
                    msg = f"Duplicate file upload: '{filename}' already exists in folder '{parts_folder[-1]}' under vessel '{parts_folder[2]}'"
                else:
                    msg = f"Duplicate file upload: '{filename}' already exists in target folder"
                raise Conflict(msg)
        except Conflict:
            raise
        except Exception:
            pass  # degrade gracefully if Graph check fails

        items: list[dict] = []
        try:
            if vessel_id is not None:
                with SessionLocal() as db:
                    ship_folder_ids = [
                        row.drive_item_id
                        for row in db.query(models.Folder)
                        .filter_by(vessel_id=vessel_id, kind="ship")
                        .all()
                    ]
                if not ship_folder_ids:
                    return  # nothing provisioned for this vessel yet
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(
                            *[gd.search_items_in(drive_id, fid, filename) for fid in ship_folder_ids],
                            return_exceptions=True,
                        ),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "Duplicate scan timed out after 3s; continuing upload: filename=%s vessel_id=%s",
                        filename,
                        vessel_id,
                    )
                    return
                for r in results:
                    if isinstance(r, Exception):
                        continue
                    items.extend(r)
            else:
                url = f"/drives/{drive_id}/list/items?$top=1000"
                while url:
                    data = await graph().get(url)
                    items.extend(data.get("value", []))
                    url = data.get("@odata.nextLink")
        except Exception:
            return  # degrade gracefully if Graph API fails

        name_lc = filename.lower()
        target_norm = (
            target_folder_path.lower().replace(" ", "").replace("\\", "/").strip("/")
            if target_folder_path
            else None
        )

        for item in items:
            if "file" not in item:
                continue
            if item.get("name", "").lower() != name_lc:
                continue

            parent_path = (item.get("parentReference") or {}).get("path", "")
            rel_path = parent_path.split("root:", 1)[1].lstrip("/") if "root:" in parent_path else ""
            found_folder_norm = rel_path.lower().replace(" ", "").strip("/")

            if target_norm is None or found_folder_norm != target_norm:
                parts_folder = [p.strip() for p in rel_path.split("/") if p.strip()]
                # Path: Vessels/Specific Vessels/{Ship}/{Main}/... → ship at index 2
                if len(parts_folder) >= 3:
                    main_folder = parts_folder[3] if len(parts_folder) > 3 else parts_folder[0]
                    vessel_name = parts_folder[2]
                    leaf_folder = parts_folder[-1]
                    msg = (
                        f"Duplicate files upload, file already exists in folder '{leaf_folder}' "
                        f"under main folder '{main_folder}' and vessel '{vessel_name}'"
                    )
                elif parts_folder:
                    msg = f"Duplicate files upload, file already exists in folder '{parts_folder[0]}'"
                else:
                    msg = f"Duplicate files upload, file already exists in another folder"
                raise Conflict(msg)

    async def upload(self, folder_id, filename, content, content_type, uploaded_by_email, uploaded_by_name, access_token: str | None = None, sp_access_token: str | None = None):
        """Non-admin uploads stage a pending approval exactly as before.
        SPE Admin uploads are filed immediately and recorded as an activity
        notification instead."""
        drive_id = await self._drive()
        started_at = time.monotonic()
        
        # Retry logic: if the folder doesn't exist yet (e.g., vessel just created),
        # wait briefly and retry up to 3 times before giving up
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                path = await self._folder_path(drive_id, folder_id)
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)  # Wait 500ms before retrying
                    continue
                raise BadRequest(
                    f"Upload folder not found. The vessel folder structure may still be provisioning. "
                    f"Please try again in a moment. ({str(e)})"
                )
        
        flags = classify(path.split("/"))
        log.info("Upload target resolved in %.3fs: folder_id=%s path=%s", time.monotonic() - started_at, folder_id, path)
        if flags.get("month_driven"):
            return await self.month_upload(
                folder_id, filename, None, content, content_type,
                uploaded_by_email, uploaded_by_name,
                access_token=access_token,
            )
        target_id, dest_path = folder_id, path
        if flags.get("kind") == "drawing_classifier":
            target_id, dest_path = await self._resolve_drawing_target(
                drive_id, folder_id, path, filename, content, content_type
            )
        department, vessel_id, vessel_name, _ = await self._resolve_department_vessel(target_id)
        await self._check_global_duplicate(drive_id, filename, target_id, dest_path, vessel_id=vessel_id)
        log.info("Upload duplicate checks completed in %.3fs: filename=%s", time.monotonic() - started_at, filename)

        display = self._display(uploaded_by_email, uploaded_by_name)

        # Build SharePoint folder webUrl via Graph so we have a real deep link
        folder_web_url = ""
        try:
            folder_meta = await gd.get_item(drive_id, target_id, select="id,webUrl")
            folder_web_url = folder_meta.get("webUrl", "")
        except Exception:
            pass

        # Always upload directly to the destination folder in SharePoint Online
        item = await gd.upload_file(drive_id, target_id, filename, content, content_type, access_token=access_token)
        log.info("Graph upload completed in %.3fs: filename=%s target_id=%s", time.monotonic() - started_at, filename, target_id)

        # Auto-tag the uploaded file with DMS metadata columns (Group / Category / SubCategory)
        # Fire-and-forget: runs concurrently with the rest of the upload handler.
        # If column patching fails (e.g., columns not yet provisioned) it is logged as a
        # warning and the upload result is returned normally.
        asyncio.create_task(
            self._tag_file_columns(drive_id, item["id"], dest_path, vessel_name, filename=filename, access_token=access_token, sp_access_token=sp_access_token)
        )

        item_url = item.get("webUrl") or folder_web_url
        approval = await self._create_activity(
            action_type="upload",
            requesting_email=uploaded_by_email or "",
            requesting_name=uploaded_by_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=item["id"],
            target_description=filename,
            payload={"webUrl": item_url, "destination_path": dest_path},
            message=(
                f"Uploaded '{filename}' to {dest_path}."
            ),
            filename=filename,
            content_type=content_type,
            destination_folder_id=target_id,
            destination_path=dest_path,
            final_path=f"{dest_path}/{filename}",
            size=len(content),
        )
        res = _approval_as_job(approval, completed=True)
        res["id"] = item.get("id") or str(approval.id)
        res["webUrl"] = item_url
        res["destinationPath"] = dest_path
        return res


    async def delete_folder(
        self, folder_id: str, requesting_email=None, requesting_name=None, reason=None,
    ) -> dict:
        drive_id = await self._drive()
        folder_exists_in_spo = True
        folder_name_from_db = None
        real_drive_item_id = folder_id
        folder_path = None

        is_path = "/" in folder_id or "\\" in folder_id or " > " in folder_id
        if is_path:
            clean_path = folder_id.replace("\\", "/").replace(" > ", "/").strip("/")
            folder_path = clean_path
            folder_name_from_db = clean_path.split("/")[-1]
            try:
                item = await gd.get_item_by_path(drive_id, clean_path)
                if item and item.get("id"):
                    real_drive_item_id = item["id"]
                else:
                    folder_exists_in_spo = False
            except Exception:
                folder_exists_in_spo = False
        else:
            try:
                await gd.get_item(drive_id, folder_id)
            except GraphError as e:
                if e.status == 404:
                    folder_exists_in_spo = False
                else:
                    raise

        # Check in DB cache to get folder path / details if available
        with SessionLocal() as db:
            folder_row = None
            if is_path and folder_path:
                folder_row = db.query(models.Folder).filter(
                    (models.Folder.path == folder_path) |
                    (models.Folder.path.ilike(f"%/{folder_name_from_db}"))
                ).first()
            elif not is_path:
                folder_row = db.query(models.Folder).filter(models.Folder.drive_item_id == folder_id).first()

            if folder_row:
                folder_path = folder_row.path
                folder_name_from_db = folder_row.path.split("/")[-1]
                if not is_path and folder_row.drive_item_id:
                    real_drive_item_id = folder_row.drive_item_id

        # Determine department and vessel name from path
        department = None
        vessel_name = None
        vessel_id = None
        if folder_path:
            parts = folder_path.split("/")
            department = parts[0] if parts else None
            if len(parts) > 1 and parts[1].lower() not in ("common for all ships", "common for all vessels", "kaizen - knowledge bank"):
                vessel_name = parts[1]

        folder_name = folder_name_from_db or (folder_path.split("/")[-1] if folder_path else folder_id)
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel_name}" if vessel_name else ""

        return await self._admin_or_pending(
            action_type="delete_folder",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=real_drive_item_id,
            target_description=folder_name,
            payload={},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to delete the "
                f"folder '{folder_name}'{vessel_clause}."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) deleted the folder '{folder_name}'"
                f"{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_delete_folder(
                real_drive_item_id,
                folder_path=folder_path,
                spo_already_deleted=not folder_exists_in_spo,
                requesting_email=requesting_email,
                requesting_name=requesting_name,
                reason=reason,
            ),
        )

    async def _execute_delete_folder(
        self, folder_id: str, folder_path: str | None = None, spo_already_deleted: bool = False,
        requesting_email=None, requesting_name=None, reason=None,
    ) -> bool:
        """Delete a folder and all its contents via Graph API and clean DB cache."""
        drive_id = await self._drive()
        from ..graph import drive as _gd

        # If logical path wasn't provided, try to resolve from database
        with SessionLocal() as db:
            if not folder_path:
                folder_row = db.query(models.Folder).filter(models.Folder.drive_item_id == folder_id).first()
                if folder_row:
                    folder_path = folder_row.path

        if not spo_already_deleted and folder_id and not ("/" in folder_id or "\\" in folder_id):
            try:
                await _gd.delete_item(drive_id, folder_id)
            except Exception as exc:
                if getattr(exc, 'status', None) != 404:
                    raise

        with SessionLocal() as db:
            site_name, site_key = self._resolve_current_site(db)
        await self._record_deletion(
            item_type="folder",
            drive_item_id=folder_id if not ("/" in folder_id or "\\" in folder_id) else None,
            name=(folder_path.split("/")[-1] if folder_path else folder_id),
            original_path=folder_path,
            site_name=site_name,
            site_key=site_key,
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            reason=reason,
            source="app",
        )

        # Remove the folder and all of its descendant folders from the database cache
        with SessionLocal() as db:
            if folder_path:
                rows = (
                    db.query(models.Folder)
                    .filter((models.Folder.path == folder_path) | (models.Folder.path.like(f"{folder_path}/%")))
                    .all()
                )
            else:
                rows = (
                    db.query(models.Folder)
                    .filter(models.Folder.drive_item_id == folder_id)
                    .all()
                )
            for row in rows:
                db.delete(row)
            db.commit()
        return True

    async def create_subfolder(
        self, folder_id: str, name: str, requesting_email=None, requesting_name=None,
    ) -> dict:
        """Manually create a named sub-folder.

        Two cases:
        - Inside a month_driven folder: unchanged legacy behavior — creates a
          "{Month YYYY}"-style folder and auto-provisions its category
          children from the template (see _execute_create_subfolder).
        - Inside any other folder that belongs to a vessel (Folder.vessel_id
          set — true for a flat Part-C vessel root and anything created
          under it, and for legacy per-vessel department/category folders
          once browsed at least once): a plain folder, any depth, no
          auto-provisioned children. classify() only knows the old
          department template, so it can't recognize a flat vessel root or
          its children as anything but a generic unrecognized folder — this
          is what actually enables Phase 3's "Add Folder" at any depth.
        Anything else (a folder with no vessel association at all) is still
        rejected — this stays scoped to vessel folder structure.
        """
        from .normalize import clean_folder_name
        cleaned = clean_folder_name(name)
        if not cleaned:
            raise BadRequest("Folder name is required")
        if not any(c.isalpha() for c in cleaned):
            raise BadRequest("Folder name must contain alphabetic characters (letters)")
        cleaned = sanitize_folder_name(cleaned)
        drive_id = await self._drive()
        parent_path = await self._folder_path(drive_id, folder_id)
        parent_parts = parent_path.split("/") if parent_path else []
        parent_flags = classify(parent_parts)

        with SessionLocal() as db:
            parent_row = self._folder_by_item(db, folder_id)
            vessel_id = parent_row.vessel_id if parent_row else None
            vessel_name = None
            if vessel_id:
                v = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
                vessel_name = v.name if v else None

        if not parent_flags.get("month_driven") and vessel_id is None:
            raise BadRequest(
                "Can only create sub-folders inside a month-driven folder or "
                "a folder that belongs to a vessel."
            )

        department = parent_parts[0] if parent_parts else "All Departments"
        parent_name = parent_parts[-1] if parent_parts else folder_id

        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" for vessel {vessel_name}" if vessel_name else ""
        payload = {
            "parent_folder_id": folder_id,
            "name": cleaned,
            "requesting_email": requesting_email,
            "requesting_name": requesting_name,
        }
        return await self._admin_or_pending(
            action_type="create_folder",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=folder_id,
            target_description=cleaned,
            payload=payload,
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to create the "
                f"folder '{cleaned}' inside '{parent_name}'{vessel_clause}."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) created the folder '{cleaned}' inside "
                f"'{parent_name}'{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_create_subfolder(payload),
        )

    async def _execute_create_subfolder(self, payload) -> dict:
        """Manually create a named sub-folder.

        Inside a month_driven folder: legacy behavior — the new folder is a
        "{Month YYYY}"-style container and its category children are
        auto-provisioned from the template.

        Anywhere else that belongs to a vessel (the guard in
        create_subfolder already restricted us to that): a plain, empty,
        upload-enabled folder — no auto-provisioned children, since there is
        no template for an ad-hoc folder. This is what lets Phase 3's
        "Add Folder" work at any depth under a flat (Part C) vessel root, or
        under any already-created folder.
        """
        folder_id = payload["parent_folder_id"]
        name = payload["name"]
        drive_id = await self._drive()
        parent_path = await self._folder_path(drive_id, folder_id)
        parent_parts = parent_path.split("/") if parent_path else []
        parent_flags = classify(parent_parts)
        is_month_driven = bool(parent_flags.get("month_driven"))

        # Check for duplicate folder names (case-insensitive and normalized)
        from .normalize import normalize_folder_name
        normalized_new_name = normalize_folder_name(name)
        existing_items = await gd.list_children(drive_id, folder_id)
        for it in existing_items:
            if "folder" in it:
                if normalize_folder_name(it["name"]) == normalized_new_name:
                    raise Conflict(f"A folder with a similar name '{it['name']}' already exists here (ignoring casing, spaces, and special characters)")

        new_item = await gd.ensure_folder(drive_id, folder_id, name)
        mpath = f"{parent_path}/{name}"
        cats = parent_flags.get("categories", []) if is_month_driven else []
        new_kind = "month" if is_month_driven else "folder"
        requesting_email = (payload.get("requesting_email") or "").strip()
        requesting_name = payload.get("requesting_name") or ""
        department, _, vessel_name, _ = await self._resolve_department_vessel(folder_id)
        with SessionLocal() as db:
            parent_row = self._folder_by_item(db, folder_id)
            vessel_id = parent_row.vessel_id if parent_row else None
            self._upsert(db, mpath, name, new_kind, new_item["id"], False, vessel_id)
            for cat_name in cats:
                cat_item = await gd.ensure_folder(drive_id, new_item["id"], cat_name)
                self._upsert(db, f"{mpath}/{cat_name}", cat_name, "leaf",
                             cat_item["id"], False, vessel_id)
            db.commit()
            # Emit a folder-creation alert for the top-header alert bell so the
            # new SharePoint Online folder surfaces there instead of only at the
            # bottom of the Documents / Vessels modules.
            self._emit_folder_alert(
                db,
                drive_item_id=new_item["id"],
                folder_name=name,
                folder_path=mpath,
                parent_folder_id=folder_id,
                vessel_name=vessel_name,
                department=department,
                created_by_email=requesting_email,
                created_by_name=requesting_name,
                alert_type="folder_created",
            )
        return {
            "id": new_item["id"],
            "name": name,
            "kind": new_kind,
            "upload": True,
            "month_driven": False,
            "has_children": bool(cats),
        }

    async def month_upload(self, folder_id, filename, category, content, content_type, uploaded_by_email, uploaded_by_name):
        drive_id = await self._drive()
        
        # Retry logic: if the folder doesn't exist yet (e.g., vessel just created),
        # wait briefly and retry up to 3 times before giving up
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                md_path = await self._folder_path(drive_id, folder_id)
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)  # Wait 500ms before retrying
                    continue
                raise BadRequest(
                    f"Upload folder not found. The vessel folder structure may still be provisioning. "
                    f"Please try again in a moment. ({str(e)})"
                )
        
        md_parts = md_path.split("/")
        flags = classify(md_parts)
        if not flags.get("month_driven"):
            raise BadRequest("This folder is not a month-driven folder")
        categories = flags.get("categories", [])
        md_spec = {"month_children": [{"name": c, "kind": "leaf"} for c in categories]}

        # Scope the duplicate check to this vessel's own folders instead of
        # the whole container — see _check_global_duplicate docstring.
        _, month_vessel_id, _, _ = await self._resolve_department_vessel(folder_id)
        await self._check_global_duplicate(drive_id, filename, "", vessel_id=month_vessel_id)

        # Check fitz (PyMuPDF) and paddleocr installations — if missing, fall back
        # to placing the file in "To be Classified" so uploads still work without OCR.
        ocr_available = True
        try:
            import fitz  # noqa: F401
            from paddleocr import PaddleOCR  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            ocr_available = False

        detected = {"year": None, "month": None, "label": None, "text_empty": False}
        if ocr_available:
            try:
                detected = (await asyncio.to_thread(
                    detect_document_month, content, filename, content_type or ""
                )) or detected
            except Exception:
                # Treat OCR errors as undetectable — route to To be Classified
                pass

        if detected and detected.get("text_empty"):
            detected = {"year": None, "month": None, "label": None, "text_empty": True}

        # 1. Determine target folder path and dest_path beforehand
        if detected and detected.get("year") is not None:
            y, m = detected["year"], detected["month"]
            detected_label = detected["label"]
            cat_name = category if category in categories else "To be Classified"
        else:
            # OCR unavailable or couldn't detect date — route to To be Classified
            detected_label = "To be Classified"
            cat_name = "To be Classified"
            y, m = None, None
        target_folder_path = f"{md_path}/{detected_label}/{cat_name}"
        dest_path = f"{md_path}/{detected_label}/{cat_name}/{filename}"


        # 2. Check duplicate in the specific target folder if it exists
        # Check target folder directly if it already exists in the DB cache
        with SessionLocal() as db:
            existing_folder = db.query(models.Folder).filter_by(path=target_folder_path).one_or_none()
            if existing_folder:
                existing_file = await gd.find_child(drive_id, existing_folder.drive_item_id, filename)
                if existing_file and "file" in existing_file:
                    parts = [p.strip() for p in target_folder_path.split("/") if p.strip()]
                    # Vessels/Specific Vessels/{Ship}/{Main}/... → ship at index 2, main at index 3
                    if len(parts) >= 3:
                        main_folder = parts[3] if len(parts) > 3 else parts[0]
                        vessel_name = parts[2]
                        leaf_folder = parts[-1]
                        msg = (
                            f"Duplicate files upload, file already exists in folder '{leaf_folder}' "
                            f"under main folder '{main_folder}' and vessel '{vessel_name}'"
                        )
                    elif parts:
                        msg = f"Duplicate files upload, file already exists in folder '{parts[-1]}'"
                    else:
                        msg = f"Duplicate files upload, '{filename}' already exists in this folder"
                    raise Conflict(msg)

        # 3. Create/provision folders only when there is no duplicate conflict
        with SessionLocal() as db:
            if detected["year"] is None:
                target = await gd.ensure_folder(drive_id, folder_id, "To be Classified")
                self._upsert(
                    db, f"{md_path}/To be Classified", "To be Classified", "leaf",
                    target["id"], False, None,
                )
                target_id, detected_label = target["id"], None
                dest_path = f"{md_path}/To be Classified"
            else:
                y, m = detected["year"], detected["month"]
                month_item = await self._ensure_month(
                    db, drive_id, folder_id, md_path, md_spec, y, m, None
                )
                cat_name = category if category in categories else "To be Classified"
                cat_item = await gd.ensure_folder(drive_id, month_item["id"], cat_name)
                self._upsert(
                    db, f"{md_path}/{detected['label']}/{cat_name}", cat_name, "leaf",
                    cat_item["id"], False, None,
                )
                target_id, detected_label = cat_item["id"], detected["label"]
                dest_path = f"{md_path}/{detected['label']}/{cat_name}"
            db.commit()

        existing = await gd.find_child(drive_id, target_id, filename)
        if existing and "file" in existing:
            # Build a descriptive message showing where the file lives
            parts = [p.strip() for p in dest_path.split("/") if p.strip()]
            # Vessels/Specific Vessels/{Ship}/{Main}/... → ship at index 2, main at index 3
            if len(parts) >= 3:
                main_folder = parts[3] if len(parts) > 3 else parts[0]
                vessel_name = parts[2]
                leaf_folder = parts[-1]
                msg = (
                    f"Duplicate files upload, file already exists in folder '{leaf_folder}' "
                    f"under main folder '{main_folder}' and vessel '{vessel_name}'"
                )
            elif parts:
                msg = f"Duplicate files upload, file already exists in folder '{parts[-1]}'"
            else:
                msg = f"Duplicate files upload, '{filename}' already exists in this folder"
            raise Conflict(msg)

        department, vessel_id, vessel_name, _ = await self._resolve_department_vessel(target_id)
        display = self._display(uploaded_by_email, uploaded_by_name)

        # Always upload directly to the destination folder in SharePoint Online
        item = await gd.upload_file(drive_id, target_id, filename, content, content_type, access_token=access_token)

        # Auto-tag the uploaded file with DMS metadata columns (Group / Category / SubCategory)
        asyncio.create_task(
            self._tag_file_columns(drive_id, item["id"], dest_path, vessel_name, filename=filename, access_token=access_token)
        )

        item_url = item.get("webUrl", "")
        approval = await self._create_activity(
            action_type="upload",
            requesting_email=uploaded_by_email or "",
            requesting_name=uploaded_by_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=item["id"],
            target_description=filename,
            payload={"webUrl": item_url, "destination_path": dest_path},
            message=(
                f"Uploaded '{filename}' to {dest_path}."
            ),
            filename=filename,
            content_type=content_type,
            destination_folder_id=target_id,
            destination_path=dest_path,
            is_month_upload=True,
            category=category,
            detected_month=detected_label,
            final_path=f"{dest_path}/{filename}",
            size=len(content),
        )
        res = _approval_as_job(approval, completed=True)
        res["id"] = item.get("id") or str(approval.id)
        res["webUrl"] = item_url
        res["destinationPath"] = dest_path
        return res

    # ------------------------------------------------------------ files
    async def get_file(self, file_id):
        # file_id may be either a SharePoint drive item ID (alphanumeric, e.g.
        # '01IKGFON...') or a numeric approval request DB row ID (e.g. '36')
        # returned by the upload endpoint before the file is approved.
        # In the latter case we look up the staged drive_item_id so the
        # user can preview the file while it awaits approval.
        resolved_id = file_id
        if file_id and file_id.isdigit():
            with SessionLocal() as db:
                row = db.get(models.ApprovalRequest, int(file_id))
                if row and (row.drive_item_id or row.target_id):
                    resolved_id = row.drive_item_id or row.target_id
        drive_id = await self._drive()
        log.info("File download requested: file_id=%s resolved_id=%s drive_id=%s", file_id, resolved_id, drive_id)
        try:
            return await gd.download_file(drive_id, resolved_id)
        except GraphError as e:
            log.warning(
                "File download failed: file_id=%s resolved_id=%s drive_id=%s graph_status=%s error=%s",
                file_id, resolved_id, drive_id, e.status, e,
            )
            return None


    async def delete_file(self, file_id: str, requesting_email=None, requesting_name=None, reason=None):
        drive_id = await self._drive()
        try:
            item = await gd.get_item(drive_id, file_id)
        except GraphError as e:
            if e.status == 404:
                raise NotFound("File not found")
            raise
        clean_reason = (reason or "").strip()
        if not self._is_admin(requesting_email) and not clean_reason:
            raise BadRequest("A reason for deletion is required")
        parent_id = (item.get("parentReference") or {}).get("id")
        filename = item.get("name") or file_id
        if parent_id:
            department, vessel_id, vessel_name, _ = await self._resolve_department_vessel(parent_id)
        else:
            department, vessel_id, vessel_name = "All Departments", None, None
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel_name}" if vessel_name else ""
        reason_clause = f" Reason: \"{clean_reason}\"" if clean_reason else ""
        return await self._admin_or_pending(
            action_type="delete_document",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=file_id,
            target_description=filename,
            payload={"reason": clean_reason} if clean_reason else {},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to delete the "
                f"document '{filename}'{vessel_clause}.{reason_clause}"
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) deleted the document '{filename}'"
                f"{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_delete_file(
                file_id, item=item, requesting_email=requesting_email,
                requesting_name=requesting_name, reason=clean_reason,
            ),
        )

    async def _execute_delete_file(self, file_id, item=None, requesting_email=None, requesting_name=None, reason=None):
        drive_id = await self._drive()
        try:
            await gd.delete_item(drive_id, file_id)
        except GraphError as e:
            if e.status == 404:
                return False
            raise

        filename = (item or {}).get("name") or file_id
        parent_path = ((item or {}).get("parentReference") or {}).get("path", "")
        rel = parent_path.split("root:", 1)[1].lstrip("/") if "root:" in parent_path else ""
        original_path = f"{rel}/{filename}".strip("/") if rel else filename
        with SessionLocal() as db:
            site_name, site_key = self._resolve_current_site(db)
        await self._record_deletion(
            item_type="file",
            drive_item_id=file_id,
            name=filename,
            original_path=original_path,
            site_name=site_name,
            site_key=site_key,
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            reason=reason,
            source="app",
        )
        return True

    def _trail(self, db, parts, leaf_id):
        """Build [{id,name}] for each path segment, resolving ids from the DB."""
        trail = []
        for i in range(len(parts)):
            if i == len(parts) - 1 and leaf_id:
                trail.append({"id": leaf_id, "name": parts[i]})
            else:
                prefix = "/".join(parts[: i + 1])
                row = db.query(models.Folder).filter_by(path=prefix).one_or_none()
                trail.append({"id": row.drive_item_id if row else "", "name": parts[i]})
        return trail

    async def search(self, q, vessel_id=None):
        """Search folders + files by name, path, and vessel. When
        `vessel_id` is given, results are restricted to that vessel's own
        ship folders (one per main folder) — never other vessels' folders,
        and never the shared "Common for all ships" areas.

        `Folder.path` is a "/"-joined chain of the folder's ancestors
        (e.g. "Technical & Crewing/MV Horizon/Drawings and Manuals/
        Electrical"), and this schema has no separate group/category/
        vessel columns on Folder — those are simply path segments. So
        matching against `path` (not just the leaf `name`) is what makes a
        group, category, sub-category, or vessel-name term find folders
        and files nested under it, in addition to matching a `Vessel.name`
        directly for vessels whose folder segment doesn't spell out the
        full registered name.

        `+`, `,` and `;` separate OR clauses — mirrors the Documents page's
        own client-side matcher (matchesSearchTokens in DocumentsPage.tsx)
        and the dashboard's _dashboard_doc_matches_text (main.py), so a
        query like "Bow Fighter + Bow Fraternity" finds either vessel
        instead of nothing: no single folder/file path ever contains both
        vessel names, so treating the whole raw string (or an AND of every
        word in it) as one literal term always came back empty for a
        multi-vessel query. Previously this method searched the whole raw
        `q` string as one literal substring, so this multi-vessel syntax
        silently didn't work here even though the Documents page's own
        local filtering already documented and supported it — that
        mismatch is what made "Bow Fighter + Bow Fraternity"-style
        searches look broken: this call is what discovers and loads a
        vessel's rows in the first place, so if it finds nothing, there is
        nothing yet loaded for the client-side matcher to filter over.
        """
        ql = q.strip()
        if not ql:
            return []
        clauses = [c.strip() for c in re.split(r"[+,;]+", ql) if c.strip()] or [ql]
        vid = int(vessel_id) if vessel_id and str(vessel_id).isdigit() else None
        out, seen = [], set()
        # 1) Folders from our DB cache — always available, no index lag.
        #    vessel_id is an indexed FK, so scoping here is a cheap filter,
        #    not a scan of every vessel's folders.
        with SessionLocal() as db:
            query = (
                db.query(models.Folder)
                .outerjoin(models.Vessel, models.Folder.vessel_id == models.Vessel.id)
                .filter(
                    sa_or(*(
                        cond
                        for clause in clauses
                        for cond in (
                            models.Folder.name.ilike(f"%{clause}%"),
                            models.Folder.path.ilike(f"%{clause}%"),
                            models.Vessel.name.ilike(f"%{clause}%"),
                        )
                    ))
                )
            )
            if vid is not None:
                query = query.filter(models.Folder.vessel_id == vid)
            rows = query.limit(50).all()
            for r in rows:
                parts = r.path.split("/")
                out.append(
                    {
                        "id": r.drive_item_id,
                        "name": r.name,
                        "kind": r.kind,
                        "trail": self._trail(db, parts, r.drive_item_id),
                        "path": r.path,
                    }
                )
                seen.add(r.drive_item_id)

            # A vessel has no single root — it has one ship folder under each
            # of the 3 main folders — so file search below is scoped to all
            # of them rather than one shared "vessel root".
            ship_root_ids = []
            if vid is not None:
                ship_root_ids = [
                    r.drive_item_id
                    for r in db.query(models.Folder).filter_by(vessel_id=vid, kind="ship").all()
                ]

        # 2) Files via Graph search (best-effort; may lag or be unavailable).
        #    Graph's own search doesn't understand +/,/; OR syntax either,
        #    so each clause is searched separately and the (deduped) results
        #    merged — same OR semantics as the DB query above.
        try:
            drive_id = await self._drive()
            items = []
            for clause in clauses:
                if len(out) + len(items) >= 50:
                    break
                if vid is not None:
                    per_root = await asyncio.gather(
                        *(gd.search_items_in(drive_id, root_id, clause) for root_id in ship_root_ids)
                    )
                    items.extend(it for lst in per_root for it in lst)
                else:
                    items.extend(await gd.search_items(drive_id, clause))
            with SessionLocal() as db:
                for it in items:
                    if "file" not in it or it["id"] in seen:
                        continue
                    ref = (it.get("parentReference") or {}).get("path", "")
                    rel = ref.split("root:", 1)[1].lstrip("/") if "root:" in ref else ""
                    parts = [p for p in rel.split("/") if p] + [it["name"]]
                    out.append(
                        {
                            "id": it["id"],
                            "name": it["name"],
                            "kind": "file",
                            "trail": self._trail(db, parts, it["id"]),
                            "path": "/".join(parts),
                        }
                    )
                    seen.add(it["id"])
        except GraphError:
            pass
        return out[:50]

    # -------------------------------------------------------------- jobs
    def _make_job(self, filename, destination, detected_month):
        with SessionLocal() as db:
            job = models.UploadJob(
                filename=filename,
                status="done",
                destination=destination,
                detected_month=detected_month,
            )
            db.add(job)
            db.commit()
            return self._job_public(job)

    async def get_job(self, job_id):
        with SessionLocal() as db:
            job = db.get(models.UploadJob, int(job_id)) if job_id.isdigit() else None
            return self._job_public(job) if job else None

    @staticmethod
    def _job_public(job):
        return {
            "id": str(job.id),
            "filename": job.filename,
            "status": job.status,
            "destination": job.destination,
            "detected_month": job.detected_month,
        }

    async def _resolve_item_context(
        self, item_id, item_type, item_name=None, department=None, vessel_name=None,
    ):
        """Best-effort name/department/vessel resolution for archive/restore
        actions. Frontend-supplied overrides win (needed for recycle-bin
        items that Graph may no longer resolve); otherwise try a live Graph
        lookup, degrading gracefully to defaults on any failure."""
        if item_name and department:
            return item_name, department, vessel_name
        try:
            drive_id = await self._drive()
            item = await gd.get_item(drive_id, item_id)
            if not item_name:
                item_name = item.get("name", item_id)
            if not department or not vessel_name:
                if item_type == "folder":
                    dept, _, vess, _ = await self._resolve_department_vessel(item_id)
                else:
                    parent_id = (item.get("parentReference") or {}).get("id")
                    dept, _, vess, _ = (
                        await self._resolve_department_vessel(parent_id)
                        if parent_id else ("All Departments", None, None, None)
                    )
                department = department or dept
                vessel_name = vessel_name or vess
        except Exception:
            pass
        return item_name or item_id, department or "All Departments", vessel_name

    async def archive_item(
        self, item_id: str, item_type: str, requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None, reason=None,
    ):
        name, dept, vessel = await self._resolve_item_context(item_id, item_type, item_name, department, vessel_name)
        clean_reason = (reason or "").strip()
        if item_type == "file" and not self._is_admin(requesting_email) and not clean_reason:
            raise BadRequest("A reason for archiving is required")
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel}" if vessel else ""
        reason_clause = f" Reason: \"{clean_reason}\"" if clean_reason else ""
        payload = {"item_type": item_type}
        if clean_reason:
            payload["reason"] = clean_reason
        return await self._admin_or_pending(
            action_type="archive_item",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=dept,
            vessel_name=vessel,
            target_id=item_id,
            target_description=name,
            payload=payload,
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to archive "
                f"'{name}'{vessel_clause}.{reason_clause}"
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) archived '{name}'{vessel_clause}. "
                f"No approval was required."
            ),
            execute=lambda: self._execute_archive(item_id, item_type),
        )

    async def _execute_archive(self, item_id, item_type):
        with SessionLocal() as db:
            row = db.query(models.ArchivedItem).filter_by(item_id=item_id).one_or_none()
            if not row:
                row = models.ArchivedItem(item_id=item_id, item_type=item_type)
                db.add(row)
                db.commit()
        return {"archived": True}

    async def restore_item(
        self, item_id: str, item_type: str = "folder", requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None,
    ):
        name, dept, vessel = await self._resolve_item_context(item_id, item_type, item_name, department, vessel_name)
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel}" if vessel else ""
        return await self._admin_or_pending(
            action_type="restore_item",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=dept,
            vessel_name=vessel,
            target_id=item_id,
            target_description=name,
            payload={},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to restore "
                f"'{name}'{vessel_clause}."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) restored '{name}'{vessel_clause}. "
                f"No approval was required."
            ),
            execute=lambda: self._execute_restore(item_id),
        )

    async def _execute_restore(self, item_id):
        with SessionLocal() as db:
            row = db.query(models.ArchivedItem).filter_by(item_id=item_id).one_or_none()
            if row:
                db.delete(row)
                db.commit()
        return {"restored": True}

    async def move_archived_to_recycle_bin(
        self, item_id: str, item_type: str, requesting_email=None, requesting_name=None,
        reason: str | None = None,
    ):
        """Archive page's 'Move to Recycle Bin' action: an archived item was
        only ever flagged (see _execute_archive) — it's still physically in
        SharePoint — so sending it to the Recycle Bin means actually
        deleting it now, through the exact same delete_file/delete_folder
        path (and admin-approval gating) as a normal Documents delete. Once
        that succeeds (or is itself pending approval), the archived_items
        flag is cleared since the item is no longer "archived": it's
        tracked by the Recycle Bin's own deletion_log from here on."""
        if item_type == "file":
            result = await self.delete_file(
                item_id, requesting_email=requesting_email, requesting_name=requesting_name, reason=reason,
            )
        else:
            result = await self.delete_folder(
                item_id, requesting_email=requesting_email, requesting_name=requesting_name, reason=reason,
            )
        if result.get("status") != "pending":
            await self._execute_restore(item_id)
        return result

    async def get_archived_ids(self) -> list[str]:
        with SessionLocal() as db:
            rows = db.query(models.ArchivedItem).all()
            return [r.item_id for r in rows]

    def _get_archived_rows(self) -> list[models.ArchivedItem]:
        """Return all ArchivedItem DB rows (includes item_id, item_type, created_at)."""
        with SessionLocal() as db:
            return db.query(models.ArchivedItem).all()

    async def get_archived_nodes(self):
        """Fetch metadata for all archived items using Graph $batch (up to 20 per request).

        Previously this made one sequential Graph API call per archived item, causing
        multi-second (sometimes 30-40 s) delays on initial load when many items are
        archived.  Graph's JSON batch endpoint lets us pack up to 20 GET requests into
        a single HTTP round-trip, reducing N calls → ceil(N/20) calls.
        """
        drive_id = await self._drive()
        db_rows = self._get_archived_rows()
        if not db_rows:
            return []

        # Build a mapping from item_id -> archived_at timestamp (stored as created_at in DB)
        archived_at_by_id: dict[str, str] = {}
        for row in db_rows:
            if row.created_at is not None:
                archived_at_by_id[row.item_id] = row.created_at.isoformat()

        ids = [row.item_id for row in db_rows]
        _BATCH_SIZE = 20  # Graph $batch limit
        out = []

        def append_item(item_id, item, item_type):
            """Convert a Graph item to the archive response shape."""
            if not item:
                out.append({
                    "id": item_id,
                    "name": item_id,
                    "kind": item_type,
                    "upload": item_type == "file",
                    "month_driven": False,
                    "has_children": False,
                    "main_folder": item_id,
                    "original_path": item_id,
                    "vessel_name": "",
                    "document_section": "",
                    "group": "",
                    "category": "",
                    "sub_category": "",
                    "archived_at": archived_at_by_id.get(item_id),
                })
                return

            is_folder = "folder" in item
            ref = (item.get("parentReference") or {}).get("path", "")
            rel = ref.split("root:", 1)[1].lstrip("/") if "root:" in ref else ""
            original_path = f"{rel}/{item['name']}".strip("/") if rel else item["name"]
            folder_path = rel
            path_parts = [part.strip() for part in folder_path.split("/") if part.strip()]
            vessel_name = ""
            document_section = ""
            group = ""
            category = ""
            sub_category = ""
            main_folder_index = next(
                (index for index, part in enumerate(path_parts)
                 if part in template.MAIN_FOLDERS or part in template.FLAT_MAIN_FOLDERS),
                -1,
            )
            if main_folder_index >= 0:
                group = path_parts[main_folder_index]
                after_group = path_parts[main_folder_index + 1:]
                if group in template.MAIN_FOLDERS and after_group:
                    vessel_name = after_group[0]
                    hierarchy = after_group[1:]
                else:
                    hierarchy = after_group
                document_section = hierarchy[0] if hierarchy else group
                category = hierarchy[1] if len(hierarchy) > 1 else ""
                sub_category = hierarchy[2] if len(hierarchy) > 2 else ""
            node = {
                "id": item["id"],
                "name": item["name"],
                "kind": "folder" if is_folder else "file",
                "upload": False,
                "month_driven": False,
                "has_children": is_folder and item.get("folder", {}).get("childCount", 0) > 0,
                "main_folder": original_path.split("/", 1)[0],
                "original_path": original_path,
                "vessel_name": vessel_name,
                "document_section": document_section,
                "group": group,
                "category": category,
                "sub_category": sub_category,
                "archived_at": archived_at_by_id.get(item_id),
            }
            if not is_folder:
                node["ext"] = item["name"].rsplit(".", 1)[-1].lower() if "." in item["name"] else ""
                node["size"] = item.get("size")
                node["modified"] = item.get("lastModifiedDateTime")
            out.append(node)

        for offset in range(0, len(ids), _BATCH_SIZE):
            chunk = ids[offset: offset + _BATCH_SIZE]

            batch_requests = [
                {
                    "id": str(idx),
                    "method": "GET",
                    "url": f"/drives/{drive_id}/items/{item_id}"
                           "?$select=id,name,folder,file,size,lastModifiedDateTime,parentReference",
                }
                for idx, item_id in enumerate(chunk)
            ]

            try:
                resp = await graph().post("/$batch", json={"requests": batch_requests})
            except Exception as e:
                log.warning("get_archived_nodes: batch request failed: %s", e)
                resp = {"responses": []}

            by_id = {r["id"]: r for r in resp.get("responses", [])}

            for idx, item_id in enumerate(chunk):
                r = by_id.get(str(idx), {})
                status = r.get("status", 0)
                if status in (200, 201) and r.get("body"):
                    append_item(item_id, r["body"], db_rows[offset + idx].item_type)
                    continue

                try:
                    item = await gd.get_item(drive_id, item_id)
                except Exception:
                    item = None
                append_item(item_id, item, db_rows[offset + idx].item_type)

        return out

    async def get_deleted_ids(self) -> list[str]:
        try:
            drive_id = await self._drive()
            url = f"/drives/{drive_id}/items/root/children?$filter=deleted ne null"
            data = await graph().get(url)
            items = data.get("value", [])
            return [it["id"] for it in items]
        except Exception:
            return []

    async def get_deleted_nodes(self):
        try:
            # Build drive -> site info map across all configured sites
            drive_site_map: dict[str, dict] = {}
            with SessionLocal() as db:
                for sc in db.query(models.SiteConfiguration).all():
                    if sc.drive_id:
                        drive_site_map[str(sc.drive_id)] = {
                            "site_name": sc.display_name or sc.site_name,
                            "site_key": sc.site_key,
                        }
            for sk, sc in Settings.discover_available_sites().items():
                try:
                    cfg = Settings.load_site_config(sk)
                    if cfg.drive_id and str(cfg.drive_id) not in drive_site_map:
                        drive_site_map[str(cfg.drive_id)] = {
                            "site_name": cfg.sp_site_name or sk,
                            "site_key": sk,
                        }
                except Exception:
                    pass

            primary_drive_id = await self._drive()
            if str(primary_drive_id) not in drive_site_map:
                drive_site_map[str(primary_drive_id)] = {
                    "site_name": settings.active_site or "Primary Site",
                    "site_key": settings.active_site or "default",
                }

            drives_to_query = list(drive_site_map.keys())
            if str(primary_drive_id) not in drives_to_query:
                drives_to_query.insert(0, str(primary_drive_id))

            out = []
            sp_vessel_names: set[str] = set()  # track vessel names returned by SharePoint
            with SessionLocal() as db:
                vessel_names = self._vessel_name_set(db)

            for d_id in drives_to_query:
                try:
                    url = f"/drives/{d_id}/items/root/children?$filter=deleted ne null"
                    data = await graph().get(url)
                    items = data.get("value", [])
                except Exception:
                    items = []

                site_info = drive_site_map.get(str(d_id), {})
                current_site_name = site_info.get("site_name", settings.active_site or "SharePoint")
                current_site_key = site_info.get("site_key", "default")

                for it in items:
                    name = it["name"]
                    is_folder = bool(it.get("folder")) or (not it.get("file") and "." not in name)
                    kind = "folder" if is_folder else "file"

                    # Parse main folder and original path from deletedFromLocation
                    loc = it.get("deletedFromLocation", "")
                    main_folder = ""
                    original_path = ""
                    path_parts: list[str] = []
                    if "Document Library/" in loc:
                        rel_part = loc.split("Document Library/", 1)[1]
                        path_parts = [part for part in rel_part.split("/") if part]
                        # deletedFromLocation is normally the parent location, so include
                        # the deleted item to make the path useful in the UI.
                        if not path_parts or path_parts[-1] != name:
                            path_parts.append(name)
                        original_path = "/".join(path_parts)
                        main_folder = path_parts[0] if path_parts else ""

                    known_main_folders = {
                        "Technical & Crewing", "Commercial & Chartering", "Insurance",
                        "Kaizen - Knowledge Bank", "Knowledge Bank",
                    }
                    parent_parts = path_parts[:-1]
                    main_index = next((i for i, part in enumerate(parent_parts) if part in known_main_folders), -1)
                    vessel_name = ""
                    category = ""
                    sub_category = ""
                    if main_index >= 0:
                        after_main = parent_parts[main_index + 1:]
                        if parent_parts[:main_index] and parent_parts[0] == "Vessels":
                            # New path: Vessels / Specific Vessels / {Ship} / {Main} / ...
                            vessel_name = parent_parts[2] if len(parent_parts) > 2 else ""
                            category_parts = after_main
                        else:
                            vessel_name = after_main[0] if after_main else ""
                            category_parts = after_main[1:]
                        category = category_parts[0] if category_parts else parent_parts[main_index]
                        sub_category = category_parts[-1] if len(category_parts) > 1 else ""
                    elif parent_parts and parent_parts[0] == "Vessels":
                        # Vessels / Specific Vessels / {Ship} / {Main} / ...
                        vessel_name = parent_parts[2] if len(parent_parts) > 2 else ""
                        category = parent_parts[3] if len(parent_parts) > 3 else ""
                        sub_category = parent_parts[-1] if len(parent_parts) > 4 else ""

                    # A deleted item directly under "Specific Vessels" is a vessel folder.
                    # Also handle paths where the container root prefix varies.
                    is_vessel_folder = (
                        is_folder and (
                            # Standard: Vessels/Specific Vessels/{name}
                            (len(parent_parts) == 2 and parent_parts[0] == "Vessels" and parent_parts[1] == "Specific Vessels")
                            # With container root prefix: .../Vessels/Specific Vessels/{name}
                            or (len(parent_parts) >= 2 and parent_parts[-2] == "Vessels" and parent_parts[-1] == "Specific Vessels")
                            or (len(parent_parts) >= 2 and parent_parts[-1] == "Specific Vessels")
                        )
                    )
                    # Term Store match (Part 2 classification): authoritative
                    # over the path-depth heuristic above — a folder whose
                    # own name is a registered vessel name is a vessel,
                    # wherever it sits. This also fills in vessel_name for
                    # folders/files that the depth heuristic above missed.
                    classification = classify_deletion(path_parts, is_folder, vessel_names)
                    if is_folder and classification["classification"] == "vessel":
                        is_vessel_folder = True
                    if is_vessel_folder:
                        kind = "vessel"
                        sp_vessel_names.add(name.lower())
                    else:
                        vessel_name = vessel_name or classification["vessel_name"]
                        category = category or classification["category"]
                        sub_category = sub_category or classification["sub_category"]

                    # Derive item_type label for display
                    if is_folder:
                        item_type = "File folder"
                    else:
                        ext = name.rsplit(".", 1)[-1].upper() if "." in name else ""
                        item_type = f"{ext} File" if ext else "File"

                    node = {
                        "id": it["id"],
                        "name": name,
                        "kind": kind,
                        "upload": False,
                        "month_driven": False,
                        "has_children": False,
                        "main_folder": main_folder,
                        "original_path": original_path,
                        "vessel_name": vessel_name,
                        "category": category,
                        "sub_category": sub_category,
                        "size": it.get("size"),
                        "deleted_at": it.get("deletedDateTime"),
                        "modified": it.get("lastModifiedDateTime"),
                        "item_type": item_type,
                        "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "",
                        "site_name": current_site_name,
                        "site_key": current_site_key,
                        "classification": classification["classification"],
                        "deleted_by_email": None,
                        "deleted_by_name": None,
                        "reason": None,
                    }
                    out.append(node)

            # Enrich with deletion_log (Deleted By / Reason / authoritative
            # classification) — the single source of truth written at
            # deletion-capture time by _record_deletion. Looked up by
            # drive_item_id so this stays cheap even with hundreds of items.
            with SessionLocal() as db:
                ids = [n["id"] for n in out if n.get("id")]
                log_rows = (
                    db.query(models.DeletionLog)
                    .filter(models.DeletionLog.drive_item_id.in_(ids))
                    .all()
                    if ids else []
                )
                log_by_id = {row.drive_item_id: row for row in log_rows}
                for node in out:
                    row = log_by_id.get(node.get("id"))
                    if row:
                        node["deleted_by_email"] = row.deleted_by_email
                        node["deleted_by_name"] = row.deleted_by_name
                        node["reason"] = row.reason
                        node["classification"] = row.classification
                        if row.classification == "vessel":
                            node["kind"] = "vessel"
                            sp_vessel_names.add(node["name"].lower())
                        node["vessel_name"] = node["vessel_name"] or row.vessel_name or ""
                        node["category"] = node["category"] or row.category or ""
                        node["sub_category"] = node["sub_category"] or row.sub_category or ""

                # Merge deletion_log rows for app-initiated folder/file deletes
                # that SharePoint's own recycle bin listing hasn't propagated
                # yet (same reasoning as the DeletedVessel merge below), so
                # a just-deleted item is visible immediately.
                seen_ids = {n["id"] for n in out}
                recent_cutoff = datetime.utcnow() - timedelta(hours=24)
                unmatched_logs = (
                    db.query(models.DeletionLog)
                    .filter(
                        models.DeletionLog.item_type != "vessel",
                        models.DeletionLog.deleted_at >= recent_cutoff,
                    )
                    .order_by(models.DeletionLog.deleted_at.desc())
                    .all()
                )
            for row in unmatched_logs:
                if row.drive_item_id and row.drive_item_id in seen_ids:
                    continue
                out.append({
                    "id": row.drive_item_id or f"log_{row.id}",
                    "name": row.item_name,
                    "kind": "folder" if row.item_type == "folder" else "file",
                    "upload": False,
                    "month_driven": False,
                    "has_children": False,
                    "main_folder": (row.original_path or "").split("/")[0] if row.original_path else "",
                    "original_path": row.original_path or row.item_name,
                    "vessel_name": row.vessel_name or "",
                    "category": row.category or "",
                    "sub_category": row.sub_category or "",
                    "size": None,
                    "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
                    "modified": None,
                    "item_type": "File folder" if row.item_type == "folder" else "File",
                    "ext": row.item_name.rsplit(".", 1)[-1].lower() if "." in row.item_name else "",
                    "site_name": row.site_name or settings.active_site or "SharePoint",
                    "site_key": row.site_key or settings.active_site or "default",
                    "classification": row.classification,
                    "deleted_by_email": row.deleted_by_email,
                    "deleted_by_name": row.deleted_by_name,
                    "reason": row.reason,
                })

            # Merge DB-tracked deleted vessels that SharePoint hasn't propagated yet.
            # This ensures all deleted vessels appear immediately, even when the
            # SharePoint recycle bin API returns a partial/delayed list.
            with SessionLocal() as db:
                db_deleted = db.query(models.DeletedVessel).order_by(
                    models.DeletedVessel.deleted_at.desc()
                ).all()
                vessel_log_by_name = {
                    row.vessel_name.lower(): row
                    for row in db.query(models.DeletionLog)
                    .filter(models.DeletionLog.item_type == "vessel")
                    .order_by(models.DeletionLog.deleted_at.asc())
                    .all()
                    if row.vessel_name
                }
            for dv in db_deleted:
                if dv.vessel_name.lower() not in sp_vessel_names:
                    fallback_site = drive_site_map.get(str(primary_drive_id), {}).get("site_name", settings.active_site or "SharePoint")
                    fallback_key = drive_site_map.get(str(primary_drive_id), {}).get("site_key", "default")
                    log_row = vessel_log_by_name.get(dv.vessel_name.lower())
                    out.append({
                        "id": dv.drive_item_id or f"db_vessel_{dv.id}",
                        "name": dv.vessel_name,
                        "kind": "vessel",
                        "upload": False,
                        "month_driven": False,
                        "has_children": False,
                        "main_folder": "Vessels",
                        "original_path": dv.original_path or f"Vessels/Specific Vessels/{dv.vessel_name}",
                        "vessel_name": "",
                        "category": "",
                        "sub_category": "",
                        "size": None,
                        "deleted_at": dv.deleted_at.isoformat() if dv.deleted_at else None,
                        "modified": None,
                        "item_type": "vessel",
                        "ext": "",
                        "vessel_imo": dv.vessel_imo,
                        "vessel_type": dv.vessel_type,
                        "site_name": dv.site_name or fallback_site,
                        "site_key": dv.site_key or fallback_key,
                        "classification": "vessel",
                        "deleted_by_email": (log_row.deleted_by_email if log_row else None) or dv.deleted_by_email,
                        "deleted_by_name": log_row.deleted_by_name if log_row else None,
                        "reason": log_row.reason if log_row else None,
                    })

            return out
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to get deleted nodes: {e}")
            return []

    async def reconcile_native_deletions(self) -> dict:
        """Backfill deletion_log for folder/file deletions this backend
        never saw directly — either made straight in SharePoint's native UI,
        or (the common case) made client-side against Graph from the SPFx
        web part without the frontend's log-deletion call landing (e.g. an
        older cached bundle, a network blip). Runs on a schedule (see
        scheduler.py); best-effort throughout and never raises, since it
        only fills in attribution that's missing, not deletion state itself.
        """
        try:
            nodes = await self.get_deleted_nodes()
        except Exception as exc:
            log.warning("[reconcile_native_deletions] get_deleted_nodes failed: %s", exc)
            return {"error": str(exc)}

        unlogged = [
            n for n in nodes
            if n.get("kind") != "vessel" and not n.get("deleted_by_email") and n.get("id")
            and not str(n["id"]).startswith(("db_vessel_", "log_"))
        ]
        if not unlogged:
            return {"backfilled": 0}

        # Pull each distinct site's SharePoint REST recycle bin once, so a
        # batch of unlogged items doesn't multiply into N REST calls.
        by_site_key: dict[str, list[dict]] = {}
        for n in unlogged:
            by_site_key.setdefault(n.get("site_key") or "default", []).append(n)

        rest_items_by_site: dict[str, list[dict]] = {}
        for site_key in by_site_key:
            site_url = None
            try:
                if site_key and site_key not in ("default", settings.active_site or ""):
                    site_url = Settings.load_site_config(site_key).sharepoint_site_url
                else:
                    site_url = settings.sharepoint_site_url
            except Exception:
                site_url = settings.sharepoint_site_url
            rest_items_by_site[site_key] = await gd.get_recycle_bin_items_rest(site_url) if site_url else []

        backfilled = 0
        for n in unlogged:
            site_key = n.get("site_key") or "default"
            rest_items = rest_items_by_site.get(site_key, [])
            name_lower = (n.get("name") or "").lower()
            node_deleted_at = n.get("deleted_at")
            matched = None
            for r in rest_items:
                if (r.get("LeafName") or "").lower() != name_lower:
                    continue
                # Timestamp proximity guard (+/- 5 min): the REST recycle bin
                # GUID and the Graph driveItem id are not the same
                # identifier, so name + close-in-time is the best available
                # match without a shared key.
                if node_deleted_at and r.get("DeletedDate"):
                    try:
                        t1 = datetime.fromisoformat(str(node_deleted_at).replace("Z", "+00:00"))
                        t2 = datetime.fromisoformat(str(r["DeletedDate"]).replace("Z", "+00:00"))
                        if abs((t1 - t2).total_seconds()) > 300:
                            continue
                    except Exception:
                        pass
                matched = r
                break

            parsed_deleted_at = None
            if node_deleted_at:
                try:
                    parsed_deleted_at = datetime.fromisoformat(str(node_deleted_at).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    parsed_deleted_at = None

            await self._record_deletion(
                item_type="folder" if n.get("kind") == "folder" else "file",
                drive_item_id=n.get("id"),
                name=n.get("name") or "",
                original_path=n.get("original_path"),
                site_name=n.get("site_name"),
                site_key=n.get("site_key"),
                requesting_email=(matched or {}).get("DeletedByEmail"),
                requesting_name=(matched or {}).get("DeletedByName"),
                reason=None,
                source="native_spo",
                deleted_at=parsed_deleted_at,
            )
            backfilled += 1

        return {"backfilled": backfilled}

    async def restore_deleted_item(
        self, item_id: str, item_type: str = "folder", requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None,
    ):
        name, dept, vessel = await self._resolve_item_context(item_id, item_type, item_name, department, vessel_name)
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel}" if vessel else ""
        return await self._admin_or_pending(
            action_type="restore_from_recycle_bin",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=dept,
            vessel_name=vessel,
            target_id=item_id,
            target_description=name,
            payload={"item_type": item_type},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to restore "
                f"'{name}' from the Recycle Bin{vessel_clause}."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) restored '{name}' from the Recycle Bin"
                f"{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_restore_deleted(item_id),
        )

    async def _execute_restore_deleted(self, item_id):
        """Restore a vessel that was moved to the recycle bin.

        The recycle-bin UI tracks deleted vessels in the `deleted_vessels` table,
        but those rows were never converted back into active vessel records. This
        method reactivates the vessel in `vessels` and removes the recycle-bin
        audit row so it immediately reappears in the vessel list.
        """
        item_key = str(item_id or "").strip()
        vessel_name = None
        deleted_vessel = None

        with SessionLocal() as db:
            if item_key.startswith("db_vessel_"):
                try:
                    row_id = int(item_key.split("_")[-1])
                    deleted_vessel = db.query(models.DeletedVessel).filter_by(id=row_id).one_or_none()
                except Exception:
                    deleted_vessel = None
            if deleted_vessel is None:
                deleted_vessel = db.query(models.DeletedVessel).filter(
                    models.DeletedVessel.drive_item_id == item_key
                ).one_or_none()
            if deleted_vessel is None:
                db.query(models.DeletedVessel).filter(
                    models.DeletedVessel.vessel_name == item_key
                ).one_or_none()
            if deleted_vessel is None:
                return {"restored": False, "message": "Deleted vessel not found"}
            vessel_name = deleted_vessel.vessel_name
            site_key = (deleted_vessel.site_key or settings.active_site or "dev").strip() or "dev"
            existing = db.query(models.Vessel).filter_by(name=vessel_name).one_or_none()
            if existing is None:
                vessel = models.Vessel(
                    name=vessel_name,
                    imo=deleted_vessel.vessel_imo,
                    shipyard=None,
                    hull_number=None,
                    vessel_type=deleted_vessel.vessel_type,
                    is_provisioned=True,
                    provisioned_site_ids=[site_key],
                    provisioned_site_key=site_key,
                    vessel_folder_path=deleted_vessel.original_path,
                    restored_at=datetime.utcnow(),
                )
                db.add(vessel)
            else:
                existing.imo = existing.imo or deleted_vessel.vessel_imo
                existing.vessel_type = existing.vessel_type or deleted_vessel.vessel_type
                existing.is_provisioned = True
                existing.provisioned_site_ids = list(existing.provisioned_site_ids or [])
                if site_key not in existing.provisioned_site_ids:
                    existing.provisioned_site_ids.append(site_key)
                existing.provisioned_site_key = existing.provisioned_site_key or site_key
                existing.vessel_folder_path = existing.vessel_folder_path or deleted_vessel.original_path
                existing.restored_at = datetime.utcnow()
            db.delete(deleted_vessel)
            db.commit()

        from ..main import invalidate_folder_caches
        invalidate_folder_caches()
        return {"restored": True, "vessel_name": vessel_name}

    async def permanent_delete_item(
        self, item_id: str, item_type: str, requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None,
    ):
        name, dept, vessel = await self._resolve_item_context(item_id, item_type, item_name, department, vessel_name)
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel}" if vessel else ""
        return await self._admin_or_pending(
            action_type="permanent_delete",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=dept,
            vessel_name=vessel,
            target_id=item_id,
            target_description=name,
            payload={"item_type": item_type},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to permanently "
                f"delete '{name}'{vessel_clause}."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) permanently deleted '{name}'"
                f"{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_permanent_delete(item_id, item_type),
        )

    async def _execute_permanent_delete(self, item_id, item_type):
        # Helper: delete the DB-tracked DeletedVessel row for this item_id.
        def _cleanup_db_vessel():
            with SessionLocal() as db:
                dv = db.query(models.DeletedVessel).filter(
                    (models.DeletedVessel.drive_item_id == item_id) |
                    (models.DeletedVessel.id == int(item_id.split("_")[-1]) if item_id.startswith("db_vessel_") else False)
                ).one_or_none()
                if dv:
                    db.delete(dv)
                    db.commit()

        # For site drives: if this is a DB-only vessel record (no real SPO item),
        # just remove the DB row. Otherwise soft-delete via Graph.
        drive_id = await self._drive()
        try:
            if not item_id.startswith("db_vessel_"):
                await gd.delete_item(drive_id, item_id)
            _cleanup_db_vessel()
            return {"deleted": True}
        except Exception as e:
            log.error(f"Failed to permanently delete item {item_id}: {e}")
            return {"deleted": False}

    # -------------------------------------------------------------- alerts
    async def list_folder_alerts(self, unread_only=False):
        with SessionLocal() as db:
            query = db.query(models.FolderAlert).order_by(models.FolderAlert.created_at.desc())
            if unread_only:
                query = query.filter(models.FolderAlert.read == False)
            rows = query.all()
            return [
                {
                    "id": str(r.id),
                    "drive_item_id": r.drive_item_id,
                    "folder_name": r.folder_name,
                    "folder_path": r.folder_path,
                    "parent_folder_id": r.parent_folder_id,
                    "vessel_name": r.vessel_name,
                    "department": r.department,
                    "created_by_email": r.created_by_email,
                    "created_by_name": r.created_by_name,
                    "alert_type": r.alert_type,
                    "read": r.read,
                    "read_at": r.read_at.isoformat() if r.read_at else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]

    async def mark_folder_alert_read(self, alert_id: str, read: bool = True):
        from datetime import datetime, timezone
        # Deletion-popup alerts (see list_all_alerts) carry a "del_{id}"
        # pseudo-id sourced from deletion_log, not folder_alerts.
        if alert_id.startswith("del_"):
            raw_id = alert_id[len("del_"):]
            if not raw_id.isdigit():
                return None
            with SessionLocal() as db:
                row = db.get(models.DeletionLog, int(raw_id))
                if row is None:
                    return None
                row.read = read
                row.read_at = datetime.now(timezone.utc) if read else None
                db.commit()
                return {"id": alert_id, "read": row.read, "read_at": row.read_at.isoformat() if row.read_at else None}
        with SessionLocal() as db:
            row = db.get(models.FolderAlert, int(alert_id)) if alert_id.isdigit() else None
            if row is None:
                return None
            row.read = read
            row.read_at = datetime.now(timezone.utc) if read else None
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            return {
                "id": str(row.id),
                "read": row.read,
                "read_at": row.read_at.isoformat() if row.read_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }

    async def mark_all_folder_alerts_read(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            result = db.query(models.FolderAlert).filter(models.FolderAlert.read == False).update(
                {models.FolderAlert.read: True, models.FolderAlert.read_at: now, models.FolderAlert.updated_at: now},
                synchronize_session=False,
            )
            result += db.query(models.DeletionLog).filter(models.DeletionLog.read == False).update(
                {models.DeletionLog.read: True, models.DeletionLog.read_at: now},
                synchronize_session=False,
            )
            db.commit()
            return {"marked": result}

    @staticmethod
    def _approval_public(row):
        # Compute a human-readable document/action name for the Approvals UI.
        # For upload actions: use the filename.
        # For non-upload actions: prefer target_description, fall back to message, then action_type.
        action_type = row.action_type or "upload"
        document_name = (
            row.filename
            or row.target_description
            or row.message
            or action_type.replace("_", " ").title()
        )
        return {
            "id": str(row.id),
            "filename": row.filename,
            "document_name": document_name,
            "content_type": row.content_type,
            "size": row.size,
            "uploaded_by_email": row.uploaded_by_email,
            "uploaded_by_name": row.uploaded_by_name,
            "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
            "destination_folder_id": row.destination_folder_id,
            "destination_path": row.destination_path,
            "is_month_upload": row.is_month_upload,
            "category": row.category,
            "detected_month": row.detected_month,
            "status": row.status,
            "decided_by_email": row.decided_by_email,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "rejection_reason": row.rejection_reason,
            "final_path": row.final_path,
            "entry_kind": row.entry_kind,
            "action_type": action_type,
            "department": row.department,
            "vessel_id": str(row.vessel_id) if row.vessel_id else None,
            "vessel_name": row.vessel_name,
            "target_id": row.target_id,
            "target_description": row.target_description,
            "payload": json.loads(row.payload_json) if row.payload_json else {},
            "changes": json.loads(row.changes_json) if row.changes_json else [],
            "message": row.message,
        }


def _approval_as_job(approval, completed=False):
    """Shape an approval request like the existing Job contract so the
    frontend's upload-toast + polling code needs no structural changes.
    completed=True (SPE Admin bypass) reports "done" so the frontend shows
    its normal immediate-success toast instead of "Awaiting approval"."""
    return {
        "id": approval["id"],
        "filename": approval["filename"],
        "status": "done" if completed else "pending",
        "destination": (approval.get("final_path") or approval["destination_path"]),
        "detected_month": approval["detected_month"],
    }
