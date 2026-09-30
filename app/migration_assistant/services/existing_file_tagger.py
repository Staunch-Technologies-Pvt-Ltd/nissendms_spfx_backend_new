"""Folder-derived Managed Metadata tagging for files already in destination.

This service deliberately never extracts content or calls any move, copy,
rename, or folder-creation operation. The current destination folder path is
the only classification source.
"""
from __future__ import annotations

import json
import re

from ..db import SessionLocal
from ..config import settings
from ..graph import drive as gd
from ..graph import fields as gf
from ..graph import site as graph_site
from ..graph import term_store
from ..models import db_models as models
from ..services import migration_common, migration_tagging, site_to_site_common

_GROUP_NAMES = {"drawing": "Drawing", "drawings": "Drawing", "manual": "Manual", "manuals": "Manual"}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _derive_tags(folder_path: str, root_path: str) -> tuple[dict[str, str] | None, str | None]:
    full_parts = [part for part in folder_path.strip("/").split("/") if part]
    root_parts = [part for part in root_path.strip("/").split("/") if part]
    if len(full_parts) <= len(root_parts):
        return None, "Unable to determine tags from folder path"
    scan_parts = full_parts[len(root_parts):]
    root_starts_at_vessel = bool(scan_parts) and _norm(scan_parts[0]) not in _GROUP_NAMES and "drawing" not in _norm(scan_parts[0]) and "manual" not in _norm(scan_parts[0])
    vessel = scan_parts[0] if root_starts_at_vessel else root_parts[-1] if root_parts else scan_parts[0]
    group_start = 1 if root_starts_at_vessel else 0
    group_index = next((i for i, part in enumerate(scan_parts[group_start:], group_start) if _norm(part) in _GROUP_NAMES), None)
    if group_index is None or group_index == len(scan_parts) - 1:
        return None, "Unable to determine tags from folder path"
    group = _GROUP_NAMES[_norm(scan_parts[group_index])]
    category = scan_parts[-1]
    return {"group": group, "category": category, "vessel": vessel}, None


async def _walk_files(drive_id: str, item_id: str, path: str) -> list[dict]:
    files: list[dict] = []
    for child in await gd.list_children(drive_id, item_id):
        child_path = f"{path}/{child['name']}" if path else child["name"]
        if "file" in child:
            files.append({**child, "folder_path": path})
        elif "folder" in child:
            files.extend(await _walk_files(drive_id, child["id"], child_path))
    return files


async def _bindings(destination_site: dict) -> dict[str, dict]:
    site_id = await graph_site.get_site_id(destination_site["hostname"], destination_site["site_path"])
    taxonomy_columns = gf.taxonomy_columns(await gf.list_site_columns(site_id))
    names = {
        "group": {"group"},
        "category": {"category"},
        # "Vessel Name"'s internal name is the escaped "Vessel_x0020_Name_x0020_".
        "vessel": {"vessel", "vesselname", "vesselx0020namex0020"},
    }
    configured = {
        "group": (settings.group_field_name, settings.group_term_set_id),
        "category": (settings.category_field_name, settings.category_term_set_id),
        "vessel": (settings.vessel_field_name, settings.vessel_term_set_id),
    }
    result = {}
    for key, (field, term_set) in configured.items():
        field = field or next((name for name in taxonomy_columns if _norm(name) in names[key]), "")
        term_set = term_set or taxonomy_columns.get(field, "")
        result[key] = {"field": field, "term_set_id": term_set}
    return result


def _current_tags(item_fields: dict, bindings: dict) -> dict[str, str | None]:
    """The item's existing Managed Metadata labels, read from Graph's `fields`
    facet — which returns `{Label, TermGuid}` for these columns in this
    tenant, so the (app-only-restricted) SharePoint REST read isn't needed
    just to show what a file is currently tagged with."""
    current: dict[str, str | None] = {}
    for key, binding in bindings.items():
        value = item_fields.get(binding["field"]) if binding["field"] else None
        current[key] = value.get("Label") if isinstance(value, dict) else (value or None)
    return current


