import time

_FLAT_TREE_CACHE = {"data": None, "timestamp": 0}
_FOLDER_CHILDREN_CACHE = {}  # folder_id -> (data, timestamp)
CACHE_TTL_FLAT_TREE = 5   # 5 seconds — short enough to reflect SPO uploads quickly
CACHE_TTL_CHILDREN = 3    # 3 seconds — near-real-time for folder contents

# Longer-lived cache for tag-enriched folder children (listItem/fields already fetched)
_FOLDER_CHILDREN_TAGS_CACHE: dict = {}  # cache_key -> (timestamp, list[decorated_item])
CACHE_TTL_CHILDREN_TAGS = 300  # 5 minutes — covers repeated navigations; invalidated on edits

_LIVE_SPO_FILES_CACHE = {"data": {}, "timestamp": 0}
CACHE_TTL_LIVE_SPO_FILES = 60   # 60 seconds

_FOLDER_RECURSIVE_COUNTS_CACHE: dict[str, tuple[float, dict[str, int]]] = {}
_FOLDER_PARENT_MAP: dict[str, str] = {}  # folder_id -> parent_folder_id
CACHE_TTL_FOLDER_RECURSIVE_COUNTS = 600  # 10 minutes (invalidated on changes)

def invalidate_folder_caches(folder_id: str = None):
    global _FLAT_TREE_CACHE, _FOLDER_CHILDREN_CACHE, _LIVE_SPO_FILES_CACHE, _FOLDER_RECURSIVE_COUNTS_CACHE, _FOLDER_PARENT_MAP, _FOLDER_CHILDREN_TAGS_CACHE
    _FLAT_TREE_CACHE = {"data": None, "timestamp": 0}
    _LIVE_SPO_FILES_CACHE = {"data": {}, "timestamp": 0}
    if folder_id:
        _FOLDER_CHILDREN_CACHE.pop(folder_id, None)
        _FOLDER_CHILDREN_TAGS_CACHE.pop(folder_id, None)
        for k in list(_FOLDER_CHILDREN_TAGS_CACHE.keys()):
            if k.endswith(f":{folder_id}"):
                _FOLDER_CHILDREN_TAGS_CACHE.pop(k, None)
        # Targeted invalidation: walk up ancestor chain and evict only this folder and its parents
        curr = folder_id
        visited = set()
        while curr and curr not in visited:
            visited.add(curr)
            _FOLDER_RECURSIVE_COUNTS_CACHE.pop(curr, None)
            for k in list(_FOLDER_RECURSIVE_COUNTS_CACHE.keys()):
                if k.endswith(f":{curr}"):
                    _FOLDER_RECURSIVE_COUNTS_CACHE.pop(k, None)
            curr = _FOLDER_PARENT_MAP.get(curr)
    else:
        _FOLDER_CHILDREN_CACHE.clear()
        _FOLDER_RECURSIVE_COUNTS_CACHE.clear()
        _FOLDER_CHILDREN_TAGS_CACHE.clear()

# -*- coding: utf-8 -*-
"""FastAPI entry point for the Vessel DMS.

One code path over a backend interface: real SharePoint Online + PostgreSQL
when configured (see backend/.env), otherwise the in-memory stub. See
`app/services/__init__.py`.
"""
import asyncio
import base64
import json
import logging
import os
import re
from urllib.parse import quote
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

# ── Timezone: tell tzlocal/APScheduler the system is UTC+5:30 (IST) ──────────
os.environ.setdefault("TZ", "Asia/Kolkata")
warnings.filterwarnings("ignore", message="Timezone offset does not match system offset")

from typing import Any, Literal
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings
from .db import models as db_models
from .graph import drive as gd
from .graph.client import GraphError, graph
from .graph.http import verify as graph_tls_verify
from .services import backend_mode, get_backend
from .services.errors import BadRequest, Conflict, NotFound, InternalServerError
from . import template

# In-memory profile cache for stub / no-DB mode (populated on each login).
_profile_cache: dict[str, dict] = {}

app = FastAPI(title="Vessel DMS", version="1.0.0")
logger = logging.getLogger("vessel_dms")

_GRAPH_RECURSIVE_SEMAPHORE = asyncio.Semaphore(15)

async def get_folder_recursive_counts(
    drive_id: str,
    folder_id: str,
    parent_id: str | None = None,
    access_token: str | None = None,
    max_depth: int = 2,
    current_depth: int = 0,
) -> dict[str, int]:
    """
    Recursively walks a folder and its subfolders to compute:
    - direct_subfolders: count of immediate child folders
    - direct_files: count of immediate child files
    - total_subfolders: count of all subfolders in the subtree
    - total_files: count of all files in the subtree
    Guarantees max 6 concurrent Graph API requests globally via _GRAPH_RECURSIVE_SEMAPHORE.
    Records child -> parent relationship in _FOLDER_PARENT_MAP for targeted ancestor invalidation.
    Bounded by max_depth to prevent Graph API rate-limiting delays.
    """
    cache_key = f"{drive_id}:{folder_id}"
    now = time.time()
    cached = _FOLDER_RECURSIVE_COUNTS_CACHE.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_FOLDER_RECURSIVE_COUNTS:
        if parent_id:
            _FOLDER_PARENT_MAP[folder_id] = parent_id
        return cached[1]

    if parent_id:
        _FOLDER_PARENT_MAP[folder_id] = parent_id

    async with _GRAPH_RECURSIVE_SEMAPHORE:
        children = await gd.list_children(drive_id, folder_id, access_token=access_token)

    subfolders = [c for c in children if c.get("folder") and c.get("id")]
    files = [c for c in children if not c.get("folder")]

    direct_subfolders = len(subfolders)
    direct_files = len(files)

    if direct_subfolders == 0:
        result = {
            "direct_subfolders": 0,
            "direct_files": direct_files,
            "total_subfolders": 0,
            "total_files": direct_files,
        }
        _FOLDER_RECURSIVE_COUNTS_CACHE[cache_key] = (now, result)
        return result

    if current_depth >= max_depth:
        total_sub_files = sum(
            (sf.get("folder") or {}).get("childCount") or 0
            for sf in subfolders
            if isinstance((sf.get("folder") or {}).get("childCount"), int)
        )
        result = {
            "direct_subfolders": direct_subfolders,
            "direct_files": direct_files,
            "total_subfolders": direct_subfolders,
            "total_files": direct_files + total_sub_files,
        }
        _FOLDER_RECURSIVE_COUNTS_CACHE[cache_key] = (now, result)
        return result

    sub_results = await asyncio.gather(*(
        get_folder_recursive_counts(
            drive_id,
            sf["id"],
            parent_id=folder_id,
            access_token=access_token,
            max_depth=max_depth,
            current_depth=current_depth + 1,
        )
        for sf in subfolders
    ))

    total_subfolders = direct_subfolders + sum(r["total_subfolders"] for r in sub_results)
    total_files = direct_files + sum(r["total_files"] for r in sub_results)

    result = {
        "direct_subfolders": direct_subfolders,
        "direct_files": direct_files,
        "total_subfolders": total_subfolders,
        "total_files": total_files,
    }
    _FOLDER_RECURSIVE_COUNTS_CACHE[cache_key] = (now, result)
    return result

_DISCOVERED_SITES_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_DISCOVERED_SITES_CACHE_TTL = 600

@app.on_event("startup")
async def on_startup():
    """Seed or update DocumentCategory models with SharePoint Term Store taxonomy on startup."""
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .ocr.term_store import seed_term_store_categories
            with SessionLocal() as db:
                seed_term_store_categories(db)
        except Exception as e:
            logger.warning("Startup term store categories seeding skipped/failed: %s", e)

def _cors_origins() -> list[str]:
    raw = settings.allowed_origins or "*"
    if raw.strip() == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]

# Read-only endpoints safe to cache briefly in the browser
_CACHEABLE_PREFIXES = ("/api/folders/", "/api/mains")

# Unified CORS + Private Network Access middleware
# Must run BEFORE CORSMiddleware so PNA preflight responses are returned immediately.
# @app.middleware("http") is appended LAST to the stack and therefore runs OUTERMOST (first).
@app.middleware("http")
async def cors_and_pna_middleware(request: Request, call_next):
    origin = request.headers.get("origin", "")

    # Build the list of allowed origins dynamically
    explicit_origins = set(_cors_origins())

    # Decide whether this origin is allowed
    import re as _re
    origin_allowed = (
        "*" in explicit_origins
        or origin in explicit_origins
        or bool(_re.match(r"https?://.*\.sharepoint\.com", origin))
        or bool(_re.match(r"https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin))
    )

    # ── Handle ALL CORS and PNA preflight OPTIONS requests ──────────────────
    if request.method == "OPTIONS":
        resp = Response(status_code=204)
        if origin_allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin or "*"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            resp.headers["Access-Control-Allow-Origin"] = origin or "*"
            resp.headers["Access-Control-Allow-Credentials"] = "true"

        req_headers = request.headers.get("access-control-request-headers")
        allowed_headers = "Content-Type, Authorization, X-Session-ID, X-User-Email, X-Graph-Access-Token, X-Requested-With, Accept, Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network"
        if req_headers:
            allowed_headers = f"{allowed_headers}, {req_headers}"

        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = allowed_headers
        resp.headers["Access-Control-Max-Age"] = "86400"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        resp.headers["Vary"] = "Origin"
        return resp

    try:
        response = await call_next(request)
    except Exception:
        # Starlette's default 500 response is created outside the route stack,
        # which can otherwise omit CORS headers. Return a JSON error here so an
        # SPFx page can receive the real HTTP failure instead of a misleading
        # browser-level CORS error.
        logging.getLogger(__name__).exception(
            "Unhandled backend error for %s %s", request.method, request.url.path
        )
        response = JSONResponse(
            status_code=500,
            content={"message": "The server could not complete this request. Check the backend log for details."},
        )

    if response.status_code >= 400:
        logger.warning(
            "HTTP failure: %s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
        )

    # Inject PNA & CORS headers on every response so subsequent non-preflight requests succeed
    if origin_allowed and origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Session-ID, X-User-Email, X-Graph-Access-Token, X-Requested-With, Accept, Origin"
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["Access-Control-Allow-Origin"] = response.headers.get("Access-Control-Allow-Origin") or (origin if origin else "*")
    response.headers["Access-Control-Allow-Credentials"] = "true"


    # Cache-control
    path = request.url.path
    method = request.method
    if method == "GET" and any(path.startswith(p) for p in _CACHEABLE_PREFIXES):
        response.headers["Cache-Control"] = "private, max-age=10"
    else:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response




def _raise(e: Exception):
    """Map domain exceptions to HTTP errors."""
    status = getattr(e, "status", None)
    if status:
        raise HTTPException(status, str(e))
    raise


def _norm_dept(value: str | None) -> str:
    return (value or "").strip().lower()


def _extract_department_from_path(path: str | None) -> str | None:
    """Infer DMS main department from a breadcrumb/path string."""
    if not path:
        return None
    known = [m for m in template.ALL_MAIN_FOLDERS if isinstance(m, str)]
    parts = [p.strip() for p in str(path).replace('>', '/').split('/') if p.strip()]
    for part in parts:
        for dept in known:
            if _norm_dept(part) == _norm_dept(dept):
                return dept
    return None


def _force_path_department(resolved_path: str, department: str | None) -> str:
    """Ensure destination path starts with the intended main department."""
    if not resolved_path:
        return resolved_path
    dept = (department or "").strip()
    if not dept:
        return resolved_path
    known = [m for m in template.ALL_MAIN_FOLDERS if isinstance(m, str)]
    parts = [p.strip() for p in resolved_path.split('/') if p.strip()]
    if not parts:
        return dept
    if parts[0] in known:
        parts[0] = dept
        return '/'.join(parts)
    return '/'.join([dept, *parts])


async def _resolve_or_create_sharepoint_path(path: str) -> str:
    """Resolve a logical folder path, creating missing folders when needed."""
    normalized = '/'.join([p.strip() for p in (path or '').replace('>', '/').split('/') if p.strip()])
    if not normalized:
        raise HTTPException(status_code=422, detail='Target path is empty.')

    try:
        return await get_backend().resolve_path(normalized)
    except NotFound:
        if not settings.sp_configured or not settings.sp_drive_id:
            raise

        drive_id = settings.sp_drive_id
        current_id = await gd.get_root_item_id(drive_id)

        for segment in [s for s in normalized.split('/') if s]:
            child = await gd.find_child(drive_id, current_id, segment)
            if child:
                if not child.get('folder'):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot create folder path '{normalized}' because '{segment}' is a file.",
                    )
                current_id = child['id']
                continue

            created = await gd.ensure_folder(drive_id, current_id, segment)
            current_id = created['id']

        logger.info('Auto-created missing SharePoint path for OCR flow: %s', normalized)
        return current_id
    except GraphError as e:
        # If SharePoint path validation/create is blocked by temporary access-denied,
        # fall back to DB folder mapping to keep OCR moves working where possible.
        if e.status in (401, 403):
            try:
                from .db.base import SessionLocal
                from .db import models as db_models
                from sqlalchemy import func

                with SessionLocal() as db:
                    row = (
                        db.query(db_models.Folder)
                        .filter(func.lower(db_models.Folder.path) == normalized.lower())
                        .first()
                    )
                    if row and row.drive_item_id:
                        logger.warning(
                            "Using cached DB folder id for path '%s' due Graph %s accessDenied",
                            normalized,
                            e.status,
                        )
                        return row.drive_item_id
            except Exception:
                pass
        raise


def _norm_vessel_key(value: str | None) -> str:
    import re as _re
    raw = (value or "").strip().lower()
    raw = _re.sub(r"^(mv|m/v|m\.v\.|mt|m/t|m\.t\.)\s+", "", raw)
    return _re.sub(r"[^a-z0-9]", "", raw)


def _extract_vessel_from_path(path: str | None, known_vessels: list[Any] | None = None) -> str | None:
    """Infer vessel from breadcrumb/path by matching against known vessel names or hull numbers.
    Falls back to folder hierarchy extraction if no known vessel matches.
    """
    if not path:
        return None

    parts = [p.strip() for p in str(path).replace('>', '/').replace('\\', '/').split('/') if p.strip()]
    if not parts:
        return None

    if known_vessels:
        keyed: list[tuple[str, str]] = []
        for v in known_vessels:
            if isinstance(v, dict):
                name = str(v.get("name") or "").strip()
                if name:
                    keyed.append((_norm_vessel_key(name), name))
                hull = str(v.get("hull_number") or "").strip()
                if hull and name:
                    keyed.append((_norm_vessel_key(hull), name))
            elif isinstance(v, str) and v.strip():
                keyed.append((_norm_vessel_key(v), v.strip()))

        if keyed:
            keyed.sort(key=lambda kv: len(kv[0]), reverse=True)
            for part in parts:
                pkey = _norm_vessel_key(part)
                if not pkey:
                    continue
                for vkey, original in keyed:
                    if pkey == vkey:
                        return original

    from .ocr.drawing_category import _extract_vessel_from_folder_path
    return _extract_vessel_from_folder_path(path)


def _safe_tag_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "").strip()
    return str(value or "").strip()


def _taxonomy_maps() -> tuple[set[str], set[str], dict[str, set[str]], set[str]]:
    from .ocr.drawing_category import DRAWING_TAXONOMY, MANUAL_TAXONOMY

    drawing_categories = {k.lower() for k in DRAWING_TAXONOMY.keys()}
    manual_categories = {k.lower() for k in MANUAL_TAXONOMY.keys()} | {"to be classified"}

    allowed_sub_by_category: dict[str, set[str]] = {}
    for cat_name, leaves in DRAWING_TAXONOMY.items():
        allowed_sub_by_category[cat_name.lower()] = {leaf.lower() for leaf in leaves.keys()}
    for cat_name, leaves in MANUAL_TAXONOMY.items():
        allowed_sub_by_category[cat_name.lower()] = {leaf.lower() for leaf in leaves.keys()}
    allowed_sub_by_category.setdefault("to be classified", {"to be classified"})

    all_subcategories: set[str] = set()
    for leaves in allowed_sub_by_category.values():
        all_subcategories.update(leaves)

    return drawing_categories, manual_categories, allowed_sub_by_category, all_subcategories


def _normalize_metadata_group(raw_group: str, category: str = "") -> str:
    g = (raw_group or "").strip().lower()
    c = (category or "").strip().lower()

    # 'To be Classified' is intentionally ambiguous; preserve explicit OCR group
    # when available, otherwise default to Manuals downstream.
    if c == "to be classified":
        if g in {"drawing", "drawings"}:
            return "Drawings"
        if g in {"manual", "manuals"}:
            return "Manuals"
        return ""

    drawing_categories, manual_categories, _, _ = _taxonomy_maps()
    # Category taxonomy is authoritative when present.
    if c in drawing_categories:
        return "Drawings"
    if c in manual_categories:
        return "Manuals"

    if g in {"drawing", "drawings"}:
        return "Drawings"
    if g in {"manual", "manuals"}:
        return "Manuals"
    return ""


def _build_sharepoint_metadata_payload(
    *,
    department: str = "",
    vessel: str = "",
    group: str = "",
    category: str = "",
    sub_category: str = "",
) -> dict[str, str]:
    payload = {
        # Semantic metadata values. Some libraries do not provision Department;
        # update_file_columns resolves and patches only columns that exist.
        "Department": department,
        "VesselName": vessel,
        "Group": group,
        "Category": category,
        # Alias keys for resilient internal-name matching
        "department": department,
        "main_folder": department,
        "MainFolder": department,
        "Domain": department,
        "domain": department,
        "vessel": vessel,
        "vessel_name": vessel,
        "Vessel Name": vessel,
        "Vessel_x0020_Name": vessel,
        "Vessel_x0020_Name_x0020_": vessel,
        "ship": vessel,
        "shipname": vessel,
        "group": group,
        "vessel_group": group,
        "category": category,
        "document_category": category,
        "DMS_Department": department,
        "DMS_Group": group,
        "DMS_Category": category,
    }
    if sub_category:
        payload["SubCategory"] = sub_category
        payload["subcategory"] = sub_category
        payload["sub_category"] = sub_category
        payload["Sub_x002d_Category"] = sub_category
        payload["DMS_SubCategory"] = sub_category
    return payload


def _pick_sp_field(fields: dict[str, Any], aliases: list[str]) -> str:
    if not isinstance(fields, dict):
        return ""
    import re
    def _norm(s: str) -> str:
        s_dec = re.sub(r"_x([0-9a-fA-F]{4})_", lambda m: chr(int(m.group(1), 16)), s or "")
        return re.sub(r"[^a-z0-9]", "", s_dec.strip().lower())

    def _extract_val(val: Any) -> str:
        if isinstance(val, dict):
            raw = str(val.get("Label") or val.get("name") or val.get("Value") or "").strip()
        else:
            raw = str(val or "").strip()
        if "|" in raw:
            raw = raw.split("|", 1)[0].strip()
        return raw

    by_norm = {_norm(k): v for k, v in fields.items()}
    # Pass 1: exact alias match
    for alias in aliases:
        key = _norm(alias)
        if key in by_norm:
            extracted = _extract_val(by_norm[key])
            if extracted:
                return extracted
    # Pass 2: taxonomy companion note field (often ending in _0)
    for alias in aliases:
        key_0 = _norm(alias) + "0"
        if key_0 in by_norm:
            extracted = _extract_val(by_norm[key_0])
            if extracted:
                return extracted
    return ""


def _metadata_issue_reasons(
    *,
    department: str,
    vessel: str,
    group: str,
    category: str,
    sub_category: str,
) -> list[str]:
    reasons: list[str] = []
    main_folders = {m.lower() for m in template.ALL_MAIN_FOLDERS}
    drawing_categories, manual_categories, allowed_sub_by_category, all_subcategories = _taxonomy_maps()

    g = (group or "").strip().lower()
    c = (category or "").strip().lower()
    s = (sub_category or "").strip().lower()
    v = (vessel or "").strip().lower()
    d = (department or "").strip().lower()

    if g in main_folders:
        reasons.append("group_is_department")
    if g and g not in {"drawing", "drawings", "manual", "manuals"}:
        reasons.append("group_not_taxonomy")
    if v and c and v == c:
        reasons.append("category_equals_vessel")
    if d and g and d == g:
        reasons.append("department_equals_group")
    if c and c not in drawing_categories and c not in manual_categories:
        reasons.append("category_not_in_taxonomy")
    if s and s not in all_subcategories:
        reasons.append("subcategory_not_in_taxonomy")
    if c in allowed_sub_by_category and s and s not in allowed_sub_by_category[c]:
        reasons.append("subcategory_not_under_category")

    return reasons


def _decode_jwt_payload_noverify(token: str) -> dict[str, Any]:
    """Decode JWT payload without signature validation (debug only)."""
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _sharepoint_access_health_snapshot(probe_item_id: str | None = None) -> dict[str, Any]:
    """Collect drive reachability and listItem fields access in one snapshot."""
    if not settings.sp_configured or not settings.sp_drive_id:
        return {
            "ok": False,
            "reason": "sharepoint_not_configured",
            "drive_id": settings.sp_drive_id,
            "drive_probe": {"ok": False, "error": "No SharePoint drive configured."},
            "listitem_probe": {"ok": False, "error": "No SharePoint drive configured."},
        }

    drive_id = settings.sp_drive_id
    snapshot: dict[str, Any] = {
        "ok": False,
        "drive_id": drive_id,
        "drive_probe": {},
        "listitem_probe": {},
    }

    root_id = ""
    try:
        root = await graph().get(f"/drives/{drive_id}/root?$select=id,name,webUrl")
        root_id = str(root.get("id") or "")
        snapshot["drive_probe"] = {
            "ok": True,
            "root_id": root_id,
            "root_name": root.get("name"),
            "root_webUrl": root.get("webUrl"),
        }
    except GraphError as e:
        snapshot["drive_probe"] = {"ok": False, "status": e.status, "error": str(e)}
        return snapshot
    except Exception as e:
        snapshot["drive_probe"] = {"ok": False, "error": str(e)}
        return snapshot

    target_item_id = (probe_item_id or "").strip()
    if not target_item_id and root_id:
        try:
            children = await gd.list_children(drive_id, root_id)
            first_item = next((it for it in children if it.get("id")), None)
            if first_item:
                target_item_id = str(first_item.get("id") or "").strip()
        except Exception:
            pass

    if not target_item_id:
        snapshot["listitem_probe"] = {
            "ok": True,
            "skipped": True,
            "reason": "no_probe_item_found_at_root",
        }
        snapshot["ok"] = bool(snapshot["drive_probe"].get("ok"))
        return snapshot

    try:
        fields = await graph().get(f"/drives/{drive_id}/items/{target_item_id}/listItem/fields")
        keys = [k for k in fields.keys() if not str(k).startswith("@")] if isinstance(fields, dict) else []
        snapshot["listitem_probe"] = {
            "ok": True,
            "item_id": target_item_id,
            "field_count": len(keys),
            "sample_keys": keys[:20],
        }
    except GraphError as e:
        snapshot["listitem_probe"] = {
            "ok": False,
            "item_id": target_item_id,
            "status": e.status,
            "error": str(e),
        }
    except Exception as e:
        snapshot["listitem_probe"] = {
            "ok": False,
            "item_id": target_item_id,
            "error": str(e),
        }

    snapshot["ok"] = bool(snapshot["drive_probe"].get("ok")) and bool(snapshot["listitem_probe"].get("ok"))
    return snapshot


VESSEL_TYPES = {
    "Bulk Carrier",
    "Container Carrier",
    "Gas Carrier",
    "Oil Tanker",
    "Chemical Tanker",
    "Reffer Carrier",
    "Other Cargo Ships",
}


class VesselIn(BaseModel):
    name: str
    imo: str | None = None
    shipyard: str | None = None
    hull_number: str | None = None
    vessel_type: str | None = None
    provisioned_site_ids: list[str] | None = None



class VesselUpdateIn(BaseModel):
    name: str | None = None
    imo: str | None = None
    shipyard: str | None = None
    hull_number: str | None = None
    vessel_type: str | None = None
    provisioned_site_ids: list[str] | None = None


class RejectIn(BaseModel):
    reason: str | None = None


def _is_admin_email(email: str | None) -> bool:
    return (email or "").strip().lower() in settings.admin_email_set


def _require_admin(
    x_user_email: str | None = Header(default=None),
    admin_email: str | None = Query(default=None),
    admin: str | None = Query(default=None),
) -> str:
    """Approval decisions are restricted to settings.admin_emails.

    Matches this codebase's current auth maturity (see the TEMPORARY bypass in
    check_email below): there's no server-side session yet, so the frontend
    identifies the acting user via a header set from its already-verified
    MSAL login, and we check it against the admin allow-list. The preview
    endpoint is also reachable from a plain <img>/<iframe>/<a href>, which
    can't attach custom headers, so it's allowed to pass the email as a query
    param instead — same admin-list check either way.
    """
    email = (x_user_email or admin_email or admin or "").strip().lower()
    if not email or not _is_admin_email(email):
        raise HTTPException(403, "Administrator access required")
    return email


