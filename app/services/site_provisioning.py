"""Multi-site vessel folder provisioning service.

Provides centralized management and idempotent execution for creating
vessel DMS folder structures across one or more SharePoint Online sites.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote, urlparse
from typing import Any

from ..config import settings, Settings, set_session_site, clear_session_site, get_active_drive_id
from ..graph import drive as gd
from ..graph.client import graph
from .. import template
from ..db.base import SessionLocal
from ..db import models

logger = logging.getLogger(__name__)


def _clean_folder_path(path: str | None) -> str:
    return "/".join(part.strip() for part in (path or "").replace("\\", "/").split("/") if part.strip())


def _site_config_for_reference(reference: str) -> tuple[str, Settings]:
    """Resolve either a configured site key or a tenant Graph site ID."""
    key = (reference or "").strip().lower()
    try:
        return key, Settings.load_site_config(key)
    except (KeyError, ValueError):
        for candidate_key, info in Settings.discover_available_sites().items():
            try:
                config = Settings.load_site_config(candidate_key)
            except (KeyError, ValueError):
                continue
            if str(getattr(config, "sp_site_id", "") or "").lower() == key or str(info.get("site_id", "")).lower() == key:
                return candidate_key.lower(), config
    raise ValueError(f"Unknown SharePoint site: {reference}")


async def list_site_folders(site_key: str, path: str = "") -> dict[str, Any]:
    """List immediate folders in a configured site's default document drive."""
    if not (site_key or "").strip():
        raise ValueError("site_key is required")
    try:
        key, config = _site_config_for_reference(site_key)
        client = graph(site_name=key, site_config=config)
        drive_id = config.drive_id

        # Configuration can retain a drive ID from a previous library/site
        # provisioning. Resolve the current document library before browsing,
        # especially for Nissenkaiun External where the library was recreated.
        site_id = getattr(config, "sp_site_id", "") or getattr(config, "site_id", "") or ""
        if not site_id and getattr(config, "sharepoint_site_url", ""):
            site_url = urlparse(str(config.sharepoint_site_url))
            if site_url.hostname:
                site_ref = f"{site_url.hostname}:{site_url.path.rstrip('/')}"
                try:
                    site_meta = await client.get(f"/sites/{quote(site_ref, safe='')}")
                    site_id = site_meta.get("id", "")
                except Exception as site_error:
                    logger.warning("Could not resolve SharePoint site ID for %s: %s", key, site_error)
        if site_id:
            try:
                drives = await client.get(f"/sites/{quote(site_id, safe='')}/drives?$select=id,name,driveType")
                document_drives = [
                    item for item in drives.get("value", [])
                    if item.get("id") and (
                        item.get("driveType") == "documentLibrary"
                        or str(item.get("name", "")).strip().lower() in {"documents", "shared documents"}
                    )
                ]
                preferred = next(
                    (item for item in document_drives
                     if str(item.get("name", "")).strip().lower() in {"documents", "shared documents"}),
                    None,
                )
                configured = next((item for item in document_drives if item.get("id") == drive_id), None)
                if preferred is not None:
                    drive_id = preferred["id"]
                elif configured is None and document_drives:
                    drive_id = document_drives[0]["id"]
            except Exception as drive_error:
                logger.warning("Could not refresh document drive for site %s: %s", key, drive_error)
    except ValueError:
        key = site_key.strip()
        client = graph()
        drives = await client.get(f"/sites/{quote(key, safe='')}/drives?$select=id,name,driveType")
        drive = next((item for item in drives.get("value", []) if item.get("driveType") == "documentLibrary" or item.get("name", "").lower() == "documents"), None)
        drive_id = drive.get("id") if drive else None
    if not drive_id:
        raise ValueError(f"No document library is configured for site '{key}'")
    clean_path = _clean_folder_path(path)
    if clean_path:
        item = await client.get(f"/drives/{drive_id}/root:/{quote(clean_path, safe='/')}?$select=id,name,folder,parentReference")
        parent_id = item["id"]
    else:
        root = await client.get(f"/drives/{drive_id}/root?$select=id,name,folder")
        parent_id = root["id"]
    children = await client.get(
        f"/drives/{drive_id}/items/{parent_id}/children"
        "?$top=200&$select=id,name,folder,parentReference"
    )
    folders = [
        {
            "id": child["id"],
            "name": child["name"],
            "path": f"{clean_path}/{child['name']}".strip("/"),
        }
        for child in children.get("value", [])
        if child.get("id") and child.get("folder") is not None
    ]
    return {"site_key": key, "drive_id": drive_id, "path": clean_path, "folders": folders}


