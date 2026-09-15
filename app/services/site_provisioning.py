"""Multi-site vessel folder provisioning service.

Provides centralized management and idempotent execution for creating
vessel DMS folder structures across one or more SharePoint Online sites.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import settings, Settings
from ..graph import drive as gd
from ..graph.client import graph
from .. import template
from ..db.base import SessionLocal
from ..db import models

logger = logging.getLogger(__name__)


def sanitize_name(name: str) -> str:
    """Sanitize folder names for SharePoint Online compatibility."""
    for ch in r'~"#%&*:<>?\/|':
        name = name.replace(ch, "_")
    return name.strip(". ")


def get_all_configured_sites(db) -> list[dict[str, Any]]:
    """Retrieve all configured and registered sites with provisioning metadata.
    
    Includes sites from Settings (e.g. dev, local, prod) and any persisted in
    the site_configurations table.
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
                    "is_available_for_provisioning": True,
                    "is_default_provisioning": (key == settings.active_site),
                }
            except Exception as e:
                logger.debug("Could not load settings config for %s: %s", key, e)

    # 2. Sites from database site_configurations
    db_records = db.query(models.SiteConfiguration).all()
    for rec in db_records:
        key = rec.site_key.lower()
        if key in sites_dict:
            # DB record takes precedence for user-configured toggle flags
            sites_dict[key]["is_available_for_provisioning"] = rec.is_available_for_provisioning
            sites_dict[key]["is_default_provisioning"] = rec.is_default_provisioning
            if rec.display_name:
                sites_dict[key]["display_name"] = rec.display_name
            if rec.drive_id:
                sites_dict[key]["drive_id"] = rec.drive_id
            if rec.site_id:
                sites_dict[key]["site_id"] = rec.site_id
        else:
            sites_dict[key] = {
                "site_key": key,
                "display_name": rec.display_name or rec.site_name or key,
                "site_name": rec.site_name or key,
                "site_id": rec.site_id,
                "drive_id": rec.drive_id,
                "is_available_for_provisioning": rec.is_available_for_provisioning,
                "is_default_provisioning": rec.is_default_provisioning,
            }

    return list(sites_dict.values())


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
                        models.Folder.path.in_(all_paths)
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
                        row = db.query(models.Folder).filter_by(path=path).one_or_none()
                        if row is None:
                            row = models.Folder(
                                path=path, name=name, kind=kind,
                                drive_item_id=item_id, month_driven=is_md,
                                vessel_id=vessel_id,
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
    """Idempotently provisions the entire DMS folder hierarchy for a vessel on a specific drive.
    
    Creates:
      1. Main department folders: Technical & Crewing, Commercial & Chartering, Insurance
      2. Vessel root folder under each main folder
      3. Full template subtrees under each vessel root
    
    Returns:
      {"site": site_key, "success": True, "error": None} or {"site": site_key, "success": False, "error": str}
    """
    try:
        root_id = await gd.get_root_item_id(drive_id)
        mains_to_provision = [
            m for m in template.MAIN_FOLDERS
            if m not in template.FLAT_MAIN_FOLDERS
        ]

        for main in mains_to_provision:
            # 1. Ensure main folder
            main_folder = await gd.ensure_folder(drive_id, root_id, main)
            main_id = main_folder["id"]

            if is_active_site:
                with SessionLocal() as db:
                    main_row = db.query(models.Folder).filter_by(path=main).one_or_none()
                    if main_row is None:
                        main_row = models.Folder(
                            path=main, name=main, kind="main",
                            drive_item_id=main_id, month_driven=False,
                        )
                        db.add(main_row)
                        db.commit()

            # 2. Ensure ship root folder: {main}/{vessel_name}
            ship_folder = await gd.ensure_folder(drive_id, main_id, vessel_name)
            ship_id = ship_folder["id"]
            ship_root_path = f"{main}/{vessel_name}"

            if is_active_site:
                with SessionLocal() as db:
                    ship_row = db.query(models.Folder).filter_by(path=ship_root_path).one_or_none()
                    if ship_row is None:
                        ship_row = models.Folder(
                            path=ship_root_path, name=vessel_name, kind="ship",
                            drive_item_id=ship_id, month_driven=False,
                            vessel_id=vessel_id,
                        )
                        db.add(ship_row)
                        db.commit()

            # 3. Ensure subtrees
            sub_specs = template.SHIP_TEMPLATE.get(main, [])
            if sub_specs:
                await _provision_subtree_for_drive(
                    drive_id, ship_id, ship_root_path, sub_specs,
                    is_active_site=is_active_site, vessel_id=vessel_id,
                )

        return {"site": site_key, "success": True, "error": None}
    except Exception as e:
        logger.exception("Failed provisioning vessel '%s' on site '%s' (drive=%s): %s", vessel_name, site_key, drive_id, e)
        return {"site": site_key, "success": False, "error": str(e)}


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
        existing_provisioned = set(vessel.provisioned_site_ids or [])
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
                is_active = (resolved_key == active_site)
                tasks.append(provision_vessel_to_drive(
                    vessel_name=vessel_name,
                    vessel_id=vessel_id,
                    drive_id=drive_id,
                    site_key=resolved_key,
                    is_active_site=is_active,
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