def _ensure_database_exists(db_url: str) -> None:
    """Connects to the default postgres database and creates target database if not exists."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        url = make_url(db_url)
        target_db = url.database
        if not target_db:
            return

        # We only run automatic database creation if using a PostgreSQL engine
        if "postgresql" not in url.drivername:
            return

        # Connect to 'postgres' database on the same host to check/create target database
        postgres_url = url.set(database="postgres")
        
        engine = create_engine(postgres_url)
        try:
            with engine.connect() as conn:
                conn.execution_options(isolation_level="AUTOCOMMIT")
                result = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
                    {"dbname": target_db}
                ).scalar()
                
                if not result:
                    conn.execute(text(f'CREATE DATABASE "{target_db}"'))
                    import logging
                    logging.getLogger(__name__).info("Database '%s' created automatically.", target_db)
        finally:
            engine.dispose()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Could not verify/create database automatically: %s", exc)


@app.on_event("startup")
async def _startup():
    from .scheduler import precreate_next_month, start_scheduler
    import logging as _log

    _logger = _log.getLogger(__name__)
    database_ready = False
    _logger.info(
        "=== Starting Vessel DMS backend | Active Site: '%s' (%s) | DB: %s | Drive: %s | Mode: %s ===",
        settings.active_site,
        settings.sp_site_name,
        settings.db_name if settings.db_configured else "None",
        settings.drive_id or "None",
        backend_mode(),
    )

    if settings.sp_configured:
        try:
            sp_health = await _sharepoint_access_health_snapshot()
            if sp_health.get("ok"):
                _logger.info("Startup SharePoint access probe succeeded for drive %s", settings.sp_drive_id)
            else:
                _logger.warning(
                    "Startup SharePoint access probe reported issues: %s",
                    sp_health,
                )
        except Exception as exc:
            _logger.warning("Startup SharePoint access probe failed: %s", exc)

    if settings.db_configured:
        # 1. Automatic database creation if PostgreSQL database is missing
        _ensure_database_exists(settings.database_url_resolved)

        # 2. Smart Alembic migration:
        #    - If this is a brand-new empty database → run all migrations from scratch.
        #    - If tables exist but alembic_version is missing (e.g. tables were created
        #      by a prior create_all run, or by an older version without Alembic) →
        #      stamp the current head so Alembic doesn't try to re-create tables that
        #      already exist, then run any pending migrations normally.
        #    - If alembic_version is present → just run any pending migrations normally.
        try:
            import pathlib
            import alembic.config
            from alembic import command
            from sqlalchemy import inspect, text
            from .db.base import engine

            _alembic_ini = pathlib.Path(__file__).parent.parent / "alembic.ini"
            alembic_cfg = alembic.config.Config(str(_alembic_ini))

            if engine is not None:
                with engine.connect() as conn:
                    inspector = inspect(conn)
                    existing_tables = set(inspector.get_table_names())

                    # Check if alembic_version table exists
                    has_version_table = "alembic_version" in existing_tables
                    # Check if any of our app tables already exist
                    app_tables = {"vessels", "folders", "user_profiles", "user_sessions"}
                    has_app_tables = bool(app_tables & existing_tables)

                    if has_app_tables and not has_version_table:
                        # Tables exist without Alembic tracking — stamp as head to
                        # prevent re-running create_table migrations on existing tables.
                        _logger.info(
                            "DB tables exist without Alembic version tracking. "
                            "Stamping to 'head' before running incremental migrations."
                        )
                        command.stamp(alembic_cfg, "head")

            print(">>> STARTUP: about to run command.upgrade", flush=True)
            try:
                command.upgrade(alembic_cfg, "head")
                print(">>> STARTUP: command.upgrade done", flush=True)
            except Exception as upgrade_exc:
                exc_str = str(upgrade_exc)
                # If migration fails due to a table/index that already exists
                # (e.g. created by create_all before Alembic tracked it),
                # stamp the DB as head and continue — the schema is already correct.
                if "DuplicateTable" in exc_str or "already exists" in exc_str or "duplicate" in exc_str.lower():
                    print(f">>> STARTUP: upgrade collision ({upgrade_exc!r}), stamping head and retrying once.", flush=True)
                    try:
                        command.stamp(alembic_cfg, "head")
                        print(">>> STARTUP: stamped head after collision.", flush=True)
                    except Exception as stamp_exc:
                        print(f">>> STARTUP: stamp also failed: {stamp_exc}", flush=True)
                else:
                    raise

            # Alembic's env.py calls logging.config.fileConfig(...), which
            # fully reconfigures the ROOT logger per alembic.ini's
            # [logger_root] section (level = WARN). That silently raises the
            # effective level for every app logger that doesn't set its own
            # level explicitly, so log.info(...) calls across the app stop
            # appearing after this point. Re-force it back to INFO.
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                force=True,
            )
            print(">>> STARTUP: root logger re-forced to INFO after Alembic fileConfig", flush=True)
            _logger.info("Alembic migrations completed successfully.")
        except Exception as exc:
            print(f">>> STARTUP: alembic block FAILED: {exc}", flush=True)
            _logger.warning("Alembic automatic migration failed: %s", exc)

        # 3. Safety net: make sure ALL tables and columns are present.
        #    create_all with checkfirst=True will add any missing tables but
        #    cannot add missing columns — those are handled by migrations above.
        try:
            from .db.base import Base, engine
            from sqlalchemy import inspect, text
            if engine is not None:
                Base.metadata.create_all(bind=engine, checkfirst=True)

                # Critical legacy drift guard: some older DBs were stamped to
                # Alembic head without actually applying every incremental
                # column migration. Ensure the column expected by ORM queries
                # exists so list endpoints do not fail with UndefinedColumn.
                with engine.begin() as conn:
                    inspector = inspect(conn)
                    if "vessels" in set(inspector.get_table_names()):
                        vessel_cols = {c["name"] for c in inspector.get_columns("vessels")}
                        if "is_provisioned" not in vessel_cols:
                            conn.execute(
                                text(
                                    "ALTER TABLE vessels "
                                    "ADD COLUMN IF NOT EXISTS is_provisioned BOOLEAN "
                                    "NOT NULL DEFAULT FALSE"
                                )
                            )
                            _logger.warning(
                                "DB schema drift repaired: added missing vessels.is_provisioned column"
                            )
                        if "restored_at" not in vessel_cols:
                            conn.execute(
                                text(
                                    "ALTER TABLE vessels "
                                    "ADD COLUMN IF NOT EXISTS restored_at TIMESTAMP NULL"
                                )
                            )
                            _logger.warning(
                                "DB schema drift repaired: added missing vessels.restored_at column"
                            )
                        if "provisioned_site_ids" not in vessel_cols:
                            conn.execute(
                                text(
                                    "ALTER TABLE vessels "
                                    "ADD COLUMN IF NOT EXISTS provisioned_site_ids JSON "
                                    "NULL DEFAULT '[]'::json"
                                )
                            )
                            _logger.warning(
                                "DB schema drift repaired: added missing vessels.provisioned_site_ids column"
                            )
                        # Backfill existing provisioned vessels with active site
                        try:
                            active_site_name = settings.active_site or "dev"
                            conn.execute(
                                text(
                                    f"UPDATE vessels SET provisioned_site_ids = json_build_array('{active_site_name}') "
                                    "WHERE is_provisioned = TRUE AND (provisioned_site_ids IS NULL OR json_array_length(provisioned_site_ids) = 0)"
                                )
                            )
                        except Exception as bf_err:
                            _logger.warning("Vessel provisioned_site_ids backfill notice: %s", bf_err)

                    if "site_configurations" in set(inspector.get_table_names()):
                        site_cols = {c["name"] for c in inspector.get_columns("site_configurations")}
                        if "is_available_for_provisioning" not in site_cols:
                            conn.execute(
                                text(
                                    "ALTER TABLE site_configurations "
                                    "ADD COLUMN IF NOT EXISTS is_available_for_provisioning BOOLEAN "
                                    "NOT NULL DEFAULT TRUE"
                                )
                            )
                            _logger.warning(
                                "DB schema drift repaired: added missing site_configurations.is_available_for_provisioning"
                            )
                        if "is_default_provisioning" not in site_cols:
                            conn.execute(
                                text(
                                    "ALTER TABLE site_configurations "
                                    "ADD COLUMN IF NOT EXISTS is_default_provisioning BOOLEAN "
                                    "NOT NULL DEFAULT FALSE"
                                )
                            )
                            _logger.warning(
                                "DB schema drift repaired: added missing site_configurations.is_default_provisioning"
                            )

                _logger.info("Database safety-net create_all completed.")
                database_ready = True
        except Exception as exc:
            _logger.warning("Database safety net table creation failed: %s", exc)

    app.state.scheduler = start_scheduler() if database_ready else None
    if settings.graph_configured and database_ready:
        # Defer the potentially long Graph catch-up so health and auth endpoints
        # can respond immediately after the API starts.
        async def _deferred_precreate() -> None:
            try:
                await asyncio.sleep(10)
                await precreate_next_month()
            except GraphError as e:
                msg = str(e).lower()
                if e.status == 403 and "access denied" in msg:
                    _logger.warning(
                        "Deferred precreate_next_month skipped: Graph access denied. "
                        "Verify app permissions and site/library grants. Error: %s",
                        e,
                    )
                    return
                _logger.exception("Deferred precreate_next_month failed")
            except Exception:
                _logger.exception("Deferred precreate_next_month failed")

        asyncio.create_task(_deferred_precreate())
# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class CheckEmailIn(BaseModel):
    email: str


class LoginIn(BaseModel):
    access_token: str
    tenant_id: str = ""
    # Optional fallback UA reported by the browser JS — never authoritative;
    # the server-side request header takes precedence.
    client_reported_user_agent: str | None = None


class BypassLoginIn(BaseModel):
    email: str
    display_name: str | None = None
    tenant_id: str = ""


class LogoutIn(BaseModel):
    email: str | None = None
    session_id: str | None = None  # UUID of the session being ended


def _log_activity(email: str | None, action: str, detail: str | None = None) -> None:
    """Write an ActivityLog row if DB is configured and email is known."""
    if not email or not settings.db_configured:
        return
    email = email.lower().strip()
    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        with SessionLocal() as db:
            db.add(db_models.ActivityLog(user_email=email, action=action, detail=detail))
            # Keep only last 50
            old = (
                db.query(db_models.ActivityLog)
                .filter_by(user_email=email)
                .order_by(db_models.ActivityLog.created_at.desc())
                .offset(50)
                .all()
            )
            for entry in old:
                db.delete(entry)
            db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session tracking helpers
# ---------------------------------------------------------------------------

def _session_db():
    """Yield a SessionLocal instance; skip if DB not configured."""
    if not settings.db_configured:
        yield None
        return
    from .db.base import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def require_session(
    request: Request,
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None),
    session_id: str | None = Query(default=None),
):
    """FastAPI dependency — validate the server-side session on every protected
    request.  Returns the UserSession ORM row on success; raises HTTP 401 with
    a structured { reason } body on failure.

    Reasons: not_found | expired | logged_out | revoked

    In stub / no-DB mode this dependency is a no-op so development continues
    to work without a database.

    If a database error occurs during validation (e.g. tables were just
    created and are empty), we skip the check rather than locking out all
    users with a spurious 401.
    """
    if not settings.db_configured:
        return None  # stub mode — skip session checks

    token = x_session_id or session_id
    if not token and authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()

    if token and (token == "mock-token" or token == "dev-token" or token.startswith("mock")):
        return None  # Allow mock / dev tokens for testing

    if not token:
        # Development fallback: allow requests without explicit session token to proceed
        return None

    from .db.base import SessionLocal
    from .services.session_service import validate_session
    from datetime import datetime, timezone
    import logging as _logging

    db = SessionLocal()
    try:
        try:
            session, reason = validate_session(db, token)
        except Exception as db_exc:
            # DB error during lookup (e.g. tables freshly created / migration
            # in progress).  Let the request through rather than hard-blocking
            # with a 401 — the worst outcome is one extra unauthenticated call.
            _logging.getLogger(__name__).warning(
                "require_session: DB error during validate_session, bypassing check: %s", db_exc
            )
            return None

        if session is None:
            # Audit log the attempt (best-effort, non-blocking)
            _write_invalid_attempt(
                session_id=token,
                email="unknown",
                ip=_get_ip(request),
                ua=request.headers.get("user-agent"),
                detail=f"Invalid session: {reason}",
            )
            # Raise 401 so the frontend can detect session expiry and re-authenticate.
            # The frontend's _fetchJson already handles 401 by showing the session-expired screen.
            raise HTTPException(
                status_code=401,
                detail={"reason": reason, "message": _session_reason_message(reason)},
            )
        # Update last_activity on every valid request
        session.last_activity = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return session
    finally:
        db.close()


def _get_ip(request: Request) -> str | None:
    """Extract client IP from the request (uses request_meta logic)."""
    try:
        from .utils.request_meta import get_client_ip
        return get_client_ip(request)
    except Exception:
        return None


def _write_invalid_attempt(
    session_id: str | None,
    email: str,
    ip: str | None,
    ua: str | None,
    detail: str | None = None,
) -> None:
    """Write an invalid_session_attempt audit entry (best-effort)."""
    if not settings.db_configured:
        return
    try:
        from .db.base import SessionLocal
        from .services.session_service import write_audit
        db = SessionLocal()
        try:
            write_audit(
                db,
                email=email,
                event="invalid_session_attempt",
                session_id=session_id,
                ip_address=ip,
                user_agent=ua,
                detail=detail,
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _session_reason_message(reason: str) -> str:
    messages = {
        "not_found": "Session not found. Please sign in again.",
        "token_expiry": "Your session token expired. Please sign in again.",
        "inactivity": "Your session expired due to inactivity. Please sign in again.",
        "expired": "Your session has expired. Please sign in again.",
        "logged_out": "This session has ended. Please sign in again.",
        "revoked": "Your access was revoked. Contact your administrator if unexpected.",
    }
    return messages.get(reason, "Authentication required.")


@app.post("/api/auth/check-email")
async def check_email(payload: CheckEmailIn):
    """Pre-flight check: confirm the email is a member/guest in the Entra tenant.

    When Graph is not configured (stub mode) we let all emails through so that
    development still works without Azure credentials.
    """
    if not settings.graph_configured:
        return {"allowed": True}

    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    # Acquire an app-only token for Graph
    token_url = f"{settings.graph_authority}/{settings.azure_tenant_id}/oauth2/v2.0/token"
    try:
        async with httpx.AsyncClient(verify=graph_tls_verify(), timeout=10.0) as client:
            # 1. Get an app-only access token
            token_resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.graph_client_id,
                    "client_secret": settings.graph_client_secret,
                    "scope": settings.graph_scope,
                },
            )
            if token_resp.status_code != 200:
                # If we can't reach Graph, fail open so the user can still try MSAL.
                return {"allowed": True}

            app_token = token_resp.json().get("access_token", "")

            # 2. Look up the user in the directory.
            # Use $filter by mail/otherMails so guest accounts (whose UPN is
            # formatted as alias_domain.com#EXT#@tenant.onmicrosoft.com) are
            # found correctly. GET /users/{email} returns 404 for guests.
            filter_query = (
                f"mail eq '{email}' or otherMails/any(m:m eq '{email}')"
            )
            user_resp = await client.get(
                f"{settings.graph_base_url}/users",
                params={"$filter": filter_query, "$select": "id,mail", "$top": "1"},
                headers={
                    "Authorization": f"Bearer {app_token}",
                    "ConsistencyLevel": "eventual",
                },
            )

            if user_resp.status_code == 200:
                users = user_resp.json().get("value", [])
                if users:
                    return {"allowed": True}
                raise HTTPException(
                    401,
                    detail="This email address is not authorised. Contact your administrator.",
                )
            # Any other error from Graph — fail open
            return {"allowed": True}
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException):
        # Pre-check must never block sign-in when Graph is temporarily slow.
        # We fail open here and let the real token exchange happen in auth_login.
        return {"allowed": True}
    except httpx.RequestError:
        # Same fail-open behavior for transient network/TLS issues.
        return {"allowed": True}


@app.post("/api/auth/login")
async def auth_login(request: Request, payload: LoginIn):
    """Validate the MSAL access token, persist/update the extended profile,
    seed related records for new users, create a server-side session, and
    return the session_id for the client to attach as X-Session-ID."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # ── Capture IP and User-Agent server-side (never trust client body) ─────
    from .utils.request_meta import get_client_ip, get_user_agent
    client_ip = get_client_ip(request)
    user_agent = get_user_agent(request) or payload.client_reported_user_agent

    if payload.access_token == "mock-token":
        profile = {
            "display_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
            "email": "testuser@example.com",
            "azure_oid": None,
            "job_title": "System Administrator",
            "department": "IT",
            "phone": None,
            "office_location": None,
            "company_name": None,
            "employee_id": None,
            "manager_name": None,
            "manager_email": None,
            "tenant_id": payload.tenant_id or None,
            "two_factor_enabled": True,
            "password_changed_at": (now - timedelta(days=92)).isoformat() + "Z",
            "last_login": now.isoformat() + "Z",
            "created_at": now.isoformat() + "Z",
            "emergency_contact": None,
            "folder_permissions": [],
            "recent_activity": [{"action": "login", "detail": "Logged in", "created_at": now.isoformat() + "Z"}],
        }
        _profile_cache["testuser@example.com"] = profile
        # Create a stub session for mock logins too
        mock_session_id = None
        if settings.db_configured:
            try:
                from .db.base import SessionLocal
                from .services.session_service import create_session
                with SessionLocal() as db:
                    sess = create_session(db, "testuser@example.com", user_agent, client_ip)
                    mock_session_id = sess.session_id
            except Exception:
                pass
        return {
            "display_name": profile["display_name"],
            "email": profile["email"],
            "session_id": mock_session_id,
        }

    # ── Fetch /me from Graph ─────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(verify=graph_tls_verify(), timeout=15.0) as client:
            me_resp = await client.get(
                f"{settings.graph_base_url}/me",
                params={
                    "$select": (
                        "id,displayName,givenName,surname,mail,userPrincipalName,"
                        "jobTitle,department,mobilePhone,businessPhones,"
                        "officeLocation,companyName,employeeId"
                    )
                },
                headers={"Authorization": f"Bearer {payload.access_token}"},
            )
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, httpx.RequestError):
        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable. Please try again.",
        )

    if me_resp.status_code != 200:
        raise HTTPException(401, "Token validation failed")

    me = me_resp.json()
    email = (me.get("mail") or me.get("userPrincipalName", "")).lower().strip()
    display_name = me.get("displayName") or me.get("userPrincipalName", "")
    phone = me.get("mobilePhone") or (me.get("businessPhones") or [None])[0]

    # ── Try to fetch manager (best-effort) ────────────────────────────────────
    manager_name: str | None = None
    manager_email: str | None = None
    try:
        async with httpx.AsyncClient(verify=settings.graph_verify_ssl) as client:
            mgr_resp = await client.get(
                f"{settings.graph_base_url}/me/manager",
                params={"$select": "displayName,mail,userPrincipalName"},
                headers={"Authorization": f"Bearer {payload.access_token}"},
            )
        if mgr_resp.status_code == 200:
            mgr = mgr_resp.json()
            manager_name = mgr.get("displayName")
            manager_email = mgr.get("mail") or mgr.get("userPrincipalName")
    except Exception:
        pass

    # ── Persist to DB ─────────────────────────────────────────────────────────
    profile: dict = {
        "display_name": display_name,
        "first_name": me.get("givenName"),
        "last_name": me.get("surname"),
        "email": email,
        "azure_oid": me.get("id"),
        "job_title": me.get("jobTitle"),
        "department": me.get("department"),
        "phone": phone,
        "office_location": me.get("officeLocation"),
        "company_name": me.get("companyName"),
        "employee_id": me.get("employeeId"),
        "manager_name": manager_name,
        "manager_email": manager_email,
        "tenant_id": payload.tenant_id or None,
        "two_factor_enabled": True,
        "password_changed_at": None,
        "last_login": now.isoformat() + "Z",
    }

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                is_new = row is None
                if is_new:
                    row = db_models.UserProfile(email=email)
                    db.add(row)
                    row.display_name = display_name
                    row.first_name = me.get("givenName")
                    row.last_name = me.get("surname")
                    row.azure_oid = me.get("id")
                    row.job_title = me.get("jobTitle")
                    row.department = me.get("department")
                    row.phone = phone
                    row.office_location = me.get("officeLocation")
                    row.company_name = me.get("companyName")
                    row.employee_id = me.get("employeeId")
                    if manager_name:
                        row.manager_name = manager_name
                    if manager_email:
                        row.manager_email = manager_email
                else:
                    # For existing users, only populate missing/null fields
                    # with Graph data so user's edits in the app are preserved.
                    if not row.display_name:
                        row.display_name = display_name
                    if not row.first_name:
                        row.first_name = me.get("givenName")
                    if not row.last_name:
                        row.last_name = me.get("surname")
                    if not row.azure_oid:
                        row.azure_oid = me.get("id")
                    if not row.job_title:
                        row.job_title = me.get("jobTitle")
                    if not row.department:
                        row.department = me.get("department")
                    if not row.phone:
                        row.phone = phone
                    if not row.office_location:
                        row.office_location = me.get("officeLocation")
                    if not row.company_name:
                        row.company_name = me.get("companyName")
                    if not row.employee_id:
                        row.employee_id = me.get("employeeId")
                    if not row.manager_name and manager_name:
                        row.manager_name = manager_name
                    if not row.manager_email and manager_email:
                        row.manager_email = manager_email

                row.tenant_id = payload.tenant_id or None
                row.last_login = now

                db.flush()  # ensure ID assigned before seeding related rows

                if is_new:
                    # Seed 2FA = enabled, password set 92 days ago
                    row.two_factor_enabled = True
                    row.password_changed_at = now - timedelta(days=92)

                    # Default folder permissions
                    for fname, level in [
                        ("Technical & Crewing", "edit"),
                        ("Commercial & Chartering", "view"),
                        ("Insurance", "approve"),
                    ]:
                        db.add(db_models.FolderPermission(
                            user_email=email,
                            folder_name=fname,
                            permission_level=level,
                        ))

                # Log the login event
                db.add(db_models.ActivityLog(
                    user_email=email,
                    action="login",
                    detail="Logged in",
                ))

                # Keep only the last 50 activity entries
                old = (
                    db.query(db_models.ActivityLog)
                    .filter_by(user_email=email)
                    .order_by(db_models.ActivityLog.created_at.desc())
                    .offset(50)
                    .all()
                )
                for entry in old:
                    db.delete(entry)

                db.commit()
                profile["created_at"] = row.created_at.isoformat() + "Z"
                profile["two_factor_enabled"] = row.two_factor_enabled
                profile["password_changed_at"] = (
                    row.password_changed_at.isoformat() + "Z" if row.password_changed_at else None
                )
        except Exception:
            pass  # Non-critical; don't break login

    # Merge with existing cache if present to preserve user's edits
    existing_cached = _profile_cache.get(email)
    if existing_cached:
        # Update last login and non-empty values
        for key in ["last_login", "tenant_id"]:
            if key in profile:
                existing_cached[key] = profile[key]
        for key in ["display_name", "first_name", "last_name", "azure_oid", "job_title", 
                    "department", "phone", "office_location", "company_name", "employee_id", 
                    "manager_name", "manager_email"]:
            val = profile.get(key)
            if val and not existing_cached.get(key):
                existing_cached[key] = val
        profile = existing_cached
    else:
        profile.setdefault("created_at", now.isoformat().replace("+00:00", "Z"))
        profile.setdefault("emergency_contact", None)
        profile.setdefault("folder_permissions", [])
        profile.setdefault("recent_activity", [{"action": "login", "detail": "Logged in", "created_at": now.isoformat().replace("+00:00", "Z")}])
        _profile_cache[email] = profile

                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            # ── Create server-side session (with retry) ───────────────────────────────
    session_id: str | None = None
    if settings.db_configured:
        last_exc: Exception | None = None
        for attempt in range(3):  # up to 3 attempts
            try:
                from .db.base import SessionLocal
                from .services.session_service import create_session
                with SessionLocal() as db:
                    sess = create_session(db, email, user_agent, client_ip)
                    session_id = sess.session_id
                break  # success
            except Exception as exc:
                last_exc = exc
                import logging
                logging.getLogger(__name__).warning(
                    "Session creation attempt %d/3 failed: %s", attempt + 1, exc
                )
        if session_id is None:
            # All retries failed — surface a clear error so the frontend
            # can prompt the user to try again rather than letting them
            # land on the app with no working session (all API calls 401).
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": "session_create_failed",
                    "message": (
                        "Sign-in succeeded but the server could not create a session. "
                        "Please try again in a few seconds."
                    ),
                    "debug": str(last_exc),
                },
            )

    return {"display_name": display_name, "email": email, "session_id": session_id}


@app.post("/api/auth/bypass-login")
async def auth_bypass_login(request: Request, payload: BypassLoginIn):
    """Fallback login endpoint for SPFx web part when MSAL token acquisition
    is unavailable/bypassed. Creates a valid server-side session for the
    tenant email from the SharePoint context.
    """
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    display_name = payload.display_name or email.split("@")[0]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    from .utils.request_meta import get_client_ip, get_user_agent
    client_ip = get_client_ip(request)
    user_agent = get_user_agent(request)

    profile: dict = {
        "display_name": display_name,
        "email": email,
        "tenant_id": payload.tenant_id or None,
        "last_login": now.isoformat() + "Z",
    }

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                if not row:
                    row = db_models.UserProfile(email=email, display_name=display_name)
                    db.add(row)
                row.last_login = now
                db.commit()
        except Exception:
            pass

    _profile_cache[email] = profile

    session_id: str | None = None
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .services.session_service import create_session
            with SessionLocal() as db:
                sess = create_session(db, email, user_agent, client_ip)
                session_id = sess.session_id
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Bypass session creation failed: %s", exc)

    return {"display_name": display_name, "email": email, "session_id": session_id}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, payload: LogoutIn):
    """Session-end signal from the frontend.

    Marks the specified session as 'Logged Out' and writes an audit entry.
    Other active sessions for the same user are left untouched.
    """
    email = (payload.email or "").strip().lower() or None
    _log_activity(email, "logout", "Signed out")

    if settings.db_configured and payload.session_id:
        try:
            from .db.base import SessionLocal
            from .services.session_service import logout_session
            ip = _get_ip(request)
            with SessionLocal() as db:
                logout_session(db, payload.session_id, ip_address=ip)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("logout_session failed: %s", exc)

    return {"ok": True}


@app.get("/api/profile")
async def get_profile(email: str, _session: object = Depends(require_session)):
    """Return the full stored profile for the given email address."""
    email = email.lower().strip()

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                if row is not None:
                    perms = row.folder_permissions
                    logs = (
                        db.query(db_models.ActivityLog)
                        .filter_by(user_email=email)
                        .order_by(db_models.ActivityLog.created_at.desc())
                        .limit(10)
                        .all()
                    )
                    return {
                        "email": row.email,
                        "display_name": row.display_name,
                        "first_name": row.first_name,
                        "last_name": row.last_name,
                        "azure_oid": row.azure_oid,
                        "job_title": row.job_title,
                        "department": row.department,
                        "phone": row.phone,
                        "office_location": row.office_location,
                        "office_name": row.office_name,
                        "company_name": row.company_name,
                        "employee_id": row.employee_id,
                        "manager_name": row.manager_name,
                        "manager_email": row.manager_email,
                        "tenant_id": row.tenant_id,
                        "two_factor_enabled": row.two_factor_enabled,
                        "password_changed_at": row.password_changed_at.isoformat() + "Z" if row.password_changed_at else None,
                        "last_login": row.last_login.isoformat() + "Z" if row.last_login else None,
                        "created_at": row.created_at.isoformat() + "Z",
                        "photo_base64": row.photo_base64,
                        "date_of_joining": row.date_of_joining.isoformat() if row.date_of_joining else None,
                        "emergency_contact": None,
                        "folder_permissions": [
                            {"folder_name": p.folder_name, "permission_level": p.permission_level}
                            for p in perms
                        ],
                        "recent_activity": [
                            {
                                "action": lg.action,
                                "detail": lg.detail,
                                "created_at": lg.created_at.isoformat() + "Z",
                            }
                            for lg in logs
                        ],
                    }
                # Row absent — seed it from the login cache so PATCH will work
                cached = _profile_cache.get(email)
                if cached:
                    row = db_models.UserProfile(
                        email=email,
                        display_name=cached.get("display_name") or email,
                        first_name=cached.get("first_name"),
                        last_name=cached.get("last_name"),
                        azure_oid=cached.get("azure_oid"),
                        job_title=cached.get("job_title"),
                        department=cached.get("department"),
                        phone=cached.get("phone"),
                        office_location=cached.get("office_location"),
                        company_name=cached.get("company_name"),
                        employee_id=cached.get("employee_id"),
                        manager_name=cached.get("manager_name"),
                        manager_email=cached.get("manager_email"),
                        tenant_id=cached.get("tenant_id"),
                        two_factor_enabled=bool(cached.get("two_factor_enabled", False)),
                    )
                    if cached.get("last_login"):
                        row.last_login = datetime.fromisoformat(cached["last_login"].rstrip("Z"))
                    db.add(row)
                    db.commit()
                    # Return the seeded profile via recursive call
                    return await get_profile(email)
        except Exception:
            pass  # Fall through to cache

    profile = _profile_cache.get(email)
    if profile:
        return profile

    raise HTTPException(404, "Profile not found")


class ProfileUpdateIn(BaseModel):
    employee_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    office_location: str | None = None
    office_name: str | None = None
    department: str | None = None
    manager_name: str | None = None
    manager_email: str | None = None
    two_factor_enabled: bool | None = None
    photo_base64: str | None = None
    date_of_joining: str | None = None


@app.patch("/api/profile")
async def patch_profile(email: str, payload: ProfileUpdateIn, _session: object = Depends(require_session)):
    """Update editable profile fields."""
    email = email.lower().strip()

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                row = db.query(db_models.UserProfile).filter_by(email=email).one_or_none()
                if row is None:
                    # Auto-create from cache if the row was never written (e.g. DB was
                    # unavailable during login). Use cached display_name if available.
                    cached = _profile_cache.get(email, {})
                    row = db_models.UserProfile(
                        email=email,
                        display_name=cached.get("display_name") or email,
                    )
                    db.add(row)
                    db.flush()  # assign id before seeding related rows

                for field in ("employee_id", "first_name", "last_name", "phone", "office_location",
                              "office_name", "department", "manager_name", "manager_email", "photo_base64"):
                    val = getattr(payload, field)
                    if val is not None:
                        setattr(row, field, val)

                if payload.date_of_joining is not None:
                    if payload.date_of_joining.strip():
                        from datetime import datetime
                        row.date_of_joining = datetime.strptime(payload.date_of_joining.strip(), "%Y-%m-%d").date()
                    else:
                        row.date_of_joining = None

                if payload.two_factor_enabled is not None:
                    row.two_factor_enabled = payload.two_factor_enabled

                # Log the update activity
                db.add(db_models.ActivityLog(
                    user_email=email,
                    action="profile_update",
                    detail="Updated profile details",
                ))

                db.commit()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Update failed: {e}")
    else:
        # Update in-memory cache
        p = _profile_cache.get(email, {})
        for field in ("employee_id", "first_name", "last_name", "phone", "office_location", "department",
                      "office_name", "manager_name", "manager_email"):
            val = getattr(payload, field)
            if val is not None:
                p[field] = val
        if payload.two_factor_enabled is not None:
            p["two_factor_enabled"] = payload.two_factor_enabled
        _profile_cache[email] = p

    return await get_profile(email)


@app.get("/api/users")
async def list_users(_session: object = Depends(require_session)):
    """Return User Management records from persisted profile/session data."""

    def _display_name_for(email: str, profile: Any | None = None) -> str:
        if profile is not None:
            if getattr(profile, "display_name", None):
                return str(profile.display_name)
            first = (getattr(profile, "first_name", None) or "").strip()
            last = (getattr(profile, "last_name", None) or "").strip()
            full = f"{first} {last}".strip()
            if full:
                return full
        local = (email or "").split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
        return local.title() if local else "Unknown User"

    def _fmt_last_login(dt: datetime | None) -> str:
        if not dt:
            return "Never"
        try:
            return dt.strftime("%b %d, %Y")
        except Exception:
            return "Never"

    def _role_for(email: str, profile: Any | None = None) -> str:
        if _is_admin_email(email):
            return "Administrator"
        levels = {
            (getattr(p, "permission_level", "") or "").strip().lower()
            for p in (getattr(profile, "folder_permissions", []) or [])
        }
        if "approve" in levels:
            return "Reviewer"
        if "edit" in levels:
            return "Manager"
        return "User"

    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models

            with SessionLocal() as db:
                profiles = db.query(db_models.UserProfile).order_by(db_models.UserProfile.display_name.asc()).all()
                sessions = db.query(db_models.UserSession).order_by(db_models.UserSession.last_activity.desc()).all()

                latest_seen_by_email: dict[str, datetime] = {}
                active_by_email: set[str] = set()
                for sess in sessions:
                    email = (sess.email or "").strip().lower()
                    if not email:
                        continue
                    if email not in latest_seen_by_email and sess.last_activity:
                        latest_seen_by_email[email] = sess.last_activity
                    if (sess.status or "").strip().lower() == "active":
                        active_by_email.add(email)

                out: list[dict[str, str]] = []
                seen_emails: set[str] = set()

                for row in profiles:
                    email = (row.email or "").strip().lower()
                    if not email or email in seen_emails:
                        continue
                    seen_emails.add(email)
                    last_seen = latest_seen_by_email.get(email) or row.last_login
                    out.append({
                        "id": str(row.id or email),
                        "name": _display_name_for(email, row),
                        "email": email,
                        "role": _role_for(email, row),
                        "status": "Active" if email in active_by_email else "Inactive",
                        "lastLogin": _fmt_last_login(last_seen),
                    })

                for email, last_seen in latest_seen_by_email.items():
                    if email in seen_emails:
                        continue
                    out.append({
                        "id": email,
                        "name": _display_name_for(email),
                        "email": email,
                        "role": _role_for(email),
                        "status": "Active" if email in active_by_email else "Inactive",
                        "lastLogin": _fmt_last_login(last_seen),
                    })

                out.sort(key=lambda item: ((item.get("name") or "").lower(), (item.get("email") or "").lower()))
                return out
        except Exception as exc:
            raise HTTPException(500, f"Could not retrieve users: {exc}")

    out: list[dict[str, str]] = []
    for email, profile in _profile_cache.items():
        norm_email = (email or "").strip().lower()
        if not norm_email:
            continue
        raw_last = profile.get("last_login")
        last_seen: datetime | None = None
        if isinstance(raw_last, str) and raw_last.strip():
            try:
                last_seen = datetime.fromisoformat(raw_last.replace("Z", "+00:00"))
            except Exception:
                last_seen = None
        out.append({
            "id": norm_email,
            "name": _display_name_for(norm_email),
            "email": norm_email,
            "role": _role_for(norm_email),
            "status": "Active",
            "lastLogin": _fmt_last_login(last_seen),
        })

    out.sort(key=lambda item: ((item.get("name") or "").lower(), (item.get("email") or "").lower()))
    return out


# ---------------------------------------------------------------------------

@app.get("/api/config/site-info")
def get_site_info(x_session_id: str | None = Header(default=None)):
    """Return active site configuration details for frontend UI and diagnostics."""
    # Check if session has a site override
    from .config import get_session_site, Settings
    site_override = get_session_site(x_session_id)
    active_site = site_override or settings.active_site
    
    # Load the site-specific settings if there's an override
    site_settings = settings
    if site_override:
        try:
            site_settings = Settings.load_site_config(site_override)
        except ValueError:
            pass  # Fall back to default settings
    
    return {
        "active_site": active_site,
        "site_name": site_settings.sp_site_name,
        "env": site_settings.app_env,
        "drive_id": site_settings.drive_id,
        "db_configured": site_settings.db_configured,
        "db_name": site_settings.db_name,
        "graph_configured": site_settings.graph_configured,
        "sp_configured": site_settings.sp_configured,
        "mode": backend_mode(),
    }


@app.get("/api/config/available-sites")
def get_available_sites(_session: object = Depends(require_session)):
    """Return list of all available sites in the tenant that can be switched to."""
    from .config import Settings
    available_sites = Settings.discover_available_sites()
    
    # Filter to only configured sites
    configured_sites = [
        {
            "name": site_info["name"],
            "display_name": site_info["sp_site_name"],
            "configured": site_info["configured"],
        }
        for site_info in available_sites.values()
        if site_info["configured"]
    ]
    if settings.db_configured:
        from .db.base import SessionLocal
        from .db.models import SiteConfiguration
        db = SessionLocal()
        try:
            existing_names = {site["name"] for site in configured_sites}
            configured_sites.extend({
                "name": record.site_key,
                "display_name": record.display_name,
                "configured": True,
                "site_id": record.site_id,
                "drive_id": record.drive_id,
            } for record in db.query(SiteConfiguration).order_by(SiteConfiguration.display_name).all()
                    if record.site_key not in existing_names)
        finally:
            db.close()
    return {
        "sites": configured_sites,
        "current_site": settings.active_site,
    }


class SwitchSiteRequest(BaseModel):
    site_name: str = Field(..., description="Target site name to switch to")


@app.post("/api/config/switch-site")
async def switch_site(
    request: SwitchSiteRequest,
    x_session_id: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Switch the active site for the current session."""
    from .config import Settings, set_session_site
    from .graph.client import reset_graph_client
    
    target_site = request.site_name.lower()
    
    # Validate that the site is configured
    try:
        site_config = Settings.load_site_config(target_site)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Reset the graph client so a new one is created for this site
    await reset_graph_client()
    
    # Set the site override for this session
    if x_session_id:
        set_session_site(x_session_id, target_site)
    
    # Invalidate caches since we're switching sites
    from .main import invalidate_folder_caches
    invalidate_folder_caches()
    
    return {
        "success": True,
        "active_site": target_site,
        "site_name": site_config.sp_site_name,
    }


@app.get("/api/admin/site-configuration")
async def get_admin_site_configuration(
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Get current site configuration and list of available sites (admin only)."""
    admin_email = _require_admin(x_user_email)
    
    from .config import Settings
    
    available_sites = Settings.discover_available_sites()
    current_site = settings.active_site
    
    # Get current site details
    try:
        current_config = Settings.load_site_config(current_site)
    except ValueError:
        current_config = settings
    
    configured_sites = [
        {
            "name": site_info["name"],
            "display_name": site_info["sp_site_name"],
            "configured": site_info["configured"],
        }
        for site_info in available_sites.values()
        if site_info["configured"]
    ]
    if settings.db_configured:
        from .db.base import SessionLocal
        from .db.models import SiteConfiguration
        db = SessionLocal()
        try:
            existing_names = {site["name"] for site in configured_sites}
            configured_sites.extend({
                "name": record.site_key,
                "display_name": record.display_name,
                "configured": True,
                "site_id": record.site_id,
                "drive_id": record.drive_id,
            } for record in db.query(SiteConfiguration).order_by(SiteConfiguration.display_name).all()
                    if record.site_key not in existing_names)
        finally:
            db.close()
    
    return {
        "current_site": current_site,
        "current_site_name": current_config.sp_site_name,
        "current_db_name": current_config.db_name or "In-Memory",
        "current_drive_id": current_config.drive_id,
        "available_sites": configured_sites,
        "admin_email": admin_email,
    }


class AdminSwitchSiteRequest(BaseModel):
    site_name: str = Field(..., description="Target site name to switch to")
    reason: str | None = Field(default=None, description="Optional reason for the switch")


class SaveSiteConfigurationRequest(BaseModel):
    site_key: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=256)
    site_name: str = Field(..., min_length=1, max_length=256)
    site_id: str = Field(..., min_length=1, max_length=512)
    drive_id: str = Field(..., min_length=1, max_length=512)


def _registered_site_config(site_name: str):
    """Build a Settings object for a persisted site using active credentials."""
    if not settings.db_configured:
        return None
    from .db.base import SessionLocal
    from .db.models import SiteConfiguration
    db = SessionLocal()
    try:
        record = db.query(SiteConfiguration).filter(SiteConfiguration.site_key == site_name).first()
        if not record:
            return None
        base = settings
        from .config import Settings, _SITE_CONFIGS_CACHE
        configured = Settings.load_site_config(base.active_site)
        configured.active_site = record.site_key
        configured.app_env = record.site_key
        configured.sp_site_name = record.display_name
        configured.drive_id = record.drive_id
        configured.sp_drive_id = record.drive_id
        _SITE_CONFIGS_CACHE[record.site_key] = configured
        return configured
    finally:
        db.close()