def _recent_scan_summary(scan_payload: dict) -> dict:
    summary = scan_payload.get("summary") or {}
    files = scan_payload.get("files") or []
    return {
        "root_path": (scan_payload.get("root_path") or "").strip("/"),
        "total_files": int(summary.get("total_files") or 0),
        "ready_files": int(summary.get("ready_files") or 0),
        "fully_tagged": int(summary.get("fully_tagged") or 0),
        "category_skipped": int(summary.get("category_skipped") or 0),
        "missing_taxonomy": int(summary.get("missing_taxonomy") or 0),
        "missing_terms": int(summary.get("missing_terms") or 0),
        "selected_count": len(files),
        "selected_ready": sum(1 for item in files if item.get("ready") is True),
    }


async def _save_recent_scan(scan_payload: dict) -> models.ExistingFileTagScanJob:
    summary = _recent_scan_summary(scan_payload)
    with SessionLocal() as db:
        job = models.ExistingFileTagScanJob(
            root_path=summary["root_path"],
            status="ready",
            summary=json.dumps(summary),
            files=json.dumps(scan_payload.get("files") or []),
            bindings=json.dumps(scan_payload.get("bindings") or {}),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


async def list_recent_scans() -> list[dict]:
    with SessionLocal() as db:
        jobs = db.query(models.ExistingFileTagScanJob).order_by(models.ExistingFileTagScanJob.id.desc()).all()
        return [
            {
                "id": str(job.id),
                "root_path": job.root_path,
                "status": job.status,
                "summary": json.loads(job.summary) if job.summary else {},
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
            for job in jobs
        ]


async def get_recent_scan(job_id: str) -> dict:
    if not job_id.isdigit():
        raise ValueError("Tag scan job not found")
    with SessionLocal() as db:
        job = db.get(models.ExistingFileTagScanJob, int(job_id))
        if job is None:
            raise ValueError("Tag scan job not found")
        payload = {
            "root_path": job.root_path,
            "bindings": json.loads(job.bindings) if job.bindings else {},
            "files": json.loads(job.files) if job.files else [],
            "summary": json.loads(job.summary) if job.summary else {},
        }
        return payload


async def _term_status(tags: dict[str, str], bindings: dict) -> dict[str, dict]:
    result = {}
    group_guid: str | None = None
    for key in ("group", "category", "vessel"):
        if key not in tags:
            continue
        label = tags[key]
        binding = bindings[key]
        if not binding["field"] or not binding["term_set_id"]:
            result[key] = {
                "label": label, "term_label": None, "guid": None, "resolved": False,
                "reason": f"'{key}' has no configured Managed Metadata field or term set",
            }
            continue
        # Group's terms are the term set's own roots; a Category is looked up
        # under its own Group, since the same category label can sit under
        # both ("Electrical" exists under Drawings and under Manuals).
        match = await term_store.find_term(
            binding["term_set_id"], term_id=None, label=label,
            max_depth=0 if key == "group" else None,
            ancestor_id=group_guid if key == "category" else None,
        )
        if key == "group" and match:
            group_guid = match["id"]
        result[key] = {
            "label": label,
            "term_label": match["label"] if match else None,
            "guid": match["id"] if match else None,
            "resolved": match is not None,
            "reason": (
                f"Resolved '{match['path']}' by {match['matched_by'].replace('_', ' ')}"
                if match else f"'{label}' is not a term in this term set"
            ),
        }
    return result


async def scan(root_path: str) -> dict:
    drive_id = await migration_common.get_destination_drive_id()
    root = await migration_common.resolve_folder_path(drive_id, root_path.strip("/"))
    destination_site = migration_common.get_destination_site()
    bindings = await _bindings(destination_site)
    files = await _walk_files(drive_id, root["id"], root_path.strip("/"))
    rows = []
    for file in files:
        tags, parse_error = _derive_tags(file["folder_path"], root_path)
        item_id, item_fields = await gf.get_list_item_id_and_fields(drive_id, file["id"])
        current = _current_tags(item_fields, bindings)
        terms = await _term_status(tags, bindings) if tags else {}
        # Group and Vessel are the mandatory tags; Category is optional — a
        # folder can legitimately sit under a category that was never given
        # a Term Store term (e.g. an ad-hoc "Other Manuals" sub-folder), and
        # that alone must not block tagging Group/Vessel or mark the file as
        # failed. See module docstring.
        group_ok = terms.get("group", {}).get("resolved", False)
        vessel_ok = terms.get("vessel", {}).get("resolved", False)
        category_ok = terms.get("category", {}).get("resolved", False)
        ready = bool(tags) and group_ok and vessel_ok
        rows.append({
            "id": file["id"], "list_item_id": item_id, "filename": file["name"],
            "folder_path": file["folder_path"], "current": current,
            "derived": tags, "terms": terms,
            "ready": ready,
            "category_skipped": ready and not category_ok,
            "error": parse_error,
        })
    result = {"root_path": root_path.strip("/"), "bindings": bindings, "files": rows,
              "summary": {"total_files": len(rows), "ready_files": sum(r["ready"] for r in rows),
                          "fully_tagged": sum(r["ready"] and not r["category_skipped"] for r in rows),
                          "category_skipped": sum(r["category_skipped"] for r in rows),
                          "missing_taxonomy": sum(not r["derived"] for r in rows),
                          "missing_terms": sum(bool(r["derived"]) and not r["ready"] for r in rows)}}
    await _save_recent_scan(result)
    return result


async def apply(root_path: str, file_ids: list[str]) -> dict:
    preview = await scan(root_path)
    selected = set(file_ids)
    destination_site = migration_common.get_destination_site()
    site_url = site_to_site_common.site_url(destination_site)
    drive_id = await migration_common.get_destination_drive_id()
    list_title = await gd.get_drive_list_title(drive_id)
    results = []
    for row in preview["files"]:
        if row["id"] not in selected:
            continue
        if not row["ready"]:
            missing = [k for k in ("group", "vessel") if not row["terms"].get(k, {}).get("resolved")]
            reason = row["error"] or "Mandatory term(s) not found: " + ", ".join(
                f"{k}='{(row.get('derived') or {}).get(k)}'" for k in missing
            )
            results.append({**row, "success": False, "failure_reason": reason})
            continue
        # Write the exact term the scan resolved, not the folder's wording
        # for it ("Drawing" resolved to the term "Drawings"), then read the
        # item back and compare term ids before calling it tagged. Only
        # actually-resolved fields are included — Category is left out
        # entirely (not sent as null/empty) when it has no Term Store term,
        # so its write/verify is simply never attempted and any existing
        # Category value on the item is left untouched.
        resolved = {
            key: {"label": term["term_label"], "id": term["guid"]}
            for key, term in row["terms"].items()
            if term["resolved"]
        }
        report = await migration_tagging.apply_term_tags(site_url, list_title, row["list_item_id"], resolved, preview["bindings"])
        verification = await migration_tagging.verify_term_tags_via_graph(drive_id, row["id"], resolved, preview["bindings"])
        success = all(entry["status"] == "applied" for entry in report) and all(entry["status"] == "verified" for entry in verification)
        results.append({**row, "success": success, "category_skipped": "category" not in resolved,
                        "write_report": report, "verification": verification,
                        "failure_reason": None if success else "Metadata write or verification failed"})
    return {"total_selected": len(selected), "successful": sum(r["success"] for r in results),
            "failed": sum(not r["success"] for r in results),
            "fully_tagged": sum(1 for r in results if r["success"] and not r.get("category_skipped")),
            "category_skipped": sum(1 for r in results if r["success"] and r.get("category_skipped")),
            "results": results}