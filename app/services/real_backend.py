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
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote
from sqlalchemy import func, or_ as sa_or

from .. import template
from ..config import settings
from ..db.base import SessionLocal
from ..db import models
from ..graph import drive as gd
from ..graph.client import GraphError, graph
from ..ocr.dates import month_label
from ..ocr.drawing_category import classify_drawing_category
from ..ocr.extract import detect_document_month, extract_text
from .classify import classify
from .errors import BadRequest, Conflict, NotFound, InternalServerError
from .normalize import normalize_vessel_name
from .notify import notify_email

import logging
log = logging.getLogger(__name__)

STAGING_FOLDER_NAME = "Pending Approvals"

_DASHBOARD_STATS_CACHE = {"data": None, "timestamp": 0}
CACHE_TTL_DASHBOARD_STATS = 15  # 15 seconds


def invalidate_dashboard_stats_cache():
    global _DASHBOARD_STATS_CACHE
    _DASHBOARD_STATS_CACHE = {"data": None, "timestamp": 0}



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
        self._staging_id = None
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

    async def _staging_folder(self, drive_id: str) -> str:
        """Idempotent "Pending Approvals" holding area, outside the ship/main
        folder hierarchy — not tracked in the `folders` cache since it isn't
        part of the document template and shouldn't appear in the explorer."""
        if not self._staging_id:
            root = await gd.get_root_item_id(drive_id)
            item = await gd.ensure_folder(drive_id, root, STAGING_FOLDER_NAME)
            self._staging_id = item["id"]
        return self._staging_id

    async def _stage_file(self, drive_id, filename, content, content_type) -> str:
        staging_id = await self._staging_folder(drive_id)
        staged_name = f"{uuid.uuid4().hex[:12]}__{filename}"
        item = await gd.upload_file(drive_id, staging_id, staged_name, content, content_type)
        return item["id"]

    async def _resolve_reject_target(self, drive_id, destination_folder_id) -> str:
        """The sibling fallback folder for a rejected upload — found inside
        the same parent as the originally-selected destination. Reuses
        whichever fallback-named leaf already exists there ("To be
        Classified", "Other Drawings", or "Other Manuals"); if the
        destination itself already is one of those, reuse it as-is."""
        item = await gd.get_item(drive_id, destination_folder_id)
        if item.get("name", "").strip().lower() in template.FALLBACK_LEAF_NAMES:
            return destination_folder_id
        parent_id = (item.get("parentReference") or {}).get("id")
        if not parent_id:
            return destination_folder_id  # fallback: reuse destination
        siblings = await gd.list_children(drive_id, parent_id)
        existing = next(
            (s for s in siblings if s.get("name", "").strip().lower() in template.FALLBACK_LEAF_NAMES),
            None,
        )
        if existing:
            return existing["id"]
        tbc = await gd.ensure_folder(drive_id, parent_id, "To be Classified")
        parent_path = await self._folder_path(drive_id, parent_id)
        with SessionLocal() as db:
            self._upsert(
                db, f"{parent_path}/To be Classified", "To be Classified", "leaf",
                tbc["id"], False, None,
            )
            db.commit()
        return tbc["id"]

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
                if parts and any(m.lower() == parts[0].lower() for m in template.ALL_MAIN_FOLDERS):
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
        if parts and any(m.lower() == parts[0].lower() for m in template.ALL_MAIN_FOLDERS):
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
    def _derive_dms_tags(folder_path: str, vessel_name: str | None) -> dict:
        """Derive Group, Category, SubCategory column values from a folder path.

        The DMS hierarchy is:
          {Main Folder}/{Vessel Name}/{Category}/{SubCategory}      (vessel folders)
          Common for all ships/{Main Folder}/{Category}/{SubCategory} (common folders)
          {Main Folder}/{Category}/{SubCategory}                    (flat/Kaizen)

        Returns a dict with keys matching the SharePoint internal column names:
          DMS_Group, DMS_Category, DMS_SubCategory
        These must match the actual internal names of the site columns you create
        in the SharePoint communication site or SPE container library.
        """
        parts = [p.strip() for p in folder_path.split("/") if p.strip()]
        group = ""
        category = ""
        sub_category = ""
        inferred_vessel = ""

        if not parts:
            return {}

        main_folders_lower = {m.lower() for m in template.MAIN_FOLDERS}
        flat_main_folders_lower = {m.lower() for m in template.FLAT_MAIN_FOLDERS}

        common_roots = {
            "common for all ships",
            "common (not ship specific)",
            "common for all vessels",
        }

        drawing_categories = {
            "basic", "electrical", "hull", "machinery", "safety", "archive"
        }
        manual_categories = {
            "automation", "auxiliary engine", "boiler", "bridge equipments", "cargo",
            "deck machinery", "electrical", "main engine", "pollution",
            "propulsion", "refrigeration", "safety", "shafting", "steering gear", "thrusters",
            "to be classified",
        }

        def _norm(s: str) -> str:
            return (s or "").strip().lower()

        def _normalize_group(label: str) -> str:
            low = _norm(label)
            if low in {"drawing", "drawings"}:
                return "Drawings"
            if low in {"manual", "manuals"}:
                return "Manuals"
            return label

        # Prefer taxonomy parse under the canonical segment: "Drawings and Manuals"
        dm_idx = next((i for i, p in enumerate(parts) if _norm(p) == "drawings and manuals"), -1)
        if dm_idx >= 0:
            t1 = parts[dm_idx + 1] if len(parts) > dm_idx + 1 else ""
            t2 = parts[dm_idx + 2] if len(parts) > dm_idx + 2 else ""

            # Pattern: .../Drawings and Manuals/{Group}/{Category}
            if _norm(t1) in {"drawing", "drawings", "manual", "manuals"}:
                group = _normalize_group(t1)
                category = t2
            # Pattern: .../Drawings and Manuals/To be Classified
            elif _norm(t1) == "to be classified":
                group = "Manuals"
                category = "To be Classified"
            # Pattern: .../Drawings and Manuals/{Category}/{Sub-Category}
            else:
                cat1 = _norm(t1)
                if cat1 in drawing_categories:
                    group = "Drawings"
                elif cat1 in manual_categories:
                    group = "Manuals"
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
                if seg3 in drawing_categories or seg3 in manual_categories or seg3 == "drawings and manuals":
                    offset = 2
            if len(parts) > offset:
                category = parts[offset]
            if len(parts) > offset + 1:
                sub_category = parts[offset + 1]
            if _norm(category) == "to be classified":
                group = "Manuals"
                sub_category = sub_category or "To be Classified"
            cat1 = _norm(category)
            if cat1 in drawing_categories:
                group = "Drawings"
            elif cat1 in manual_categories:
                group = "Manuals"
            if len(parts) > 1:
                inferred_vessel = parts[1]
        elif _norm(parts[0]) in flat_main_folders_lower:
            if len(parts) >= 2:
                category = parts[1]
            if len(parts) >= 3:
                sub_category = parts[2]
            cat1 = _norm(category)
            if cat1 in drawing_categories:
                group = "Drawings"
            elif cat1 in manual_categories:
                group = "Manuals"

        # Fallback: use whatever we have
        else:
            group = parts[0] if parts else ""
            category = parts[1] if len(parts) > 1 else ""
            sub_category = parts[2] if len(parts) > 2 else ""

        # Final normalization guardrails:
        # - Group must be Drawings/Manuals when inferable
        # - Category must not be a main folder or vessel name
        main_folder_values = {_norm(x) for x in (template.MAIN_FOLDERS + template.FLAT_MAIN_FOLDERS)}
        vessel_norm = _norm(vessel_name or inferred_vessel)
        if _norm(group) in main_folder_values or (vessel_norm and _norm(group) == vessel_norm):
            group = ""
        if _norm(category) in main_folder_values or (vessel_norm and _norm(category) == vessel_norm):
            category = ""

        cat1 = _norm(category)
        if not group:
            if cat1 in drawing_categories:
                group = "Drawings"
            elif cat1 in manual_categories or cat1 == "to be classified":
                group = "Manuals"
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
        if vname and vname.lower() not in {"common for all ships", "common for all vessels", "kaizen - knowledge bank"}:
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
                    ai_group = (
                        "Drawings" if raw_group.lower().startswith("draw")
                        else "Manuals" if raw_group.lower().startswith("man")
                        else ""
                    )
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
        """Gate a mutating action on admin status.

        SPE Admins: run `execute()` immediately and record it as a completed
        activity notification. Everyone else: create a pending approval and
        defer `execute()`-equivalent work until an admin approves it (see
        approve_request's action_type branching below).
        """
        if self._is_admin(requesting_email):
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
        approval = await self._create_pending_action(
            action_type=action_type,
            requesting_email=requesting_email or "",
            requesting_name=requesting_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=target_id,
            target_description=target_description,
            payload=payload,
            changes=changes,
            message=pending_message,
        )
        return {"status": "pending", "approval_id": approval["id"], "message": pending_message}

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

    async def _create_pending_action(
        self, *, action_type, requesting_email, requesting_name=None,
        department=None, vessel_id=None, vessel_name=None, target_id=None,
        target_description=None, payload=None, changes=None, message=None,
    ):
        with SessionLocal() as db:
            row = models.ApprovalRequest(
                uploaded_by_email=requesting_email,
                uploaded_by_name=requesting_name or "",
                status="pending",
                entry_kind="approval",
                action_type=action_type,
                department=department,
                vessel_id=int(vessel_id) if vessel_id else None,
                vessel_name=vessel_name,
                target_id=target_id,
                target_description=target_description,
                payload_json=json.dumps(payload or {}),
                changes_json=json.dumps(changes or []),
                message=message,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._approval_public(row)

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
        label = month_label(year, month)
        month_item = await gd.ensure_folder(drive_id, md_id, label)
        mpath = f"{md_path}/{label}"
        self._upsert(db, mpath, label, "month", month_item["id"], False, vessel_id)
        for cat in md_spec.get("month_children", []):
            cat_name = sanitize_folder_name(cat["name"])
            cat_item = await gd.ensure_folder(drive_id, month_item["id"], cat_name)
            self._upsert(
                db, f"{mpath}/{cat_name}", cat_name, "leaf", cat_item["id"], False, vessel_id
            )
        return month_item

    async def _remove_legacy_kaizen_folders(self, drive_id, specific_id, common_id):
        """Remove Kaizen folders left in vessel-specific locations.

        Kaizen is a single Documents-root folder. Older provisioning placed
        copies under Common for all ships and individual vessel folders.
        """
        kaizen_name = template.FLAT_MAIN_FOLDERS[0]
        misplaced: list[tuple[str, str]] = []

        common_kaizen = await gd.find_child(drive_id, common_id, kaizen_name)
        if common_kaizen:
            misplaced.append((f"{template.VESSELS_ROOT}/{template.COMMON_SHIPS_ROOT}/{kaizen_name}", common_kaizen["id"]))

        for vessel in await gd.list_children(drive_id, specific_id):
            if not vessel.get("folder"):
                continue
            vessel_kaizen = await gd.find_child(drive_id, vessel["id"], kaizen_name)
            if vessel_kaizen:
                misplaced.append((
                    f"{template.VESSELS_ROOT}/{template.SPECIFIC_VESSELS_ROOT}/{vessel['name']}/{kaizen_name}",
                    vessel_kaizen["id"],
                ))

        for path, item_id in misplaced:
            await gd.delete_item(drive_id, item_id)
            with SessionLocal() as db:
                db.query(models.Folder).filter(
                    (models.Folder.path == path) | models.Folder.path.like(f"{path}/%")
                ).delete(synchronize_session=False)
                db.commit()

    async def ensure_base_structure(self):
        if self._base_ready:
            return
        drive_id = await self._drive()

        with SessionLocal() as db:
            existing_mains = {r.path: r for r in db.query(models.Folder).filter_by(kind="main")}
            existing_commons = {r.path: r for r in db.query(models.Folder).filter_by(kind="common")}

        kaizen_name = template.FLAT_MAIN_FOLDERS[0]
        missing_mains = [m for m in template.MAIN_FOLDERS if m not in existing_mains]
        missing_kaizen = kaizen_name not in existing_mains
        missing_commons = [
            m for m in template.MAIN_FOLDERS
            if f"{m}/{template.COMMON_SHIPS_ROOT}" not in existing_commons
        ]

        if not missing_mains and not missing_kaizen and not missing_commons:
            self._base_ready = True
            return

        root = await gd.get_root_item_id(drive_id)

        async def valid_cached_folder(row, expected_name):
            if row is None or not row.drive_item_id:
                return None
            try:
                item = await gd.get_item(
                    drive_id, row.drive_item_id, select="id,name,folder"
                )
            except GraphError:
                return None
            if item.get("name", "").lower() != expected_name.lower() or item.get("folder") is None:
                return None
            return item["id"]

        # 1. Ensure Kaizen - Knowledge Bank directly at Documents root
        kaizen_row = existing_mains.get(kaizen_name)
        kaizen_id = await valid_cached_folder(kaizen_row, kaizen_name)
        if kaizen_id is None:
            item = await gd.ensure_folder(drive_id, root, kaizen_name)
            kaizen_id = item["id"]
            with SessionLocal() as db:
                self._upsert(db, kaizen_name, kaizen_name, "main", kaizen_id, False, None)
                db.commit()
            await self._provision_subtree_batched(
                drive_id, kaizen_id, kaizen_name, template.FLAT_TEMPLATE[kaizen_name], None
            )

        # 2. Ensure each Main Folder at Documents root & its common folder inside each
        for main in template.MAIN_FOLDERS:
            main_row = existing_mains.get(main)
            main_id = await valid_cached_folder(main_row, main)
            if main_id is None:
                item = await gd.ensure_folder(drive_id, root, main)
                main_id = item["id"]
                with SessionLocal() as db:
                    self._upsert(db, main, main, "main", main_id, False, None)
                    db.commit()

            # Insurance uses its own common folder name; all others use COMMON_SHIPS_ROOT
            common_folder_name = (
                template.INSURANCE_COMMON_FOLDER_NAME
                if main == "Insurance"
                else template.COMMON_SHIPS_ROOT
            )
            common_path = f"{main}/{common_folder_name}"
            common_row = existing_commons.get(common_path)
            common_id = await valid_cached_folder(common_row, common_folder_name)
            if common_id is None:
                c_item = await gd.ensure_folder(drive_id, main_id, common_folder_name)
                common_id = c_item["id"]
                with SessionLocal() as db:
                    self._upsert(db, common_path, common_folder_name, "common", common_id, False, None)
                    db.commit()
                if main in template.COMMON_TEMPLATE and template.COMMON_TEMPLATE[main]:
                    await self._provision_subtree_batched(
                        drive_id, common_id, common_path, template.COMMON_TEMPLATE[main], None
                    )

        self._base_ready = True
    # -------------------------------------------------------------- vessels
    async def list_vessels(self):
        with SessionLocal() as db:
            rows = db.query(models.Vessel).order_by(models.Vessel.created_at.desc().nulls_last(), models.Vessel.id.desc()).all()

            return [
                {
                    "id": str(v.id),
                    "name": v.name,
                    "imo": v.imo,
                    "shipyard": v.shipyard,
                    "hull_number": v.hull_number,
                    "vessel_type": v.vessel_type,
                    "is_provisioned": v.is_provisioned,
                    "restored_at": v.restored_at.isoformat() if v.restored_at else None,
                    "status": "Active",
                }
                for v in rows
            ]

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
    ):
        """Creating a vessel never requires approval — for anyone, admin or
        not. It always executes immediately and is always recorded as a
        completed activity entry for audit purposes.

        Tries the pre-provisioned pool first (claim + rename a folder tree
        that already exists — sub-second); falls back to full from-scratch
        provisioning (the original ~2.4 minute path) only when the pool is
        empty or the claimed slot fails to link cleanly.
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
        slot = self._claim_pool_slot()
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
            log.info("[create_vessel] Creating vessel DB record immediately for '%s'", clean_name)
            creation_method = "fast_db_async"
            with SessionLocal() as db:
                v_db = models.Vessel(
                    name=clean_name,
                    imo=clean_imo,
                    shipyard=shipyard,
                    hull_number=hull_number,
                    vessel_type=vessel_type,
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
            }
            # Background task for SPO folder creation (non-blocking)
            await self.start_vessel_provisioning(str(vessel_id_num))

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
        """Rename a claimed pool slot's ship folders to the real vessel
        name, create the Vessel row, and re-point every folder under it
        (ship folder + full subtree) to that vessel — the fast path.
        Raises on any failure so create_vessel can release the slot and
        fall back to full provisioning rather than leave a half-linked
        vessel behind.
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
        with SessionLocal() as db:
            vessel = models.Vessel(
                name=name, imo=imo, shipyard=payload.get("shipyard"),
                hull_number=payload.get("hull_number"), vessel_type=payload.get("vessel_type"),
                is_provisioned=True,
            )
            db.add(vessel)
            db.flush()
            vessel_id, vname, vimo = vessel.id, vessel.name, vessel.imo
            vshipyard, vhull, vtype = vessel.shipyard, vessel.hull_number, vessel.vessel_type

            for ship_info in ship_data:
                old_prefix = ship_info["path"]
                new_prefix = f"{ship_info['path'].rsplit('/', 1)[0]}/{name}"
                rows = db.query(models.Folder).filter(
                    sa_or(
                        models.Folder.path == old_prefix,
                        models.Folder.path.like(f"{old_prefix}/%"),
                    )
                ).all()
                for folder in rows:
                    folder.vessel_id = vessel_id
                    if folder.path == old_prefix:
                        folder.name = name
                        folder.path = new_prefix
                    else:
                        folder.path = new_prefix + folder.path[len(old_prefix):]
            db.commit()
        log.info("[_link_claimed_slot] DB vessel+path rewrite: %.3fs", _time.monotonic() - _t3)

        return {
            "id": str(vessel_id), "name": vname, "imo": vimo,
            "shipyard": vshipyard, "hull_number": vhull, "vessel_type": vtype,
        }


    async def _build_pool_slot(self) -> int:
        """Build one new pool slot from scratch: a full vessel folder tree
        under a unique placeholder name (never linked to a vessel) inside each
        main folder.
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
        created_ship_roots: list[tuple[str, str]] = []

        try:
            mains_to_provision = [
                m for m in template.MAIN_FOLDERS if m not in template.FLAT_MAIN_FOLDERS
            ]
            for main in mains_to_provision:
                root = await gd.get_root_item_id(drive_id)
                with SessionLocal() as db:
                    main_row = db.query(models.Folder).filter_by(path=main).one_or_none()
                    cached_main_id = main_row.drive_item_id if main_row else None

                # A drive/container change can leave a valid-looking Folder row
                # pointing at an item from the previous drive. Validate the
                # cached ID before using it as a parent for Graph writes.
                main_item = None
                if cached_main_id:
                    try:
                        candidate = await gd.get_item(
                            drive_id, cached_main_id, select="id,name,folder"
                        )
                        if candidate.get("name", "").lower() == main.lower() and candidate.get("folder") is not None:
                            main_item = candidate
                    except GraphError:
                        main_item = None

                if main_item is None:
                    main_item = await gd.ensure_folder(drive_id, root, main)
                    with SessionLocal() as db:
                        self._upsert(db, main, main, "main", main_item["id"], False, None)
                        db.commit()
                main_id = main_item["id"]

                ship = await gd.ensure_folder(drive_id, main_id, slug)
                ship_root_path = f"{main}/{slug}"
                created_ship_roots.append((ship["id"], ship_root_path))
                with SessionLocal() as db:
                    row = self._upsert(db, ship_root_path, slug, "ship", ship["id"], False, None)
                    row.pool_slot_id = slot_id
                    db.commit()
                await self._provision_subtree_batched(
                    drive_id, ship["id"], ship_root_path, template.SHIP_TEMPLATE[main], None,
                )
        except Exception:
            # A completed build failure is no longer an active build. Mark it
            # failed immediately so reconcile_pool does not count it as capacity.
            with SessionLocal() as db:
                failed_slot = db.query(models.PoolSlot).filter_by(id=slot_id).one_or_none()
                if failed_slot is not None and failed_slot.status == "building":
                    failed_slot.status = "failed"
                    db.commit()
            # Best-effort cleanup of a partially built slot. Keep the row as
            # failed rather than deleting it so the attempt remains auditable
            # and partially created Folder rows can be cleaned up safely.
            for ship_id, ship_path in created_ship_roots:
                try:
                    await gd.delete_item(drive_id, ship_id)
                except Exception:
                    pass
                with SessionLocal() as db:
                    rows = db.query(models.Folder).filter(
                        sa_or(
                            models.Folder.path == ship_path,
                            models.Folder.path.like(f"{ship_path}/%"),
                        )
                    ).all()
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

        try:
            mains_to_provision = [
                m for m in template.MAIN_FOLDERS
                if m not in template.FLAT_MAIN_FOLDERS
            ]

            async def provision_main(main):
                with SessionLocal() as db:
                    main_row = db.query(models.Folder).filter_by(path=main).one_or_none()
                    if main_row is None:
                        root = await gd.get_root_item_id(drive_id)
                        main_item = await gd.ensure_folder(drive_id, root, main)
                        main_row = self._upsert(db, main, main, "main", main_item["id"], False, None)
                        db.commit()
                    main_id = main_row.drive_item_id

                ship = await gd.ensure_folder(drive_id, main_id, name)
                ship_root_path = f"{main}/{name}"
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
                        parent_folder_id=main_id,
                        vessel_name=name,
                        department=main,
                        created_by_email=requesting_email,
                        created_by_name=requesting_name,
                        alert_type="vessel_provisioned",
                    )
                await self._provision_subtree_batched(
                    drive_id, ship["id"], ship_root_path,
                    template.SHIP_TEMPLATE[main], vessel_id,
                )

            results = await asyncio.gather(
                *(provision_main(m) for m in mains_to_provision),
                return_exceptions=True,
            )
            first_error = next((r for r in results if isinstance(r, Exception)), None)
            if first_error is not None:
                raise first_error
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
            return {
                "id": str(v_updated.id),
                "name": v_updated.name,
                "imo": v_updated.imo,
                "shipyard": v_updated.shipyard,
                "hull_number": v_updated.hull_number,
                "vessel_type": v_updated.vessel_type,
                "sp_success": sp_success,
                "sp_errors": sp_errors,
            }
    
    async def delete_vessel(
        self, vessel_id: str, requesting_email=None, requesting_name=None,
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
            execute=lambda: self._execute_delete_vessel(vessel_id),
        )

    async def _execute_delete_vessel(self, vessel_id: str) -> dict:
        """Delete a vessel: delete its root ship folders via Graph API (moving them to
        SharePoint Recycle Bin) and delete the vessel + folder rows from SQLite DB."""
        drive_id = await self._drive()
        from ..graph import drive as _gd

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

            # Find ship root folders for this vessel
            ship_folders = (
                db.query(models.Folder)
                .filter(models.Folder.vessel_id == vid, models.Folder.kind == "ship")
                .all()
            )
            ship_folder_refs = [
                (f.drive_item_id, f.path)
                for f in ship_folders
                if f.path
            ]

        # Build candidate root paths for vessel folders across both current and
        # legacy structures.
        candidate_paths: list[str] = []
        seen_paths: set[str] = set()

        for _, p in ship_folder_refs:
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

        # Resolve live delete targets from path first, then name search as a fallback.
        live_targets: dict[str, str] = {}  # item_id -> best-known path
        for cp in candidate_paths:
            try:
                item = await asyncio.wait_for(
                    _gd.get_item_by_path(drive_id, cp, select="id,name,folder,parentReference"),
                    timeout=10,
                )
                if item and item.get("id") and item.get("folder") is not None:
                    live_targets[item["id"]] = cp
            except GraphError as exc:
                if exc.status != 404:
                    log.warning("[_execute_delete_vessel] path lookup failed for %s: %s", cp, exc)
            except Exception as exc:
                log.warning("[_execute_delete_vessel] path lookup error for %s: %s", cp, exc)

        if not live_targets:
            try:
                hits = await asyncio.wait_for(_gd.search_items(drive_id, vname), timeout=12)
                valid_prefixes = [
                    f"root:/{main}/".lower()
                    for main in template.MAIN_FOLDERS
                    if main not in template.FLAT_MAIN_FOLDERS
                ] + [
                    "root:/vessels/specific vessels/",
                    "root:/specific vessels/",
                ]
                for hit in hits:
                    if hit.get("folder") is None:
                        continue
                    if (hit.get("name") or "").strip().lower() != vname.strip().lower():
                        continue
                    parent_path = ((hit.get("parentReference") or {}).get("path") or "").lower()
                    if any(parent_path.startswith(pfx) for pfx in valid_prefixes):
                        live_targets[hit["id"]] = parent_path
            except Exception as exc:
                log.warning("[_execute_delete_vessel] search fallback failed for '%s': %s", vname, exc)

        if not live_targets:
            log.info("[_execute_delete_vessel] No active ship folders found in SharePoint for '%s' (already moved to Recycle Bin or absent). Proceeding with DB cleanup.", vname)

        # Delete ship folders via Graph API -> automatically moved to SharePoint
        # Recycle Bin. Use bounded per-call timeouts and a path-based fallback
        # so stale cache IDs (wrong drive/item) do not silently remove the
        # vessel from DB while folders still exist in SharePoint.
        async def _delete_ship_folder(item_id: str, ship_path: str) -> tuple[str, bool, str | None]:
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
            *(_delete_ship_folder(item_id, ship_path) for item_id, ship_path in live_targets.items())
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
        ship_paths = list(live_targets.values())

        # Record deletion in DB after successful SPO deletion so app state
        # matches SharePoint state.
        original_path = ship_paths[0] if ship_paths else f"Vessels/Specific Vessels/{vname}"
        primary_drive_item_id = ship_folder_ids[0] if ship_folder_ids else None
        with SessionLocal() as db:
            existing = db.query(models.DeletedVessel).filter_by(vessel_name=vname).one_or_none()
            if existing:
                existing.vessel_imo = vimo
                existing.vessel_type = vtype
                existing.drive_item_id = primary_drive_item_id
                existing.original_path = original_path
                existing.deleted_at = datetime.utcnow()
            else:
                db.add(models.DeletedVessel(
                    vessel_name=vname,
                    vessel_imo=vimo,
                    vessel_type=vtype,
                    drive_item_id=primary_drive_item_id,
                    original_path=original_path,
                ))
            db.commit()

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

    async def reprovision_vessel(self, vessel_id: str) -> dict:
        """Idempotently re-run folder provisioning for an existing vessel.

        Safe to call at any time: `ensure_folder` is a create-or-fetch operation,
        so existing folders are left untouched and only missing ones are created.

        Returns a summary with lists of created / existed / failed paths so the
        frontend can surface a meaningful result.
        """
        await self.ensure_base_structure()
        drive_id = await self._drive()

        with SessionLocal() as db:
            vessel = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
            if vessel is None:
                raise NotFound(f"Vessel {vessel_id!r} not found")
            name = vessel.name
            vid = vessel.id

        # Collect per-path outcomes from all concurrent tasks.
        created_paths: list[str] = []
        existed_paths: list[str] = []
        failed_paths: list[str] = []

        async def _ensure_node_tracked(drive_id, parent_id, parent_path, spec, vessel_id):
            """Like _ensure_node but records created/existed/failed outcomes."""
            node_name = spec["name"]
            path = f"{parent_path}/{node_name}" if parent_path else node_name
            kind = spec["kind"]
            try:
                with SessionLocal() as db:
                    cached = db.query(models.Folder).filter_by(path=path).one_or_none()
                    cached_id = cached.drive_item_id if cached else None

                if cached_id:
                    existed_paths.append(path)
                    item_id = cached_id
                else:
                    async with self._semaphore():
                        item = await gd.ensure_folder(drive_id, parent_id, node_name)
                    item_id = item["id"]
                    with SessionLocal() as db:
                        self._upsert(db, path, node_name, kind, item_id, kind == "month_driven", vessel_id)
                        db.commit()
                    created_paths.append(path)

                if kind != "month_driven":
                    children = spec.get("children", [])
                    await asyncio.gather(*(
                        _ensure_node_tracked(drive_id, item_id, path, child, vessel_id)
                        for child in children
                    ))
            except Exception as exc:
                log.warning("reprovision_vessel: failed to create %r: %s", path, exc)
                failed_paths.append(path)

        async def reprovision_main(main):
            with SessionLocal() as db:
                main_row = db.query(models.Folder).filter_by(path=main).one_or_none()
                if main_row is None:
                    root = await gd.get_root_item_id(drive_id)
                    main_item = await gd.ensure_folder(drive_id, root, main)
                    main_row = self._upsert(db, main, main, "main", main_item["id"], False, None)
                    db.commit()
                    created_paths.append(main)
                else:
                    existed_paths.append(main)
                main_id = main_row.drive_item_id

            # Ship root folder
            ship_root_path = f"{main}/{name}"
            with SessionLocal() as db:
                ship_cached = db.query(models.Folder).filter_by(path=ship_root_path).one_or_none()
                ship_cached_id = ship_cached.drive_item_id if ship_cached else None

            if ship_cached_id:
                existed_paths.append(ship_root_path)
                ship_id = ship_cached_id
            else:
                ship_item = await gd.ensure_folder(drive_id, main_id, name)
                ship_id = ship_item["id"]
                with SessionLocal() as db:
                    self._upsert(db, ship_root_path, name, "ship", ship_id, False, vid)
                    db.commit()
                created_paths.append(ship_root_path)

            await asyncio.gather(*(
                _ensure_node_tracked(drive_id, ship_id, ship_root_path, spec, vid)
                for spec in template.SHIP_TEMPLATE[main]
            ))

        await asyncio.gather(*[
            reprovision_main(m)
            for m in template.MAIN_FOLDERS
            if m not in template.FLAT_MAIN_FOLDERS
        ])

        # Mark vessel as fully provisioned if no failures
        if not failed_paths:
            with SessionLocal() as db:
                v = db.query(models.Vessel).filter_by(id=vid).one_or_none()
                if v:
                    v.is_provisioned = True
                    db.commit()

        return {
            "ok": True,
            "vessel_id": vessel_id,
            "name": name,
            "is_provisioned": len(failed_paths) == 0,
            "created": created_paths,
            "existed": existed_paths,
            "failed": failed_paths,
            "summary": {
                "created_count": len(created_paths),
                "existed_count": len(existed_paths),
                "failed_count": len(failed_paths),
            },
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
        try:
            parent_path = await self._folder_path(drive_id, folder_id)
            items = await gd.list_children(drive_id, folder_id)
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
                    flags = classify(child_parts)
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


    async def get_dashboard_stats(self, force_refresh: bool = False) -> dict:
        now_ts = time.time()
        if not force_refresh and _DASHBOARD_STATS_CACHE["data"] is not None and (now_ts - _DASHBOARD_STATS_CACHE["timestamp"]) < CACHE_TTL_DASHBOARD_STATS:
            return _DASHBOARD_STATS_CACHE["data"]

        with SessionLocal() as db:
            total_vessels = db.query(models.Vessel).count()
            vessel_objs = db.query(models.Vessel).all()
            known_vessels = {v.name.strip().lower(): v.name for v in vessel_objs}
            pending_approvals = db.query(models.ApprovalRequest).filter(
                models.ApprovalRequest.status == "pending",
                models.ApprovalRequest.entry_kind == "approval"
            ).count()

        now_dt = datetime.now(timezone.utc)
        thirty_days = timedelta(days=30)
        one_year = timedelta(days=365)

        all_docs = []
        drive_id = await self._drive() if settings.graph_configured else None
        if drive_id:
            try:
                g = graph()
                res = await g.get(
                    f"/drives/{drive_id}/root/search(q='')?$select=id,name,size,file,folder,lastModifiedDateTime,createdDateTime,webUrl,parentReference&$top=500"
                )
                items = res.get("value", []) if isinstance(res, dict) else []
                for f in items:
                    if "file" not in f or not f.get("name"):
                        continue
                    name = f["name"]
                    url = f.get("webUrl", "")
                    path = ""
                    if "Shared%20Documents/" in url:
                        path = unquote(url.split("Shared%20Documents/")[1])
                    elif "Shared Documents/" in url:
                        path = url.split("Shared Documents/")[1]

                    parts = [p.strip() for p in path.split("/") if p.strip()] if path else []
                    group = parts[0] if len(parts) > 0 else "Shared Documents"

                    vessel = "Shared Documents"
                    for part in parts:
                        if part.lower() in known_vessels:
                            vessel = known_vessels[part.lower()]
                            break
                    if vessel == "Shared Documents" and len(parts) > 1 and parts[1].lower() not in (
                        "invoices & payments", "drawings and manuals", "po & invoice", "common for all ships", "to be classified"
                    ):
                        vessel = parts[1]

                    doc_type = parts[-2] if len(parts) > 2 else "Document"
                    sub_folder_path = " > ".join(parts[:-1]) if len(parts) > 1 else group

                    mod_str = f.get("lastModifiedDateTime") or f.get("createdDateTime") or ""
                    try:
                        mod_dt = datetime.fromisoformat(mod_str.replace("Z", "+00:00")) if mod_str else now_dt
                    except Exception:
                        mod_dt = now_dt

                    mod_epoch = int(mod_dt.timestamp() * 1000)
                    date_str = mod_dt.strftime("%b %d, %Y")

                    is_cert = bool(re.search(r"insurance|certificate|survey|crew|audit|statutory|compliance", path, re.I))
                    status = "Valid"
                    if is_cert:
                        exp_dt = mod_dt + one_year
                        if exp_dt < now_dt:
                            status = "Expired"
                        elif exp_dt <= now_dt + thirty_days:
                            status = "Expiring Soon"
                        else:
                            status = "Valid"

                    size_b = f.get("size", 0) or 0
                    if size_b < 1024 * 1024:
                        size_str = f"{size_b / 1024:.1f} KB"
                    else:
                        size_str = f"{size_b / (1024 * 1024):.1f} MB"

                    all_docs.append({
                        "id": f.get("id") or name,
                        "name": name,
                        "vessel": vessel,
                        "type": doc_type,
                        "modified": date_str,
                        "modifiedEpoch": mod_epoch,
                        "status": status,
                        "fileSize": size_str,
                        "subFolderPath": sub_folder_path,
                        "webUrl": url,
                    })
            except Exception as e:
                log.warning("get_dashboard_stats graph file search failed: %s", e)

        all_docs.sort(key=lambda d: d["modifiedEpoch"], reverse=True)
        total_docs = len(all_docs)
        expired = sum(1 for d in all_docs if d["status"] == "Expired")
        expiring = sum(1 for d in all_docs if d["status"] == "Expiring Soon")
        valid = sum(1 for d in all_docs if d["status"] == "Valid")

        result = {
            "total_documents": total_docs,
            "total_vessels": total_vessels,
            "pending_approvals": pending_approvals,
            "expiring_soon_count": expiring,
            "expired_count": expired,
            "valid_count": valid,
            "recent_documents": all_docs[:10],
            "documents": all_docs,
            "expiry_overview": {
                "total": total_docs,
                "expired": expired,
                "expiring_soon": expiring,
                "valid": valid,
                "expired_pct": round((expired / total_docs * 100)) if total_docs else 0,
                "expiring_pct": round((expiring / total_docs * 100)) if total_docs else 0,
                "valid_pct": round((valid / total_docs * 100)) if total_docs else 0,
            },
        }
        _DASHBOARD_STATS_CACHE["data"] = result
        _DASHBOARD_STATS_CACHE["timestamp"] = time.time()
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
        self, folder_id: str, requesting_email=None, requesting_name=None,
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
                spo_already_deleted=not folder_exists_in_spo
            ),
        )

    async def _execute_delete_folder(
        self, folder_id: str, folder_path: str | None = None, spo_already_deleted: bool = False
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
        """Manually create a named sub-folder inside a month_driven folder."""
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
        if not parent_flags.get("month_driven"):
            raise BadRequest("Can only create sub-folders inside month-driven folders")

        department = parent_parts[0] if parent_parts else "All Departments"
        parent_name = parent_parts[-1] if parent_parts else folder_id
        with SessionLocal() as db:
            parent_row = self._folder_by_item(db, folder_id)
            vessel_id = parent_row.vessel_id if parent_row else None
            vessel_name = None
            if vessel_id:
                v = db.query(models.Vessel).filter_by(id=vessel_id).one_or_none()
                vessel_name = v.name if v else None

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
        """Manually create a named sub-folder inside a month_driven folder,
        then provision its category children from the template."""
        folder_id = payload["parent_folder_id"]
        name = payload["name"]
        drive_id = await self._drive()
        parent_path = await self._folder_path(drive_id, folder_id)
        parent_parts = parent_path.split("/") if parent_path else []
        parent_flags = classify(parent_parts)

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
        cats = parent_flags.get("categories", [])
        requesting_email = (payload.get("requesting_email") or "").strip()
        requesting_name = payload.get("requesting_name") or ""
        department, _, vessel_name, _ = await self._resolve_department_vessel(folder_id)
        with SessionLocal() as db:
            parent_row = self._folder_by_item(db, folder_id)
            vessel_id = parent_row.vessel_id if parent_row else None
            self._upsert(db, mpath, name, "month", new_item["id"], False, vessel_id)
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
            "kind": "month",
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
            execute=lambda: self._execute_delete_file(file_id),
        )

    async def _execute_delete_file(self, file_id):
        drive_id = await self._drive()
        try:
            await gd.delete_item(drive_id, file_id)
            return True
        except GraphError as e:
            if e.status == 404:
                return False
            raise

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
        """Search folders + files by name. When `vessel_id` is given, results
        are restricted to that vessel's own ship folders (one per main
        folder) — never other vessels' folders, and never the shared "Common
        for all ships" areas.
        """
        ql = q.strip()
        if not ql:
            return []
        vid = int(vessel_id) if vessel_id and str(vessel_id).isdigit() else None
        out, seen = [], set()
        # 1) Folders from our DB cache — always available, no index lag.
        #    vessel_id is an indexed FK, so scoping here is a cheap filter,
        #    not a scan of every vessel's folders.
        with SessionLocal() as db:
            query = db.query(models.Folder).filter(models.Folder.name.ilike(f"%{ql}%"))
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
        try:
            drive_id = await self._drive()
            if vid is not None:
                # Scoped, recursive search inside just this vessel's ship
                # folders — Graph does the subtree walk server-side, so other
                # vessels' documents are never scanned or returned.
                per_root = await asyncio.gather(
                    *(gd.search_items_in(drive_id, root_id, ql) for root_id in ship_root_ids)
                )
                items = [it for lst in per_root for it in lst]
            else:
                items = await gd.search_items(drive_id, ql)
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
            drive_id = await self._drive()
            url = f"/drives/{drive_id}/items/root/children?$filter=deleted ne null"
            data = await graph().get(url)
            items = data.get("value", [])
            out = []
            sp_vessel_names: set[str] = set()  # track vessel names returned by SharePoint
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
                if is_vessel_folder:
                    kind = "vessel"
                    sp_vessel_names.add(name.lower())

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
                }
                out.append(node)

            # Merge DB-tracked deleted vessels that SharePoint hasn't propagated yet.
            # This ensures all deleted vessels appear immediately, even when the
            # SharePoint recycle bin API returns a partial/delayed list.
            with SessionLocal() as db:
                db_deleted = db.query(models.DeletedVessel).order_by(
                    models.DeletedVessel.deleted_at.desc()
                ).all()
            for dv in db_deleted:
                if dv.vessel_name.lower() not in sp_vessel_names:
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
                    })

            return out
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to get deleted nodes: {e}")
            return []

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
        # Site drive: Graph doesn't expose a recycle-bin restore API for site drives
        import logging
        logging.getLogger(__name__).warning("Restore from recycle bin not supported for site drives")
        return {"restored": False}

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

    # -------------------------------------------------------------- approvals
    async def _create_approval(
        self, drive_id, destination_folder_id, destination_path, filename, content,
        content_type, uploaded_by_email, uploaded_by_name, *,
        is_month_upload=False, category=None, detected_month=None,
        department=None, vessel_id=None, vessel_name=None, message=None,
    ):
        # Overwrite/replace any previous pending request for the same file in this folder
        with SessionLocal() as db:
            existing_pendings = (
                db.query(models.ApprovalRequest)
                .filter(
                    models.ApprovalRequest.status == "pending",
                    models.ApprovalRequest.action_type == "upload",
                    models.ApprovalRequest.destination_folder_id == destination_folder_id,
                    func.lower(models.ApprovalRequest.filename) == filename.lower(),
                )
                .all()
            )
            for old_p in existing_pendings:
                db.delete(old_p)
            if existing_pendings:
                db.commit()

        staged_item_id = await self._stage_file(drive_id, filename, content, content_type)
        try:
            with SessionLocal() as db:
                row = models.ApprovalRequest(
                    filename=filename,
                    content_type=content_type or "application/octet-stream",
                    size=len(content),
                    uploaded_by_email=uploaded_by_email,
                    uploaded_by_name=uploaded_by_name or "",
                    destination_folder_id=destination_folder_id,
                    destination_path=destination_path,
                    is_month_upload=is_month_upload,
                    category=category,
                    detected_month=detected_month,
                    drive_item_id=staged_item_id,
                    entry_kind="approval",
                    action_type="upload",
                    department=department,
                    vessel_id=int(vessel_id) if vessel_id else None,
                    vessel_name=vessel_name,
                    message=message,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                public = self._approval_public(row)
        except Exception:
            # Don't leave an orphaned staged file if the DB write failed.
            try:
                await gd.delete_item(drive_id, staged_item_id)
            except GraphError:
                pass
            raise
        await notify_email(
            settings.admin_emails,
            f"New document pending approval: {filename}",
            f"{uploaded_by_name or uploaded_by_email} uploaded '{filename}' to "
            f"{destination_path}. Review it in the DMS approvals page.",
        )
        return public

    async def list_approvals(self, status=None, q=None):
        with SessionLocal() as db:
            # Only surface real approval-workflow rows (entry_kind='approval').
            # 'activity' rows are admin audit-log entries (status='completed')
            # and must NOT appear in the approval queue.
            query = db.query(models.ApprovalRequest).filter(
                models.ApprovalRequest.entry_kind == "approval"
            )
            if status and status != "all":
                query = query.filter_by(status=status)
            else:
                # Exclude cancelled rows from the default view
                query = query.filter(
                    models.ApprovalRequest.status.in_(["pending", "approved", "rejected"])
                )
            if q:
                ql = f"%{q.strip()}%"
                query = query.filter(
                    sa_or(
                        models.ApprovalRequest.filename.ilike(ql),
                        models.ApprovalRequest.uploaded_by_email.ilike(ql),
                        models.ApprovalRequest.destination_path.ilike(ql),
                        models.ApprovalRequest.target_description.ilike(ql),
                        models.ApprovalRequest.vessel_name.ilike(ql),
                        models.ApprovalRequest.message.ilike(ql),
                    )
                )
            rows = query.order_by(models.ApprovalRequest.uploaded_at.desc()).all()
            return [self._approval_public(r) for r in rows]

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
            db.commit()
            return {"marked": result}

    async def get_approval(self, request_id):
        with SessionLocal() as db:
            row = db.get(models.ApprovalRequest, int(request_id)) if request_id.isdigit() else None
            if row is None:
                raise NotFound("Approval request not found")
            return self._approval_public(row)

    async def get_approval_file(self, request_id):
        if not request_id.isdigit():
            return None
        with SessionLocal() as db:
            row = db.get(models.ApprovalRequest, int(request_id))
            if row is None:
                return None
            item_id, content_type, filename = row.drive_item_id, row.content_type, row.filename
        drive_id = await self._drive()
        try:
            content, _, _ = await gd.download_file(drive_id, item_id)
        except GraphError:
            return None
        return content, content_type, filename

    def _claim_pending(self, request_id: str, new_status: str):
        """Row-lock the request and flip it to `new_status` iff still pending —
        this is what makes concurrent approve/reject calls safe: whichever call
        commits first wins the lock, and the loser sees a non-pending status."""
        if not request_id.isdigit():
            raise NotFound("Approval request not found")
        with SessionLocal() as db:
            row = (
                db.query(models.ApprovalRequest)
                .filter_by(id=int(request_id))
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                raise NotFound("Approval request not found")
            if row.status != "pending":
                raise Conflict(f"This request has already been {row.status}")
            row.status = new_status
            row.decided_at = datetime.utcnow()
            db.commit()
            return {
                "action_type": row.action_type or "upload",
                "filename": row.filename,
                "content_type": row.content_type,
                "drive_item_id": row.drive_item_id,
                "destination_folder_id": row.destination_folder_id,
                "uploaded_by_email": row.uploaded_by_email,
                "target_id": row.target_id,
                "payload": json.loads(row.payload_json) if row.payload_json else {},
            }

    def _revert_to_pending(self, request_id: str):
        with SessionLocal() as db:
            row = db.get(models.ApprovalRequest, int(request_id))
            if row is not None:
                row.status = "pending"
                row.decided_by_email = None
                row.decided_at = None
                row.rejection_reason = None
                db.commit()

    def _finalize(self, request_id: str, decided_by_email: str, final_path: str, reason=None):
        with SessionLocal() as db:
            row = db.get(models.ApprovalRequest, int(request_id))
            row.decided_by_email = decided_by_email
            row.final_path = final_path
            if reason is not None:
                row.rejection_reason = reason
            db.commit()
            db.refresh(row)
            return self._approval_public(row)

    def _mark_approved_row(self, request_id: str, decided_by_email: str):
        """Non-upload actions: the claim already flipped status to 'approved'
        — this just records who decided it, after the deferred mutation has
        already run successfully."""
        with SessionLocal() as db:
            row = db.get(models.ApprovalRequest, int(request_id))
            row.decided_by_email = decided_by_email
            db.commit()
            db.refresh(row)
            return self._approval_public(row)

    def _mark_rejected_row(self, request_id: str, decided_by_email: str, reason=None):
        with SessionLocal() as db:
            row = db.get(models.ApprovalRequest, int(request_id))
            row.status = "rejected"
            row.decided_by_email = decided_by_email
            row.rejection_reason = reason
            db.commit()
            db.refresh(row)
            return self._approval_public(row)

    async def approve_request(self, request_id, decided_by_email):
        claimed = self._claim_pending(request_id, "approved")
        action_type = claimed.get("action_type") or "upload"

        if action_type == "upload":
            drive_id = await self._drive()
            try:
                existing = await gd.find_child(drive_id, claimed["destination_folder_id"], claimed["filename"])
                if existing and "file" in existing:
                    raise Conflict(f"'{claimed['filename']}' already exists in the destination folder")
                await gd.move_item(
                    drive_id, claimed["drive_item_id"], claimed["destination_folder_id"],
                    new_name=claimed["filename"],
                )
            except Exception:
                self._revert_to_pending(request_id)
                raise
            dest_path = await self._folder_path(drive_id, claimed["destination_folder_id"])
            result = self._finalize(request_id, decided_by_email, f"{dest_path}/{claimed['filename']}")
            await notify_email(
                claimed["uploaded_by_email"],
                f"Your document '{claimed['filename']}' was approved",
                f"'{claimed['filename']}' has been approved and filed to {result['final_path']}.",
            )
            return result

        # Non-upload actions: re-validate the target still exists, execute
        # the deferred mutation, then finalize. If the target vanished in the
        # meantime, resolve the request as rejected rather than erroring.
        payload = claimed.get("payload") or {}
        target_id = claimed.get("target_id")
        try:
            if action_type == "delete_document":
                drive_id = await self._drive()
                try:
                    await gd.get_item(drive_id, target_id)
                except GraphError as e:
                    if e.status == 404:
                        return self._mark_rejected_row(request_id, decided_by_email, "Target no longer exists")
                    raise
                await self._execute_delete_file(target_id)
            elif action_type == "delete_folder":
                drive_id = await self._drive()
                try:
                    await gd.get_item(drive_id, target_id)
                except GraphError as e:
                    if e.status == 404:
                        return self._mark_rejected_row(request_id, decided_by_email, "Target no longer exists")
                    raise
                await self._execute_delete_folder(target_id)
            elif action_type == "create_folder":
                await self._execute_create_subfolder(payload)
            elif action_type == "create_vessel":
                await self._provision_vessel(payload)
            elif action_type == "update_vessel":
                with SessionLocal() as db:
                    exists = db.query(models.Vessel).filter_by(id=int(payload["vessel_id"])).one_or_none()
                if not exists:
                    return self._mark_rejected_row(request_id, decided_by_email, "Target no longer exists")
                await self._execute_update_vessel(payload)
            elif action_type == "delete_vessel":
                v_id = payload.get("vessel_id") or target_id
                with SessionLocal() as db:
                    try:
                        vid = int(v_id)
                        exists = db.query(models.Vessel).filter_by(id=vid).one_or_none()
                    except ValueError:
                        exists = None
                if not exists:
                    return self._mark_rejected_row(request_id, decided_by_email, "Target vessel no longer exists")
                await self._execute_delete_vessel(str(v_id))
            elif action_type == "archive_item":
                await self._execute_archive(target_id, payload.get("item_type", "folder"))
            elif action_type == "restore_item":
                await self._execute_restore(target_id)
            elif action_type == "restore_from_recycle_bin":
                await self._execute_restore_deleted(target_id)
            elif action_type == "permanent_delete":
                await self._execute_permanent_delete(target_id, payload.get("item_type", "folder"))
            else:
                raise BadRequest(f"Unknown action type: {action_type}")
        except Exception:
            self._revert_to_pending(request_id)
            raise
        return self._mark_approved_row(request_id, decided_by_email)

    async def reject_request(self, request_id, decided_by_email, reason=None):
        claimed = self._claim_pending(request_id, "rejected")
        action_type = claimed.get("action_type") or "upload"

        if action_type == "upload":
            drive_id = await self._drive()
            try:
                target_id = await self._resolve_reject_target(drive_id, claimed["destination_folder_id"])
                target_path = await self._folder_path(drive_id, target_id)
                existing = await gd.find_child(drive_id, target_id, claimed["filename"])
                if existing and "file" in existing:
                    raise Conflict(
                        f"'{claimed['filename']}' already exists in the "
                        f"'{target_path.split('/')[-1]}' folder"
                    )
                await gd.move_item(drive_id, claimed["drive_item_id"], target_id, new_name=claimed["filename"])
            except Exception:
                self._revert_to_pending(request_id)
                raise
            result = self._finalize(
                request_id, decided_by_email, f"{target_path}/{claimed['filename']}", reason
            )
            await notify_email(
                claimed["uploaded_by_email"],
                f"Your document '{claimed['filename']}' was rejected",
                f"'{claimed['filename']}' was rejected"
                + (f" ({reason})" if reason else "")
                + f" and moved to {result['final_path']}.",
            )
            return result

        # Non-upload actions: nothing was staged/created, so rejection is
        # just a state transition (the claim already flipped status).
        return self._mark_rejected_row(request_id, decided_by_email, reason)

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