async def create_vessel_at_path(
    site_key: str,
    parent_path: str,
    vessel_name: str,
    subfolders: list[str] | None = None,
) -> dict[str, Any]:
    """Create or reuse a vessel folder and custom child folders in one site."""
    try:
        key, config = _site_config_for_reference(site_key)
        client = graph(site_name=key, site_config=config)
        drive_id = config.drive_id
    except ValueError:
        key = site_key.strip()
        client = graph()
        drives = await client.get(f"/sites/{quote(key, safe='')}/drives?$select=id,name,driveType")
        drive = next((item for item in drives.get("value", []) if item.get("driveType") == "documentLibrary" or item.get("name", "").lower() == "documents"), None)
        drive_id = drive.get("id") if drive else None
    if not drive_id:
        raise ValueError(f"No document library is configured for site '{key}'")

    async def ensure_child(current_id: str, name: str) -> dict[str, Any]:
        safe_name = " ".join((name or "").strip().split())
        if not safe_name or any(ch in safe_name for ch in '~"#%&*:<>?/\\{|}'):
            raise ValueError(f"Invalid SharePoint folder name: {name!r}")
        listing = await client.get(f"/drives/{drive_id}/items/{current_id}/children?$top=200&$select=id,name,folder")
        existing = next((x for x in listing.get("value", []) if x.get("folder") is not None and x.get("name", "").casefold() == safe_name.casefold()), None)
        if existing:
            return existing
        return await client.post(
            f"/drives/{drive_id}/items/{current_id}/children",
            json={"name": safe_name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )

    async def ensure_path(parent_relative: str) -> str:
        clean_parent = _clean_folder_path(parent_relative)
        if not clean_parent:
            root = await client.get(f"/drives/{drive_id}/root?$select=id,name,folder")
            return root["id"]

        current_id = (await client.get(f"/drives/{drive_id}/root?$select=id,name,folder"))["id"]
        for segment in clean_parent.split("/"):
            current_id = (await ensure_child(current_id, segment))["id"]
        return current_id

    parent = _clean_folder_path(parent_path)
    parent_id = await ensure_path(parent)
    vessel_folder = await ensure_child(parent_id, vessel_name)
    created_subfolders: list[str] = []
    for raw_name in subfolders or []:
        path_parts = [
            " ".join(part.strip().split())
            for part in (raw_name or "").replace("\\", "/").split("/")
            if part.strip()
        ]
        if not path_parts:
            raise ValueError(f"Invalid SharePoint folder name: {raw_name!r}")

        current_id = vessel_folder["id"]
        created_parts: list[str] = []
        for part in path_parts:
            child = await ensure_child(current_id, part)
            current_id = child["id"]
            created_parts.append(child["name"])
        created_subfolders.append("/".join(created_parts))
    full_path = "/".join(part for part in (parent, vessel_folder["name"]) if part)
    return {
        "site_key": key,
        "drive_id": drive_id,
        "vessel_folder_id": vessel_folder["id"],
        "vessel_folder_path": full_path,
        "subfolders": created_subfolders,
    }


def sanitize_name(name: str) -> str:
    """Sanitize folder names for SharePoint Online compatibility."""
    for ch in r'~"#%&*:<>?\/|':
        name = name.replace(ch, "_")
    return name.strip(". ")


def get_all_configured_sites(db, include_hidden: bool = False) -> list[dict[str, Any]]:
    """Retrieve all configured and registered sites with provisioning metadata.
    
    Includes sites from Settings (e.g. dev, local, prod) and any persisted in
    the site_configurations table.
    Filters out hidden sites by default unless include_hidden is True.
    """
    discovered = Settings.discover_available_sites()
    sites_dict: dict[str, dict[str, Any]] = {}

    # 1. Sites from Settings / .env
    for key, site_info in discovered.items():
        if site_info.get("configured"):
            try:
                conf = Settings.load_site_config(key)
                sites_dict[key] = {
                    "site_key": key,
                    "display_name": conf.sp_site_name or f"Vessel DMS ({key})",
                    "site_name": conf.sp_site_name or key,
                    "site_id": getattr(conf, "sp_site_id", "") or key,
                    "drive_id": conf.drive_id,
                    "web_url": conf.sharepoint_site_url or getattr(conf, "web_url", ""),
                    "is_available_for_provisioning": True,
                    "is_default_provisioning": (key == settings.active_site),
                    "is_hidden": False,
                }
            except Exception as e:
                logger.debug("Could not load settings config for %s: %s", key, e)

    # 2. Sites from database site_configurations
    from ..config import compute_sp_site_url
    db_records = db.query(models.SiteConfiguration).all()
    for rec in db_records:
        key = rec.site_key.lower()
        rec_is_hidden = bool(getattr(rec, "is_hidden", False))
        if key in sites_dict:
            # DB record takes precedence for user-configured toggle flags
            sites_dict[key]["is_available_for_provisioning"] = rec.is_available_for_provisioning
            sites_dict[key]["is_default_provisioning"] = rec.is_default_provisioning
            sites_dict[key]["is_hidden"] = rec_is_hidden
            if rec.display_name:
                sites_dict[key]["display_name"] = rec.display_name
            if rec.drive_id:
                sites_dict[key]["drive_id"] = rec.drive_id
            if rec.site_id:
                sites_dict[key]["site_id"] = rec.site_id
            computed = compute_sp_site_url(key, rec.site_name or rec.display_name)
            if "/sites/" in computed or not sites_dict[key].get("web_url"):
                sites_dict[key]["web_url"] = computed
        else:
            sites_dict[key] = {
                "site_key": key,
                "display_name": rec.display_name or rec.site_name or key,
                "site_name": rec.site_name or key,
                "site_id": rec.site_id,
                "drive_id": rec.drive_id,
                "web_url": compute_sp_site_url(key, rec.site_name or rec.display_name),
                "is_available_for_provisioning": rec.is_available_for_provisioning,
                "is_default_provisioning": rec.is_default_provisioning,
                "is_hidden": rec_is_hidden,
            }

    all_sites = list(sites_dict.values())
    if not include_hidden:
        return [s for s in all_sites if not s.get("is_hidden")]
    return all_sites


def get_available_provisioning_sites(db) -> dict[str, dict[str, Any]]:
    """Return dictionary of sites that are currently available for provisioning."""
    all_sites = get_all_configured_sites(db)
    return {s["site_key"]: s for s in all_sites if s.get("is_available_for_provisioning", True)}


async def resolve_site_drive(site_key_or_id: str, db=None) -> tuple[str, str, str]:
    """Resolve a site key or site ID to (site_key, drive_id, display_name).
    
    Raises ValueError if the drive cannot be resolved.
    """
    key = site_key_or_id.strip().lower()
    
    # 1. Check DB records
    if db is not None:
        rec = db.query(models.SiteConfiguration).filter(
            (models.SiteConfiguration.site_key == key) |
            (models.SiteConfiguration.site_id == site_key_or_id)
        ).first()
        if rec and rec.drive_id:
            return rec.site_key, rec.drive_id, rec.display_name

    # 2. Check Settings config
    try:
        conf = Settings.load_site_config(key)
        if conf and conf.drive_id:
            return key, conf.drive_id, conf.sp_site_name or key
    except Exception:
        pass

    # 3. Fallback: if it matches the active site
    if key == (settings.active_site or "").lower():
        return key, settings.drive_id, settings.sp_site_name or key

    # 4. Check if site_key_or_id is a SharePoint site ID format (domain,uuid,uuid)
    if "," in site_key_or_id:
        try:
            client = graph()
            drive_data = await client.get(f"/sites/{site_key_or_id}/drive")
            if drive_data and drive_data.get("id"):
                return key, drive_data["id"], site_key_or_id
        except Exception as e:
            logger.warning("Failed to resolve drive for SharePoint site ID %s: %s", site_key_or_id, e)

    raise ValueError(f"Could not resolve document library drive for site '{site_key_or_id}'")


async def _provision_subtree_for_drive(
    drive_id: str,
    root_id: str,
    root_path: str,
    specs: list[dict],
    is_active_site: bool = False,
    vessel_id: int | None = None,
) -> None:
    """Provision a subtree level-by-level on the specified drive using Graph JSON $batch."""
    queue: list[tuple[str, str, list]] = [(root_id, root_path, specs)]

    while queue:
        pending: list[tuple[str, str, dict]] = []
        for parent_id, parent_path, spec_list in queue:
            for spec in spec_list:
                pending.append((parent_id, parent_path, spec))

        item_id_map: dict[str, str] = {}
        uncached: list[tuple[str, str, dict]] = []

        if is_active_site:
            all_paths = [f"{pp}/{sanitize_name(s['name'])}" for _, pp, s in pending]
            with SessionLocal() as db:
                cached_map: dict[str, str] = {
                    row.path: row.drive_item_id
                    for row in db.query(models.Folder).filter(
                        models.Folder.path.in_(all_paths),
                        models.Folder.site_id == drive_id,
                    ).all()
                }
            item_id_map.update(cached_map)
            uncached = [
                (pid, pp, spec)
                for pid, pp, spec in pending
                if f"{pp}/{sanitize_name(spec['name'])}" not in cached_map
            ]
        else:
            uncached = pending

        if uncached:
            created = await gd.batch_create_folders(
                drive_id, [(pid, sanitize_name(spec["name"])) for pid, pp, spec in uncached]
            )
            rows: list[tuple[str, str, str, str, bool]] = []
            for parent_id, parent_path, spec in uncached:
                safe_name = sanitize_name(spec["name"])
                path = f"{parent_path}/{safe_name}"
                item = created.get((parent_id, safe_name))
                if item:
                    item_id_map[path] = item["id"]
                    if is_active_site:
                        rows.append((
                            path, safe_name, spec["kind"],
                            item["id"], spec["kind"] == "month_driven",
                        ))
            if is_active_site and rows:
                with SessionLocal() as db:
                    for path, name, kind, item_id, is_md in rows:
                        row = db.query(models.Folder).filter_by(path=path, site_id=drive_id).one_or_none()
                        if row is None:
                            row = models.Folder(
                                path=path, name=name, kind=kind,
                                drive_item_id=item_id, month_driven=is_md,
                                vessel_id=vessel_id, site_id=drive_id,
                            )
                            db.add(row)
                        else:
                            row.drive_item_id = item_id
                            row.vessel_id = vessel_id
                    db.commit()

        next_queue: list[tuple[str, str, list]] = []
        for parent_id, parent_path, spec in pending:
            path = f"{parent_path}/{sanitize_name(spec['name'])}"
            item_id = item_id_map.get(path)
            if item_id and spec.get("kind") != "month_driven":
                children = spec.get("children", [])
                if children:
                    next_queue.append((item_id, path, children))

        queue = next_queue
        await asyncio.sleep(0.05)


async def provision_vessel_to_drive(
    vessel_name: str,
    vessel_id: int,
    drive_id: str,
    site_key: str,
    is_active_site: bool = False,
) -> dict[str, Any]:
    """Idempotently provisions a vessel's folder on a specific drive.

    Part C (2026-09-21): this no longer creates the MAIN_FOLDERS/department
    subtree at all. A vessel is a single flat root folder at the drive
    root, with no automatic internal structure — no main-department
    nesting, no SHIP_TEMPLATE subtree. Any subfolder structure a vessel
    needs is created manually afterward (Phase 3's folder creation flow),
    not auto-built here. This is what this function does now:

      1. Ensure one root folder named `vessel_name` directly at the
         drive's root.
      2. Cache that folder's Folder row (site-scoped) when provisioning
         the site currently being written to.

    No root folders (e.g. "Kaizen - Knowledge Bank" or the department
    folders) are auto-created anywhere; ensure_base_structure() is a no-op.

    Returns:
      {"site": site_key, "success": True, "error": None} or {"site": site_key, "success": False, "error": str}
    """
    provision_context = f"provision:{site_key}:{vessel_id}"
    context_token = settings.set_current_session(provision_context)
    set_session_site(provision_context, site_key)
    try:
        root_id = await gd.get_root_item_id(drive_id)

        vessel_folder = await gd.ensure_folder(drive_id, root_id, vessel_name)
        vessel_folder_id = vessel_folder["id"]

        if is_active_site:
            with SessionLocal() as db:
                row = db.query(models.Folder).filter_by(path=vessel_name, site_id=drive_id).one_or_none()
                if row is None:
                    row = models.Folder(
                        path=vessel_name, name=vessel_name, kind="ship",
                        drive_item_id=vessel_folder_id, month_driven=False,
                        vessel_id=vessel_id, site_id=drive_id,
                    )
                    db.add(row)
                    db.commit()
                elif row.drive_item_id != vessel_folder_id or row.vessel_id != vessel_id:
                    row.drive_item_id = vessel_folder_id
                    row.vessel_id = vessel_id
                    db.commit()

        return {"site": site_key, "success": True, "error": None}
    except Exception as e:
        logger.exception("Failed provisioning vessel '%s' on site '%s' (drive=%s): %s", vessel_name, site_key, drive_id, e)
        return {"site": site_key, "success": False, "error": str(e)}
    finally:
        settings.reset_current_session(context_token)
        clear_session_site(provision_context)


async def provision_vessel_multi_site(
    vessel_id: int,
    vessel_name: str,
    target_site_keys: list[str],
) -> dict[str, Any]:
    """Execute multi-site folder provisioning for a vessel.
    
    Guards:
      1. Silently skips deactivated sites (is_available_for_provisioning == False).
      2. Diff-based retry idempotency: skips sites already successfully provisioned in vessel.provisioned_site_ids.
      3. Skip-and-report: executes remaining sites concurrently; reports successes and failures per site.
      4. Atomically updates vessel.provisioned_site_ids with newly succeeded sites.
    """
    with SessionLocal() as db:
        vessel = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
        if not vessel:
            return {"error": f"Vessel with id {vessel_id} not found", "results": {}}

        available_sites = get_available_provisioning_sites(db)
        existing_provisioned = set(vessel.provisioned_site_ids or []) if vessel.is_provisioned else set()
        active_site = (settings.active_site or "dev").lower()

    # 1. Filter against available sites (deactivated site guard)
    valid_targets = [s.lower() for s in target_site_keys if s.lower() in available_sites]

    # 2. Diff for retry idempotency: only attempt sites not already provisioned
    sites_to_attempt = [s for s in valid_targets if s not in existing_provisioned]

    results: dict[str, dict[str, Any]] = {}
    for s in valid_targets:
        if s in existing_provisioned:
            results[s] = {"status": "success", "message": "Already provisioned"}

    if not sites_to_attempt:
        return {
            "vessel_id": vessel_id,
            "vessel_name": vessel_name,
            "provisioned_sites": list(existing_provisioned),
            "results": results,
        }

    # 3. Resolve drives for sites to attempt
    tasks = []
    attempt_keys = []
    with SessionLocal() as db:
        for s_key in sites_to_attempt:
            try:
                resolved_key, drive_id, disp_name = await resolve_site_drive(s_key, db=db)
                # NOTE: `is_active_site` here does NOT mean "this is
                # settings.active_site" (that used to matter when Folder
                # rows weren't scoped by site_id and only the one active
                # drive's cache was safe to write). Now that every Folder
                # write/lookup in provision_vessel_to_drive is scoped by
                # site_id=drive_id, it's always correct to persist the
                # folders-table cache for whichever site we're actually
                # provisioning — so this is intentionally always True, not
                # `is_active` (kept below for logging/clarity only).
                is_active = (resolved_key == active_site)
                tasks.append(provision_vessel_to_drive(
                    vessel_name=vessel_name,
                    vessel_id=vessel_id,
                    drive_id=drive_id,
                    site_key=resolved_key,
                    is_active_site=True,
                ))
                attempt_keys.append(resolved_key)
            except Exception as res_err:
                logger.warning("Could not resolve drive for %s: %s", s_key, res_err)
                results[s_key] = {"status": "failed", "error": str(res_err)}

    # 4. Concurrently provision across all candidate drives
    if tasks:
        raw_outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        newly_succeeded: list[str] = []

        for s_key, outcome in zip(attempt_keys, raw_outcomes):
            if isinstance(outcome, Exception):
                results[s_key] = {"status": "failed", "error": str(outcome)}
            elif isinstance(outcome, dict):
                if outcome.get("success"):
                    results[s_key] = {"status": "success", "error": None}
                    newly_succeeded.append(s_key)
                else:
                    results[s_key] = {"status": "failed", "error": outcome.get("error")}
            else:
                results[s_key] = {"status": "failed", "error": "Unknown error"}

        # 5. Persist newly succeeded sites into DB
        if newly_succeeded:
            with SessionLocal() as db:
                v = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
                if v:
                    curr = list(v.provisioned_site_ids or [])
                    for ns in newly_succeeded:
                        if ns not in curr:
                            curr.append(ns)
                    v.provisioned_site_ids = curr
                    v.is_provisioned = True
                    db.commit()
                    existing_provisioned = set(curr)

    return {
        "vessel_id": vessel_id,
        "vessel_name": vessel_name,
        "provisioned_sites": list(existing_provisioned),
        "results": results,
    }