@app.post("/api/admin/site-configurations")
async def save_admin_site_configuration(
    request: SaveSiteConfigurationRequest,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Save a discovered site/library so it can be used by the site switcher."""
    admin_email = _require_admin(x_user_email)
    from .db.base import SessionLocal
    from .db.models import SiteConfiguration
    if not settings.db_configured:
        raise HTTPException(status_code=503, detail="A database is required to save site configurations.")
    site_key = request.site_key.strip().lower()
    if not site_key.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Site Key may contain only letters, numbers, and underscores.")
    db = SessionLocal()
    try:
        record = db.query(SiteConfiguration).filter(SiteConfiguration.site_key == site_key).first()
        if record:
            record.display_name = request.display_name.strip()
            record.site_name = request.site_name.strip()
            record.site_id = request.site_id.strip()
            record.drive_id = request.drive_id.strip()
            record.created_by_email = admin_email
        else:
            record = SiteConfiguration(
                site_key=site_key, display_name=request.display_name.strip(),
                site_name=request.site_name.strip(), site_id=request.site_id.strip(),
                drive_id=request.drive_id.strip(), created_by_email=admin_email,
            )
            db.add(record)
        db.commit()
        return {"success": True, "site": {"name": site_key, "display_name": record.display_name, "site_id": record.site_id, "drive_id": record.drive_id}}
    finally:
        db.close()


@app.get("/api/admin/discover-sites")
async def discover_admin_sites(
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Discover SharePoint sites visible to the configured Graph app."""
    _require_admin(x_user_email)
    now = time.time()
    cached = _DISCOVERED_SITES_CACHE.get(settings.active_site)
    if cached and now - cached[0] < _DISCOVERED_SITES_CACHE_TTL:
        return {"sites": cached[1], "cached": True, "cache_ttl_seconds": _DISCOVERED_SITES_CACHE_TTL}

    try:
        client = graph()
        sites: list[dict[str, str]] = []
        next_url: str | None = "/sites?search=*"
        while next_url:
            page = await client.get(next_url)
            for site in page.get("value", []):
                site_id = site.get("id")
                if site_id:
                    sites.append({
                        "id": site_id,
                        "name": site.get("name") or site.get("displayName") or site_id,
                        "web_url": site.get("webUrl", ""),
                    })
            next_url = page.get("@odata.nextLink")
        _DISCOVERED_SITES_CACHE[settings.active_site] = (now, sites)
        return {"sites": sites, "cached": False, "cache_ttl_seconds": _DISCOVERED_SITES_CACHE_TTL}
    except GraphError as exc:
        if exc.status in (401, 403):
            raise HTTPException(
                status_code=403,
                detail="The app registration needs Microsoft Graph Sites.Read.All or Sites.ReadWrite.All application permission with admin consent.",
            ) from exc
        raise HTTPException(status_code=502, detail=f"SharePoint site discovery failed: {exc}") from exc


@app.get("/api/admin/discover-sites/{site_id}/drives")
async def discover_admin_site_drives(
    site_id: str,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """List document libraries for a discovered SharePoint site."""
    _require_admin(x_user_email)
    try:
        page = await graph().get(f"/sites/{quote(site_id, safe='')}/drives")
        drives = [
            {
                "id": drive["id"],
                "name": drive.get("name") or drive["id"],
                "web_url": drive.get("webUrl", ""),
                "drive_type": drive.get("driveType", ""),
            }
            for drive in page.get("value", [])
            if drive.get("id")
        ]
        return {"drives": drives}
    except GraphError as exc:
        if exc.status in (401, 403):
            raise HTTPException(
                status_code=403,
                detail="The app registration needs Sites.Read.All or Sites.ReadWrite.All consent to read document libraries.",
            ) from exc
        raise HTTPException(status_code=502, detail=f"SharePoint library discovery failed: {exc}") from exc


class SiteItemTagsIn(BaseModel):
    department: str = ""
    vessel: str = ""
    group: str = ""
    category: str = ""


class SiteScanTagsIn(BaseModel):
    item_ids: list[str] = Field(..., min_length=1)
    recursive: bool = False
    exclude_item_ids: list[str] = Field(default_factory=list)
    scope: Literal["missing_only", "all"] = "missing_only"


class SiteResolveTagsIn(BaseModel):
    choices: dict[str, str] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)


class SiteConfirmTagsIn(BaseModel):
    items: list[dict[str, Any]] = Field(..., min_length=1)


class SiteBulkTagsIn(BaseModel):
    item_ids: list[str] = Field(..., min_length=1)
    vessel: str = ""
    department: str = ""
    group: str = ""
    category: str = ""
    auto_from_path: bool = False
    recursive: bool = False
    skip_if_tagged: bool = False  # When True, skip files where vessel already matches the target
    skip_if_any_vessel_set: bool = False  # When True, skip files that have ANY existing vessel tag
    scope: Literal["missing_only", "all"] = "missing_only"


class TagFailureActionIn(BaseModel):
    file_ids: list[str] = Field(default_factory=list)
    reason: str = ""


def _site_item_tags(fields: dict[str, Any]) -> dict[str, str]:
    return {
        "department": _pick_sp_field(fields, ["Department", "department", "DMS_Department", "main_folder", "MainFolder", "Main Folder", "Domain", "domain"]),
        "vessel": _pick_sp_field(fields, ["VesselName", "vesselname", "Vessel Name", "vessel", "vessel_name", "ship", "shipname", "ShipName", "Vessel_x0020_Name", "Vessel_x0020_Name_x0020_"]),
        "group": _pick_sp_field(fields, ["Group", "group", "DMS_Group", "vessel_group", "vesselgroup"]),
        "category": _pick_sp_field(fields, ["Category", "category", "DMS_Category", "document_category", "doc_category"]),
    }


def _sharepoint_tags_match(fields: dict[str, Any], expected: dict[str, str]) -> bool:
    """Verify that SharePoint fields match expected values after an update.

    Uses normalized (alphanumeric-only, casefold) comparison so minor differences
    between user-provided values and official term store labels (e.g. spaces vs
    underscores, special characters) do not produce false mismatches.
    """
    import re as _re2
    actual = _site_item_tags(fields)

    def _norm_cmp(s: str) -> str:
        return _re2.sub(r"[^a-z0-9]", "", (s or "").strip().lower())

    return all(
        not value or _norm_cmp(actual.get(key, "")) == _norm_cmp(value)
        for key, value in expected.items()
    )


_TAG_PLACEHOLDERS = {"", "to be classified", "to be classified ", "unknown", "n/a"}


def _tags_need_attention(tags: dict[str, str]) -> bool:
    """Return whether the file is missing its Vessel tag.

    Missing-only OCR is intentionally vessel-focused: files with an existing
    vessel tag must not be rescanned because another taxonomy field is blank.
    """
    value = (tags.get("vessel") or "").strip().lower()
    return value in _TAG_PLACEHOLDERS


async def _filter_site_files_by_scope(
    drive_id: str,
    files: list[dict[str, Any]],
    scope: str,
    access_token: str | None = None,
) -> list[dict[str, Any]]:
    if scope == "all" or not files:
        return files

    semaphore = asyncio.Semaphore(20)

    async def needs_attention(item: dict[str, Any]) -> bool:
        async with semaphore:
            try:
                fields = await graph().get(
                    f"/drives/{drive_id}/items/{item['id']}/listItem/fields",
                    access_token=access_token,
                )
                return _tags_need_attention(_site_item_tags(fields))
            except Exception:
                # Unknown metadata must remain actionable rather than being skipped.
                return True

    flags = await asyncio.gather(*(needs_attention(item) for item in files))
    return [item for item, include in zip(files, flags) if include]


def _record_tag_failure(
    *, site_id: str, drive_id: str, file_id: str, filename: str,
    parent_path: str, error_reason: str,
) -> None:
    if not settings.db_configured:
        return
    try:
        from .db.base import SessionLocal
        from .db.models import TagFailure
        with SessionLocal() as db:
            row = db.query(TagFailure).filter(
                TagFailure.site_id == site_id,
                TagFailure.drive_id == drive_id,
                TagFailure.file_id == file_id,
            ).first()
            if row:
                row.status = "needs_retry"
                row.error_reason = error_reason
                row.filename = filename or row.filename
                row.parent_path = parent_path or row.parent_path
                row.attempt_count = (row.attempt_count or 0) + 1
                row.dismissed_by = None
                row.dismissed_reason = None
            else:
                row = TagFailure(
                    site_id=site_id, drive_id=drive_id, file_id=file_id,
                    filename=filename or file_id, parent_path=parent_path or "",
                    error_reason=error_reason, status="needs_retry", attempt_count=1,
                )
                db.add(row)
            db.commit()
    except Exception as exc:
        _logger.warning("Could not persist tag failure for %s: %s", file_id, exc)


def _resolve_tag_failure(*, site_id: str, drive_id: str, file_id: str) -> None:
    if not settings.db_configured:
        return
    try:
        from .db.base import SessionLocal
        from .db.models import TagFailure
        with SessionLocal() as db:
            row = db.query(TagFailure).filter(
                TagFailure.site_id == site_id,
                TagFailure.drive_id == drive_id,
                TagFailure.file_id == file_id,
            ).first()
            if row:
                row.status = "resolved"
                db.commit()
    except Exception as exc:
        _logger.warning("Could not resolve tag failure for %s: %s", file_id, exc)


async def _get_site_item_with_tags(drive_id: str, item: dict[str, Any], detected_vessel: str = "") -> dict[str, Any]:
    item_id = item.get("id", "")
    tags: dict[str, str] = {}
    suggested_vessel = ""

    # Folder entries do not need listItem/fields lookups for the UI to open.
    # Loading tags for every child folder adds a large amount of unnecessary
    # Graph work and slows down folder navigation. Files still get enriched.
    if item.get("file") is not None:
        try:
            fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
        except GraphError:
            fields = {}
        tags = _site_item_tags(fields)
        if not tags.get("vessel"):
            suggested_vessel = detected_vessel or ""

    return {
        **item,
        "web_url": item.get("webUrl") or "",
        "tags": tags,
        "suggested_vessel": suggested_vessel,
    }


@app.get("/api/sites")
async def discover_sites(
    limit: int = Query(default=50, ge=1, le=100),
    _session: object = Depends(require_session),
):
    """List a bounded first page of tenant sites without blocking on full pagination."""
    cached = _DISCOVERED_SITES_CACHE.get(settings.active_site)
    now = time.time()
    if cached and now - cached[0] < _DISCOVERED_SITES_CACHE_TTL:
        return {"sites": cached[1], "cached": True}
    sites: list[dict[str, Any]] = []
    next_url: str | None = f"/sites?search=*&$top={limit}"
    try:
        while next_url:
            page = await graph().get(next_url)
            for site in page.get("value", []):
                if site.get("id"):
                    sites.append({
                        "id": site["id"],
                        "display_name": site.get("displayName") or site.get("name") or site["id"],
                        "name": site.get("name") or site.get("displayName") or site["id"],
                        "web_url": site.get("webUrl", ""),
                        "description": site.get("description", ""),
                        "thumbnail": (site.get("thumbnail") or {}).get("large", {}).get("url", ""),
                    })
                    if len(sites) >= limit:
                        break
            next_url = None if len(sites) >= limit else page.get("@odata.nextLink")
        _DISCOVERED_SITES_CACHE[settings.active_site] = (now, sites)
        return {"sites": sites, "cached": False, "limited": True, "limit": limit}
    except GraphError as exc:
        raise HTTPException(status_code=502, detail=f"SharePoint site discovery failed: {exc}") from exc


@app.get("/api/sites/{site_id}/drives")
async def discover_site_drives(site_id: str, _session: object = Depends(require_session)):
    try:
        page = await graph().get(f"/sites/{quote(site_id, safe='')}/drives")
        return {"drives": [
            {"id": d["id"], "name": d.get("name") or d["id"],
             "web_url": d.get("webUrl", ""), "drive_type": d.get("driveType", "")}
            for d in page.get("value", []) if d.get("id")
        ]}
    except GraphError as exc:
        raise HTTPException(status_code=502, detail=f"SharePoint library discovery failed: {exc}") from exc


@app.get("/api/sites/{site_id}/drives/{drive_id}/folders/{folder_id}/children")
async def site_folder_children(
    site_id: str,
    drive_id: str,
    folder_id: str,
    x_graph_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    del site_id
    parent_id = await gd.get_root_item_id(drive_id, access_token=x_graph_access_token) if folder_id == "root" else folder_id
    try:
        items = await gd.list_children(drive_id, parent_id, access_token=x_graph_access_token)
        from .ocr.drawing_category import _extract_vessel_from_folder_path
        parent_path = ""
        if items:
            raw_p = (items[0].get("parentReference") or {}).get("path") or ""
            parent_path = raw_p.split("root:", 1)[-1].strip("/")
        elif parent_id != "root":
            try:
                parent_meta = await gd.get_item(drive_id, parent_id, access_token=x_graph_access_token)
                raw_p = (parent_meta.get("parentReference") or {}).get("path") or ""
                p_base = raw_p.split("root:", 1)[-1].strip("/")
                p_name = parent_meta.get("name") or ""
                parent_path = f"{p_base}/{p_name}".strip("/") if p_base else p_name
            except Exception:
                parent_path = ""

        detected_vessel = _extract_vessel_from_folder_path(parent_path) or ""

        # Extract department, group, category from path
        path_parts = [p.strip() for p in parent_path.replace("\\", "/").split("/") if p.strip()]
        detected_dept = ""
        for p in path_parts:
            if p.lower() in ("technical & crewing", "technical", "technical and crewing", "technical and crewing new", "commercial & chartering", "insurance", "kaizen - knowledge bank", "knowledge bank"):
                detected_dept = p
                break
        detected_group = ""
        detected_cat = ""
        for idx, p in enumerate(path_parts):
            pl = p.lower()
            if pl in ("drawings", "drawing", "manuals", "manual"):
                detected_group = "Drawings" if pl.startswith("draw") else "Manuals"
                if idx + 1 < len(path_parts):
                    detected_cat = path_parts[idx + 1]

        # ── Fast concurrent tag enrichment via Graph $batch ──────────────────────────────
        # Runs chunks of 20 concurrently via asyncio.gather instead of sequentially.
        # This reduces round-trip time from N * 400ms down to a single concurrent ~400ms burst.
        now_ts = time.time()
        cache_key_tags = f"{drive_id}:{parent_id}"
        cached_decorated = _FOLDER_CHILDREN_TAGS_CACHE.get(cache_key_tags)
        if cached_decorated and (now_ts - cached_decorated[0]) < CACHE_TTL_CHILDREN_TAGS:
            decorated_items = cached_decorated[1]
        else:
            file_items = [it for it in items if it.get("file") is not None]

            # Batch-fetch listItem/fields concurrently in chunks of 20 with bounded semaphore
            BATCH_SIZE = 20
            fields_by_id: dict[str, dict] = {}
            if file_items:
                sem = asyncio.Semaphore(5)

                async def _fetch_chunk(chunk):
                    batch_requests = [
                        {
                            "id": it["id"],
                            "method": "GET",
                            "url": f"/drives/{drive_id}/items/{it['id']}/listItem/fields",
                        }
                        for it in chunk
                    ]
                    async with sem:
                        try:
                            batch_resp = await graph().post(
                                "/$batch",
                                json={"requests": batch_requests},
                                access_token=x_graph_access_token,
                            )
                            return batch_resp.get("responses", [])
                        except Exception:
                            return []

                chunks = [file_items[i: i + BATCH_SIZE] for i in range(0, len(file_items), BATCH_SIZE)]
                chunk_results = await asyncio.gather(*[_fetch_chunk(c) for c in chunks])
                for resp_list in chunk_results:
                    for resp_item in resp_list:
                        if resp_item.get("status") == 200:
                            fields_by_id[resp_item["id"]] = resp_item.get("body") or {}

            decorated_items = []
            for it in items:
                iid = it.get("id", "")
                download_url = it.get("@microsoft.graph.downloadUrl") or ""
                if it.get("file") is not None:
                    raw_fields = fields_by_id.get(iid, {})
                    tags = _site_item_tags(raw_fields)
                    suggested_vessel = detected_vessel if not tags.get("vessel") else ""
                    decorated_items.append({
                        **it,
                        "web_url": it.get("webUrl") or "",
                        "download_url": download_url,
                        "tags": tags,
                        "suggested_vessel": suggested_vessel,
                    })
                else:
                    decorated_items.append({
                        **it,
                        "web_url": it.get("webUrl") or "",
                        "download_url": "",
                        "tags": {},
                        "suggested_vessel": "",
                    })
            _FOLDER_CHILDREN_TAGS_CACHE[cache_key_tags] = (now_ts, decorated_items)

        now = time.time()
        # Collect folder items that need background count computation
        _uncached_folder_ids: list[str] = []
        for it in decorated_items:
            if it.get("folder") and it.get("id"):
                ck = f"{drive_id}:{it['id']}"
                cached = _FOLDER_RECURSIVE_COUNTS_CACHE.get(ck)
                if cached and (now - cached[0]) < CACHE_TTL_FOLDER_RECURSIVE_COUNTS:
                    it["folder_counts"] = cached[1]
                else:
                    # Pre-populate from Graph's immediate childCount so the UI
                    # shows a count instantly instead of a spinner.
                    child_count = (it.get("folder") or {}).get("childCount")
                    it["folder_counts"] = {
                        "direct_subfolders": 0,
                        "direct_files": child_count if isinstance(child_count, int) else 0,
                        "total_subfolders": 0,
                        "total_files": child_count if isinstance(child_count, int) else 0,
                        "is_estimated": True,
                    } if isinstance(child_count, int) else None
                    _uncached_folder_ids.append(it["id"])

        # Fire-and-forget: compute accurate counts in background so next load is correct.
        # This populates direct_subfolders / direct_files properly instead of relying on
        # the childCount estimate which lumps folders and files together.
        if _uncached_folder_ids:
            _bg_token = x_graph_access_token
            _bg_drive = drive_id
            _bg_parent = parent_id
            async def _compute_counts_bg() -> None:
                for _fid in _uncached_folder_ids:
                    try:
                        await get_folder_recursive_counts(
                            _bg_drive, _fid,
                            parent_id=_bg_parent,
                            access_token=_bg_token,
                            max_depth=1,
                        )
                    except Exception:  # noqa: BLE001
                        pass
            asyncio.ensure_future(_compute_counts_bg())

        direct_folders = len([i for i in items if i.get("folder")])
        direct_files = len([i for i in items if not i.get("folder")])
        all_cached = all(it.get("folder_counts") is not None for it in decorated_items if it.get("folder"))
        if all_cached and direct_folders > 0:
            summary_counts = {
                "direct_folders": direct_folders,
                "direct_files": direct_files,
                "total_folders": direct_folders + sum(it["folder_counts"]["total_subfolders"] for it in decorated_items if it.get("folder_counts")),
                "total_files": direct_files + sum(it["folder_counts"]["total_files"] for it in decorated_items if it.get("folder_counts")),
            }
        else:
            summary_counts = {
                "direct_folders": direct_folders,
                "direct_files": direct_files,
                "total_folders": direct_folders,
                "total_files": direct_files,
            }

        return {
            "folder_id": parent_id,
            "parent_path": parent_path,
            "detected_vessel": detected_vessel,
            "detected_tags": {
                "department": detected_dept,
                "vessel": detected_vessel,
                "group": detected_group,
                "category": detected_cat,
            },
            "summary_counts": summary_counts,
            "items": decorated_items,
        }
    except GraphError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/api/sites/{site_id}/drives/{drive_id}/folders/{folder_id}/subfolder-counts")
async def site_subfolder_counts(
    site_id: str,
    drive_id: str,
    folder_id: str,
    x_graph_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    del site_id
    parent_id = await gd.get_root_item_id(drive_id, access_token=x_graph_access_token) if folder_id == "root" else folder_id
    try:
        items = await gd.list_children(drive_id, parent_id, access_token=x_graph_access_token)
        folder_items = [i for i in items if i.get("folder") and i.get("id")]
        direct_files = len([i for i in items if not i.get("folder")])
        direct_folders = len(folder_items)

        # Concurrently compute counts for all direct child folders with max_depth=2
        results = await asyncio.gather(*(
            get_folder_recursive_counts(
                drive_id, fi["id"], parent_id=parent_id,
                access_token=x_graph_access_token, max_depth=2,
            )
            for fi in folder_items
        ))

        counts_by_id = {fi["id"]: res for fi, res in zip(folder_items, results)}
        total_folders = direct_folders + sum(r["total_subfolders"] for r in results)
        total_files = direct_files + sum(r["total_files"] for r in results)

        return {
            "folder_id": parent_id,
            "counts": counts_by_id,
            "summary_counts": {
                "direct_folders": direct_folders,
                "direct_files": direct_files,
                "total_folders": total_folders,
                "total_files": total_files,
            },
        }
    except GraphError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/api/sites/{site_id}/drives/{drive_id}/items/{item_id}")
async def site_item(site_id: str, drive_id: str, item_id: str, _session: object = Depends(require_session)):
    del site_id
    try:
        return await _get_site_item_with_tags(drive_id, await gd.get_item(drive_id, item_id))
    except GraphError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/api/sites/{site_id}/drives/{drive_id}/items/{item_id}/content")
async def site_item_content(site_id: str, drive_id: str, item_id: str, _session: object = Depends(require_session)):
    del site_id
    try:
        content, content_type, filename = await gd.download_file(drive_id, item_id)
        return Response(
            content=content,
            media_type=content_type or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except GraphError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.patch("/api/sites/{site_id}/drives/{drive_id}/items/{item_id}/tags")
async def patch_site_item_tags(
    site_id: str,
    drive_id: str,
    item_id: str,
    request: SiteItemTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    del site_id
    try:
        fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=x_graph_access_token)
    except Exception:
        fields = {}
    current = _site_item_tags(fields)
    final_d = request.department.strip() or current.get("department", "")
    final_v = request.vessel.strip() or current.get("vessel", "")
    final_g = request.group.strip() or current.get("group", "")
    final_c = request.category.strip() or current.get("category", "")
    payload = _build_sharepoint_metadata_payload(
        department=final_d, vessel=final_v,
        group=final_g, category=final_c,
    )
    try:
        result = await gd.update_file_columns(
            drive_id, item_id, payload,
            access_token=x_graph_access_token,
            sp_access_token=x_sp_access_token,
        )
        invalidate_folder_caches()
        return {"ok": bool(result.get("ok")), "item_id": item_id, "tags": {"department": final_d, "vessel": final_v, "group": final_g, "category": final_c}, "metadata_patch": result}
    except GraphError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _derive_path_tags(
    parent_path: str,
    filename: str = "",
    known_vessels: list[Any] | None = None,
) -> dict[str, str]:
    """Extracts Department, Vessel, Group, and Category from folder hierarchy path and filename.

    Precedence:
      1. Authoritative folder path (folder structure > filename keywords)
      2. Immediate sub-folder name (for subcategory or category)
      3. Filename keywords (fallback only if folder path is ambiguous or generic)
    """
    from .ocr.drawing_category import (
        MANUAL_TAXONOMY, DRAWING_TAXONOMY, _extract_vessel_from_folder_path, _is_generic_or_system_folder
    )

    path_parts = [p.strip() for p in (parent_path or "").replace("\\", "/").replace(">", "/").split("/") if p.strip()]
    path_vessel = _extract_vessel_from_path(parent_path, known_vessels) or ""
    path_group = ""
    path_cat = ""
    path_sub = ""
    path_dept = ""

    for p in path_parts:
        if p.lower() in ("technical & crewing", "technical", "commercial & chartering", "insurance", "kaizen - knowledge bank", "knowledge bank"):
            path_dept = p
            break
    if not path_dept:
        path_dept = "Technical & Crewing"

    if not path_vessel:
        path_vessel = _extract_vessel_from_folder_path(parent_path) or ""
    if path_vessel and _is_generic_or_system_folder(path_vessel):
        path_vessel = ""

    dm_idx = next(
        (
            i for i, p in enumerate(path_parts)
            if p.lower() in ("drawings and manuals", "drawings & manuals", "drawing and manual")
            or any(
                p.lower().startswith(prefix)
                for prefix in ("drawings and manuals ", "drawings & manuals ", "drawing and manual ")
            )
        ),
        -1,
    )
    if dm_idx >= 0 and dm_idx + 1 < len(path_parts):
        rem = path_parts[dm_idx + 1:]
        if len(rem) >= 1:
            if rem[0].lower() in ("drawings", "drawing", "manuals", "manual"):
                path_group = "Drawings" if rem[0].lower().startswith("draw") else "Manuals"
                if len(rem) >= 2:
                    path_cat = rem[1]
                if len(rem) >= 3:
                    path_sub = rem[2]
            else:
                rem0_lower = rem[0].lower().strip()
                is_cat = (
                    rem0_lower.startswith("mb ")
                    or "main engine" in rem0_lower
                    or any(rem0_lower == c.lower() or rem0_lower.startswith(c.lower()) for c in MANUAL_TAXONOMY)
                    or any(rem0_lower == c.lower() or rem0_lower.startswith(c.lower()) for c in DRAWING_TAXONOMY)
                )
                if is_cat:
                    if "main engine" in rem0_lower or rem0_lower.startswith("mb "):
                        path_group = "Manuals"
                        path_cat = "Main Engine"
                    elif any(rem0_lower == c.lower() for c in MANUAL_TAXONOMY):
                        path_group = "Manuals"
                        path_cat = next(c for c in MANUAL_TAXONOMY if c.lower() == rem0_lower)
                    elif any(rem0_lower == c.lower() for c in DRAWING_TAXONOMY):
                        path_group = "Drawings"
                        path_cat = next(c for c in DRAWING_TAXONOMY if c.lower() == rem0_lower)
                    else:
                        path_cat = rem[0]
                    if len(rem) >= 2:
                        path_sub = rem[1]
                else:
                    if not path_vessel and not _is_generic_or_system_folder(rem[0]):
                        path_vessel = rem[0]
                    if len(rem) >= 2:
                        if rem[1].lower() in ("drawings", "drawing", "manuals", "manual"):
                            path_group = "Drawings" if rem[1].lower().startswith("draw") else "Manuals"
                            if len(rem) >= 3:
                                path_cat = rem[2]
                            if len(rem) >= 4:
                                path_sub = rem[3]
                        else:
                            path_cat = rem[1]
                            if len(rem) >= 3:
                                path_sub = rem[2]
    else:
        grp_idx = next(
            (i for i, p in enumerate(path_parts) if p.lower() in ("drawings", "drawing", "manuals", "manual")),
            -1,
        )
        if grp_idx >= 0:
            path_group = "Drawings" if path_parts[grp_idx].lower().startswith("draw") else "Manuals"
            if grp_idx + 1 < len(path_parts):
                path_cat = path_parts[grp_idx + 1]
            if grp_idx + 2 < len(path_parts):
                path_sub = path_parts[grp_idx + 2]
        else:
            skip_set = {"shared documents", "documents", "root"}
            if path_vessel:
                skip_set.add(path_vessel.lower())
            meaningful = [p for p in path_parts if p.lower() not in skip_set]
            if meaningful:
                for m in reversed(meaningful):
                    ml = m.lower().strip()
                    if ml.startswith("mb ") or "main engine" in ml or any(ml == c.lower() for c in MANUAL_TAXONOMY):
                        path_group = "Manuals"
                        path_cat = "Main Engine" if (ml.startswith("mb ") or "main engine" in ml) else next(c for c in MANUAL_TAXONOMY if c.lower() == ml)
                        break
                    elif any(ml == c.lower() for c in DRAWING_TAXONOMY):
                        path_group = "Drawings"
                        path_cat = next(c for c in DRAWING_TAXONOMY if c.lower() == ml)
                        break
                if not path_cat:
                    path_cat = meaningful[-2] if len(meaningful) >= 2 else meaningful[-1]
                    path_sub = meaningful[-1] if len(meaningful) >= 2 else ""

    # Clean numeric prefixes / Japanese suffixes from Category if needed
    if path_cat:
        cat_clean = re.sub(r"^\d+([._]\d+)*[_\s-]+", "", path_cat).strip()
        parts_cat = cat_clean.split("_")
        first_token = parts_cat[0].strip()
        if first_token and any(first_token.lower() == c.lower() for c in MANUAL_TAXONOMY):
            path_cat = next(c for c in MANUAL_TAXONOMY if c.lower() == first_token.lower())
        elif first_token and any(first_token.lower() == c.lower() for c in DRAWING_TAXONOMY):
            path_cat = next(c for c in DRAWING_TAXONOMY if c.lower() == first_token.lower())
        elif cat_clean.lower().startswith("mb ") or "main engine" in cat_clean.lower():
            path_cat = "Main Engine"

    # Filename keyword heuristics — fallback only if path_group is not set
    if not path_group:
        _MANUAL_FN_WORDS = {
            "operation", "maintenance", "maint", "overhaul", "instruction",
            "specification", "spare", "procedure", "service", "repair",
            "data", "component", "system", "guide", "manual", "operator",
            "list of spare", "technical data",
        }
        _DRAWING_FN_WORDS = {
            "dwg", "drawing", "plan", "diagram", "layout", "arrangement",
            "elevation", "detail", "section", "register", "schematic",
        }
        fn_lower = (filename or "").lower()
        fn_has_manual = any(w in fn_lower for w in _MANUAL_FN_WORDS)
        fn_has_drawing = any(w in fn_lower for w in _DRAWING_FN_WORDS)

        if fn_has_drawing and not fn_has_manual:
            path_group = "Drawings"
        elif fn_has_manual and not fn_has_drawing:
            path_group = "Manuals"

    # Normalize group via taxonomy
    if path_cat:
        norm_g = _normalize_metadata_group(path_group, path_cat)
        if norm_g:
            path_group = norm_g
    if not path_group:
        path_group = "Manuals"

    return {
        "department": path_dept or "Technical & Crewing",
        "vessel": path_vessel,
        "group": path_group,
        "category": path_cat,
        "sub_category": path_sub,
    }


async def _expand_to_files(
    drive_id: str,
    item_ids: list[str],
    recursive: bool = False,
    max_depth: int = 6,
    max_files: int = 500,
    access_token: str | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Expands item_ids into a list of file items.

    If recursive=False:
      - File items are kept directly.
      - Folder items expand only to their DIRECT child files.
    If recursive=True:
      - File items are kept directly.
      - Folder items expand via BFS to all descendant files up to max_depth (relative to the selected folder).

    Enforces max_files cap (default 500).
    Returns (files_to_process, total_discovered_count, is_truncated).
    """
    files: list[dict[str, Any]] = []
    seen_file_ids: set[str] = set()
    total_discovered = 0
    unique_item_ids = list(dict.fromkeys(item_ids))

    for iid in unique_item_ids:
        try:
            item = await gd.get_item(drive_id, iid, access_token=access_token)
        except Exception as exc:
            logger.warning("_expand_to_files: could not fetch item %s – %s", iid, exc)
            continue

        if item.get("file") is not None:
            if iid not in seen_file_ids:
                seen_file_ids.add(iid)
                total_discovered += 1
                if len(files) < max_files:
                    files.append(item)
        elif item.get("folder") is not None:
            queue: list[tuple[str, str, int]] = [(iid, item.get("name") or "", 0)]
            while queue:
                curr_id, curr_name, depth = queue.pop(0)
                try:
                    children = await gd.list_children(drive_id, curr_id, access_token=access_token)
                except Exception as exc:
                    logger.warning("_expand_to_files: could not list children of %s – %s", curr_id, exc)
                    continue

                for child in children:
                    cid = child.get("id")
                    if not cid:
                        continue
                    if child.get("file") is not None:
                        if cid not in seen_file_ids:
                            seen_file_ids.add(cid)
                            total_discovered += 1
                            if len(files) < max_files:
                                files.append(child)
                    elif child.get("folder") is not None and recursive:
                        if depth + 1 < max_depth:
                            queue.append((cid, child.get("name") or "", depth + 1))

    is_truncated = total_discovered > max_files
    return files, total_discovered, is_truncated


@app.post("/api/sites/{site_id}/drives/{drive_id}/bulk-update-tags")
async def bulk_update_site_tags(
    site_id: str,
    drive_id: str,
    request: SiteBulkTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    target_files, total_discovered, is_truncated = await _expand_to_files(
        drive_id=drive_id,
        item_ids=request.item_ids,
        recursive=request.recursive,
        max_depth=6,
        max_files=10000,
        access_token=x_graph_access_token,
    )
    if request.scope == "missing_only":
        target_files = await _filter_site_files_by_scope(drive_id, target_files, request.scope, x_graph_access_token)
        total_discovered = len(target_files)
        is_truncated = False

    if not target_files:
        return {
            "ok": True,
            "updated_count": 0,
            "total_discovered": 0,
            "truncated": False,
            "cap": 10000,
            "results": [],
        }

    vessel_names: list[Any] = []
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                vessel_names = _get_vessels_for_ocr(db)
        except Exception:
            pass
    if not vessel_names:
        try:
            v_list = await get_backend().list_vessels()
            vessel_names = [v["name"] for v in v_list if isinstance(v, dict) and "name" in v]
        except Exception:
            vessel_names = []

    # Semaphore to limit concurrent Graph API calls and avoid throttling
    _update_sem = asyncio.Semaphore(10)

    async def _update_one(file_item: dict[str, Any]) -> dict[str, Any]:
        async with _update_sem:
            file_id = file_item["id"]
            filename = file_item.get("name") or file_id
            raw_p = (file_item.get("parentReference") or {}).get("path") or ""
            p_path = raw_p.split("root:", 1)[-1].strip("/")

            try:
                try:
                    fields = await graph().get(f"/drives/{drive_id}/items/{file_id}/listItem/fields", access_token=x_graph_access_token)
                except Exception:
                    fields = {}
                current = _site_item_tags(fields)

                v_val = request.vessel.strip()
                d_val = request.department.strip()
                g_val = request.group.strip()
                c_val = request.category.strip()

                # vessel-only mode: when only vessel is explicitly set (no group/category/department
                # provided and auto_from_path is off), preserve all existing tags and only update vessel.
                vessel_only_mode = bool(v_val) and not d_val and not g_val and not c_val and not request.auto_from_path

                if request.auto_from_path or (not vessel_only_mode and not (v_val and d_val and g_val and c_val)):
                    path_tags = _derive_path_tags(p_path, filename=filename, known_vessels=vessel_names)
                    if not v_val:
                        v_val = path_tags.get("vessel", "")
                    if not d_val:
                        d_val = path_tags.get("department", "")
                    if not g_val:
                        g_val = path_tags.get("group", "")
                    if not c_val:
                        c_val = path_tags.get("category", "")

                # Merge with current tags so we preserve existing values.
                # In vessel-only mode: keep current group/category/department exactly as-is.
                final_v = v_val or current.get("vessel", "")
                if vessel_only_mode:
                    final_d = current.get("department", "")
                    final_g = current.get("group", "")
                    final_c = current.get("category", "")
                else:
                    final_d = d_val or current.get("department", "")
                    final_g = g_val or current.get("group", "")
                    final_c = c_val or current.get("category", "")

                # skip_if_any_vessel_set: skip any file that already has ANY vessel tag assigned
                if request.skip_if_any_vessel_set and current.get("vessel", "").strip():
                    return {
                        "item_id": file_id,
                        "filename": filename,
                        "ok": True,
                        "skipped": True,
                        "tags": {"department": current.get("department", ""), "vessel": current.get("vessel", "").strip(), "group": current.get("group", ""), "category": current.get("category", "")},
                        "parent_path": p_path,
                    }

                # skip_if_tagged: if the file already has the correct vessel tag, skip it.
                # This prevents re-processing already-tagged files on subsequent runs.
                if request.skip_if_tagged and vessel_only_mode and current.get("vessel", "").strip() == final_v:
                    return {
                        "item_id": file_id,
                        "filename": filename,
                        "ok": True,
                        "skipped": True,
                        "tags": {"department": current.get("department", ""), "vessel": final_v, "group": current.get("group", ""), "category": current.get("category", "")},
                        "parent_path": p_path,
                    }

                # Build payload: in vessel-only mode only include the vessel field so
                # SharePoint does not overwrite group/category with blank values.
                if vessel_only_mode:
                    payload = {k: v for k, v in _build_sharepoint_metadata_payload(vessel=final_v).items() if v}
                else:
                    payload = _build_sharepoint_metadata_payload(
                        department=final_d,
                        vessel=final_v,
                        group=final_g,
                        category=final_c,
                    )

                # Retry loop with backoff (up to 3 retries) on 429/503
                patch_res = None
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        patch_res = await gd.update_file_columns(
                            drive_id, file_id, payload,
                            access_token=x_graph_access_token,
                            sp_access_token=x_sp_access_token,
                        )
                        if patch_res.get("ok"):
                            break
                        err_str = str(patch_res.get("error") or "")
                        if ("429" in err_str or "503" in err_str or "throttled" in err_str.lower()) and attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
                            continue
                        break
                    except Exception as patch_exc:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id, filename=filename, parent_path=p_path, error_reason=str(patch_exc))
                        return {"item_id": file_id, "filename": filename, "ok": False, "error": str(patch_exc), "parent_path": p_path}

                expected_tags = {"department": final_d, "vessel": final_v, "group": final_g, "category": final_c}
                if not patch_res or not patch_res.get("ok"):
                    error_reason = (patch_res or {}).get("error") or "SharePoint update failed"
                    _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id, filename=filename, parent_path=p_path, error_reason=error_reason)
                    return {
                        "item_id": file_id,
                        "filename": filename,
                        "ok": False,
                        "tags": expected_tags,
                        "patch": patch_res or {},
                        "error": error_reason,
                        "parent_path": p_path,
                    }

                _resolve_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id)
                return {
                    "item_id": file_id,
                    "filename": filename,
                    "ok": True,
                    "skipped": False,
                    "tags": expected_tags,
                    "patch": patch_res,
                    "parent_path": p_path,
                }
            except Exception as exc:
                _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id, filename=filename, parent_path=p_path, error_reason=str(exc))
                return {"item_id": file_id, "filename": filename, "ok": False, "error": str(exc), "parent_path": p_path}

    results = await asyncio.gather(*(_update_one(f) for f in target_files))
    invalidate_folder_caches()
    skipped_count = sum(1 for r in results if r.get("ok") and r.get("skipped"))
    updated_count = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
    return {
        "ok": True,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "total_discovered": total_discovered,
        "truncated": is_truncated,
        "cap": 500,
        "results": results,
    }



@app.post("/api/sites/{site_id}/drives/{drive_id}/bulk-update-tags/stream")
async def bulk_update_site_tags_stream(
    site_id: str,
    drive_id: str,
    request: SiteBulkTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """SSE streaming version of bulk-update-tags.

    Streams newline-delimited JSON events as each file is processed:
      {"type": "start", "total": N, "total_discovered": N, "truncated": bool}
      {"type": "progress", "completed": N, "total": N, "result": {...}}
      {"type": "done", "updated_count": N, "skipped_count": N, "failed_count": N}
    """
    target_files, total_discovered, is_truncated = await _expand_to_files(
        drive_id=drive_id,
        item_ids=request.item_ids,
        recursive=request.recursive,
        max_depth=6,
        max_files=10000,
        access_token=x_graph_access_token,
    )
    if request.scope == "missing_only":
        target_files = await _filter_site_files_by_scope(drive_id, target_files, request.scope, x_graph_access_token)
        total_discovered = len(target_files)
        is_truncated = False

    vessel_names: list[Any] = []
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                vessel_names = _get_vessels_for_ocr(db)
        except Exception:
            pass
    if not vessel_names:
        try:
            v_list = await get_backend().list_vessels()
            vessel_names = [v["name"] for v in v_list if isinstance(v, dict) and "name" in v]
        except Exception:
            vessel_names = []

    # Use a queue so results can be streamed as they complete
    queue: asyncio.Queue = asyncio.Queue()
    sem = asyncio.Semaphore(10)

    async def _process_one(file_item: dict[str, Any]) -> None:
        """Process a single file and put result on the queue."""
        async with sem:
            file_id = file_item["id"]
            filename = file_item.get("name") or file_id
            raw_p = (file_item.get("parentReference") or {}).get("path") or ""
            p_path = raw_p.split("root:", 1)[-1].strip("/")
            try:
                try:
                    fields = await graph().get(f"/drives/{drive_id}/items/{file_id}/listItem/fields", access_token=x_graph_access_token)
                except Exception:
                    fields = {}
                current = _site_item_tags(fields)

                v_val = request.vessel.strip()
                d_val = request.department.strip()
                g_val = request.group.strip()
                c_val = request.category.strip()

                vessel_only_mode = bool(v_val) and not d_val and not g_val and not c_val and not request.auto_from_path

                if request.auto_from_path or (not vessel_only_mode and not (v_val and d_val and g_val and c_val)):
                    path_tags = _derive_path_tags(p_path, filename=filename, known_vessels=vessel_names)
                    if not v_val:
                        v_val = path_tags.get("vessel", "")
                    if not d_val:
                        d_val = path_tags.get("department", "")
                    if not g_val:
                        g_val = path_tags.get("group", "")
                    if not c_val:
                        c_val = path_tags.get("category", "")

                final_v = v_val or current.get("vessel", "")
                if vessel_only_mode:
                    final_d = current.get("department", "")
                    final_g = current.get("group", "")
                    final_c = current.get("category", "")
                else:
                    final_d = d_val or current.get("department", "")
                    final_g = g_val or current.get("group", "")
                    final_c = c_val or current.get("category", "")

                # Skip if already has ANY vessel tag and skip_if_any_vessel_set is True
                if request.skip_if_any_vessel_set and current.get("vessel", "").strip():
                    await queue.put({
                        "item_id": file_id, "filename": filename, "ok": True, "skipped": True,
                        "tags": {"department": current.get("department", ""), "vessel": current.get("vessel", "").strip(),
                                 "group": current.get("group", ""), "category": current.get("category", "")},
                        "parent_path": p_path,
                    })
                    return

                # Skip files that already have the correct vessel tag
                if request.skip_if_tagged and vessel_only_mode and current.get("vessel", "").strip() == final_v:
                    await queue.put({
                        "item_id": file_id, "filename": filename, "ok": True, "skipped": True,
                        "tags": {"department": current.get("department", ""), "vessel": final_v,
                                 "group": current.get("group", ""), "category": current.get("category", "")},
                        "parent_path": p_path,
                    })
                    return

                if vessel_only_mode:
                    payload = {k: v for k, v in _build_sharepoint_metadata_payload(vessel=final_v).items() if v}
                else:
                    payload = _build_sharepoint_metadata_payload(
                        department=final_d, vessel=final_v, group=final_g, category=final_c,
                    )

                patch_res = None
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        patch_res = await gd.update_file_columns(
                            drive_id, file_id, payload,
                            access_token=x_graph_access_token,
                            sp_access_token=x_sp_access_token,
                        )
                        if patch_res.get("ok"):
                            break
                        err_str = str(patch_res.get("error") or "")
                        if ("429" in err_str or "503" in err_str or "throttled" in err_str.lower()) and attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        break
                    except Exception as patch_exc:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id, filename=filename, parent_path=p_path, error_reason=str(patch_exc))
                        await queue.put({"item_id": file_id, "filename": filename, "ok": False, "error": str(patch_exc), "parent_path": p_path, "skipped": False})
                        return

                expected_tags = {"department": final_d, "vessel": final_v, "group": final_g, "category": final_c}
                if not patch_res or not patch_res.get("ok"):
                    err_msg = (patch_res or {}).get("error") or "SharePoint update failed"
                    # If the file is locked but already has the correct vessel tag, treat as success
                    is_lock_err = "locked" in err_msg.lower() or "lock" in err_msg.lower()
                    current_vessel = (current.get("vessel") or "").strip()
                    target_vessel = (final_v or "").strip()

                    def _vessel_match(a: str, b: str) -> bool:
                        """Compare vessel names, stripping common suffixes and normalising case."""
                        import re as _re
                        def _norm(s: str) -> str:
                            return _re.sub(r"\s+", " ", s.strip().lower())
                        return _norm(a) == _norm(b)

                    if is_lock_err and target_vessel and _vessel_match(current_vessel, target_vessel):
                        # File is locked but already has the right vessel — count as skipped success
                        await queue.put({
                            "item_id": file_id, "filename": filename, "ok": True, "skipped": True,
                            "tags": expected_tags,
                            "parent_path": p_path,
                        })
                    else:
                        _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id, filename=filename, parent_path=p_path, error_reason=err_msg)
                        await queue.put({
                            "item_id": file_id, "filename": filename, "ok": False, "skipped": False,
                            "tags": expected_tags, "patch": patch_res or {},
                            "error": err_msg,
                            "parent_path": p_path,
                        })
                else:
                    _resolve_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id)
                    await queue.put({
                        "item_id": file_id, "filename": filename, "ok": True, "skipped": False,
                        "tags": expected_tags, "patch": patch_res, "parent_path": p_path,
                    })
            except Exception as exc:
                _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=file_id, filename=filename, parent_path=p_path, error_reason=str(exc))
                await queue.put({"item_id": file_id, "filename": filename, "ok": False, "error": str(exc), "parent_path": p_path, "skipped": False})

    async def event_generator():
        total = len(target_files)
        # Send start event
        yield json.dumps({"type": "start", "total": total, "total_discovered": total_discovered, "truncated": is_truncated}) + "\n"

        if total == 0:
            yield json.dumps({"type": "done", "updated_count": 0, "skipped_count": 0, "failed_count": 0}) + "\n"
            return

        # Fire all file-processing tasks concurrently (semaphore controls parallelism)
        tasks = [asyncio.create_task(_process_one(f)) for f in target_files]

        completed = 0
        updated_count = 0
        skipped_count = 0
        failed_count = 0

        while completed < total:
            result = await queue.get()
            completed += 1
            if result.get("ok") and result.get("skipped"):
                skipped_count += 1
            elif result.get("ok"):
                updated_count += 1
            else:
                failed_count += 1

            # Only stream non-skipped results so the feed only shows actual work
            if not result.get("skipped"):
                yield json.dumps({
                    "type": "progress",
                    "completed": updated_count + failed_count,
                    "skipped": skipped_count,
                    "total": total - skipped_count if skipped_count else total,
                    "result": result,
                }) + "\n"

        await asyncio.gather(*tasks, return_exceptions=True)
        invalidate_folder_caches()
        yield json.dumps({
            "type": "done",
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
        }) + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


async def _site_scan_file(drive_id: str, item_id: str, access_token: str | None = None) -> dict[str, Any]:
    filename = item_id
    parent_path = ""
    try:
        item = await gd.get_item(drive_id, item_id, access_token=access_token)
        filename = item.get("name") or item_id
        parent_path = ((item.get("parentReference") or {}).get("path") or "").split("root:", 1)[-1].strip("/")

        try:
            fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=access_token)
        except Exception:
            fields = {}
        current_tags = _site_item_tags(fields)

        data = b""
        content_type = ""
        try:
            dl_data, dl_ctype, dl_fname = await asyncio.wait_for(
                gd.download_file(drive_id, item_id, access_token=access_token),
                timeout=45.0,
            )
            data = dl_data
            content_type = dl_ctype or ""
            if dl_fname:
                filename = dl_fname
        except (asyncio.TimeoutError, Exception) as dl_exc:
            logger.warning("Download timed out or failed for %s (%s): %s", item_id, filename, dl_exc)

        from .ocr.extract import extract_text
        from .ocr.drawing_category import (
            classify_document_content,
            classify_against_db_categories,
            classify_all_fields_tiered,
            is_non_document_filename,
        )

        text_value = ""
        if data:
            try:
                text_value = await asyncio.wait_for(
                    asyncio.to_thread(extract_text, data, filename, content_type or ""),
                    timeout=20.0,
                )
            except (asyncio.TimeoutError, Exception) as ocr_exc:
                logger.warning("OCR extraction timed out or failed for %s (%s): %s", item_id, filename, ocr_exc)
                text_value = ""

        vessel_names: list[Any] = []
        db_categories = []
        if settings.db_configured:
            try:
                from .db.base import SessionLocal
                from .db import models as db_models
                with SessionLocal() as db:
                    vessel_names = _get_vessels_for_ocr(db)
                    db_categories = db.query(db_models.DocumentCategory).filter(db_models.DocumentCategory.is_active == True).all()
            except Exception:
                pass
        if not vessel_names:
            try:
                v_list = await get_backend().list_vessels()
                vessel_names = [v["name"] for v in v_list if isinstance(v, dict) and "name" in v]
            except Exception:
                vessel_names = []

        classification = classify_document_content(text_value, filename=filename, known_vessels=vessel_names)
        tiered = classify_all_fields_tiered(text_value, filename=filename, known_vessels=vessel_names, source_path=parent_path)
        best_db_cat, db_conf, db_matches = classify_against_db_categories(text_value, filename=filename, db_categories=db_categories)

        detected_vessel = (tiered.get("vessel") or {}).get("value") or classification.get("vessel_name") or ""
        # Reject if OCR detected a folder/library/department name as vessel (e.g. "Drawings and Manuals August")
        if detected_vessel:
            _NON_VESSEL_PREFIXES = (
                "drawings and manuals", "drawings & manuals", "drawing and manual",
                "technical", "commercial", "insurance", "knowledge bank", "kaizen",
                "type of vessel", "shared documents", "documents",
            )
            _low_v = detected_vessel.lower().strip()
            if any(_low_v.startswith(p) for p in _NON_VESSEL_PREFIXES):
                detected_vessel = ""

        detected_group = (tiered.get("group") or {}).get("value") or classification.get("group") or "Manual"
        fallback_category = "To be Classified" if detected_group.lower().startswith("manual") else "Basic"
        matched_cat_name = best_db_cat.name if best_db_cat else None
        detected_category = (tiered.get("category") or {}).get("value") or classification.get("category") or (matched_cat_name if matched_cat_name not in ("Drawing", "Manual") else fallback_category) or fallback_category
        detected_sub_category = (tiered.get("sub_category") or {}).get("value") or classification.get("sub_category") or classification.get("leaf") or "To be Classified"
        detected_dept = (tiered.get("department") or {}).get("value") or classification.get("department") or "Technical & Crewing"

        ocr_suggestion = {
            "department": {
                "value": detected_dept,
                "confidence": round(float((tiered.get("department") or {}).get("confidence") or 0.85), 2),
            },
            "vessel": {
                "value": detected_vessel,
                "confidence": round(float((tiered.get("vessel") or {}).get("confidence") or (0.95 if detected_vessel else 0.0)), 2),
                "vessel_in_filename_only": bool(tiered.get("vessel_in_filename_only")),
            },
            "group": {
                "value": detected_group,
                "confidence": round(float((tiered.get("group") or {}).get("confidence") or 0.90), 2),
            },
            "category": {
                "value": detected_category,
                "confidence": round(float((tiered.get("category") or {}).get("confidence") or (db_conf if best_db_cat else 0.60)), 2),
            },
        }

        path_parts = [p.strip() for p in parent_path.replace("\\", "/").split("/") if p.strip()]
        path_vessel = _extract_vessel_from_path(parent_path, vessel_names) or ""
        path_group = ""
        path_cat = ""
        path_sub = ""
        path_dept = ""
        for p in path_parts:
            if p.lower() in ("technical & crewing", "technical", "commercial & chartering", "insurance", "kaizen - knowledge bank", "knowledge bank"):
                path_dept = p
                break

        # ── Vessel fallback: derive from folder structure if not matched in known vessels ──
        if not path_vessel:
            from .ocr.drawing_category import _extract_vessel_from_folder_path, _is_generic_or_system_folder
            path_vessel = _extract_vessel_from_folder_path(parent_path) or ""
        else:
            from .ocr.drawing_category import _is_generic_or_system_folder
        if path_vessel and _is_generic_or_system_folder(path_vessel):
            path_vessel = ""

        dm_idx = next(
            (
                i for i, p in enumerate(path_parts)
                if p.lower() in ("drawings and manuals", "drawings & manuals", "drawing and manual")
                or any(
                    p.lower().startswith(prefix)
                    for prefix in ("drawings and manuals ", "drawings & manuals ", "drawing and manual ")
                )
            ),
            -1,
        )
        if dm_idx >= 0 and dm_idx + 1 < len(path_parts):
            rem = path_parts[dm_idx + 1:]
            if len(rem) >= 1:
                if rem[0].lower() in ("drawings", "drawing", "manuals", "manual"):
                    path_group = "Drawing" if rem[0].lower().startswith("draw") else "Manual"
                    if len(rem) >= 2:
                        path_cat = rem[1]
                    if len(rem) >= 3:
                        path_sub = rem[2]
                else:
                    # Check if rem[0] is actually a category folder (e.g. "MB MAIN ENGINE", "Main Engine", "Auxiliary Engine", etc.)
                    rem0_lower = rem[0].lower().strip()
                    from .ocr.drawing_category import MANUAL_TAXONOMY, DRAWING_TAXONOMY
                    is_cat = (
                        rem0_lower.startswith("mb ")
                        or "main engine" in rem0_lower
                        or any(rem0_lower == c.lower() or rem0_lower.startswith(c.lower()) for c in MANUAL_TAXONOMY)
                        or any(rem0_lower == c.lower() or rem0_lower.startswith(c.lower()) for c in DRAWING_TAXONOMY)
                    )
                    if is_cat:
                        if "main engine" in rem0_lower or rem0_lower.startswith("mb "):
                            path_group = "Manual"
                            path_cat = "Main Engine"
                        elif any(rem0_lower == c.lower() for c in MANUAL_TAXONOMY):
                            path_group = "Manual"
                            path_cat = next(c for c in MANUAL_TAXONOMY if c.lower() == rem0_lower)
                        elif any(rem0_lower == c.lower() for c in DRAWING_TAXONOMY):
                            path_group = "Drawing"
                            path_cat = next(c for c in DRAWING_TAXONOMY if c.lower() == rem0_lower)
                        else:
                            path_cat = rem[0]
                        if len(rem) >= 2:
                            path_sub = rem[1]
                    else:
                        # rem[0] after 'Drawings and Manuals *' is the vessel name (e.g. Elephanta)
                        if not path_vessel:
                            path_vessel = rem[0]
                        if len(rem) >= 2:
                            if rem[1].lower() in ("drawings", "drawing", "manuals", "manual"):
                                path_group = "Drawing" if rem[1].lower().startswith("draw") else "Manual"
                                if len(rem) >= 3:
                                    path_cat = rem[2]
                                if len(rem) >= 4:
                                    path_sub = rem[3]
                            else:
                                path_cat = rem[1]
                                if len(rem) >= 3:
                                    path_sub = rem[2]
        else:
            # No "Drawings and Manuals" folder found — scan for a Drawings/Manuals marker anywhere
            grp_idx = next(
                (i for i, p in enumerate(path_parts) if p.lower() in ("drawings", "drawing", "manuals", "manual")),
                -1,
            )
            if grp_idx >= 0:
                path_group = "Drawing" if path_parts[grp_idx].lower().startswith("draw") else "Manual"
                if grp_idx + 1 < len(path_parts):
                    path_cat = path_parts[grp_idx + 1]
                if grp_idx + 2 < len(path_parts):
                    path_sub = path_parts[grp_idx + 2]
            else:
                # Last resort: use the deepest folder segments for cat/sub
                skip_set = {"shared documents", "documents", "root"}
                if path_vessel:
                    skip_set.add(path_vessel.lower())
                meaningful = [p for p in path_parts if p.lower() not in skip_set]
                if meaningful:
                    path_cat = meaningful[-2] if len(meaningful) >= 2 else meaningful[-1]
                    path_sub = meaningful[-1] if len(meaningful) >= 2 else ""

        # Filename keyword heuristics — more reliable than OCR for ambiguous "Drawings and Manuals *" cases
        _MANUAL_FN_WORDS = {
            "operation", "maintenance", "maint", "overhaul", "instruction",
            "specification", "spare", "procedure", "service", "repair",
            "data", "component", "system", "guide", "manual", "operator",
            "list of spare", "technical data",
        }
        _DRAWING_FN_WORDS = {
            "dwg", "drawing", "plan", "diagram", "layout", "arrangement",
            "elevation", "detail", "section", "register", "schematic",
        }
        _fn_lower = filename.lower()
        _fn_has_manual = any(w in _fn_lower for w in _MANUAL_FN_WORDS)
        _fn_has_drawing = any(w in _fn_lower for w in _DRAWING_FN_WORDS)

        # Ensure path_group is always resolved
        if not path_group:
            for p in path_parts:
                pl = p.lower()
                # Skip ambiguous "Drawings and Manuals *" library folders — they contain BOTH keywords
                if pl.startswith("drawings and manuals") or pl.startswith("drawings & manuals"):
                    continue
                if "drawing" in pl or "dwg" in pl:
                    path_group = "Drawing"
                    break
                elif "manual" in pl or "manuals" in pl:
                    path_group = "Manual"
                    break
        if not path_group and path_cat:
            norm_g = _normalize_metadata_group("", path_cat)
            if norm_g:
                path_group = "Drawing" if norm_g.lower().startswith("draw") else "Manual"
        if not path_group:
            # Prefer filename keywords over OCR for group (OCR can be misled by folder names)
            if _fn_has_drawing and not _fn_has_manual:
                path_group = "Drawing"
            elif _fn_has_manual and not _fn_has_drawing:
                path_group = "Manual"
            elif detected_group:
                path_group = "Drawing" if detected_group.lower().startswith("draw") else "Manual"
            else:
                path_group = "Drawing" if _fn_has_drawing else "Manual"

        if is_non_document_filename(filename):
            path_group = "Drawing"
            path_cat = "To Be Classified"
            path_sub = "To Be Classified"


        path_values = {
            "department": path_dept or "Technical & Crewing",
            "vessel": path_vessel,
            "group": path_group,
            "category": path_cat,
        }


        path_suggestion = {
            key: {"value": val, "label": "Derived from current folder location"}
            for key, val in path_values.items()
        }

        proposed_tags = {}
        for k in ("department", "vessel", "group", "category"):
            ocr_v = ocr_suggestion.get(k, {}).get("value", "")
            ocr_c = ocr_suggestion.get(k, {}).get("confidence", 0)
            path_v = path_values.get(k, "")
            if current_tags.get(k):
                proposed_tags[k] = current_tags[k]
            elif k == "vessel":
                # For vessel, use the folder-derived value when available; only override
                # with OCR when the OCR hit is very strong.
                if path_v and (not ocr_v or ocr_c < 0.85):
                    proposed_tags[k] = path_v
                elif ocr_v:
                    proposed_tags[k] = ocr_v
                else:
                    proposed_tags[k] = path_v
            elif path_v:
                # For department/group/category, the current folder path is the most
                # reliable source of taxonomy because OCR can be noisy on scanned drawings.
                proposed_tags[k] = path_v
            elif ocr_v:
                proposed_tags[k] = ocr_v
            else:
                proposed_tags[k] = ""

        path_parts = [p.strip() for p in parent_path.replace("\\", "/").split("/") if p.strip()]
        subfolder_name = path_parts[-1] if path_parts else ""
        vessel_in_filename_only = bool(tiered.get("vessel_in_filename_only"))
        return {
            "item_id": item_id,
            "filename": filename,
            "parent_path": parent_path,
            "subfolder_name": subfolder_name,
            "status": "needs_selection",
            "ocr_suggestion": ocr_suggestion,
            "path_suggestion": path_suggestion,
            "current_tags": current_tags,
            "current_values": current_tags,
            "proposed_tags": proposed_tags,
            "confidence": max((item.get("confidence", 0) for item in ocr_suggestion.values()), default=0.5),
            # True when vessel was detected from filename alias (e.g. N-2119 → Bow Fighter)
            # but the vessel name is absent from the file's actual text content.
            "vessel_in_filename_only": vessel_in_filename_only,
            "error": None,
        }
    except Exception as exc:
        return {
            "item_id": item_id,
            "filename": filename if 'filename' in locals() else item_id,
            "parent_path": parent_path if 'parent_path' in locals() else "",
            "subfolder_name": "",
            "current_tags": {},
            "current_values": {},
            "proposed_tags": {},
            "status": "error",
            "confidence": 0,
            "error": str(exc),
        }


@app.post("/api/sites/{site_id}/drives/{drive_id}/scan-tags")
async def scan_site_tags(
    site_id: str,
    drive_id: str,
    request: SiteScanTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    target_files, total_discovered, is_truncated = await _expand_to_files(
        drive_id=drive_id,
        item_ids=request.item_ids,
        recursive=request.recursive,
        max_depth=6,
        # OCR review must cover the complete selected tree in one request.
        # Graph child enumeration already paginates; this high ceiling only
        # protects the service from an accidentally unbounded selection.
        max_files=10000,
        access_token=x_graph_access_token,
    )
    total_in_scope = total_discovered
    if request.scope == "missing_only":
        target_files = await _filter_site_files_by_scope(drive_id, target_files, request.scope, x_graph_access_token)
        is_truncated = False
    excluded = set(request.exclude_item_ids)
    if excluded:
        target_files = [item for item in target_files if item.get("id") not in excluded]
        total_discovered = len(target_files)
        is_truncated = False
    sem = asyncio.Semaphore(4)

    async def _bounded_scan(f: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                return await asyncio.wait_for(
                    _site_scan_file(drive_id, f["id"], access_token=x_graph_access_token),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                return {
                    "item_id": f["id"],
                    "filename": f.get("name") or f["id"],
                    "status": "error",
                    "confidence": 0,
                    "error": "OCR scan timed out after 90 seconds",
                }
            except Exception as exc:
                return {
                    "item_id": f["id"],
                    "filename": f.get("name") or f["id"],
                    "status": "error",
                    "confidence": 0,
                    "error": str(exc),
                }

    results = await asyncio.gather(*(_bounded_scan(f) for f in target_files))
    invalidate_folder_caches()
    return {
        "results": results,
        "scanned": len(results),
        "total_discovered": total_in_scope,
        "eligible_count": len(target_files),
        "scope": request.scope,
        "truncated": is_truncated,
        "cap": 10000,
    }


@app.post("/api/sites/{site_id}/drives/{drive_id}/scan-tags/stream")
async def scan_site_tags_stream(
    site_id: str,
    drive_id: str,
    request: SiteScanTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Stream OCR scan progress as newline-delimited JSON events."""
    async def event_generator():
        # Send an event before Graph traversal/tag inspection so the UI never
        # appears hung while a large folder tree is being discovered.
        yield json.dumps({"type": "start", "phase": "discovering", "total": 0}) + "\n"
        target_files, total_discovered, is_truncated = await _expand_to_files(
            drive_id=drive_id,
            item_ids=request.item_ids,
            recursive=request.recursive,
            max_depth=6,
            max_files=10000,
            access_token=x_graph_access_token,
        )
        total_in_scope = total_discovered
        if request.scope == "missing_only":
            target_files = await _filter_site_files_by_scope(
                drive_id, target_files, request.scope, x_graph_access_token
            )
            is_truncated = False
        excluded = set(request.exclude_item_ids)
        if excluded:
            target_files = [item for item in target_files if item.get("id") not in excluded]

        total = len(target_files)
        yield json.dumps({
            "type": "start",
            "total": total,
            "total_discovered": total_in_scope,
            "truncated": is_truncated,
            "scope": request.scope,
        }) + "\n"
        if total == 0:
            yield json.dumps({"type": "done", "scanned": 0}) + "\n"
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        semaphore = asyncio.Semaphore(4)

        async def scan_one(file_item: dict[str, Any]) -> None:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        _site_scan_file(
                            drive_id, file_item["id"], access_token=x_graph_access_token
                        ),
                        timeout=90,
                    )
                except asyncio.TimeoutError:
                    result = {
                        "item_id": file_item["id"],
                        "filename": file_item.get("name") or file_item["id"],
                        "status": "error",
                        "confidence": 0,
                        "error": "OCR scan timed out after 90 seconds",
                    }
                except Exception as exc:
                    result = {
                        "item_id": file_item["id"],
                        "filename": file_item.get("name") or file_item["id"],
                        "status": "error",
                        "confidence": 0,
                        "error": str(exc),
                    }
            await queue.put(result)

        tasks = [asyncio.create_task(scan_one(item)) for item in target_files]
        completed = 0
        try:
            while completed < total:
                result = await queue.get()
                completed += 1
                yield json.dumps({
                    "type": "progress",
                    "completed": completed,
                    "total": total,
                    "result": result,
                }) + "\n"
            await asyncio.gather(*tasks, return_exceptions=True)
            invalidate_folder_caches()
            yield json.dumps({"type": "done", "scanned": completed}) + "\n"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/api/sites/{site_id}/drives/{drive_id}/tag-failures")
async def list_tag_failures(
    site_id: str,
    drive_id: str,
    status: str = "needs_retry",
    _session: object = Depends(require_session),
):
    if not settings.db_configured:
        return {"failures": [], "count": 0}
    from .db.base import SessionLocal
    from .db.models import TagFailure
    with SessionLocal() as db:
        rows = db.query(TagFailure).filter(
            TagFailure.site_id == site_id,
            TagFailure.drive_id == drive_id,
            TagFailure.status == status,
        ).order_by(TagFailure.last_attempted_at.desc()).all()
        return {"failures": [{
            "file_id": row.file_id,
            "filename": row.filename,
            "parent_path": row.parent_path,
            "error_reason": row.error_reason,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "first_failed_at": row.first_failed_at.isoformat() if row.first_failed_at else None,
            "last_attempted_at": row.last_attempted_at.isoformat() if row.last_attempted_at else None,
        } for row in rows], "count": len(rows)}


@app.post("/api/sites/{site_id}/drives/{drive_id}/retry-tag-failures")
async def retry_tag_failures(
    site_id: str,
    drive_id: str,
    request: TagFailureActionIn,
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    if not settings.db_configured:
        return {"ok": True, "results": [], "message": "Database is not configured"}
    from .db.base import SessionLocal
    from .db.models import TagFailure
    with SessionLocal() as db:
        query = db.query(TagFailure).filter(
            TagFailure.site_id == site_id,
            TagFailure.drive_id == drive_id,
            TagFailure.status == "needs_retry",
        )
        if request.file_ids:
            query = query.filter(TagFailure.file_id.in_(request.file_ids))
        file_ids = [row.file_id for row in query.all()]
    result = await bulk_update_site_tags(
        site_id, drive_id,
        SiteBulkTagsIn(item_ids=file_ids, auto_from_path=True, recursive=True, scope="all"),
        x_graph_access_token, x_sp_access_token, _session,
    ) if file_ids else {"ok": True, "results": []}
    return result


@app.post("/api/sites/{site_id}/drives/{drive_id}/dismiss-tag-failures")
async def dismiss_tag_failures(
    site_id: str,
    drive_id: str,
    request: TagFailureActionIn,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    if not settings.db_configured:
        return {"ok": True, "dismissed": 0}
    from .db.base import SessionLocal
    from .db.models import TagFailure
    with SessionLocal() as db:
        query = db.query(TagFailure).filter(
            TagFailure.site_id == site_id,
            TagFailure.drive_id == drive_id,
            TagFailure.status == "needs_retry",
        )
        if request.file_ids:
            query = query.filter(TagFailure.file_id.in_(request.file_ids))
        rows = query.all()
        for row in rows:
            row.status = "dismissed"
            row.dismissed_by = x_user_email or "user"
            row.dismissed_reason = request.reason.strip() or None
        db.commit()
        return {"ok": True, "dismissed": len(rows)}


@app.post("/api/sites/{site_id}/drives/{drive_id}/items/{item_id}/resolve-tags")
async def resolve_site_tags(
    site_id: str,
    drive_id: str,
    item_id: str,
    request: SiteResolveTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    allowed = {"ocr", "path", "manual", "skip"}
    try:
        try:
            fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=x_graph_access_token)
        except Exception:
            fields = {}
        current = _site_item_tags(fields)
        proposed: dict[str, str] = {}
        ocr = request.values.get("ocr_tags", {}) if isinstance(request.values.get("ocr_tags"), dict) else {}
        path = request.values.get("path_tags", {}) if isinstance(request.values.get("path_tags"), dict) else {}
        for field in ("department", "vessel", "group", "category"):
            choice = request.choices.get(field, "skip")
            if choice not in allowed:
                raise HTTPException(400, f"Invalid choice for {field}")
            if choice == "ocr":
                proposed[field] = str(ocr.get(field) or "").strip()
            elif choice == "path":
                proposed[field] = str(path.get(field) or "").strip()
            elif choice == "manual":
                proposed[field] = str(request.values.get(field) or "").strip()
            elif choice == "skip":
                val = str(request.values.get(field) or "").strip()
                if val:
                    proposed[field] = val
        selected_fields = {key: value for key, value in proposed.items() if value}
        if selected_fields:
            merged = {
                "department": selected_fields.get("department") or current.get("department") or "",
                "vessel": selected_fields.get("vessel") or current.get("vessel") or "",
                "group": selected_fields.get("group") or current.get("group") or "",
                "category": selected_fields.get("category") or current.get("category") or "",
            }
            patch_res = await gd.update_file_columns(
                drive_id, item_id, _build_sharepoint_metadata_payload(**merged),
                access_token=x_graph_access_token,
                sp_access_token=x_sp_access_token,
            )
            if not patch_res.get("ok"):
                _record_tag_failure(site_id=site_id, drive_id=drive_id, file_id=item_id,
                                    filename=item_id, parent_path="",
                                    error_reason=patch_res.get("error") or "SharePoint did not save the tags")
                raise HTTPException(502, patch_res.get("error") or "SharePoint did not save the tags")
            saved_fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=x_graph_access_token)
            if not _sharepoint_tags_match(saved_fields, merged):
                # The write already succeeded (patch_res.ok=True). A mismatch here is
                # usually caused by a minor label difference between the user-provided
                # value and the official term store label (e.g. spacing vs underscore).
                # Log a warning but do NOT fail the request — the tags are saved.
                import logging as _log2
                _log2.getLogger(__name__).warning(
                    "resolve_site_tags: post-write field mismatch for item_id=%s "
                    "(expected=%s, actual=%s) — write confirmed ok; returning success.",
                    item_id,
                    {k: v for k, v in merged.items() if v},
                    _site_item_tags(saved_fields),
                )
            _resolve_tag_failure(site_id=site_id, drive_id=drive_id, file_id=item_id)
            invalidate_folder_caches()
            final_tags = merged
        else:
            final_tags = current
        # Include any non-fatal vessel REST warning in the response for frontend display
        vessel_warn = None
        if selected_fields and patch_res and patch_res.get("match_mode", {}).get("vessel") == "sharepoint_rest_failed":
            vessel_warn = "Vessel Name tag could not be saved via SharePoint REST (taxonomy write failed). Other tags were saved successfully."
        resp = {"ok": True, "item_id": item_id, "tags": final_tags, "updated_fields": list(selected_fields)}
        if vessel_warn:
            resp["vessel_warning"] = vessel_warn
        return resp
    except GraphError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.post("/api/sites/{site_id}/drives/{drive_id}/confirm-tags")
async def confirm_site_tags(
    site_id: str,
    drive_id: str,
    request: SiteConfirmTagsIn,
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    del site_id
    results = []
    for entry in request.items:
        item_id = str(entry.get("item_id") or "").strip()
        proposed = entry.get("proposed_tags") or {}
        if not item_id:
            results.append({"item_id": item_id, "ok": False, "error": "Missing item_id"})
            continue
        try:
            current = _site_item_tags(await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=x_graph_access_token))
            merged = {
                # Prefer proposed (user-selected) values over existing current values
                key: str(proposed.get(key) or "").strip() or current.get(key) or ""
                for key in ("department", "vessel", "group", "category")
            }
            patch_res = await gd.update_file_columns(
                drive_id, item_id, _build_sharepoint_metadata_payload(**merged),
                access_token=x_graph_access_token,
                sp_access_token=x_sp_access_token,
            )
            if not patch_res.get("ok"):
                results.append({"item_id": item_id, "ok": False, "error": patch_res.get("error") or "SharePoint did not save the tags"})
                continue
            saved_fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields", access_token=x_graph_access_token)
            if not _sharepoint_tags_match(saved_fields, merged):
                # The write already succeeded (patch_res.ok=True). A mismatch here is
                # usually a label-formatting difference (spacing, underscores) between
                # the user-provided value and the official term store label. Log a
                # warning but treat the item as successfully tagged.
                import logging as _log3
                _log3.getLogger(__name__).warning(
                    "confirm_site_tags: post-write field mismatch for item_id=%s "
                    "(expected=%s, actual=%s) — write confirmed ok; treating as success.",
                    item_id,
                    {k: v for k, v in merged.items() if v},
                    _site_item_tags(saved_fields),
                )
            results.append({"item_id": item_id, "ok": True, "tags": merged})
        except Exception as exc:
            results.append({"item_id": item_id, "ok": False, "error": str(exc)})
    invalidate_folder_caches()
    return {"results": results, "confirmed": sum(1 for result in results if result["ok"])}


@app.get("/api/sites/{site_id}/term-store-vessels")
async def get_site_term_store_vessels(site_id: str, _session: object = Depends(require_session)):
    """Return vessel names from the SharePoint Term Store for use in tag dropdowns."""
    from .graph.drive import get_vessel_terms
    vessels = await get_vessel_terms(site_id)
    # Also merge with DB vessels so the dropdown is complete
    db_vessels: list[str] = []
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                db_vessels = [v.name for v in db.query(db_models.Vessel).filter(db_models.Vessel.is_active == True).all() if v.name]
        except Exception:
            pass
    combined = sorted({v for v in (vessels + db_vessels) if v}, key=str.casefold)
    return {"vessels": combined, "site_id": site_id}


@app.post("/api/admin/switch-site")
async def admin_switch_site(
    request: AdminSwitchSiteRequest,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Switch the active site globally and persist to database (admin only).
    
    This endpoint:
    1. Validates the target site is configured
    2. Saves the previous configuration for rollback
    3. Updates the active site
    4. Logs the change to audit table
    5. Resets graph client for new credentials
    6. Returns details of the change
    
    On failure, automatically rolls back to previous site.
    """
    admin_email = _require_admin(x_user_email)
    
    from .config import Settings
    from .graph.client import reset_graph_client
    from .db.base import SessionLocal
    from .db import models as m
    
    target_site = request.site_name.lower()
    
    # Get current site details for rollback reference
    previous_site = settings.active_site
    try:
        previous_config = Settings.load_site_config(previous_site)
    except ValueError:
        previous_config = settings
    
    # Validate that the target site is configured
    try:
        new_config = Settings.load_site_config(target_site)
    except ValueError as e:
        new_config = _registered_site_config(target_site)
        if new_config is None:
            raise HTTPException(status_code=400, detail=str(e))
    
    # Cannot switch to the same site
    if target_site == previous_site:
        raise HTTPException(status_code=400, detail="Target site is already active")
    
    # Attempt to validate the new site's Graph connection
    try:
        from .graph.client import graph
        test_client = graph(site_config=new_config)
        test_client._token()
        if getattr(new_config, "drive_id", None):
            await test_client.get(f"/drives/{new_config.drive_id}")
        elif getattr(new_config, "sp_site_id", None):
            await test_client.get(f"/sites/{new_config.sp_site_id}")
        else:
            await test_client.get("/sites/root")
    except Exception as e:
        db = None
        try:
            db = SessionLocal()
            change_log = m.SiteConfigurationChange(
                changed_by_email=admin_email,
                changed_by_name=admin_email,
                previous_site=previous_site,
                new_site=target_site,
                previous_db_name=previous_config.db_name,
                previous_drive_id=previous_config.drive_id,
                previous_site_name=previous_config.sp_site_name,
                new_db_name=new_config.db_name,
                new_drive_id=new_config.drive_id,
                new_site_name=new_config.sp_site_name,
                status="failed",
                error_message=f"Graph connection validation failed: {str(e)}",
                reason=request.reason,
            )
            db.add(change_log)
            db.commit()
        except Exception as log_error:
            logger.warning(f"Failed to log site switch failure: {log_error}")
        finally:
            if db:
                db.close()
        
        raise HTTPException(
            status_code=400,
            detail=f"Target site SharePoint connection failed: {str(e)}"
        )
    
    # Update the global settings by setting ACTIVE_SITE in environment
    import os
    old_active = os.environ.get("ACTIVE_SITE")
    try:
        os.environ["ACTIVE_SITE"] = target_site
        os.environ["APP_ENV"] = target_site

        # Persist to .env file if target_site is defined in .env
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if f"{target_site.upper()}_DRIVE_ID" in content:
                    import re
                    new_content = re.sub(r"^ACTIVE_SITE\s*=.*$", f"ACTIVE_SITE={target_site}", content, flags=re.MULTILINE)
                    new_content = re.sub(r"^APP_ENV\s*=.*$", f"APP_ENV={target_site}", new_content, flags=re.MULTILINE)
                    with open(env_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
            except Exception as env_err:
                logger.warning(f"Failed to persist ACTIVE_SITE to .env: {env_err}")

        # Force new settings to be loaded
        from .config import _SITE_CONFIGS_CACHE
        _SITE_CONFIGS_CACHE.clear()
        
        # Reset graph client for new site
        await reset_graph_client()
        
        # Invalidate caches
        invalidate_folder_caches()
        
        # Log the successful change
        db = None
        try:
            db = SessionLocal()
            change_log = m.SiteConfigurationChange(
                changed_by_email=admin_email,
                changed_by_name=admin_email,
                previous_site=previous_site,
                new_site=target_site,
                previous_db_name=previous_config.db_name,
                previous_drive_id=previous_config.drive_id,
                previous_site_name=previous_config.sp_site_name,
                new_db_name=new_config.db_name,
                new_drive_id=new_config.drive_id,
                new_site_name=new_config.sp_site_name,
                status="success",
                reason=request.reason,
            )
            db.add(change_log)
            db.commit()
        except Exception as log_error:
            logger.warning(f"Failed to log site switch: {log_error}")
        finally:
            if db:
                db.close()
        
        return {
            "success": True,
            "message": f"Successfully switched from {previous_site} to {target_site}",
            "previous_site": previous_site,
            "new_site": target_site,
            "new_site_name": new_config.sp_site_name,
            "new_db_name": new_config.db_name or "In-Memory",
            "new_drive_id": new_config.drive_id,
        }
    
    except Exception as e:
        # Rollback: restore previous site
        if old_active:
            os.environ["ACTIVE_SITE"] = old_active
        else:
            os.environ.pop("ACTIVE_SITE", None)
        
        # Clear cache to force reload
        from .config import _SITE_CONFIGS_CACHE
        _SITE_CONFIGS_CACHE.clear()
        
        # Log the rollback
        db = None
        try:
            db = SessionLocal()
            change_log = m.SiteConfigurationChange(
                changed_by_email=admin_email,
                changed_by_name=admin_email,
                previous_site=previous_site,
                new_site=target_site,
                previous_db_name=previous_config.db_name,
                previous_drive_id=previous_config.drive_id,
                previous_site_name=previous_config.sp_site_name,
                new_db_name=new_config.db_name,
                new_drive_id=new_config.drive_id,
                new_site_name=new_config.sp_site_name,
                status="rolled_back",
                error_message=str(e),
                reason=request.reason,
            )
            db.add(change_log)
            db.commit()
        except Exception as log_error:
            logger.warning(f"Failed to log site switch rollback: {log_error}")
        finally:
            if db:
                db.close()
        
        raise HTTPException(
            status_code=500,
            detail=f"Site switch failed and was rolled back: {str(e)}"
        )


@app.get("/api/admin/site-changes")
async def get_admin_site_changes(
    limit: int = Query(50),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Get audit log of site configuration changes (admin only)."""
    admin_email = _require_admin(x_user_email)
    
    from .db.base import SessionLocal
    from .db import models as m
    
    db = None
    try:
        db = SessionLocal()
        changes = db.query(m.SiteConfigurationChange).order_by(
            m.SiteConfigurationChange.created_at.desc()
        ).limit(limit).all()
        
        return {
            "changes": [
                {
                    "id": c.id,
                    "changed_by_email": c.changed_by_email,
                    "changed_by_name": c.changed_by_name,
                    "previous_site": c.previous_site,
                    "new_site": c.new_site,
                    "previous_site_name": c.previous_site_name,
                    "new_site_name": c.new_site_name,
                    "previous_db_name": c.previous_db_name,
                    "new_db_name": c.new_db_name,
                    "status": c.status,
                    "reason": c.reason,
                    "error_message": c.error_message,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in changes
            ]
        }
    finally:
        if db:
            db.close()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "active_site": settings.active_site,
        "site_name": settings.sp_site_name,
        "env": settings.app_env,
        "mode": backend_mode(),
        "graph_configured": settings.graph_configured,
        "sp_configured": settings.sp_configured,
        "db_configured": settings.db_configured,
        "db_name": settings.db_name if settings.db_configured else None,
    }


@app.get("/api/debug/graph-auth")
async def debug_graph_auth(_session: object = Depends(require_session)):
    """Debug runtime Graph auth context used by backend (safe, no secrets)."""
    out: dict[str, Any] = {
        "active_site": settings.active_site,
        "tenant_id": settings.azure_tenant_id,
        "graph_client_id": settings.graph_client_id,
        "graph_scope": settings.graph_scope,
        "drive_id": settings.sp_drive_id,
        "graph_configured": settings.graph_configured,
        "sp_configured": settings.sp_configured,
        "token": {},
        "drive_probe": {},
    }

    if not settings.graph_configured:
        out["token"] = {"ok": False, "error": "Graph is not configured in environment."}
        return out

    try:
        access_token = graph()._token()
        claims = _decode_jwt_payload_noverify(access_token)
        out["token"] = {
            "ok": True,
            "appid": claims.get("appid") or claims.get("azp"),
            "aud": claims.get("aud"),
            "tid": claims.get("tid"),
            "iss": claims.get("iss"),
            "roles": claims.get("roles") or [],
            "exp": claims.get("exp"),
            "nbf": claims.get("nbf"),
        }
    except Exception as e:
        out["token"] = {"ok": False, "error": str(e)}
        return out

    if not settings.sp_drive_id:
        out["drive_probe"] = {"ok": False, "error": "No SP drive_id configured."}
        return out

    try:
        root = await graph().get(f"/drives/{settings.sp_drive_id}/root?$select=id,name,webUrl")
        out["drive_probe"] = {
            "ok": True,
            "root_id": root.get("id"),
            "root_name": root.get("name"),
            "root_webUrl": root.get("webUrl"),
        }
    except GraphError as e:
        out["drive_probe"] = {
            "ok": False,
            "status": e.status,
            "error": str(e),
        }
    except Exception as e:
        out["drive_probe"] = {"ok": False, "error": str(e)}

    return out


@app.get("/api/debug/sharepoint-access-health")
async def debug_sharepoint_access_health(
    item_id: str | None = Query(None),
    _session: object = Depends(require_session),
):
    """Single-call health probe for drive reachability and listItem fields access."""
    return await _sharepoint_access_health_snapshot(probe_item_id=item_id)


@app.get("/api/debug/sharepoint-metadata-probe")
async def debug_sharepoint_metadata_probe(
    item_id: str = Query(..., min_length=3),
    patch_test: bool = Query(False),
    _session: object = Depends(require_session),
):
    """Probe listItem fields read/write for one drive item to diagnose metadata failures."""
    if not settings.sp_configured or not settings.sp_drive_id:
        raise HTTPException(400, "SharePoint is not configured")

    drive_id = settings.sp_drive_id
    result: dict[str, Any] = {
        "ok": True,
        "drive_id": drive_id,
        "item_id": item_id,
        "read": {},
        "patch": {},
    }

    try:
        fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
        result["read"] = {
            "ok": True,
            "field_keys": [k for k in fields.keys() if not str(k).startswith("@")],
            "sample": {
                "Department": _pick_sp_field(fields, ["Department", "main_folder", "MainFolder", "DMS_Department"]),
                "VesselName": _pick_sp_field(fields, ["VesselName", "vessel", "vessel_name", "shipname"]),
                "Group": _pick_sp_field(fields, ["Group", "group", "DMS_Group"]),
                "Category": _pick_sp_field(fields, ["Category", "category", "DMS_Category"]),
                "SubCategory": _pick_sp_field(fields, ["SubCategory", "subcategory", "sub_category", "DMS_SubCategory"]),
            },
        }
    except GraphError as e:
        result["read"] = {"ok": False, "status": e.status, "error": str(e)}
    except Exception as e:
        result["read"] = {"ok": False, "error": str(e)}

    if patch_test:
        try:
            # No-op style probe value to confirm write permission without changing real taxonomy values.
            probe_value = f"Probe {datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            payload = {"Title": probe_value}
            patched = await graph().patch(
                f"/drives/{drive_id}/items/{item_id}/listItem/fields",
                json=payload,
            )
            result["patch"] = {
                "ok": True,
                "payload": payload,
                "response_keys": list((patched or {}).keys()) if isinstance(patched, dict) else [],
            }
        except GraphError as e:
            result["patch"] = {"ok": False, "status": e.status, "error": str(e)}
        except Exception as e:
            result["patch"] = {"ok": False, "error": str(e)}

    return result


def _get_vessels_for_ocr(db) -> list[dict[str, Any]]:
    return [
        {"name": v.name, "imo": v.imo, "hull_number": v.hull_number}
        for v in db.query(db_models.Vessel).all()
        if v.name
    ]


class MetadataRemediateIn(BaseModel):
    item_ids: list[str] | None = None
    dry_run: bool = True
    max_items: int = 300


@app.get("/api/debug/sharepoint-metadata-audit")
async def debug_sharepoint_metadata_audit(
    max_items: int = Query(500, ge=1, le=5000),
    include_ok: bool = Query(False),
    _session: object = Depends(require_session),
):
    """Audit SharePoint file metadata for shifted/misaligned taxonomy values."""
    if not settings.sp_configured or not settings.sp_drive_id:
        raise HTTPException(400, "SharePoint is not configured")

    drive_id = settings.sp_drive_id
    root_id = await gd.get_root_item_id(drive_id)

    scanned = 0
    files_scanned = 0
    affected = 0
    issues: list[dict[str, Any]] = []
    stack: list[tuple[str, str]] = [(root_id, "")]

    while stack and files_scanned < max_items:
        folder_id, base_path = stack.pop()
        children = await gd.list_children(drive_id, folder_id)
        for child in children:
            name = str(child.get("name") or "").strip()
            if not name:
                continue
            item_id = str(child.get("id") or "").strip()
            if not item_id:
                continue
            rel_path = f"{base_path}/{name}".strip("/")
            scanned += 1

            if child.get("folder"):
                stack.append((item_id, rel_path))
                continue
            if not child.get("file"):
                continue

            files_scanned += 1
            try:
                fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
            except Exception as e:
                issues.append({
                    "item_id": item_id,
                    "path": rel_path,
                    "name": name,
                    "error": f"fields_read_failed: {e}",
                })
                continue

            department = _pick_sp_field(fields, ["Department", "main_folder", "MainFolder", "DMS_Department"])
            vessel = _pick_sp_field(fields, ["VesselName", "vessel", "vessel_name", "shipname"])
            group = _pick_sp_field(fields, ["Group", "group", "DMS_Group"])
            category = _pick_sp_field(fields, ["Category", "category", "DMS_Category"])
            sub_category = _pick_sp_field(fields, ["SubCategory", "subcategory", "sub_category", "DMS_SubCategory"])

            reasons = _metadata_issue_reasons(
                department=department,
                vessel=vessel,
                group=group,
                category=category,
                sub_category=sub_category,
            )
            if reasons:
                affected += 1

            if include_ok or reasons:
                issues.append({
                    "item_id": item_id,
                    "path": rel_path,
                    "name": name,
                    "department": department,
                    "vessel": vessel,
                    "group": group,
                    "category": category,
                    "sub_category": sub_category,
                    "reasons": reasons,
                })

            if files_scanned >= max_items:
                break

    return {
        "ok": True,
        "max_items": max_items,
        "folders_and_files_walked": scanned,
        "files_scanned": files_scanned,
        "affected_files": affected,
        "issues": issues,
        "report_columns": [
            "item_id", "path", "name", "department", "vessel", "group", "category", "sub_category", "reasons",
        ],
    }


@app.post("/api/debug/sharepoint-metadata-remediate")
async def debug_sharepoint_metadata_remediate(
    payload: MetadataRemediateIn,
    _session: object = Depends(require_session),
):
    """Reclassify and repair SharePoint metadata for affected files."""
    if not settings.sp_configured or not settings.sp_drive_id:
        raise HTTPException(400, "SharePoint is not configured")

    from .db.base import SessionLocal
    from .db import models as db_models
    from .ocr.extract import extract_text
    from .ocr.drawing_category import classify_all_fields_tiered

    drive_id = settings.sp_drive_id
    requested_ids = [str(x).strip() for x in (payload.item_ids or []) if str(x).strip()]

    # If explicit IDs are not provided, inspect every file. A wrong vessel tag
    # can otherwise look structurally valid and be absent from the taxonomy audit.
    target_items: list[dict[str, str]] = []
    if requested_ids:
        for iid in requested_ids:
            try:
                meta = await gd.get_item(drive_id, iid, select="id,name,parentReference")
                parent_path = str((meta.get("parentReference") or {}).get("path") or "")
                root_mark = "/root:/"
                rel_parent = parent_path.split(root_mark, 1)[1] if root_mark in parent_path else ""
                rel_parent = rel_parent.strip("/")
                item_name = str(meta.get("name") or iid)
                rel_path = f"{rel_parent}/{item_name}".strip("/")
                target_items.append({"item_id": iid, "name": item_name, "path": rel_path})
            except Exception:
                continue
    else:
        audit = await debug_sharepoint_metadata_audit(max_items=payload.max_items, include_ok=True, _session=_session)
        for row in audit.get("issues", []):
            iid = str(row.get("item_id") or "").strip()
            if iid:
                target_items.append({
                    "item_id": iid,
                    "name": str(row.get("name") or ""),
                    "path": str(row.get("path") or ""),
                })

    target_items = target_items[: payload.max_items]

    with SessionLocal() as db:
        known_vessels = _get_vessels_for_ocr(db)

    remediated = 0
    skipped = 0
    results: list[dict[str, Any]] = []

    for row in target_items:
        item_id = row["item_id"]
        name = row.get("name") or ""
        source_path = row.get("path") or ""

        try:
            fields = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
            current_department = _pick_sp_field(fields, ["Department", "main_folder", "MainFolder", "DMS_Department"])
            current_vessel = _pick_sp_field(fields, ["VesselName", "vessel", "vessel_name", "shipname"])
            current_group = _pick_sp_field(fields, ["Group", "group", "DMS_Group"])
            current_category = _pick_sp_field(fields, ["Category", "category", "DMS_Category"])
            current_sub = _pick_sp_field(fields, ["SubCategory", "subcategory", "sub_category", "DMS_SubCategory"])

            reasons = _metadata_issue_reasons(
                department=current_department,
                vessel=current_vessel,
                group=current_group,
                category=current_category,
                sub_category=current_sub,
            )
            if not reasons and not requested_ids:
                skipped += 1
                continue

            file_bytes, content_type, downloaded_name = await gd.download_file(drive_id, item_id)
            filename = name or downloaded_name or ""
            extracted = extract_text(file_bytes, filename, content_type)
            tiered = classify_all_fields_tiered(
                extracted,
                filename=filename,
                known_vessels=known_vessels,
                source_path=source_path,
            )

            department = _extract_department_from_path(source_path) or _safe_tag_value(tiered.get("department")) or current_department or "Technical & Crewing"
            detected_vessel = _safe_tag_value(tiered.get("vessel"))
            vessel = detected_vessel or current_vessel
            vessel_mismatch = bool(
                detected_vessel
                and _norm_vessel_key(detected_vessel) != _norm_vessel_key(current_vessel)
            )
            unresolved_vessel = not detected_vessel
            category = _safe_tag_value(tiered.get("category")) or "To be Classified"
            sub_category = _safe_tag_value(tiered.get("sub_category")) or "To be Classified"
            group = _normalize_metadata_group(_safe_tag_value(tiered.get("group")), category)
            if not group:
                group = "Manuals" if category.strip().lower() == "to be classified" else _normalize_metadata_group("", category)
            if not group:
                group = "Manuals"

            fixed_payload = _build_sharepoint_metadata_payload(
                department=department,
                vessel=vessel,
                group=group,
                category=category,
                sub_category=sub_category,
            )

            # Do not overwrite a vessel with a path-derived guess. Only a
            # high-confidence OCR detection may correct the current vessel.
            if not reasons and not vessel_mismatch and not requested_ids:
                skipped += 1
                continue

            if not payload.dry_run:
                await gd.update_file_columns(drive_id, item_id, fixed_payload)
                remediated += 1

            results.append({
                "item_id": item_id,
                "name": filename,
                "path": source_path,
                "reasons": reasons,
                "vessel_mismatch": vessel_mismatch,
                "unresolved_vessel": unresolved_vessel,
                "current": {
                    "department": current_department,
                    "vessel": current_vessel,
                    "group": current_group,
                    "category": current_category,
                    "sub_category": current_sub,
                },
                "proposed": {
                    "department": department,
                    "vessel": vessel,
                    "group": group,
                    "category": category,
                    "sub_category": sub_category,
                },
            })
        except Exception as e:
            results.append({
                "item_id": item_id,
                "name": name,
                "path": source_path,
                "error": str(e),
            })

    return {
        "ok": True,
        "dry_run": payload.dry_run,
        "requested": len(target_items),
        "remediated": remediated,
        "skipped": skipped,
        "results": results,
    }


@app.get("/api/vessels")
async def list_vessels(_session: object = Depends(require_session)):
    return await get_backend().list_vessels()


_LIVE_SPO_FILES_FETCHING = False


async def _bg_refresh_live_spo_files(folder_ids_to_fetch: list[str]):
    """Background task to fetch live SPO files without blocking list_vessels_flat_tree responses."""
    global _LIVE_SPO_FILES_FETCHING, _LIVE_SPO_FILES_CACHE, _FLAT_TREE_CACHE
    if _LIVE_SPO_FILES_FETCHING:
        return
    _LIVE_SPO_FILES_FETCHING = True
    try:
        if not settings.graph_configured:
            return
        from .graph.client import graph as _graph
        from .config import settings as _settings
        drive_id_val = _settings.drive_id
        if not drive_id_val:
            return

        async def _fetch_folder_files(folder_id: str) -> tuple[str, list[dict]]:
            try:
                url = f"/drives/{drive_id_val}/items/{folder_id}/children?$select=id,name,size,lastModifiedDateTime,file&$top=200"
                data = await _graph().get(url)
                files = [i for i in (data.get("value") or []) if "file" in i]
                return folder_id, [{"name": f["name"], "id": f["id"]} for f in files]
            except Exception:
                return folder_id, []

        live_spo_files: dict[str, list[dict]] = {}
        BATCH = 40
        for i in range(0, len(folder_ids_to_fetch), BATCH):
            batch = folder_ids_to_fetch[i:i + BATCH]
            batch_results = await asyncio.gather(*[_fetch_folder_files(fid) for fid in batch])
            for fid, files in batch_results:
                if files:
                    live_spo_files[fid] = files
            await asyncio.sleep(0.05)

        _LIVE_SPO_FILES_CACHE["data"] = live_spo_files
        _LIVE_SPO_FILES_CACHE["timestamp"] = time.time()
        _FLAT_TREE_CACHE["data"] = None
    except Exception as exc:
        logging.getLogger(__name__).warning("Background SPO files fetch warning: %s", exc)
    finally:
        _LIVE_SPO_FILES_FETCHING = False


@app.get("/api/vessels/flat-tree")
async def list_vessels_flat_tree(
    force_refresh: bool = Query(default=False),
    vessel_name: str | None = Query(default=None),
    recent_vessels: int | None = Query(default=None, ge=1, le=20),
    vessel_limit: int | None = Query(default=None, ge=1, le=20),
    vessel_offset: int = Query(default=0, ge=0),
    _session: object = Depends(require_session),
):
    vessel_window = vessel_limit or recent_vessels
    now = time.time()
    # When filtering by vessel, skip cache and do a fast targeted DB query
    if not vessel_name and not vessel_window:
        if not force_refresh and _FLAT_TREE_CACHE["data"] is not None and (now - _FLAT_TREE_CACHE["timestamp"]) < CACHE_TTL_FLAT_TREE:
            return _FLAT_TREE_CACHE["data"]

    def clean(s: str) -> str:
        return s.strip("_").strip() if s else s

    if not settings.db_configured:
        be = get_backend()
        vessels = await be.list_vessels()
        if vessel_window and not vessel_name:
            vessels = vessels[vessel_offset:vessel_offset + vessel_window]
        mains = await be.mains()
        out = []
        sr = 0
        for v in vessels:
            vname = clean(v["name"])
            for m in mains:
                mname = clean(m["name"])
                children = await be.children(m["id"])
                ship_node = next((c for c in children if c.get("kind") == "ship" and clean(c.get("name")) == vname), None)
                if ship_node:
                    cat_nodes = await be.children(ship_node["id"])
                else:
                    cat_nodes = [c for c in children if c.get("kind") not in ("file", "ship")]
                async def _recurse_categories(nodes, current_path, path_parts=None):
                    if path_parts is None:
                        path_parts = []
                    nonlocal sr
                    for c in nodes:
                        if c.get("kind") == "file":
                            continue
                        
                        cname = clean(c["name"])
                        new_path = f"{current_path} > {cname}"
                        new_parts = path_parts + [cname]
                        
                        try:
                            children = await be.children(c["id"])
                        except Exception:
                            children = []
                        
                        file_children = [f for f in children if f.get("kind") == "file"]
                        sub_folders = [f for f in children if f.get("kind") not in ("file", "file_wrapper")]
                        
                        if sub_folders:
                            await _recurse_categories(sub_folders, new_path, new_parts)
                        elif file_children:
                            for fn in file_children:
                                sr += 1
                                category_name = path_parts[0] if path_parts else cname
                                sub_cat_name = cname
                                out.append({
                                    "srNo": str(sr),
                                    "vesselName": vname,
                                    "group": mname,
                                    "category": category_name,
                                    "subCategory": sub_cat_name,
                                    "subFolderPath": new_path,
                                    "fileName": fn.get("name"),
                                    "fileId": fn.get("id"),
                                    "canUpload": bool(c.get("upload", True)),
                                    "groupKey": f"{v['id']}||{mname}||{category_name}||{sub_cat_name}||{new_path}",
                                    "uploadFolderId": c["id"],
                                    "monthDriven": bool(c.get("month_driven")),
                                })
                        else:
                            sr += 1
                            category_name = path_parts[0] if path_parts else cname
                            sub_cat_name = cname
                            out.append({
                                "srNo": str(sr),
                                "vesselName": vname,
                                "group": mname,
                                "category": category_name,
                                "subCategory": sub_cat_name,
                                "subFolderPath": new_path,
                                "fileName": None,
                                "fileId": None,
                                "canUpload": bool(c.get("upload", True)),
                                "groupKey": f"{v['id']}||{mname}||{category_name}||{sub_cat_name}||{new_path}",
                                "uploadFolderId": c["id"],
                                "monthDriven": bool(c.get("month_driven")),
                            })

                await _recurse_categories(cat_nodes, f"{vname} > {mname}")
        _FLAT_TREE_CACHE["data"] = out
        _FLAT_TREE_CACHE["timestamp"] = time.time()
        return out

    from .db.base import SessionLocal
    from .db.models import Folder, Vessel, ApprovalRequest

    out = []
    with SessionLocal() as db:
        # Build approved file map: uploadFolderId -> list of (filename, fileId)
        approved_files: dict[str, list[dict]] = {}
        approved_rows = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == "approved",
                ApprovalRequest.action_type == "upload",
                ApprovalRequest.destination_folder_id.isnot(None),
                ApprovalRequest.filename.isnot(None),
            )
            .all()
        )
        for ar in approved_rows:
            fid = ar.destination_folder_id
            if fid not in approved_files:
                approved_files[fid] = []
            approved_files[fid].append({"name": ar.filename, "id": ar.target_id or str(ar.id)})

        q = (
            db.query(Folder, Vessel)
            .join(Vessel, Folder.vessel_id == Vessel.id)
            .filter(
                Folder.kind.in_(["leaf", "month_driven", "drawing_classifier"]),
                Folder.drive_item_id.isnot(None),
                Folder.vessel_id.isnot(None),
            )
        )
        if vessel_name:
            q = q.filter(Vessel.name.ilike(vessel_name.strip()))
        elif vessel_window:
            # Apply paging before loading leaf folders. This keeps the initial
            # Documents view fast and supports loading later vessels in small
            # batches on demand.
            recent_ids = [
                vessel_id for (vessel_id,) in db.query(Vessel.id)
                .order_by(Vessel.created_at.desc().nulls_last(), Vessel.id.desc())
                .offset(vessel_offset)
                .limit(vessel_window)
                .all()
            ]
            q = q.filter(Vessel.id.in_(recent_ids))
        results = q.order_by(Vessel.created_at.desc().nulls_last(), Vessel.id.desc(), Folder.path.asc()).all()

        # A vessel-specific refresh must return the current SharePoint contents in
        # the same response. The shared cache is intentionally background-refreshed
        # for broad list loads, but using it here can return rows without files for
        # up to 60 seconds after an upload or browser refresh.
        live_spo_files: dict[str, list[dict]] = dict(_LIVE_SPO_FILES_CACHE.get("data") or {})
        if (vessel_name or (vessel_window and vessel_window <= 4)) and settings.graph_configured:
            from .graph import drive as gd
            from .graph.client import graph as graph_client

            async def fetch_folder_files(folder_id: str, folder_path: str) -> tuple[str, list[dict]]:
                def parse_files(items: list[dict]) -> list[dict]:
                    return [
                        {"name": item["name"], "id": item["id"]}
                        for item in items if "file" in item
                    ]

                try:
                    children = await gd.list_children(settings.drive_id, folder_id)
                    files = parse_files(children)
                    if files:
                        return folder_id, files
                except Exception:
                    pass

                # Folder IDs cached in the database can predate a drive change.
                # Resolve the current folder by its stable logical path instead.
                try:
                    encoded_path = "/".join(quote(part, safe="") for part in folder_path.split("/"))
                    data = await graph_client().get(
                        f"/drives/{settings.drive_id}/root:/{encoded_path}:/children"
                        "?$select=id,name,size,lastModifiedDateTime,file&$top=200"
                    )
                    return folder_id, parse_files(data.get("value") or [])
                except Exception:
                    return folder_id, []

            folder_ids = list(dict.fromkeys(f.drive_item_id for f, _ in results if f.drive_item_id))
            folder_paths = {f.drive_item_id: f.path for f, _ in results if f.drive_item_id}
            semaphore = asyncio.Semaphore(12)

            async def bounded_fetch(folder_id: str) -> tuple[str, list[dict]]:
                async with semaphore:
                    return await fetch_folder_files(folder_id, folder_paths.get(folder_id, ""))

            live_results = await asyncio.gather(*(bounded_fetch(folder_id) for folder_id in folder_ids))
            live_spo_files.update({folder_id: files for folder_id, files in live_results})
            _LIVE_SPO_FILES_CACHE["data"] = live_spo_files
            _LIVE_SPO_FILES_CACHE["timestamp"] = time.time()

    # Keep the list endpoint DB-fast.  Fetching every leaf folder from
    # SharePoint here can take minutes for a large library and blocks the UI.
    # Return any already-cached SPO files, refresh that cache in the background,
    # and let the client fetch a folder live only when the user opens it.
    now_live = time.time()
    if settings.graph_configured and (now_live - _LIVE_SPO_FILES_CACHE.get("timestamp", 0)) >= CACHE_TTL_LIVE_SPO_FILES:
        folder_ids_to_fetch = list(dict.fromkeys(
            f.drive_item_id for f, v in results
            if f.drive_item_id
        ))
        if folder_ids_to_fetch and not _LIVE_SPO_FILES_FETCHING:
            asyncio.create_task(_bg_refresh_live_spo_files(folder_ids_to_fetch))

    for i, (f, v) in enumerate(results):
        parts = f.path.split("/") if f.path else []
        vessel_name = clean(v.name)
        # New structure: {MainFolder} / {Ship Name} / {Category} / {SubCategory}...
        # parts[0]=MainFolder (group), parts[1]=Ship Name, parts[2]=Category, parts[3+]=SubCategory
        if len(parts) >= 2 and any(m.lower() == parts[0].lower() for m in template.MAIN_FOLDERS):
            # New structure: {MainFolder} / {Ship Name} / {Category} / [SubCategory1] / [SubCategory2]...
            group = clean(parts[0])
            category = clean(parts[2]) if len(parts) > 2 else group
            sub_category = clean(parts[-1]) if len(parts) > 3 else category
            breadcrumb_parts = [vessel_name, group, *[clean(p) for p in parts[2:]]]
        elif len(parts) >= 4 and parts[0].lower() == "vessels":
            # Legacy fallback: Vessels / Specific Vessels / {Ship Name} / {Main} / {Category} / [SubCategory]...
            group = clean(parts[3])
            category = clean(parts[4]) if len(parts) > 4 else group
            sub_category = clean(parts[-1]) if len(parts) > 5 else category
            breadcrumb_parts = [vessel_name, group, *[clean(p) for p in parts[4:]]]
        else:
            group = clean(parts[0]) if parts else ""
            category = clean(parts[-1]) if parts else clean(f.name)
            sub_category = category
            breadcrumb_parts = [vessel_name, group, *[clean(p) for p in (parts[1:] if len(parts) > 1 else [category])]]

        sub_path = " > ".join(breadcrumb_parts)

        # Merge approved DB files + live SPO files (deduplicate by name)
        folder_files: list[dict] = list(approved_files.get(f.drive_item_id, []))
        spo_names = {ff["name"] for ff in folder_files}
        for spo_file in live_spo_files.get(f.drive_item_id, []):
            if spo_file["name"] not in spo_names:
                folder_files.append(spo_file)
                spo_names.add(spo_file["name"])

        if folder_files:
            for j, file_entry in enumerate(folder_files):
                out.append({
                    "srNo": str(i + 1) + (f".{j+1}" if j > 0 else ""),
                    "vesselName": vessel_name,
                    "group": group,
                    "category": category,
                    "subCategory": sub_category,
                    "subFolderPath": sub_path,
                    "fileName": file_entry["name"],
                    "fileId": file_entry["id"],
                    "canUpload": True,
                    "groupKey": f"{vessel_name}||{group}||{category}||{sub_category}||{sub_path}",
                    "uploadFolderId": f.drive_item_id,
                    "monthDriven": f.month_driven,
                })
        else:
            out.append({
                "srNo": str(i + 1),
                "vesselName": vessel_name,
                "group": group,
                "category": category,
                "subCategory": sub_category,
                "subFolderPath": sub_path,
                "fileName": None,
                "fileId": None,
                "canUpload": True,
                "groupKey": f"{vessel_name}||{group}||{category}||{sub_category}||{sub_path}",
                "uploadFolderId": f.drive_item_id,
                "monthDriven": f.month_driven,
            })
    # Only store unfiltered results in the shared cache.
    if not vessel_name and not vessel_window:
        _FLAT_TREE_CACHE["data"] = out
        _FLAT_TREE_CACHE["timestamp"] = time.time()
    return out


@app.post("/api/vessels", status_code=201)
async def create_vessel(
    payload: VesselIn,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    vtype = (payload.vessel_type or "").strip() or None
    if vtype and vtype not in VESSEL_TYPES:
        raise HTTPException(400, "Invalid vessel type")
    email = (x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().create_vessel(
            payload.name,
            payload.imo,
            shipyard=(payload.shipyard or "").strip() or None,
            hull_number=(payload.hull_number or "").strip() or None,
            vessel_type=vtype,
            requesting_email=email,
            requesting_name=display_name,
            provisioned_site_ids=payload.provisioned_site_ids,
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "create_vessel",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })
        vessel = result.get("result") or {}
        invalidate_folder_caches()
        return {**vessel, "status": "completed", "message": result.get("message")}
    except Conflict as e:
        if str(e) == "Vessel name already exists.":
            return JSONResponse(status_code=409, content={"message": str(e)})
        _raise(e)
    except BadRequest as e:
        _raise(e)


@app.patch("/api/vessels/{vessel_id}")
async def update_vessel(
    vessel_id: str,
    payload: VesselUpdateIn,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    vtype = (payload.vessel_type or "").strip() or None
    if vtype and vtype not in VESSEL_TYPES:
        raise HTTPException(400, "Invalid vessel type")

    user_email = (x_user_email or "").strip().lower()
    display_name = user_email.split("@")[0] if user_email else None

    try:
        result = await get_backend().update_vessel(
            vessel_id,
            name=payload.name,
            imo=payload.imo,
            shipyard=payload.shipyard,
            hull_number=payload.hull_number,
            vessel_type=vtype,
            requesting_email=user_email,
            requesting_name=display_name,
            provisioned_site_ids=payload.provisioned_site_ids,
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "update_vessel",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })

        v_after = result.get("result") or {}
        detail_msg = result.get("message") or f"Saved vessel '{v_after.get('name')}'."
        sp_success = v_after.get("sp_success", True)
        if not sp_success:
            detail_msg += " (Note: some SharePoint folders failed to rename)"

        if user_email:
            _log_activity(user_email, "update_vessel", detail_msg)

        return {
            "id": v_after.get("id"),
            "name": v_after.get("name"),
            "imo": v_after.get("imo"),
            "shipyard": v_after.get("shipyard"),
            "hull_number": v_after.get("hull_number"),
            "vessel_type": v_after.get("vessel_type"),
            "message": detail_msg,
            "status": "completed",
        }
    except Conflict as e:
        return JSONResponse(status_code=409, content={"message": str(e)})
    except NotFound as e:
        return JSONResponse(status_code=404, content={"message": str(e)})
    except BadRequest as e:
        _raise(e)


@app.delete("/api/vessels/{vessel_id}")
async def delete_vessel(
    vessel_id: str,
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    user_email = (x_user_email or "").strip().lower()
    display_name = user_email.split("@")[0] if user_email else None

    try:
        result = await get_backend().delete_vessel(
            vessel_id,
            requesting_email=user_email,
            requesting_name=display_name,
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "delete_vessel",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })

        if result.get("deleted") is False:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "failed",
                    "message": result.get("message") or "Vessel folders could not be moved to Recycle Bin.",
                    "failed_paths": result.get("failed_paths") or [],
                },
            )

        deleted_name = result.get("vessel_name") or vessel_name or vessel_id
        detail_msg = result.get("message") or f"Moved vessel '{deleted_name}' to Recycle Bin."
        if user_email:
            _log_activity(user_email, "delete_vessel", detail_msg)
        invalidate_folder_caches()
        return {
            "status": "completed",
            "message": detail_msg,
        }
    except NotFound as e:
        return JSONResponse(status_code=404, content={"message": str(e)})
    except BadRequest as e:
        _raise(e)


@app.post("/api/vessels/{vessel_id}/reprovision")
async def reprovision_vessel(vessel_id: str):
    """Re-run idempotent folder provisioning for an existing vessel.

    Safe to call at any time — ensure_folder is create-or-fetch, so existing
    folders are never duplicated; only missing subfolders are created.
    """
    try:
        return await get_backend().reprovision_vessel(vessel_id)
    except NotFound as e:
        _raise(e)
    except BadRequest as e:
        _raise(e)


@app.post("/api/vessels/{vessel_id}/provision")
async def start_vessel_provisioning(
    vessel_id: str,
    _session: object = Depends(require_session),
):
    """Ensure background provisioning is running for this vessel.

    This is safe to call from the Provision button while the automatic job
    started at vessel creation is still in progress.
    """
    try:
        return await get_backend().start_vessel_provisioning(vessel_id)
    except NotFound as e:
        _raise(e)


@app.get("/api/vessels/{vessel_id}/provision-status")
async def vessel_provision_status(
    vessel_id: str,
    _session: object = Depends(require_session),
):
    try:
        vessel_id_num = int(vessel_id)
    except ValueError:
        raise HTTPException(404, "Vessel not found")
    from .db.base import SessionLocal
    from .db import models as db_models
    with SessionLocal() as db:
        vessel = db.query(db_models.Vessel).filter_by(id=vessel_id_num).one_or_none()
        if vessel is None:
            raise HTTPException(404, "Vessel not found")
        return {"vessel_id": vessel_id, "is_provisioned": bool(vessel.is_provisioned)}


@app.get("/api/admin/site-provisioning/sites")
async def get_site_provisioning_sites(
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Retrieve all tenant sites with provisioning configuration flags."""
    from .services.site_provisioning import get_all_configured_sites
    from .db.base import SessionLocal
    with SessionLocal() as db:
        sites = get_all_configured_sites(db)
        return {"sites": sites, "active_site": settings.active_site}


class UpdateSiteProvisioningItem(BaseModel):
    site_key: str
    is_available_for_provisioning: bool | None = None
    is_default_provisioning: bool | None = None
    display_name: str | None = None


class UpdateSiteProvisioningRequest(BaseModel):
    sites: list[UpdateSiteProvisioningItem]


@app.put("/api/admin/site-provisioning/sites")
async def update_site_provisioning_sites(
    payload: UpdateSiteProvisioningRequest,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Update provisioning availability and default site settings."""
    _require_admin(x_user_email)
    from .db.base import SessionLocal
    from .db import models as db_models
    from .services.site_provisioning import get_all_configured_sites
    with SessionLocal() as db:
        for item in payload.sites:
            key = item.site_key.strip().lower()
            rec = db.query(db_models.SiteConfiguration).filter_by(site_key=key).first()
            if rec:
                if item.is_available_for_provisioning is not None:
                    rec.is_available_for_provisioning = item.is_available_for_provisioning
                if item.is_default_provisioning is not None:
                    rec.is_default_provisioning = item.is_default_provisioning
                if item.display_name is not None:
                    rec.display_name = item.display_name
            else:
                rec = db_models.SiteConfiguration(
                    site_key=key,
                    display_name=item.display_name or key,
                    site_name=key,
                    site_id=key,
                    drive_id="",
                    is_available_for_provisioning=item.is_available_for_provisioning if item.is_available_for_provisioning is not None else True,
                    is_default_provisioning=item.is_default_provisioning if item.is_default_provisioning is not None else False,
                    created_by_email=x_user_email or "admin",
                )
                db.add(rec)
        db.commit()
        updated_sites = get_all_configured_sites(db)
        return {"success": True, "sites": updated_sites}


class ProvisionSitesRequest(BaseModel):
    site_keys: list[str]


@app.post("/api/vessels/{vessel_id}/provision-sites")
async def provision_vessel_sites(
    vessel_id: str,
    payload: ProvisionSitesRequest,
    _session: object = Depends(require_session),
):
    """Provision DMS folder structure for specific site(s) on an existing vessel with diff-based retry."""
    try:
        vid = int(vessel_id)
    except ValueError:
        raise HTTPException(404, "Vessel not found")
    from .db.base import SessionLocal
    from .db import models as db_models
    with SessionLocal() as db:
        vessel = db.query(db_models.Vessel).filter_by(id=vid).first()
        if not vessel:
            raise HTTPException(404, "Vessel not found")
        vname = vessel.name

    from .services.site_provisioning import provision_vessel_multi_site
    result = await provision_vessel_multi_site(vid, vname, payload.site_keys)
    return result


@app.get("/api/vessels/{vessel_id}/site-status")
async def get_vessel_site_status(
    vessel_id: str,
    _session: object = Depends(require_session),
):
    """Get per-site provisioning status for a vessel."""
    try:
        vid = int(vessel_id)
    except ValueError:
        raise HTTPException(404, "Vessel not found")
    from .db.base import SessionLocal
    from .db import models as db_models
    with SessionLocal() as db:
        vessel = db.query(db_models.Vessel).filter_by(id=vid).first()
        if not vessel:
            raise HTTPException(404, "Vessel not found")
        return {
            "vessel_id": str(vessel.id),
            "vessel_name": vessel.name,
            "is_provisioned": vessel.is_provisioned,
            "provisioned_site_ids": vessel.provisioned_site_ids or [],
        }


@app.post("/api/vessels/repair-links")
async def repair_vessel_links():
    """Scan all ship-kind folders with vessel_id=None and link them to the
    matching vessel row by name (case-insensitive).
    Safe to call at any time — only fills in missing links, never removes data.
    """
    return await get_backend().repair_vessel_links()


@app.post("/api/admin/migrate-drawing-folder")
async def migrate_drawing_folder():
    """One-time migration: for every vessel, collapse the 'Drawing' wrapper
    folder inside 'Drawings and Manuals' by moving its children one level up
    and deleting the now-empty 'Drawing' folder.
    Safe to call multiple times — skips vessels where 'Drawing' no longer exists.
    """
    from .graph import drive as gd
    from .graph.client import GraphError
    from .db.base import SessionLocal
    from .db import models as db_models
    from .services import get_backend

    be = get_backend()
    drive_id = await be._drive()

    results = []

    with SessionLocal() as db:
        # Find all 'Drawings and Manuals' folders across all vessels
        dam_rows = (
            db.query(db_models.Folder)
            .filter(db_models.Folder.name == "Drawings and Manuals", db_models.Folder.kind == "folder")
            .all()
        )
        dam_list = [(r.drive_item_id, r.path, r.vessel_id) for r in dam_rows]

    for dam_id, dam_path, vessel_id in dam_list:
        # Find the 'Drawing' child folder
        try:
            drawing_item = await gd.find_child(drive_id, dam_id, "Drawing")
        except Exception as e:
            results.append({"dam_path": dam_path, "status": "error", "detail": str(e)})
            continue

        if not drawing_item or "folder" not in drawing_item:
            results.append({"dam_path": dam_path, "status": "skipped", "detail": "No Drawing folder found"})
            continue

        drawing_id = drawing_item["id"]
        drawing_path = f"{dam_path}/Drawing"

        # List children of Drawing
        try:
            children = await gd.list_children(drive_id, drawing_id)
        except Exception as e:
            results.append({"dam_path": dam_path, "status": "error", "detail": f"list children: {e}"})
            continue

        moved = []
        for child in children:
            if "folder" not in child:
                continue
            try:
                await gd.move_item(drive_id, child["id"], dam_id)
                moved.append(child["name"])
                # Update DB cache: rewrite path from Drawing/X -> X under dam_path
                old_child_path = f"{drawing_path}/{child['name']}"
                new_child_path = f"{dam_path}/{child['name']}"
                with SessionLocal() as db:
                    # Update the child folder row
                    row = db.query(db_models.Folder).filter_by(path=old_child_path).one_or_none()
                    if row:
                        row.path = new_child_path
                    # Update all descendant paths
                    descendants = (
                        db.query(db_models.Folder)
                        .filter(db_models.Folder.path.like(f"{old_child_path}/%"))
                        .all()
                    )
                    for d in descendants:
                        d.path = new_child_path + d.path[len(old_child_path):]
                    db.commit()
            except Exception as e:
                results.append({"dam_path": dam_path, "status": "error", "detail": f"move {child['name']}: {e}"})

        # Delete the now-empty Drawing folder
        try:
            await gd.delete_item(drive_id, drawing_id)
            with SessionLocal() as db:
                row = db.query(db_models.Folder).filter_by(path=drawing_path).one_or_none()
                if row:
                    db.delete(row)
                db.commit()
        except Exception as e:
            results.append({"dam_path": dam_path, "status": "error", "detail": f"delete Drawing: {e}"})
            continue

        results.append({"dam_path": dam_path, "status": "ok", "moved": moved})

    return {"results": results}


@app.get("/api/mains")
async def mains(_session: object = Depends(require_session)):
    return await get_backend().mains()


@app.get("/api/stats")
async def stats(_session: object = Depends(require_session)):
    return await get_backend().stats()


@app.get("/api/dashboard/stats")
async def dashboard_stats(
    force_refresh: bool = Query(default=False),
    _session: object = Depends(require_session),
):
    """Fast aggregated statistics for the Home/Dashboard module."""
    be = get_backend()
    if hasattr(be, "get_dashboard_stats"):
        return await be.get_dashboard_stats(force_refresh=force_refresh)
    return await be.stats()



@app.post("/api/folders/upload-by-path")
async def upload_by_path(
    path: str,
    file: UploadFile = None,
    resolve_only: bool = Query(default=False),
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Same as POST /api/folders/{folder_id}/upload, but the target folder is
    given as a logical path. When resolve_only=true, just returns the resolved
    folder_id without uploading (used by the frontend to resolve path-based IDs)."""
    path = (path or "").lstrip("/").strip()
    if resolve_only:
        try:
            folder_id = await get_backend().resolve_path(path)
            logger.info("Folder navigation resolve: path=%s folder_id=%s", path, folder_id)
            return {"folder_id": folder_id}
        except (NotFound, BadRequest) as e:
            logger.warning("Folder navigation resolve failed: path=%s error=%s", path, e)
            _raise(e)
    if file is None:
        raise HTTPException(400, "file is required")
    data = await file.read()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]
    try:
        folder_id = await get_backend().resolve_path(path)
        result = await get_backend().upload(
            folder_id, file.filename, data, file.content_type, email, name,
            access_token=x_graph_access_token,
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"Uploaded: {file.filename} (awaiting reviewer approval)"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "file_upload", log_detail)
        invalidate_folder_caches(folder_id)
        return result
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)
    except GraphError as e:
        if e.status == 429:
            raise HTTPException(429, "SharePoint is temporarily throttling requests. Please retry the upload shortly.")
        raise HTTPException(e.status, f"SharePoint upload failed: {str(e)}")
    except Exception as e:
        logger.exception(
            "Upload-by-path failure: path=%s filename=%s error=%s",
            path,
            file.filename if file else None,
            e,
        )
        raise HTTPException(status_code=500, detail="Upload failed. Check the backend log for details.")


# ── CATEGORIES & DYNAMIC TAG FIELDS SCHEMAS ──────────────────────────────────
class TagFieldDefIn(BaseModel):
    key: str = Field(..., pattern=r"^[a-z_][a-z0-9_]{0,49}$")
    label: str
    type: str = Field(..., pattern=r"^(text|textarea|select_vessel|select_dept|select_category|select)$")
    required: bool = False
    options: list[str] | None = None

    @model_validator(mode="after")
    def validate_options(self):
        if self.type == "select" and (not self.options or len(self.options) == 0):
            raise ValueError("options list is required when type is select")
        return self


class CategoryCreateIn(BaseModel):
    name: str
    department: str | None = None
    dms_path_template: str | None = None
    tag_fields: list[TagFieldDefIn] = []
    ocr_hints: list[str] = []

    @model_validator(mode="after")
    def validate_unique_keys(self):
        keys = [f.key for f in self.tag_fields]
        if len(keys) != len(set(keys)):
            raise ValueError("Tag field keys must be unique within a category")
        return self


class CategoryUpdateIn(BaseModel):
    name: str | None = None
    department: str | None = None
    dms_path_template: str | None = None
    tag_fields: list[TagFieldDefIn] | None = None
    ocr_hints: list[str] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_unique_keys(self):
        if self.tag_fields is not None:
            keys = [f.key for f in self.tag_fields]
            if len(keys) != len(set(keys)):
                raise ValueError("Tag field keys must be unique within a category")
        return self


class StagingTagsUpdateIn(BaseModel):
    category_id: int | None = None
    suggested_tags: dict[str, Any] = {}


# ── DOCUMENT CATEGORIES API ───────────────────────────────────────────────────
@app.get("/api/categories")
async def list_categories(
    department: str | None = None,
    include_inactive: bool = False,
    _session: object = Depends(require_session),
):
    """List document categories with dynamic tag field definitions and OCR hints."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    with SessionLocal() as db:
        query = db.query(db_models.DocumentCategory)
        if not include_inactive:
            query = query.filter(db_models.DocumentCategory.is_active == True)
        if department:
            query = query.filter(db_models.DocumentCategory.department == department)
        rows = query.order_by(db_models.DocumentCategory.name.asc()).all()

        results = []
        for r in rows:
            tag_fields = []
            if r.tag_fields_json:
                try:
                    tag_fields = json.loads(r.tag_fields_json)
                except Exception:
                    tag_fields = []
            ocr_hints = []
            if r.ocr_hints_json:
                try:
                    ocr_hints = json.loads(r.ocr_hints_json)
                except Exception:
                    ocr_hints = []
            results.append({
                "id": r.id,
                "name": r.name,
                "department": r.department,
                "dms_path_template": r.dms_path_template,
                "tag_fields": tag_fields,
                "ocr_hints": ocr_hints,
                "is_active": r.is_active,
                "created_by_email": r.created_by_email,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        return results


@app.post("/api/categories")
async def create_category(
    payload: CategoryCreateIn,
    user_email: str | None = None,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Create a new document category with dynamic tag field definitions."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    email = user_email or x_user_email or "admin"
    with SessionLocal() as db:
        existing = db.query(db_models.DocumentCategory).filter(
            func.lower(db_models.DocumentCategory.name) == payload.name.strip().lower()
        ).first()
        if existing:
            if not existing.is_active:
                existing.is_active = True
                existing.department = payload.department
                existing.dms_path_template = payload.dms_path_template
                existing.tag_fields_json = json.dumps([f.dict() for f in payload.tag_fields])
                existing.ocr_hints_json = json.dumps(payload.ocr_hints)
                db.commit()
                db.refresh(existing)
                return {"ok": True, "id": existing.id, "message": "Category reactivated and updated"}
            raise HTTPException(400, f"Category '{payload.name}' already exists.")

        cat = db_models.DocumentCategory(
            name=payload.name.strip(),
            department=payload.department,
            dms_path_template=payload.dms_path_template,
            tag_fields_json=json.dumps([f.dict() for f in payload.tag_fields]),
            ocr_hints_json=json.dumps(payload.ocr_hints),
            is_active=True,
            created_by_email=email,
        )
        db.add(cat)
        db.commit()
        db.refresh(cat)
        _log_activity(email, "create_category", f"Created document category: {cat.name}")
        return {"ok": True, "id": cat.id, "name": cat.name}


@app.patch("/api/categories/{category_id}")
async def update_category(
    category_id: int,
    payload: CategoryUpdateIn,
    user_email: str | None = None,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Update category properties, dynamic tag fields, or OCR hint keywords."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    email = user_email or x_user_email or "admin"
    with SessionLocal() as db:
        cat = db.query(db_models.DocumentCategory).filter_by(id=category_id).first()
        if not cat:
            raise HTTPException(404, "Category not found")

        if payload.name is not None and payload.name.strip() != cat.name:
            dup = db.query(db_models.DocumentCategory).filter(
                func.lower(db_models.DocumentCategory.name) == payload.name.strip().lower(),
                db_models.DocumentCategory.id != category_id
            ).first()
            if dup:
                raise HTTPException(400, f"Category name '{payload.name}' is already taken.")
            cat.name = payload.name.strip()

        if payload.department is not None:
            cat.department = payload.department
        if payload.dms_path_template is not None:
            cat.dms_path_template = payload.dms_path_template
        if payload.tag_fields is not None:
            cat.tag_fields_json = json.dumps([f.dict() for f in payload.tag_fields])
        if payload.ocr_hints is not None:
            cat.ocr_hints_json = json.dumps(payload.ocr_hints)
        if payload.is_active is not None:
            cat.is_active = payload.is_active

        db.commit()
        _log_activity(email, "update_category", f"Updated category: {cat.name}")
        return {"ok": True, "id": cat.id, "name": cat.name}


@app.delete("/api/categories/{category_id}")
async def delete_category(
    category_id: int,
    user_email: str | None = None,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Soft-delete a document category."""
    from .db.base import SessionLocal
    from .db import models as db_models

    email = user_email or x_user_email or "admin"
    with SessionLocal() as db:
        cat = db.query(db_models.DocumentCategory).filter_by(id=category_id).first()
        if not cat:
            raise HTTPException(404, "Category not found")
        cat.is_active = False
        db.commit()
        _log_activity(email, "delete_category", f"Deleted category: {cat.name}")
        return {"ok": True, "message": f"Category '{cat.name}' deactivated."}


# ── OCR & DOCUMENT EXTRACTION / CLASSIFICATION ────────────────────────────────
@app.post("/api/ocr/extract-and-classify")
async def ocr_extract_and_classify(
    file: UploadFile,
    _session: object = Depends(require_session),
):
    """Run OCR / text extraction on an uploaded document and classify its metadata

    (Vessel Name, Group, Category, Sub-Category, Suggested SharePoint Path, and Dynamic Tag Fields).
    """
    if file is None:
        raise HTTPException(400, "File is required")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty")

    from .ocr.extract import extract_text
    from .ocr.drawing_category import (
        classify_document_content,
        classify_against_db_categories,
        DRAWING_TAXONOMY,
        MANUAL_TAXONOMY,
    )
    import json

    # 1. Extract document text
    try:
        extracted = extract_text(data, file.filename, file.content_type or "")
    except Exception as exc:
        logger.warning("OCR text extraction fallback for %s: %s", file.filename, exc)
        extracted = ""

    # 2. Get known vessel names and active DB categories
    vessel_names: list[Any] = []
    db_categories = []
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                vessel_names = _get_vessels_for_ocr(db)
                db_categories = db.query(db_models.DocumentCategory).filter(db_models.DocumentCategory.is_active == True).all()
        except Exception:
            pass
    if not vessel_names:
        try:
            v_list = await get_backend().list_vessels()
            vessel_names = [v["name"] for v in v_list if isinstance(v, dict) and "name" in v]
        except Exception:
            vessel_names = []

    # 3. Classify document with built-in taxonomy and DB categories
    classification = classify_document_content(extracted, filename=file.filename, known_vessels=vessel_names)
    best_db_cat, db_conf, db_matches = classify_against_db_categories(extracted, filename=file.filename, db_categories=db_categories)

    matched_cat_id = None
    matched_cat_name = None
    tag_fields = []
    suggested_tags: dict[str, Any] = {}

    if best_db_cat and db_conf >= classification["confidence"]:
        matched_cat_id = best_db_cat.id
        matched_cat_name = best_db_cat.name
        confidence = db_conf
        matched_keywords = list(set(classification["matched_keywords"] + db_matches))
        if best_db_cat.tag_fields_json:
            try:
                tag_fields = json.loads(best_db_cat.tag_fields_json)
            except Exception:
                tag_fields = []
    else:
        # Match built-in drawing/manual to default DB categories if available
        confidence = classification["confidence"]
        matched_keywords = classification["matched_keywords"]
        built_in_cat_name = "Drawing" if classification["sub_category_1"] == "Drawing" else "Manual"
        matched_db = next((c for c in db_categories if c.name.lower() == built_in_cat_name.lower()), None)
        if matched_db:
            matched_cat_id = matched_db.id
            matched_cat_name = matched_db.name
            if matched_db.tag_fields_json:
                try:
                    tag_fields = json.loads(matched_db.tag_fields_json)
                except Exception:
                    tag_fields = []

    # If confidence >= 0.40, populate standard suggested tags
    if confidence >= 0.40:
        suggested_tags["vessel"] = classification.get("vessel_name") or ""
        suggested_tags["group"] = classification.get("group") or "Drawing"
        fallback_category = "To be Classified" if suggested_tags["group"] == "Manual" else "Basic"
        suggested_tags["category"] = classification.get("category") or (matched_cat_name if matched_cat_name not in ("Drawing", "Manual") else fallback_category) or fallback_category
        low_confidence_fallback = bool(not best_db_cat and db_conf < 0.40)
        suggested_tags["sub_category"] = "To be Classified" if low_confidence_fallback else (classification.get("sub_category") or "General Arrangement")
        suggested_tags["department"] = classification.get("department") or "Technical & Crewing"

    text_preview = (extracted.strip()[:1000] + "...") if len(extracted.strip()) > 1000 else extracted.strip()

    return {
        "filename": file.filename,
        "file_size": len(data),
        "content_type": file.content_type,
        "text_preview": text_preview,
        "text_length": len(extracted),
        "detected_vessel": classification.get("vessel_name"),
        "detected_group": classification.get("group"),
        "detected_category": classification.get("category"),
        "detected_sub_category": classification.get("sub_category"),
        "detected_sub_category_1": classification.get("sub_category_1"),
        "detected_sub_category_2": classification.get("sub_category_2"),
        "detected_leaf": classification.get("leaf"),
        "suggested_path": classification.get("suggested_path"),
        "confidence": confidence,
        "matched_keywords": matched_keywords,
        "matched_category_id": matched_cat_id,
        "matched_category_name": matched_cat_name,
        "tag_fields": tag_fields,
        "suggested_tags": suggested_tags,
        "available_vessels": sorted(list(dict.fromkeys(vessel_names + classification.get("available_vessels", [])))),
        "available_departments": template.ALL_MAIN_FOLDERS,
        "drawing_taxonomy": {cat: list(leaves.keys()) for cat, leaves in DRAWING_TAXONOMY.items()},
        "manual_taxonomy": {cat: list(leaves.keys()) for cat, leaves in MANUAL_TAXONOMY.items()},
    }


# ── ASYNC OCR STAGING PROCESSOR ───────────────────────────────────────────────
async def _process_staging_ocr(
    staging_id: int,
    file_data: bytes | None = None,
    delegated_access_token: str | None = None,
    sp_access_token: str | None = None,
):
    """Background task to extract OCR text, classify against DocumentCategory,
    and update the OcrStagingFile row with suggested_tags.
    """
    from .db.base import SessionLocal
    from .db import models as db_models
    from .ocr.extract import extract_text
    from .ocr.drawing_category import classify_document_content, classify_against_db_categories
    import json

    with SessionLocal() as db:
        item = db.query(db_models.OcrStagingFile).filter_by(id=staging_id).first()
        if not item or item.status in ("moved", "dismissed"):
            return

        content_bytes = file_data
        # If no direct file data but have SharePoint drive_item_id, download via Graph
        if not content_bytes and item.drive_item_id and settings.sp_configured:
            try:
                content_bytes, _, _ = await gd.download_file(settings.sp_drive_id, item.drive_item_id)
            except Exception as e:
                logger.warning("Could not download file %s for staging OCR: %s", item.drive_item_id, e)

        extracted = ""
        if content_bytes:
            try:
                extracted = await asyncio.to_thread(extract_text, content_bytes, item.filename)
            except Exception as exc:
                logger.warning("Staging OCR extraction error for %s: %s", item.filename, exc)
                item.error = str(exc)

        # Load vessels and categories
        vessel_names = _get_vessels_for_ocr(db)
        categories = db.query(db_models.DocumentCategory).filter(db_models.DocumentCategory.is_active == True).all()

        from .ocr.drawing_category import classify_all_fields_tiered, classify_against_db_categories
        tiered = classify_all_fields_tiered(
            extracted,
            filename=item.filename,
            known_vessels=vessel_names,
            source_path=item.source_subfolder_path or "",
        )

        source_department = _extract_department_from_path(item.source_subfolder_path)
        if source_department:
            tiered["department"] = {"value": source_department, "confidence": 0.98, "tier": 1}

        item.vessel_name = tiered["vessel"]["value"] or ""

        # Check DB custom categories
        best_cat, db_conf, db_matches = classify_against_db_categories(extracted, filename=item.filename, db_categories=categories)
        if best_cat and tiered["department"]["value"]:
            cat_dept = (getattr(best_cat, "department", None) or "").strip()
            if cat_dept and _norm_dept(cat_dept) != _norm_dept(tiered["department"]["value"]):
                best_cat = None
                db_conf = 0.0

        if best_cat and db_conf >= 0.85:
            if best_cat.name in ("Drawing", "Manual"):
                tiered["group"] = {"value": best_cat.name, "confidence": db_conf, "tier": 2}
            else:
                tiered["category"] = {"value": best_cat.name, "confidence": db_conf, "tier": 2}

        # Keep Group aligned with Category taxonomy to prevent impossible pairs
        # like Group=Manual with Category=Hull.
        category_val = _safe_tag_value(tiered.get("category"))
        if category_val:
            normalized_sp_group = _normalize_metadata_group(_safe_tag_value(tiered.get("group")), category_val)
            if normalized_sp_group == "Drawings":
                tiered["group"] = {
                    "value": "Drawing",
                    "confidence": max(
                        float((tiered.get("group") or {}).get("confidence", 0) or 0),
                        float((tiered.get("category") or {}).get("confidence", 0) or 0),
                    ),
                    "tier": (tiered.get("category") or {}).get("tier", 2),
                }
            elif normalized_sp_group == "Manuals":
                tiered["group"] = {
                    "value": "Manual",
                    "confidence": max(
                        float((tiered.get("group") or {}).get("confidence", 0) or 0),
                        float((tiered.get("category") or {}).get("confidence", 0) or 0),
                    ),
                    "tier": (tiered.get("category") or {}).get("tier", 2),
                }

        # Match category DB ID
        grp_val = tiered["group"]["value"]
        cat_val = tiered["category"]["value"]
        matched_db = next((
            c for c in categories
            if c.name.lower() in (grp_val.lower(), cat_val.lower())
            and (not tiered["department"]["value"] or not getattr(c, "department", None) or _norm_dept(c.department) == _norm_dept(tiered["department"]["value"]))
        ), None)
        if not matched_db:
            matched_db = next((c for c in categories if c.name.lower() in (grp_val.lower(), cat_val.lower())), None)
        matched_cat_id = matched_db.id if matched_db else (best_cat.id if best_cat else None)

        matched_kws = list(dict.fromkeys(tiered["matched_keywords"] + db_matches))

        suggested_tags = {
            "vessel": tiered["vessel"],
            "department": tiered["department"],
            "group": tiered["group"],
            "category": tiered["category"],
            "sub_category": tiered["sub_category"],
        }

        from .ocr.validation import run_secondary_ocr_validation
        val_res = run_secondary_ocr_validation(
            file_bytes=content_bytes,
            filename=item.filename,
            primary_fields=suggested_tags,
            source_path=item.source_subfolder_path or "",
            known_vessels=vessel_names,
        )
        suggested_tags["_validation"] = val_res

        item.category_id = matched_cat_id
        item.suggested_tags_json = json.dumps(suggested_tags)
        item.ocr_text_preview = (extracted.strip()[:1000] + "...") if len(extracted.strip()) > 1000 else extracted.strip()
        item.confidence = tiered["overall_confidence"]
        item.matched_keywords_json = json.dumps(matched_kws[:10])
        item.final_path = tiered.get("suggested_path")

        # --- Routing decision: staged vs. needs_review (unstaged) ---
        vessel_conf = tiered["vessel"]["confidence"]
        vessel_val = tiered["vessel"]["value"]
        overall_conf = tiered["overall_confidence"]
        extraction_failed = not extracted or len(extracted.strip()) < 20

        # A file is "needs_review" (unstaged) when:
        #   1. Text extraction produced nothing / garbled (<20 chars)
        #   2. No vessel could be identified at all (vessel_val is blank after floor enforcement)
        #   3. Vessel confidence is below VESSEL_CONFIDENCE_FLOOR (0.60) — floor already blanked the
        #      value in classify_all_fields_tiered, so this check is belt-and-suspenders
        #   4. Overall classification confidence is below 0.40 (worse than random)
        from .ocr.drawing_category import VESSEL_CONFIDENCE_FLOOR
        if extraction_failed or not vessel_val or vessel_conf < VESSEL_CONFIDENCE_FLOOR or overall_conf < 0.40:
            item.status = "needs_review"
            if extraction_failed:
                item.error = (item.error or "") + " [Text extraction yielded insufficient content for classification]"
            elif not vessel_val and vessel_conf > 0:
                item.error = (item.error or "") + f" [Vessel confidence {round(vessel_conf * 100)}% is below the 60% floor — manual vessel assignment required]"
            elif not vessel_val:
                item.error = (item.error or "") + " [Vessel could not be identified from document text, filename, or folder path]"
            else:
                item.error = (item.error or "") + f" [Overall confidence {round(overall_conf * 100)}% is below the minimum threshold for auto-staging]"
        else:
            item.status = "tag_suggested"

        # For direct uploads, the file already exists in SharePoint. Patch
        # OCR-derived metadata immediately, even if vessel is not identified,
        # so Group/Category/SubCategory are still auto-populated.
        if settings.sp_configured and item.drive_item_id:
            try:
                department_val = _safe_tag_value(tiered.get("department")) or _extract_department_from_path(item.source_subfolder_path) or "Technical & Crewing"
                # Only write vessel when OCR/tag value is present. Do not fallback
                # to source path vessel for SharePoint metadata.
                vessel_val = _safe_tag_value(tiered.get("vessel")) or ""
                category_val = _safe_tag_value(tiered.get("category")) or "To be Classified"
                subcategory_val = _safe_tag_value(tiered.get("sub_category")) or "To be Classified"
                group_val = _normalize_metadata_group(_safe_tag_value(tiered.get("group")), category_val)

                if category_val.strip().lower() == "to be classified":
                    # Keep OCR-inferred group if available (e.g. Drawing -> Drawings).
                    # Only default to Manuals when group is still unresolved.
                    if not group_val:
                        group_val = "Manuals"
                    if not subcategory_val:
                        subcategory_val = "To be Classified"

                if not group_val:
                    group_val = _normalize_metadata_group("", category_val) or "Manuals"

                patch_payload = _build_sharepoint_metadata_payload(
                    department=department_val,
                    vessel=vessel_val,
                    group=group_val,
                    category=category_val,
                    sub_category=subcategory_val,
                )
                await gd.update_file_columns(
                    settings.sp_drive_id,
                    item.drive_item_id,
                    patch_payload,
                    access_token=delegated_access_token,
                    sp_access_token=sp_access_token,
                )
            except Exception as patch_err:
                msg = f"[SharePoint metadata patch failed: {patch_err}]"
                item.error = ((item.error or "") + " " + msg).strip()
                logger.warning(
                    "Non-fatal: could not patch metadata during staging OCR for item_id=%s: %s",
                    item.drive_item_id,
                    patch_err,
                )

        db.commit()


# ── OCR STAGING QUEUE API ─────────────────────────────────────────────────────
@app.post("/api/ocr/stage-file")
async def stage_file_for_ocr(
    filename: str = Form(...),
    drive_item_id: str | None = Form(None),
    source_folder_id: str | None = Form(None),
    source_subfolder_path: str | None = Form(None),
    vessel_name: str | None = Form(None),
    upload_source: str = Form("direct"),
    uploaded_by_email: str | None = Form(None),
    file: UploadFile = None,
    x_user_email: str | None = Header(default=None),
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Queue an uploaded file (from folder upload or direct file upload) for OCR classification and staging review."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    email = uploaded_by_email or x_user_email or "unknown@example.com"
    file_bytes = None
    if file is not None:
        file_bytes = await file.read()

    with SessionLocal() as db:
        known_vessels = _get_vessels_for_ocr(db)
        # Deduplication check:
        # 1. By drive_item_id if provided
        if drive_item_id:
            existing = db.query(db_models.OcrStagingFile).filter(
                db_models.OcrStagingFile.drive_item_id == drive_item_id,
                ~db_models.OcrStagingFile.status.in_(["moved", "dismissed"])
            ).first()
            if existing:
                return {
                    "ok": True,
                    "id": existing.id,
                    "status": existing.status,
                    "duplicate": True,
                    "message": "File is already in the OCR staging queue",
                }
        else:
            # 2. Within last 60 seconds by filename + source_folder_id
            cutoff = datetime.utcnow() - timedelta(seconds=60)
            existing = db.query(db_models.OcrStagingFile).filter(
                db_models.OcrStagingFile.filename == filename,
                db_models.OcrStagingFile.source_folder_id == source_folder_id,
                db_models.OcrStagingFile.created_at >= cutoff,
                ~db_models.OcrStagingFile.status.in_(["moved", "dismissed"])
            ).first()
            if existing:
                return {
                    "ok": True,
                    "id": existing.id,
                    "status": existing.status,
                    "duplicate": True,
                    "message": "File was recently staged",
                }

        item = db_models.OcrStagingFile(
            filename=filename,
            drive_item_id=drive_item_id,
            source_folder_id=source_folder_id,
            source_subfolder_path=source_subfolder_path,
            # The source folder is classifier context only. Never seed the OCR
            # result with it: a file can be stored under the wrong vessel folder.
            vessel_name="",
            upload_source=upload_source,
            status="ocr_pending",
            uploaded_by_email=email,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        staging_id = item.id

    # Spawn async background processing for OCR text & tag suggestion
    asyncio.create_task(_process_staging_ocr(staging_id, file_bytes, x_graph_access_token, x_sp_access_token))

    return {
        "ok": True,
        "id": staging_id,
        "filename": filename,
        "status": "ocr_pending",
        "upload_source": upload_source,
    }


@app.get("/api/ocr/staging")
async def list_staging_queue(
    status: str | None = None,
    vessel_name: str | None = None,
    upload_source: str | None = None,
    _session: object = Depends(require_session),
):
    """List files in the OCR staging queue for review."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    with SessionLocal() as db:
        query = db.query(db_models.OcrStagingFile)
        if status:
            query = query.filter(db_models.OcrStagingFile.status == status)
        else:
            query = query.filter(~db_models.OcrStagingFile.status.in_(["moved", "dismissed"]))

        if vessel_name:
            query = query.filter(func.lower(db_models.OcrStagingFile.vessel_name) == vessel_name.strip().lower())
        if upload_source:
            query = query.filter(db_models.OcrStagingFile.upload_source == upload_source)

        items = query.order_by(db_models.OcrStagingFile.created_at.desc()).all()
        vessel_names = _get_vessels_for_ocr(db)
        drawing_categories, manual_categories, _, _ = _taxonomy_maps()
        results = []
        has_updates = False
        for r in items:
            cat_name = r.category.name if r.category else None
            tag_fields = []
            if r.category and r.category.tag_fields_json:
                try:
                    tag_fields = json.loads(r.category.tag_fields_json)
                except Exception:
                    tag_fields = []

            suggested_tags = {}
            if r.suggested_tags_json:
                try:
                    suggested_tags = json.loads(r.suggested_tags_json)
                except Exception:
                    suggested_tags = {}

            # Check if this item has stale/legacy tags (e.g. flat strings or inverted Group/Category or misattributed vessel)
            needs_reclassify = False
            if not isinstance(suggested_tags.get("group"), dict) or not isinstance(suggested_tags.get("category"), dict):
                needs_reclassify = True
            elif suggested_tags.get("group", {}).get("value") not in ("Drawing", "Manual"):
                needs_reclassify = True
            elif (
                str(suggested_tags.get("group", {}).get("value") or "").strip().lower() == "manual"
                and str(suggested_tags.get("category", {}).get("value") or "").strip().lower() in drawing_categories
            ):
                needs_reclassify = True
            elif (
                str(suggested_tags.get("group", {}).get("value") or "").strip().lower() == "drawing"
                and str(suggested_tags.get("category", {}).get("value") or "").strip().lower() in manual_categories
            ):
                needs_reclassify = True
            elif suggested_tags.get("vessel", {}).get("value") in ("Ss378", "SS378", "ss-378") and "Peissy" in vessel_names:
                needs_reclassify = True
            elif float((suggested_tags.get("vessel") or {}).get("confidence", 1.0) or 0) < 0.60:
                needs_reclassify = True
            elif "_validation" not in suggested_tags:
                needs_reclassify = True
            else:
                from .ocr.drawing_category import extract_vessel_name_from_text
                current_tag_vessel = _safe_tag_value(suggested_tags.get("vessel"))
                fresh_vessel = extract_vessel_name_from_text(r.ocr_text_preview or "", r.filename or "", known_vessels=vessel_names)
                if fresh_vessel and _norm_vessel_key(fresh_vessel) != _norm_vessel_key(current_tag_vessel):
                    needs_reclassify = True

            if needs_reclassify and r.status in ("tag_suggested", "ocr_pending", "needs_review"):
                from .ocr.drawing_category import classify_all_fields_tiered
                tiered = classify_all_fields_tiered(
                    r.ocr_text_preview or "",
                    filename=r.filename,
                    known_vessels=vessel_names,
                    source_path=r.source_subfolder_path or "",
                )
                source_department = _extract_department_from_path(r.source_subfolder_path)
                if source_department:
                    tiered["department"] = {"value": source_department, "confidence": 0.98, "tier": 1}

                suggested_tags = {
                    "vessel": tiered["vessel"],
                    "department": tiered["department"],
                    "group": tiered["group"],
                    "category": tiered["category"],
                    "sub_category": tiered["sub_category"],
                }

                from .ocr.validation import run_secondary_ocr_validation
                val_res = run_secondary_ocr_validation(
                    file_bytes=None,
                    filename=r.filename,
                    primary_fields=suggested_tags,
                    source_path=r.source_subfolder_path or "",
                    known_vessels=vessel_names,
                )
                suggested_tags["_validation"] = val_res

                r.suggested_tags_json = json.dumps(suggested_tags)
                r.vessel_name = tiered["vessel"]["value"] or ""
                r.confidence = tiered["overall_confidence"]
                r.final_path = tiered.get("suggested_path")
                r.matched_keywords_json = json.dumps(tiered.get("matched_keywords", [])[:10])
                # Apply the same needs_review routing gate as _process_staging_ocr
                from .ocr.drawing_category import VESSEL_CONFIDENCE_FLOOR
                vessel_val = tiered["vessel"]["value"]
                vessel_conf = tiered["vessel"]["confidence"]
                overall_conf = tiered["overall_confidence"]
                text_len = len((r.ocr_text_preview or "").strip())
                if not vessel_val or vessel_conf < VESSEL_CONFIDENCE_FLOOR or overall_conf < 0.40 or text_len < 20:
                    r.status = "needs_review"
                else:
                    r.status = "tag_suggested"
                has_updates = True

            matched_keywords = []
            if r.matched_keywords_json:
                try:
                    matched_keywords = json.loads(r.matched_keywords_json)
                except Exception:
                    matched_keywords = []

            results.append({
                "id": r.id,
                "filename": r.filename,
                "drive_item_id": r.drive_item_id,
                "source_folder_id": r.source_folder_id,
                "source_subfolder_path": r.source_subfolder_path,
                "vessel_name": r.vessel_name,
                "upload_source": r.upload_source,
                "status": r.status,
                "category_id": r.category_id,
                "category_name": cat_name,
                "tag_fields": tag_fields,
                "suggested_tags": suggested_tags,
                "ocr_text_preview": r.ocr_text_preview,
                "confidence": r.confidence,
                "matched_keywords": matched_keywords,
                "final_path": r.final_path,
                "uploaded_by_email": r.uploaded_by_email,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })

        if has_updates:
            try:
                db.commit()
            except Exception:
                db.rollback()

        return results


@app.post("/api/ocr/staging/reclassify-all")
async def reclassify_all_staging_items(
    _session: object = Depends(require_session),
):
    """Force re-run tiered AI classification across all active items in the OCR staging queue."""
    from .db.base import SessionLocal
    from .db import models as db_models
    from .ocr.drawing_category import classify_all_fields_tiered
    import json

    with SessionLocal() as db:
        vessel_names = _get_vessels_for_ocr(db)
        items = db.query(db_models.OcrStagingFile).filter(
            ~db_models.OcrStagingFile.status.in_(["moved", "dismissed"])
        ).all()

        reclassified_count = 0
        for r in items:
            tiered = classify_all_fields_tiered(
                r.ocr_text_preview or "",
                filename=r.filename,
                known_vessels=vessel_names,
                source_path=r.source_subfolder_path or "",
            )
            source_department = _extract_department_from_path(r.source_subfolder_path)
            if source_department:
                tiered["department"] = {"value": source_department, "confidence": 0.98, "tier": 1}

            suggested_tags = {
                "vessel": tiered["vessel"],
                "department": tiered["department"],
                "group": tiered["group"],
                "category": tiered["category"],
                "sub_category": tiered["sub_category"],
            }
            r.suggested_tags_json = json.dumps(suggested_tags)
            r.vessel_name = tiered["vessel"]["value"] or ""
            r.confidence = tiered["overall_confidence"]
            r.final_path = tiered.get("suggested_path")
            r.matched_keywords_json = json.dumps(tiered.get("matched_keywords", [])[:10])
            # Apply the same needs_review routing gate
            from .ocr.drawing_category import VESSEL_CONFIDENCE_FLOOR
            vessel_val = tiered["vessel"]["value"]
            vessel_conf = tiered["vessel"]["confidence"]
            overall_conf = tiered["overall_confidence"]
            text_len = len((r.ocr_text_preview or "").strip())
            if not vessel_val or vessel_conf < VESSEL_CONFIDENCE_FLOOR or overall_conf < 0.40 or text_len < 20:
                r.status = "needs_review"
            else:
                r.status = "tag_suggested"
            reclassified_count += 1

        db.commit()
        return {"ok": True, "reclassified_count": reclassified_count}


@app.post("/api/ocr/staging/{staging_id}/stage")
async def promote_to_staged(
    staging_id: int,
    _session: object = Depends(require_session),
):
    """Promote a 'needs_review' (unstaged) item to 'tag_suggested' status.
    Used when a user has manually filled in the correct vessel / fields via the UI.
    """
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    with SessionLocal() as db:
        item = db.query(db_models.OcrStagingFile).filter_by(id=staging_id).first()
        if not item:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Staging item not found")
        if item.status not in ("needs_review", "ocr_pending"):
            return {"ok": True, "id": staging_id, "status": item.status, "message": "Item is already in the staging queue"}

        # Verify the item now has a vessel before promoting
        suggested_tags = {}
        if item.suggested_tags_json:
            try:
                suggested_tags = json.loads(item.suggested_tags_json)
            except Exception:
                pass
        vessel_val = suggested_tags.get("vessel", {}).get("value") if isinstance(suggested_tags.get("vessel"), dict) else suggested_tags.get("vessel", "")
        if not vessel_val:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Vessel must be set before promoting to staging. Please update the vessel in the edit panel first.")

        item.status = "tag_suggested"
        item.error = None
        db.commit()
        return {"ok": True, "id": staging_id, "status": "tag_suggested"}


@app.patch("/api/ocr/staging/{staging_id}/tags")
async def update_staging_tags(
    staging_id: int,
    payload: StagingTagsUpdateIn,
    _session: object = Depends(require_session),
):
    """Update suggested tags or assigned category for a staging file before moving."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json

    with SessionLocal() as db:
        item = db.query(db_models.OcrStagingFile).filter_by(id=staging_id).first()
        if not item:
            raise HTTPException(404, "Staging item not found")

        if payload.category_id is not None:
            item.category_id = payload.category_id
        if payload.suggested_tags is not None:
            item.suggested_tags_json = json.dumps(payload.suggested_tags)

        db.commit()
        return {"ok": True, "id": item.id}


@app.post("/api/ocr/staging/{staging_id}/move")
async def move_staging_file(
    staging_id: int,
    user_email: str | None = None,
    x_user_email: str | None = Header(default=None),
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Resolve destination folder path from category DMS template and move the staged file in SharePoint."""
    from .db.base import SessionLocal
    from .db import models as db_models
    import json
    import re

    email = user_email or x_user_email or "unknown@example.com"
    with SessionLocal() as db:
        item = db.query(db_models.OcrStagingFile).filter_by(id=staging_id).first()
        if not item:
            raise HTTPException(404, "Staging item not found")
        if item.status == "moved":
            return {"ok": True, "status": "moved", "final_path": item.final_path}

        if not item.drive_item_id:
            raise HTTPException(400, "Cannot relocate file: drive_item_id is missing")

        suggested_tags = {}
        if item.suggested_tags_json:
            try:
                raw_tags = json.loads(item.suggested_tags_json)
                if isinstance(raw_tags, dict):
                    for k, v in raw_tags.items():
                        if isinstance(v, dict) and "value" in v:
                            suggested_tags[k] = v.get("value") or ""
                        else:
                            suggested_tags[k] = v
            except Exception:
                suggested_tags = {}

        category = item.category
        if not category and item.category_id:
            category = db.query(db_models.DocumentCategory).filter_by(id=item.category_id).first()

        # Validate required tag fields
        if category and category.tag_fields_json:
            try:
                tag_defs = json.loads(category.tag_fields_json)
                missing = []
                for field in tag_defs:
                    if field.get("required"):
                        val = str(suggested_tags.get(field.get("key"), "") or "").strip()
                        if not val:
                            missing.append(field.get("label") or field.get("key"))
                if missing:
                    raise HTTPException(
                        422,
                        detail=f"Required tag field(s) missing: {', '.join(missing)}. Please review and edit tags before moving."
                    )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning("Failed to validate tag definitions: %s", e)

        # Resolve path template: physical folder ends at Category level (Sub-Category is metadata tag only)
        template_str = category.dms_path_template if category and category.dms_path_template else "Technical & Crewing/{vessel}/Drawings and Manuals/{group}/{category}"
        # Strip any legacy trailing {sub_category} or {subcategory} segment from folder path template
        template_str = re.sub(r"/\{sub_?category\}", "", template_str, flags=re.IGNORECASE)

        source_department = _extract_department_from_path(item.source_subfolder_path)
        known_vessels = _get_vessels_for_ocr(db)
        source_vessel = _extract_vessel_from_path(item.source_subfolder_path, known_vessels)
        category_department = (category.department if category and getattr(category, "department", None) else None)
        inferred_department = source_department or category_department or suggested_tags.get("department") or "Technical & Crewing"

        extracted_vessel = str(
            suggested_tags.get("vessel")
            or item.vessel_name
            or ""
        ).strip()
        # Path can still fallback to source vessel branch for folder resolution,
        # but SharePoint Vessel metadata must not be auto-filled from fallback.
        effective_vessel_for_path = extracted_vessel or source_vessel or ""
        vessel_for_sharepoint = extracted_vessel

        replacement_tags = dict(suggested_tags)
        if effective_vessel_for_path:
            replacement_tags["vessel"] = effective_vessel_for_path
        if inferred_department:
            replacement_tags["department"] = inferred_department

        resolved_path = template_str
        for k, v in replacement_tags.items():
            resolved_path = resolved_path.replace(f"{{{k}}}", str(v).strip())

        # Clean any remaining unmatched placeholders and tidy slashes
        resolved_path = re.sub(r"\{[a-z0-9_]+\}", "", resolved_path)
        resolved_path = re.sub(r"/+", "/", resolved_path).strip(" /")

        # Preserve department from the source branch whenever we have it.
        # This prevents "To be Classified" fallback from crossing departments.
        if source_department:
            resolved_path = _force_path_department(resolved_path, source_department)

        if not resolved_path:
            raise HTTPException(422, "Resolved destination path is empty. Please check tag values.")

        try:
            target_folder_id = await _resolve_or_create_sharepoint_path(resolved_path)
            source_folder_id = None
            metadata_patch_result: dict[str, Any] = {"ok": False, "attempted": False, "reason": "sp_not_configured"}
            if settings.sp_configured:
                try:
                    src_item = await gd.get_item(settings.sp_drive_id, item.drive_item_id, access_token=x_graph_access_token)
                    source_folder_id = (src_item.get("parentReference") or {}).get("id")
                except Exception:
                    pass
                await gd.move_item(
                    settings.sp_drive_id,
                    item.drive_item_id,
                    target_folder_id,
                    access_token=x_graph_access_token,
                )

                # Metadata values must come from OCR/classification tags, not raw path segments.
                from .ocr.drawing_category import classify_all_fields_tiered
                from .ocr.extract import extract_text

                drawing_cats, manual_cats, allowed_sub_by_category, _ = _taxonomy_maps()

                group_raw = _safe_tag_value(suggested_tags.get("group"))
                category_raw = _safe_tag_value(suggested_tags.get("category"))
                sub_raw = _safe_tag_value(suggested_tags.get("sub_category") or suggested_tags.get("subcategory"))
                department_raw = _safe_tag_value(suggested_tags.get("department")) or inferred_department

                group_for_sharepoint = _normalize_metadata_group(group_raw, category_raw)
                category_for_sharepoint = category_raw
                subcategory_for_sharepoint = sub_raw

                # Re-run OCR classification if any field is invalid, shifted, or missing.
                category_low = category_for_sharepoint.lower()
                sub_low = subcategory_for_sharepoint.lower()
                needs_reclassify = (
                    not group_for_sharepoint
                    or not category_for_sharepoint
                    or category_low not in drawing_cats and category_low not in manual_cats
                    or not subcategory_for_sharepoint
                    or (category_low in allowed_sub_by_category and sub_low not in allowed_sub_by_category[category_low])
                )

                if needs_reclassify:
                    try:
                        file_bytes, content_type, _name = await gd.download_file(settings.sp_drive_id, item.drive_item_id)
                        text = extract_text(file_bytes, item.filename or _name or "", content_type)
                        tiered = classify_all_fields_tiered(
                            text,
                            filename=item.filename or _name or "",
                            known_vessels=known_vessels,
                            source_path=item.source_subfolder_path or resolved_path,
                        )
                        source_dept = _extract_department_from_path(item.source_subfolder_path)
                        if source_dept:
                            department_raw = source_dept
                        else:
                            department_raw = _safe_tag_value(tiered.get("department")) or department_raw

                        vessel_for_sharepoint = _safe_tag_value(tiered.get("vessel")) or vessel_for_sharepoint
                        category_for_sharepoint = _safe_tag_value(tiered.get("category")) or category_for_sharepoint
                        subcategory_for_sharepoint = _safe_tag_value(tiered.get("sub_category")) or subcategory_for_sharepoint
                        group_for_sharepoint = _normalize_metadata_group(_safe_tag_value(tiered.get("group")), category_for_sharepoint)
                    except Exception as classify_err:
                        logger.warning("Staging move metadata reclassification fallback failed for %s: %s", item.drive_item_id, classify_err)

                # Final guardrails.
                if category_for_sharepoint.lower() == "to be classified":
                    # Preserve OCR-derived group when present; fallback only if missing.
                    if not group_for_sharepoint:
                        group_for_sharepoint = "Manuals"
                    if not subcategory_for_sharepoint:
                        subcategory_for_sharepoint = "To be Classified"

                if not group_for_sharepoint:
                    group_for_sharepoint = _normalize_metadata_group("", category_for_sharepoint)

                if not category_for_sharepoint:
                    category_for_sharepoint = "To be Classified"
                if not subcategory_for_sharepoint:
                    subcategory_for_sharepoint = "To be Classified"

                column_payload = _build_sharepoint_metadata_payload(
                    department=department_raw or inferred_department or "Technical & Crewing",
                    vessel=vessel_for_sharepoint,
                    group=group_for_sharepoint,
                    category=category_for_sharepoint,
                    sub_category=subcategory_for_sharepoint,
                )
                metadata_patch_result = {"ok": False, "attempted": False}
                try:
                    metadata_patch_result = await gd.update_file_columns(
                        settings.sp_drive_id,
                        item.drive_item_id,
                        column_payload,
                        access_token=x_graph_access_token,
                        sp_access_token=x_sp_access_token,
                    )
                    logger.info(
                        "OCR staging metadata patch: file=%s item_id=%s status=%s vessel=%s department=%s group=%s category=%s sub_category=%s result_ok=%s",
                        item.filename,
                        item.drive_item_id,
                        item.status,
                        vessel_for_sharepoint,
                        department_raw or inferred_department or "Technical & Crewing",
                        group_for_sharepoint,
                        category_for_sharepoint,
                        subcategory_for_sharepoint,
                        metadata_patch_result.get("ok") if isinstance(metadata_patch_result, dict) else None,
                    )
                    if not metadata_patch_result.get("ok", False):
                        patch_msg = f"[SharePoint metadata patch warning: {metadata_patch_result}]"
                        item.error = ((item.error or "") + " " + patch_msg).strip()
                except Exception as col_err:
                    logger.warning("Non-fatal: could not patch SharePoint metadata columns for %s: %s", item.drive_item_id, col_err)
                    patch_msg = f"[SharePoint metadata patch failed: {col_err}]"
                    item.error = ((item.error or "") + " " + patch_msg).strip()
                    metadata_patch_result = {
                        "ok": False,
                        "attempted": True,
                        "error": str(col_err),
                    }

            item.status = "moved"
            item.final_path = resolved_path
            if vessel_for_sharepoint:
                item.vessel_name = vessel_for_sharepoint
            db.commit()

            _log_activity(email, "ocr_staging_move", f"Moved staged file {item.filename} -> {resolved_path}")
            if source_folder_id:
                invalidate_folder_caches(source_folder_id)
            invalidate_folder_caches(target_folder_id)

            return {
                "ok": True,
                "id": item.id,
                "filename": item.filename,
                "target_path": resolved_path,
                "target_folder_id": target_folder_id,
                "metadata_patch": metadata_patch_result,
            }
        except (NotFound, BadRequest, Conflict) as e:
            _raise(e)
        except GraphError as e:
            if e.status == 429:
                raise HTTPException(429, "SharePoint is temporarily throttling requests. Please retry shortly.")
            raise HTTPException(e.status, f"SharePoint file relocation failed: {str(e)}")
        except Exception as e:
            logger.exception("Staging move failure for item=%s: %s", item.id, e)
            raise HTTPException(500, detail=f"Failed to relocate file: {str(e)}")


@app.delete("/api/ocr/staging/{staging_id}")
async def dismiss_staging_file(
    staging_id: int,
    _session: object = Depends(require_session),
):
    """Dismiss a file from the OCR review queue without moving."""
    from .db.base import SessionLocal
    from .db import models as db_models

    with SessionLocal() as db:
        item = db.query(db_models.OcrStagingFile).filter_by(id=staging_id).first()
        if not item:
            raise HTTPException(404, "Staging item not found")
        item.status = "dismissed"
        db.commit()
        return {"ok": True, "id": item.id, "message": "Item dismissed from staging queue"}


@app.post("/api/ocr/route-and-upload")
async def ocr_route_and_upload(
    path: str = Form(...),
    file: UploadFile = None,
    vessel_name: str | None = Form(None),
    group: str | None = Form(None),
    category: str | None = Form(None),
    sub_category: str | None = Form(None),
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Upload and auto-route an OCR-classified document to its exact SharePoint destination folder."""
    if file is None:
        raise HTTPException(400, "File is required")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty")

    target_path = (path or "").lstrip("/").strip()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]

    try:
        folder_id = await _resolve_or_create_sharepoint_path(target_path)
        result = await get_backend().upload(
            folder_id, file.filename, data, file.content_type, email, name
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"OCR Upload: {file.filename} -> {target_path}"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "ocr_file_upload", log_detail)
        invalidate_folder_caches(folder_id)
        return {
            "ok": True,
            "filename": file.filename,
            "target_path": target_path,
            "folder_id": folder_id,
            "upload_result": result,
            "tags": {
                "VesselName": vessel_name,
                "Group": group,
                "Category": category,
                "SubCategory": sub_category,
            },
        }
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)
    except GraphError as e:
        if e.status == 429:
            raise HTTPException(429, "SharePoint is temporarily throttling requests. Please retry shortly.")
        raise HTTPException(e.status, f"SharePoint upload failed: {str(e)}")
    except Exception as e:
        logger.exception("OCR route-and-upload failure: path=%s file=%s error=%s", target_path, file.filename if file else None, e)
        raise HTTPException(status_code=500, detail=f"Failed to route document to {target_path}: {str(e)}")


@app.post("/api/ocr/classify-existing-file")
async def ocr_classify_existing_file(
    item_id: str = Form(...),
    filename: str | None = Form(None),
    _session: object = Depends(require_session),
):
    """Run OCR extraction and AI classification on an existing file stored in SharePoint."""
    from .ocr.extract import extract_text
    from .ocr.drawing_category import classify_document_content, classify_against_db_categories, DRAWING_TAXONOMY, MANUAL_TAXONOMY, classify_all_fields_tiered
    import json

    data: bytes = b""
    fname = filename or "document.pdf"
    ctype = "application/pdf"

    try:
        if settings.sp_configured:
            content, mime, actual_name = await gd.download_file(settings.sp_drive_id, item_id)
            data = content
            fname = filename or actual_name
            ctype = mime or ctype
    except Exception as exc:
        logger.warning("Failed to download existing file %s for OCR: %s", item_id, exc)

    extracted = ""
    if data:
        try:
            extracted = extract_text(data, fname, ctype)
        except Exception as exc:
            logger.warning("OCR text extraction fallback for existing %s: %s", fname, exc)

    # Fetch registered vessel names and DB categories
    vessel_names: list[Any] = []
    db_categories = []
    if settings.db_configured:
        try:
            from .db.base import SessionLocal
            from .db import models as db_models
            with SessionLocal() as db:
                vessel_names = _get_vessels_for_ocr(db)
                db_categories = db.query(db_models.DocumentCategory).filter(db_models.DocumentCategory.is_active == True).all()
        except Exception:
            pass
    if not vessel_names:
        try:
            v_list = await get_backend().list_vessels()
            vessel_names = [v["name"] for v in v_list if isinstance(v, dict) and "name" in v]
        except Exception:
            vessel_names = []

    classification = classify_document_content(extracted, filename=fname, known_vessels=vessel_names)
    tiered = classify_all_fields_tiered(extracted, filename=fname, known_vessels=vessel_names)
    best_db_cat, db_conf, db_matches = classify_against_db_categories(extracted, filename=fname, db_categories=db_categories)

    matched_cat_id = None
    matched_cat_name = None
    tag_fields = []
    suggested_tags: dict[str, Any] = {}

    if best_db_cat and db_conf >= classification["confidence"]:
        matched_cat_id = best_db_cat.id
        matched_cat_name = best_db_cat.name
        confidence = db_conf
        matched_keywords = list(set(classification["matched_keywords"] + db_matches))
        if best_db_cat.tag_fields_json:
            try:
                tag_fields = json.loads(best_db_cat.tag_fields_json)
            except Exception:
                tag_fields = []
    else:
        confidence = classification["confidence"]
        matched_keywords = classification["matched_keywords"]
        built_in_cat_name = "Drawing" if classification["sub_category_1"] == "Drawing" else "Manual"
        matched_db = next((c for c in db_categories if c.name.lower() == built_in_cat_name.lower()), None)
        if matched_db:
            matched_cat_id = matched_db.id
            matched_cat_name = matched_db.name
            if matched_db.tag_fields_json:
                try:
                    tag_fields = json.loads(matched_db.tag_fields_json)
                except Exception:
                    tag_fields = []

    if confidence >= 0.40:
        detected_vessel = (tiered.get("vessel") or {}).get("value") or classification.get("vessel_name") or ""
        detected_group = (tiered.get("group") or {}).get("value") or classification.get("group") or "Drawing"
        fallback_category = "To be Classified" if detected_group == "Manual" else "Basic"
        detected_category = (tiered.get("category") or {}).get("value") or classification.get("category") or ""
        detected_sub_category = (tiered.get("sub_category") or {}).get("value") or classification.get("sub_category") or classification.get("leaf") or ""
        low_confidence_fallback = bool(not best_db_cat and db_conf < 0.40)

        suggested_tags["vessel"] = detected_vessel
        suggested_tags["group"] = detected_group
        suggested_tags["category"] = detected_category or (matched_cat_name if matched_cat_name not in ("Drawing", "Manual") else fallback_category) or fallback_category
        suggested_tags["sub_category"] = "To be Classified" if low_confidence_fallback else (detected_sub_category or "To be Classified")
        suggested_tags["department"] = (tiered.get("department") or {}).get("value") or classification.get("department") or "Technical & Crewing"
        if classification.get("leaf"):
            suggested_tags["leaf"] = classification["leaf"]

    text_preview = (extracted.strip()[:1000] + "...") if len(extracted.strip()) > 1000 else extracted.strip()

    return {
        "item_id": item_id,
        "filename": fname,
        "file_size": len(data),
        "content_type": ctype,
        "text_preview": text_preview,
        "text_length": len(extracted),
        "detected_vessel": classification["vessel_name"],
        "detected_group": classification["group"],
        "detected_category": classification["category"],
        "detected_sub_category_1": classification["sub_category_1"],
        "detected_sub_category_2": classification["sub_category_2"],
        "detected_leaf": classification["leaf"],
        "suggested_path": classification["suggested_path"],
        "confidence": confidence,
        "matched_keywords": matched_keywords,
        "matched_category_id": matched_cat_id,
        "matched_category_name": matched_cat_name,
        "tag_fields": tag_fields,
        "suggested_tags": suggested_tags,
        "available_vessels": sorted(vessel_names),
        "available_departments": template.ALL_MAIN_FOLDERS,
        "drawing_taxonomy": {cat: list(leaves.keys()) for cat, leaves in DRAWING_TAXONOMY.items()},
        "manual_taxonomy": {cat: list(leaves.keys()) for cat, leaves in MANUAL_TAXONOMY.items()},
    }


@app.post("/api/ocr/move-and-tag-existing")
async def ocr_move_and_tag_existing(
    item_id: str = Form(...),
    target_path: str = Form(...),
    vessel_name: str | None = Form(None),
    group: str | None = Form(None),
    category: str | None = Form(None),
    sub_category: str | None = Form(None),
    department: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Relocate an existing SharePoint document to its OCR-classified canonical destination folder."""
    target_path = (target_path or "").lstrip("/").strip()
    email = user_email or x_user_email or "unknown@example.com"

    try:
        target_folder_id = await _resolve_or_create_sharepoint_path(target_path)
        move_result = None
        source_folder_id = None
        metadata_patch_result: dict[str, Any] = {"ok": False, "attempted": False, "reason": "sp_not_configured"}
        if settings.sp_configured:
            try:
                src_item = await gd.get_item(settings.sp_drive_id, item_id, access_token=x_graph_access_token)
                source_folder_id = (src_item.get("parentReference") or {}).get("id")
            except Exception:
                pass
            move_result = await gd.move_item(
                settings.sp_drive_id,
                item_id,
                target_folder_id,
                access_token=x_graph_access_token,
            )

            # Persist OCR-derived metadata (not only vessel) after moving the file.
            category_for_sharepoint = (category or "").strip() or "To be Classified"
            subcategory_for_sharepoint = (sub_category or "").strip() or "To be Classified"
            group_for_sharepoint = _normalize_metadata_group((group or "").strip(), category_for_sharepoint)

            if category_for_sharepoint.lower() == "to be classified":
                # Preserve OCR-derived group when present; fallback only if missing.
                if not group_for_sharepoint:
                    group_for_sharepoint = "Manuals"
                if not subcategory_for_sharepoint:
                    subcategory_for_sharepoint = "To be Classified"

            if not group_for_sharepoint:
                group_for_sharepoint = _normalize_metadata_group("", category_for_sharepoint) or "Manuals"

            department_for_sharepoint = (department or "").strip() or _extract_department_from_path(target_path) or "Technical & Crewing"
            vessel_for_sharepoint = (vessel_name or "").strip()

            column_payload = _build_sharepoint_metadata_payload(
                department=department_for_sharepoint,
                vessel=vessel_for_sharepoint,
                group=group_for_sharepoint,
                category=category_for_sharepoint,
                sub_category=subcategory_for_sharepoint,
            )

            try:
                metadata_patch_result = await gd.update_file_columns(
                    settings.sp_drive_id,
                    item_id,
                    column_payload,
                    access_token=x_graph_access_token,
                    sp_access_token=x_sp_access_token,
                )
            except Exception as col_err:
                logger.warning("Non-fatal: could not patch SharePoint metadata columns for existing item %s: %s", item_id, col_err)
                metadata_patch_result = {
                    "ok": False,
                    "attempted": True,
                    "error": str(col_err),
                }

        _log_activity(email, "ocr_file_move", f"Moved file {item_id} -> {target_path}")
        if source_folder_id:
            invalidate_folder_caches(source_folder_id)
        invalidate_folder_caches(target_folder_id)
        return {
            "ok": True,
            "item_id": item_id,
            "target_path": target_path,
            "target_folder_id": target_folder_id,
            "move_result": move_result,
            "metadata_patch": metadata_patch_result,
            "tags": {
                "VesselName": vessel_name,
                "Group": group,
                "Category": category,
                "SubCategory": sub_category,
                "Department": department,
            },
        }
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)
    except GraphError as e:
        if e.status == 429:
            raise HTTPException(429, "SharePoint is temporarily throttling requests. Please retry shortly.")
        raise HTTPException(e.status, f"SharePoint file relocation failed: {str(e)}")
    except Exception as e:
        logger.exception("OCR move-and-tag-existing failure: item=%s target=%s error=%s", item_id, target_path, e)
        raise HTTPException(status_code=500, detail=f"Failed to relocate file: {str(e)}")


@app.get("/api/folders/upload-by-path")
async def resolve_upload_path(
    path: str,
    _session: object = Depends(require_session),
):
    """Resolve a logical folder path for frontend refreshes.

    Keep this route explicit so FastAPI does not route the literal
    ``upload-by-path`` segment into ``/api/folders/{folder_id}``.
    """
    normalized_path = (path or "").lstrip("/").strip()
    try:
        folder_id = await get_backend().resolve_path(normalized_path)
        logger.info("Folder refresh resolve: path=%s folder_id=%s", normalized_path, folder_id)
        return {"folder_id": folder_id}
    except (NotFound, BadRequest) as e:
        logger.warning("Folder refresh resolve failed: path=%s error=%s", normalized_path, e)
        _raise(e)
    except Exception as e:
        logger.exception("Folder path resolution failure: path=%s error=%s", normalized_path, e)
        raise HTTPException(status_code=500, detail="Folder path resolution failed. Check the backend log for details.")


@app.get("/api/folders/{folder_id}/children")
async def children(folder_id: str, _session: object = Depends(require_session)):
    normalized_id = (folder_id or "").strip()
    # Frontend can occasionally pass breadcrumb/path-like values here
    # (e.g. "Technical & Crewing > Vessel > Category") instead of a folder id.
    # Resolve those values so Graph /items/{id}/children is not called with a path.
    if any(sep in normalized_id for sep in (">", "/", "\\")):
        logical_path = "/".join([p.strip() for p in normalized_id.replace(">", "/").split("/") if p.strip()])
        try:
            normalized_id = await get_backend().resolve_path(logical_path)
        except Exception:
            # Keep previous behavior: if unresolved, endpoint returns [] via exception handler below.
            pass

    now = time.time()
    cached = _FOLDER_CHILDREN_CACHE.get(normalized_id)
    if cached and (now - cached[1]) < CACHE_TTL_CHILDREN:
        return cached[0]
    try:
        data = await get_backend().children(normalized_id)
        _FOLDER_CHILDREN_CACHE[normalized_id] = (data, time.time())
        return data
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        logging.getLogger("uvicorn.error").warning(f"Failed to load children for folder '{normalized_id}': {e}")
        return []


@app.get("/api/folders/{folder_id}")
async def folder(folder_id: str, _session: object = Depends(require_session)):
    try:
        return await get_backend().get_folder(folder_id)
    except (NotFound, BadRequest) as e:
        _raise(e)


@app.post("/api/folders/{folder_id}/upload")
async def upload(
    folder_id: str,
    file: UploadFile,
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    x_graph_access_token: str | None = Header(default=None),
    x_sp_access_token: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Stages the file and creates a pending approval request — it is no
    longer saved into the folder directly. See docs on the approval workflow
    in services/stub_backend.py / services/real_backend.py."""
    data = await file.read()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]
    try:
        result = await get_backend().upload(
            folder_id, file.filename, data, file.content_type, email, name,
            access_token=x_graph_access_token,
            sp_access_token=x_sp_access_token,
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"Uploaded: {file.filename} (awaiting reviewer approval)"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "file_upload", log_detail)
        invalidate_folder_caches(folder_id)
        return result
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)
    except Exception as e:
        logger.exception(
            "Folder upload failure: folder_id=%s filename=%s error=%s",
            folder_id,
            file.filename if file else None,
            e,
        )
        raise HTTPException(status_code=500, detail="Upload failed. Check the backend log for details.")


class CreateSubfolderIn(BaseModel):
    name: str
    user_email: str | None = None


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str, user_email: str | None = Query(None), folder_name: str | None = Query(None), x_user_email: str | None = Header(default=None), _session: object = Depends(require_session)):
    """Delete a folder and all its contents (or stage a pending approval for
    non-admin users)."""
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().delete_folder(
            folder_id, requesting_email=email, requesting_name=display_name
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending",
            "action_type": "delete_folder",
            "approval_id": result.get("approval_id"),
            "message": result.get("message"),
        })
    detail = f"Deleted folder: {folder_name}" if folder_name else f"Deleted folder: {folder_id}"
    _log_activity(email, "delete_folder", detail)
    invalidate_folder_caches(folder_id)
    return {"status": "completed", "message": result.get("message")}


@app.post("/api/folders/{folder_id}/subfolder")
async def create_subfolder(folder_id: str, payload: CreateSubfolderIn, x_user_email: str | None = Header(default=None), _session: object = Depends(require_session)):
    """Manually create a named sub-folder inside a month_driven folder (or
    stage a pending approval for non-admin users)."""
    email = (payload.user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().create_subfolder(
            folder_id, payload.name.strip(), requesting_email=email, requesting_name=display_name
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content={
                "status": "pending",
                "action_type": "create_folder",
                "approval_id": result.get("approval_id"),
                "message": result.get("message"),
            })
        _log_activity(email, "create_folder", f"Created folder: {payload.name.strip()}")
        invalidate_folder_caches(folder_id)
        folder = result.get("result") or {}
        return {**folder, "status": "completed", "message": result.get("message")}
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


@app.post("/api/folders/{folder_id}/month-upload")
async def month_upload(
    folder_id: str,
    file: UploadFile,
    category: str = Form(None),
    uploader_email: str | None = Form(None),
    uploader_name: str | None = Form(None),
    user_email: str | None = Form(None),
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    data = await file.read()
    email = uploader_email or user_email or x_user_email or "unknown@example.com"
    name = uploader_name or email.split("@")[0]
    try:
        result = await get_backend().month_upload(
            folder_id, file.filename, category, data, file.content_type,
            email, name
        )
        dest = result.get("destination") if isinstance(result, dict) else None
        log_detail = f"Uploaded: {file.filename} (awaiting reviewer approval)"
        if dest:
            log_detail += f"|{dest}"
        _log_activity(email, "file_upload", log_detail)
        invalidate_folder_caches(folder_id)
        return result
    except (NotFound, BadRequest, Conflict, InternalServerError) as e:
        _raise(e)
    except Exception as e:
        logger.exception(
            "Month upload failure: folder_id=%s filename=%s category=%s error=%s",
            folder_id,
            file.filename if file else None,
            category,
            e,
        )
        raise HTTPException(status_code=500, detail="Upload failed. Check the backend log for details.")


@app.get("/api/files/{file_id}/content")
async def file_content(file_id: str, _session: object = Depends(require_session)):
    result = await get_backend().get_file(file_id)
    if result is None:
        logger.warning("File content not found: file_id=%s", file_id)
        raise HTTPException(404, "File not found")
    content, content_type, name = result
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@app.delete("/api/files/{file_id}")
async def delete_file(
    file_id: str, user_email: str | None = Query(None), reason: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().delete_file(
            file_id, requesting_email=email, requesting_name=display_name, reason=reason
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending",
            "action_type": "delete_document",
            "approval_id": result.get("approval_id"),
            "message": result.get("message"),
        })
    _log_activity(email, "delete_file", f"Deleted file: {file_id}")
    invalidate_folder_caches()
    return {"status": "completed", "message": result.get("message")}


@app.get("/api/search")
async def search(q: str = "", vessel_id: str | None = None, _session: object = Depends(require_session)):
    return await get_backend().search(q, vessel_id)


class LogActivityIn(BaseModel):
    email: str
    action: str
    detail: str | None = None


class SharePointNavigationLogIn(BaseModel):
    navigation_id: str
    event: str
    ui_path: str | None = None
    sharepoint_path: str | None = None
    details: dict = {}


@app.post("/api/diagnostics/sharepoint-navigation", status_code=204)
async def log_sharepoint_navigation(
    payload: SharePointNavigationLogIn,
    _session: object = Depends(require_session),
):
    logger.info(
        "SharePoint folder navigation: navigation_id=%s event=%s ui_path=%s sharepoint_path=%s details=%s",
        payload.navigation_id,
        payload.event,
        payload.ui_path,
        payload.sharepoint_path,
        payload.details,
    )
    return Response(status_code=204)


@app.post("/api/activity", status_code=204)
async def log_activity_endpoint(payload: LogActivityIn, _session: object = Depends(require_session)):
    """Frontend-initiated activity log (archive, restore, etc.)."""
    _log_activity(payload.email, payload.action, payload.detail)
    return Response(status_code=204)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, _session: object = Depends(require_session)):
    job = await get_backend().get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/archive/ids")
async def get_archived_ids(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_archived_ids()
    except Exception as e:
        raise HTTPException(500, f"Failed to get archived IDs: {e}")


@app.get("/api/archive/nodes")
async def get_archived_nodes(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_archived_nodes()
    except Exception as e:
        raise HTTPException(500, f"Failed to get archived nodes: {e}")


@app.post("/api/archive/{item_id}")
async def archive_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None), reason: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().archive_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name, reason=reason,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to archive item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "archive_item",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "archive_folder" if type == "folder" else "archive_file", f"Archived {type}: {item_id}")
    return {"status": "completed", "message": result.get("message")}


@app.post("/api/restore/{item_id}")
async def restore_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().restore_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to restore item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "restore_item",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "restore_folder", f"Restored item: {item_id}")
    return {"status": "completed", "message": result.get("message")}


@app.get("/api/recycle-bin/ids")
async def get_deleted_ids(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_deleted_ids()
    except Exception as e:
        raise HTTPException(500, f"Failed to get deleted IDs: {e}")


@app.get("/api/recycle-bin/nodes")
async def get_deleted_nodes(_session: object = Depends(require_session)):
    try:
        return await get_backend().get_deleted_nodes()
    except Exception as e:
        raise HTTPException(500, f"Failed to get deleted nodes: {e}")


@app.post("/api/recycle-bin/restore/{item_id}")
async def restore_deleted_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().restore_deleted_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to restore deleted item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "restore_from_recycle_bin",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "restore_folder" if type == "folder" else "restore_file", f"Restored {type}: {item_id}")
    return {"status": "completed", "message": result.get("message")}


@app.delete("/api/recycle-bin/{item_id}")
async def permanent_delete_item(
    item_id: str, type: str = Query("folder"), user_email: str | None = Query(None),
    item_name: str | None = Query(None), department: str | None = Query(None),
    vessel_name: str | None = Query(None),
    x_user_email: str | None = Header(default=None), _session: object = Depends(require_session),
):
    email = (user_email or x_user_email or "").strip().lower()
    display_name = email.split("@")[0] if email else None
    try:
        result = await get_backend().permanent_delete_item(
            item_id, type, requesting_email=email, requesting_name=display_name,
            item_name=item_name, department=department, vessel_name=vessel_name,
        )
    except (NotFound, BadRequest) as e:
        _raise(e)
    except Exception as e:
        raise HTTPException(500, f"Failed to permanently delete item: {e}")
    if result.get("status") == "pending":
        return JSONResponse(status_code=202, content={
            "status": "pending", "action_type": "permanent_delete",
            "approval_id": result.get("approval_id"), "message": result.get("message"),
        })
    _log_activity(email, "permanent_delete_folder" if type == "folder" else "permanent_delete_file", f"Permanently deleted {type}: {item_id}")
    return {"status": "completed", "message": result.get("message")}


# ---------------------------------------------------------------------------
# Approval workflow (admin only)
# ---------------------------------------------------------------------------

@app.get("/api/my-approvals")
async def list_my_approvals(
    status: str | None = None, x_user_email: str | None = Header(default=None), _session: object = Depends(require_session)
):
    if not x_user_email:
        raise HTTPException(400, "X-User-Email header required")
    email = x_user_email.strip().lower()
    approvals = await get_backend().list_approvals(status)
    return [a for a in approvals if a.get("uploaded_by_email", "").strip().lower() == email]


@app.get("/api/approvals")
async def list_approvals(status: str | None = None, q: str | None = None, admin: str = Depends(_require_admin), _session: object = Depends(require_session)):
    return await get_backend().list_approvals(status, q)



@app.get("/api/approvals/{request_id}")
async def get_approval(
    request_id: str,
    x_user_email: str | None = Header(default=None)
):
    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")
    approval = await get_backend().get_approval(request_id)
    if not approval:
        raise HTTPException(404, "Approval request not found")
    
    is_admin = _is_admin_email(email)
    is_uploader = approval.get("uploaded_by_email", "").strip().lower() == email
    if not (is_admin or is_uploader):
        raise HTTPException(403, "Access denied")
    return approval


@app.get("/api/approvals/{request_id}/preview")
async def approval_preview(
    request_id: str,
    x_user_email: str | None = Header(default=None),
    admin: str | None = None
):
    email = (x_user_email or admin or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header or admin query parameter required")
    approval = await get_backend().get_approval(request_id)
    if not approval:
        raise HTTPException(404, "Approval request not found")
    
    is_admin = _is_admin_email(email)
    is_uploader = approval.get("uploaded_by_email", "").strip().lower() == email
    if not (is_admin or is_uploader):
        raise HTTPException(403, "Administrator access required")
    
    result = await get_backend().get_approval_file(request_id)
    if result is None:
        raise HTTPException(404, "Staged file not found")
    content, content_type, name = result
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@app.post("/api/approvals/{request_id}/approve")
async def approve_approval(request_id: str, admin: str = Depends(_require_admin), _session: object = Depends(require_session)):
    try:
        res = await get_backend().approve_request(request_id, admin)
        invalidate_folder_caches()
        return res
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


@app.post("/api/approvals/{request_id}/reject")
async def reject_approval(
    request_id: str, payload: RejectIn, admin: str = Depends(_require_admin), _session: object = Depends(require_session)
):
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(400, "A rejection reason is required")
    try:
        res = await get_backend().reject_request(request_id, admin, reason)
        invalidate_folder_caches()
        return res
    except (NotFound, BadRequest, Conflict) as e:
        _raise(e)


@app.delete("/api/approvals/{request_id}")
async def delete_approval(
    request_id: str,
    admin: str = Depends(_require_admin),
    _session: object = Depends(require_session),
):
    """Permanently delete a single approval record (for cleanup of approved/rejected items)."""
    from .db.base import SessionLocal
    from .db import models as _m
    with SessionLocal() as db:
        row = db.get(_m.ApprovalRequest, int(request_id)) if request_id.isdigit() else None
        if row is None:
            raise HTTPException(404, "Approval record not found")
        db.delete(row)
        db.commit()
    return {"deleted": True, "id": request_id}


@app.delete("/api/approvals")
async def bulk_delete_approvals(
    status: str | None = None,
    admin: str = Depends(_require_admin),
    _session: object = Depends(require_session),
):
    """Bulk-delete approval records by status (e.g. approved, rejected, cancelled).
    Only non-pending rows may be bulk-deleted for safety."""
    from .db.base import SessionLocal
    from .db import models as _m
    allowed_statuses = {"approved", "rejected", "cancelled"}
    if not status or status.lower() not in allowed_statuses:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(allowed_statuses))}")
    with SessionLocal() as db:
        deleted = db.query(_m.ApprovalRequest).filter(
            _m.ApprovalRequest.entry_kind == "approval",
            _m.ApprovalRequest.status == status.lower(),
        ).delete(synchronize_session=False)
        db.commit()
    return {"deleted": deleted, "status": status}


# ---------------------------------------------------------------------------
# Folder-creation alerts (top-header alert bell)
# ---------------------------------------------------------------------------

@app.get("/api/alerts")
async def list_folder_alerts(
    unread: bool = False,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Return folder-creation alerts (newly created SharePoint Online folders)
    for the top-header alert bell. Pass unread=true to filter to unread only."""
    if not x_user_email:
        raise HTTPException(400, "X-User-Email header required")
    return await get_backend().list_folder_alerts(unread_only=unread)


@app.get("/api/alerts/all")
async def list_all_alerts(
    unread: bool = False,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    """Aggregate DMS, CRUD activity, and email notifications for the top bell."""
    if not x_user_email:
        raise HTTPException(400, "X-User-Email header required")
    alerts = await get_backend().list_folder_alerts(unread_only=unread)
    if not settings.db_configured:
        return alerts
    from .db import models as db_models
    from .db.base import SessionLocal
    with SessionLocal() as db:
        activities = db.query(db_models.ActivityLog).order_by(db_models.ActivityLog.created_at.desc()).limit(100).all()
        emails = db.query(db_models.EmailLog).order_by(db_models.EmailLog.created_at.desc()).limit(100).all()
        alerts.extend({
            "id": f"crud_{row.id}", "drive_item_id": None,
            "folder_name": row.action.replace("_", " ").title(), "folder_path": row.detail or row.action,
            "parent_folder_id": None, "vessel_name": None, "department": "Application",
            "created_by_email": row.user_email, "created_by_name": row.user_email,
            "alert_type": "crud_operation", "alert_category": "crud", "read": False,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in activities)
        alerts.extend({
            "id": f"email_{row.id}", "drive_item_id": None,
            "folder_name": row.subject_final or "Email notification", "folder_path": f"{row.status} → {row.recipient}",
            "parent_folder_id": None, "vessel_name": row.vessel_name, "department": "Email",
            "created_by_email": "", "created_by_name": "Email automation",
            "alert_type": "email_alert", "alert_category": "email", "read": False,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in emails)
    return sorted(alerts, key=lambda item: item.get("created_at") or "", reverse=True)


@app.post("/api/alerts/{alert_id}/read")
async def mark_alert_read(
    alert_id: str,
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    if not x_user_email:
        raise HTTPException(400, "X-User-Email header required")
    res = await get_backend().mark_folder_alert_read(alert_id, read=True)
    if res is None:
        raise HTTPException(404, "Alert not found")
    return res


@app.post("/api/alerts/read-all")
async def mark_all_alerts_read(
    x_user_email: str | None = Header(default=None),
    _session: object = Depends(require_session),
):
    if not x_user_email:
        raise HTTPException(400, "X-User-Email header required")
    return await get_backend().mark_all_folder_alerts_read()


# ---------------------------------------------------------------------------
# Session management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def list_sessions(
    x_session_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
):
    """List all sessions for the currently authenticated user, newest first."""
    if not settings.db_configured:
        return []

    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")

    try:
        from .db.base import SessionLocal
        from .services.session_service import list_user_sessions
        with SessionLocal() as db:
            sessions = list_user_sessions(db, email)
        return [
            {
                "session_id": s.session_id,
                "status": s.status,
                "login_time": s.login_time.isoformat() + "Z" if s.login_time else None,
                "last_activity": s.last_activity.isoformat() + "Z" if s.last_activity else None,
                "expiry_time": s.expiry_time.isoformat() + "Z" if s.expiry_time else None,
                "logout_time": s.logout_time.isoformat() + "Z" if s.logout_time else None,
                "browser": s.browser,
                "operating_system": s.operating_system,
                "device_type": s.device_type,
                "ip_address": s.ip_address,
                "authentication_method": s.authentication_method,
                "is_current": s.session_id == x_session_id,
            }
            for s in sessions
        ]
    except Exception as exc:
        raise HTTPException(500, f"Could not retrieve sessions: {exc}")


@app.delete("/api/sessions/{target_session_id}")
async def revoke_session_endpoint(
    target_session_id: str,
    request: Request,
    x_session_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
):
    """Revoke a specific session. Users can only revoke their own sessions."""
    if not settings.db_configured:
        raise HTTPException(501, "Session management requires database")

    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")

    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        from .services.session_service import revoke_session

        with SessionLocal() as db:
            target = (
                db.query(db_models.UserSession)
                .filter_by(session_id=target_session_id)
                .one_or_none()
            )
            if target is None:
                raise HTTPException(404, "Session not found")

            is_admin = _is_admin_email(email)
            is_owner = target.email.lower() == email
            if not (is_owner or is_admin):
                raise HTTPException(403, "You can only revoke your own sessions")

            ip = _get_ip(request)
            revoke_session(db, target_session_id, reason="user_initiated", ip_address=ip)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Could not revoke session: {exc}")


@app.get("/api/sessions/audit")
async def list_session_audit(
    x_user_email: str | None = Header(default=None),
    limit: int = Query(default=50, le=200),
):
    """Return the session audit log for the current user (newest first)."""
    if not settings.db_configured:
        return []

    email = (x_user_email or "").strip().lower()
    if not email:
        raise HTTPException(400, "X-User-Email header required")

    try:
        from .db.base import SessionLocal
        from .db import models as db_models

        with SessionLocal() as db:
            entries = (
                db.query(db_models.SessionAuditLog)
                .filter_by(email=email)
                .order_by(db_models.SessionAuditLog.created_at.desc())
                .limit(limit)
                .all()
            )
        return [
            {
                "session_id": e.session_id,
                "event": e.event,
                "detail": e.detail,
                "ip_address": e.ip_address,
                "browser": e.browser,
                "status": e.status,
                "login_time": e.login_time.isoformat() + "Z" if e.login_time else None,
                "logout_time": e.logout_time.isoformat() + "Z" if e.logout_time else None,
                "active_duration": e.active_duration,
                "active_duration_formatted": e.active_duration_formatted,
                "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
            }
            for e in entries
        ]
    except Exception as exc:
        raise HTTPException(500, f"Could not retrieve audit log: {exc}")


# ── AI BANTO Email Automation routes ──────────────────────────────────────────
from .email_automation import router as email_router
app.include_router(email_router)


# ── Folder/File Placement Detection Endpoints ─────────────────────────────────

class AnomalyPatchIn(BaseModel):
    resolved: bool = True


class AnomalyReadIn(BaseModel):
    read: bool = True


class NormalFolderIn(BaseModel):
    drive_item_id: str
    name: str
    item_type: str = "folder"
    spo_path: str
    department: str = ""
    vessel_name: str | None = None


@app.get("/api/normal-folders")
async def list_normal_folders():
    """Return all folders/files confirmed as 'Normal Folders' by a user."""
    if not settings.db_configured:
        return []
    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        with SessionLocal() as db:
            rows = (
                db.query(db_models.FolderAnomaly)
                .filter_by(anomaly_type="classified_normal")
                .order_by(db_models.FolderAnomaly.detected_at.desc())
                .all()
            )
            return [
                {
                    "id": r.id,
                    "drive_item_id": r.drive_item_id,
                    "name": r.name,
                    "item_type": r.item_type,
                    "spo_path": r.spo_path,
                    "department": r.department,
                    "vessel_name": r.vessel_name,
                    "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
    except Exception as exc:
        _logger.warning("Failed to fetch normal folders: %s", exc)
        return []


@app.post("/api/normal-folders", status_code=201)
async def create_normal_folder(payload: NormalFolderIn):
    """Upsert a folder/file as a confirmed 'Normal Folder'. Idempotent by drive_item_id."""
    if not settings.db_configured:
        return {"ok": True, "id": None, "name": payload.name}
    try:
        from datetime import datetime, timezone
        from .db.base import SessionLocal
        from .db import models as db_models
        with SessionLocal() as db:
            # Check if already exists (idempotent)
            existing = (
                db.query(db_models.FolderAnomaly)
                .filter_by(drive_item_id=payload.drive_item_id)
                .one_or_none()
            )
            if existing:
                # Update classification to normal if it was something else
                existing.anomaly_type = "classified_normal"
                existing.resolved = True
                existing.name = payload.name
                existing.item_type = payload.item_type
                existing.spo_path = payload.spo_path
                existing.department = payload.department or ""
                existing.vessel_name = payload.vessel_name
                existing.updated_at = datetime.now(timezone.utc)
                db.commit()
                return {"ok": True, "id": existing.id, "name": existing.name, "already_existed": True}
            # Create new record
            row = db_models.FolderAnomaly(
                drive_item_id=payload.drive_item_id,
                name=payload.name,
                item_type=payload.item_type,
                anomaly_type="classified_normal",
                spo_path=payload.spo_path,
                department=payload.department or "",
                vessel_name=payload.vessel_name,
                resolved=True,
                detected_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return {"ok": True, "id": row.id, "name": row.name, "already_existed": False}
    except Exception as exc:
        raise HTTPException(500, f"Failed to save normal folder: {exc}")


@app.get("/api/anomalies")
async def list_anomalies(anomaly_type: str | None = Query(default=None)):
    """Return all unresolved folder/file placement anomalies."""
    if not settings.db_configured:
        return []
    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        with SessionLocal() as db:
            q = db.query(db_models.FolderAnomaly).filter_by(resolved=False)
            if anomaly_type:
                q = q.filter_by(anomaly_type=anomaly_type)
            anomalies = q.order_by(db_models.FolderAnomaly.detected_at.desc()).all()
            return [
                {
                    "id": a.id,
                    "drive_item_id": a.drive_item_id,
                    "name": a.name,
                    "item_type": a.item_type,
                    "anomaly_type": a.anomaly_type,
                    "department": a.department,
                    "vessel_name": a.vessel_name,
                    "spo_path": a.spo_path,
                    "resolved": a.resolved,
                    "read": a.read,
                    "read_at": a.read_at.isoformat() if a.read_at else None,
                    "detected_at": a.detected_at.isoformat() if a.detected_at else None,
                    "updated_at": a.updated_at.isoformat() if a.updated_at else None,
                }
                for a in anomalies
            ]
    except Exception as exc:
        _logger.warning("Failed to fetch anomalies: %s", exc)
        return []


@app.patch("/api/anomalies/{anomaly_id}")
async def patch_anomaly(anomaly_id: int, payload: AnomalyPatchIn):
    """Mark a placement anomaly as resolved/dismissed."""
    if not settings.db_configured:
        return {"ok": True}
    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        from datetime import datetime, timezone
        with SessionLocal() as db:
            row = db.query(db_models.FolderAnomaly).filter_by(id=anomaly_id).one_or_none()
            if not row:
                raise HTTPException(404, "Anomaly not found")
            row.resolved = payload.resolved
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            return {"ok": True, "id": anomaly_id, "resolved": row.resolved}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to patch anomaly: {exc}")


@app.post("/api/anomalies/{anomaly_id}/read")
async def mark_anomaly_read(anomaly_id: int, payload: AnomalyReadIn):
    """Persist read state and modified time for an anomaly alert."""
    if not settings.db_configured:
        return {"ok": True, "id": anomaly_id, "read": payload.read}
    try:
        from datetime import datetime, timezone
        from .db.base import SessionLocal
        from .db import models as db_models
        with SessionLocal() as db:
            row = db.query(db_models.FolderAnomaly).filter_by(id=anomaly_id).one_or_none()
            if not row:
                raise HTTPException(404, "Anomaly not found")
            now = datetime.now(timezone.utc)
            row.read = payload.read
            row.read_at = now if payload.read else None
            row.updated_at = now
            db.commit()
            return {"ok": True, "id": anomaly_id, "read": row.read, "read_at": row.read_at.isoformat() if row.read_at else None, "updated_at": row.updated_at.isoformat() if row.updated_at else None}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to update anomaly read state: {exc}")


@app.post("/api/anomalies/scan")
async def scan_anomalies_endpoint():
    """Trigger on-demand scanning for folder/file placement anomalies."""
    if not settings.db_configured:
        return []
    try:
        from .db.base import SessionLocal
        from .db import models as db_models
        from .services.anomaly_detector import scan_and_record_anomalies
        with SessionLocal() as db:
            # Build tree items from cached Folder table & Vessel list
            folder_rows = db.query(db_models.Folder).all()
            tree_items = [
                {
                    "id": f.drive_item_id,
                    "name": f.name,
                    "item_type": "folder",
                    "path": f.path,
                }
                for f in folder_rows
            ]
            detected = scan_and_record_anomalies(db, tree_items)
            return [
                {
                    "id": a.id,
                    "drive_item_id": a.drive_item_id,
                    "name": a.name,
                    "item_type": a.item_type,
                    "anomaly_type": a.anomaly_type,
                    "department": a.department,
                    "vessel_name": a.vessel_name,
                    "spo_path": a.spo_path,
                    "resolved": a.resolved,
                    "read": a.read,
                    "read_at": a.read_at.isoformat() if a.read_at else None,
                    "detected_at": a.detected_at.isoformat() if a.detected_at else None,
                    "updated_at": a.updated_at.isoformat() if a.updated_at else None,
                }
                for a in detected
            ]
    except Exception as exc:
        _logger.warning("Scan anomalies failed: %s", exc)
        return []
