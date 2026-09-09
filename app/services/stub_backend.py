"""Async adapter over the in-memory `store` (stub mode).

Presents the same interface as RealBackend so the API layer is backend-agnostic.
"""
from datetime import datetime, timezone
from ..config import settings
from ..store import store
from .normalize import normalize_vessel_name
from .errors import BadRequest, Conflict, NotFound
from .notify import notify_email
from ..db.base import SessionLocal
from ..db import models
from sqlalchemy import func
from .. import template

def sanitize_folder_name(name: str) -> str:
    name = name.replace("/", "-").replace("\\", "-")
    name = name.replace(":", "-")
    for c in '*?"<>|':
        name = name.replace(c, "_")
    name = name.strip(" .")
    return name


class StubBackend:
    # ---------------------------------------------------------- admin/activity
    def _is_admin(self, email: str | None) -> bool:
        return (email or "").strip().lower() in settings.admin_email_set

    def _display(self, email: str | None, name: str | None) -> str:
        if name:
            return name
        if email:
            return email.split("@")[0]
        return "A user"

    def _resolve_department(self, folder_id: str | None) -> str:
        """The business area (one of template.MAIN_FOLDERS) a folder/file
        lives under, derived from its ancestor path. Falls back to
        "All Departments" for vessel-level actions with no single folder."""
        if not folder_id:
            return "All Departments"
        path = store.path_of(folder_id)
        if not path:
            return "All Departments"
        return path.split(" / ")[0]

    def _resolve_vessel_name_for_node(self, node_id: str | None) -> str | None:
        node = store.get_node(node_id) if node_id else None
        while node is not None:
            if node.get("kind") == "ship":
                return node.get("vessel") or node.get("name")
            parent_id = node.get("parent_id")
            node = store.get_node(parent_id) if parent_id else None
        return None

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
            store.create_activity(
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
                message=activity_message,
            )
            return {"status": "completed", "message": activity_message, "result": result}
        approval = store.create_pending_action(
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

    # ----------------------------------------------------------------- vessels
    async def list_vessels(self):
        return [
            {
                "id": v["id"],
                "name": v["name"],
                "imo": v.get("imo"),
                "shipyard": v.get("shipyard"),
                "hull_number": v.get("hull_number"),
                "vessel_type": v.get("vessel_type"),
                "is_provisioned": True,
            }
            for v in reversed(store.vessels)
        ]

    def _validate_create_vessel(self, name, imo, exclude_vessel_id=None):
        name = (name or "").strip()
        imo = (imo or "").strip()
        if not name:
            raise BadRequest("Vessel name is required")
        if not imo or imo in ("—", "None", "null", "auto", "0000000"):
            import random as _rand
            imo = str(_rand.randint(1000000, 9999999))
        elif not imo.isdigit() or len(imo) != 7:
            raise BadRequest("IMO number must be exactly 7 digits")

        normalized_name = normalize_vessel_name(name)
        for v in store.vessels:
            if exclude_vessel_id and v["id"] == exclude_vessel_id:
                continue
            if normalize_vessel_name(v["name"]) == normalized_name:
                raise Conflict("Vessel name already exists.")
            if v.get("imo") == imo:
                raise Conflict("A vessel with that IMO number already exists")
        return name, imo

    async def create_vessel(
        self, name, imo, shipyard=None, hull_number=None, vessel_type=None,
        requesting_email=None, requesting_name=None,
    ):
        """Creating a vessel never requires approval — for anyone, admin or
        not. It always executes immediately and is always recorded as a
        completed activity entry for audit purposes."""
        clean_name, clean_imo = self._validate_create_vessel(name, imo)
        payload = {
            "name": clean_name, "imo": clean_imo, "shipyard": shipyard,
            "hull_number": hull_number, "vessel_type": vessel_type,
            "requesting_email": requesting_email, "requesting_name": requesting_name,
        }
        display = self._display(requesting_email, requesting_name)
        vessel = await self._execute_create_vessel(payload)
        activity_message = (
            f"{display} ({requesting_email}) created vessel '{clean_name}'. "
            f"No approval was required."
        )
        store.create_activity(
            action_type="create_vessel",
            requesting_email=requesting_email or "",
            requesting_name=requesting_name,
            department="All Departments",
            target_description=clean_name,
            payload=payload,
            message=activity_message,
        )
        return {"status": "completed", "message": activity_message, "result": vessel}

    async def _execute_create_vessel(self, payload):
        # Re-validate at execution time — covers the approve-time path, where
        # the name/IMO may have been taken by someone else since the request
        # was filed.
        self._validate_create_vessel(payload["name"], payload["imo"])
        vessel = store.add_vessel(
            payload["name"], payload["imo"],
            shipyard=payload.get("shipyard"), hull_number=payload.get("hull_number"),
            vessel_type=payload.get("vessel_type"),
        )
        # Emit a vessel-provisioned alert for the top-header alert bell.
        store.add_folder_alert(
            drive_item_id=None,
            folder_name=f"Vessel: {vessel['name']}",
            folder_path=f"Vessels / Specific Vessels / {vessel['name']}",
            parent_folder_id=None,
            vessel_name=vessel["name"],
            department="All Departments",
            created_by_email=payload.get("requesting_email") or "",
            created_by_name=payload.get("requesting_name") or "",
            alert_type="vessel_provisioned",
        )
        return vessel

    def _validate_update_vessel(self, vessel_id, name, imo, shipyard, hull_number, vessel_type):
        vessel = next((v for v in store.vessels if v["id"] == vessel_id), None)
        if not vessel:
            raise NotFound("Vessel not found")
        old_name = vessel["name"]
        old_imo = vessel["imo"]

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
            if any(
                normalize_vessel_name(v["name"]) == normalized_name
                for v in store.vessels if v["id"] != vessel_id
            ):
                raise Conflict("Vessel name already exists.")

        if new_imo and new_imo != old_imo:
            if any(v.get("imo") == new_imo for v in store.vessels if v["id"] != vessel_id):
                raise Conflict("A vessel with that IMO number already exists")

        return vessel, new_name, new_imo

    async def update_vessel(
        self, vessel_id: str, name: str | None = None, imo: str | None = None,
        shipyard: str | None = None, hull_number: str | None = None, vessel_type: str | None = None,
        requesting_email=None, requesting_name=None,
    ):
        vessel, new_name, new_imo = self._validate_update_vessel(
            vessel_id, name, imo, shipyard, hull_number, vessel_type
        )
        changes = []
        if new_name and new_name != vessel["name"]:
            changes.append({"field": "Name", "old": vessel["name"], "new": new_name})
        if new_imo and new_imo != vessel["imo"]:
            changes.append({"field": "IMO", "old": vessel["imo"], "new": new_imo})
        if shipyard is not None and (shipyard.strip() or None) != vessel.get("shipyard"):
            changes.append({"field": "Shipyard", "old": vessel.get("shipyard"), "new": shipyard.strip() or None})
        if hull_number is not None and (hull_number.strip() or None) != vessel.get("hull_number"):
            changes.append({"field": "Hull Number", "old": vessel.get("hull_number"), "new": hull_number.strip() or None})
        if vessel_type is not None and (vessel_type.strip() or None) != vessel.get("vessel_type"):
            changes.append({"field": "Vessel Type", "old": vessel.get("vessel_type"), "new": vessel_type.strip() or None})

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
            vessel_name=vessel["name"],
            target_id=vessel_id,
            target_description=vessel["name"],
            payload=payload,
            changes=changes,
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to update the "
                f"vessel details for {vessel['name']} ({change_summary})."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) updated the vessel details for "
                f"{vessel['name']}. No approval was required."
            ),
            execute=lambda: self._execute_update_vessel(payload),
        )

    async def _execute_update_vessel(self, payload):
        vessel_id = payload["vessel_id"]
        vessel, new_name, new_imo = self._validate_update_vessel(
            vessel_id, payload["name"], payload["imo"],
            payload["shipyard"], payload["hull_number"], payload["vessel_type"],
        )
        old_name = vessel["name"]
        updated = store.update_vessel(
            vessel_id, name=new_name or None, imo=new_imo or None,
            shipyard=payload["shipyard"], hull_number=payload["hull_number"],
            vessel_type=payload["vessel_type"],
        )
        if not updated:
            raise NotFound("Vessel not found")

        # Keep the SQLAlchemy cache DB in sync (mirrors prior behavior). This
        # cache is only present when a DB is actually configured — pure
        # stub mode (no .env) has no SessionLocal at all.
        if settings.db_configured:
            with SessionLocal() as db:
                v_db = db.query(models.Vessel).filter_by(id=int(vessel_id)).first()
                if v_db:
                    if new_name:
                        v_db.name = new_name
                    if new_imo:
                        v_db.imo = new_imo
                    if payload["shipyard"] is not None:
                        v_db.shipyard = payload["shipyard"].strip() or None
                    if payload["hull_number"] is not None:
                        v_db.hull_number = payload["hull_number"].strip() or None
                    if payload["vessel_type"] is not None:
                        v_db.vessel_type = payload["vessel_type"].strip() or None

                    if new_name and new_name != old_name:
                        folders = db.query(models.Folder).filter_by(vessel_id=v_db.id).all()
                        for folder in folders:
                            if folder.kind == "ship" and folder.name == old_name:
                                folder.name = new_name
                            for main in template.MAIN_FOLDERS:
                                old_prefix = f"{main}/{old_name}"
                                new_prefix = f"{main}/{new_name}"
                                if folder.path == old_prefix:
                                    folder.path = new_prefix
                                elif folder.path.startswith(f"{old_prefix}/"):
                                    folder.path = new_prefix + folder.path[len(old_prefix):]
                    db.commit()

        return {
            "id": str(updated["id"]),
            "name": updated["name"],
            "imo": updated["imo"],
            "shipyard": updated["shipyard"],
            "hull_number": updated["hull_number"],
            "vessel_type": updated["vessel_type"],
            "sp_success": True,
            "sp_errors": [],
        }

    async def delete_vessel(self, vessel_id: str, requesting_email=None, requesting_name=None):
        vessel = next((v for v in store.vessels if v["id"] == vessel_id), None)
        if not vessel:
            raise NotFound("Vessel not found")
        vname = vessel["name"]
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

    async def _execute_delete_vessel(self, vessel_id: str):
        vessel = next((v for v in store.vessels if v["id"] == vessel_id), None)
        if not vessel:
            return {"deleted": False, "message": "Vessel not found"}
        vname = vessel["name"]
        deleted_vessels = getattr(store, "deleted_vessel_records", {})
        deleted_vessels[f"vessel_{vessel_id}"] = {
            "id": f"vessel_{vessel_id}",
            "name": vname,
            "kind": "vessel",
            "item_type": "vessel",
            "original_path": f"Vessels/{vname}",
            "main_folder": "Vessels",
            "vessel_name": vname,
            "category": "",
            "sub_category": "",
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "imo": vessel.get("imo"),
            "shipyard": vessel.get("shipyard"),
            "hull_number": vessel.get("hull_number"),
            "vessel_type": vessel.get("vessel_type"),
            "vessel": dict(vessel),
            "ship_folder_ids": list(vessel.get("ship_folders", {}).values()),
        }
        store.deleted_vessel_records = deleted_vessels
        store.vessels = [v for v in store.vessels if v["id"] != vessel_id]
        store.deleted_ids.update(vessel.get("ship_folders", {}).values())
        if hasattr(settings, "db_configured") and settings.db_configured:
            with SessionLocal() as db:
                v_db = db.query(models.Vessel).filter_by(id=int(vessel_id)).first()
                if v_db:
                    db.delete(v_db)
                    db.commit()
        return {"deleted": True, "vessel_name": vname, "message": f"Moved vessel '{vname}' to Recycle Bin."}

    async def reprovision_vessel(self, vessel_id: str) -> dict:
        """Stub: no-op — the in-memory store always uses the current template."""
        vessel = next((v for v in store.vessels if v["id"] == vessel_id), None)
        if vessel is None:
            raise NotFound(f"Vessel {vessel_id!r} not found")
        return {"ok": True, "vessel_id": vessel_id, "name": vessel["name"]}

    async def start_vessel_provisioning(self, vessel_id: str) -> dict:
        vessel = next((v for v in store.vessels if v["id"] == vessel_id), None)
        if vessel is None:
            raise NotFound(f"Vessel {vessel_id!r} not found")
        return {"status": "completed", "is_provisioned": True}

    async def repair_vessel_links(self) -> dict:
        """Stub: no-op — in-memory store is always consistent."""
        return {"fixed": 0, "unmatched": []}

    async def mains(self):
        return store.mains()

    async def get_folder(self, folder_id):
        node = store.get_node(folder_id)
        if node is None:
            raise NotFound("Folder not found")
        return store.serialize(node, depth=0)

    async def children(self, folder_id):
        if store.get_node(folder_id) is None:
            raise NotFound("Folder not found")
        return store.children(folder_id)

    async def get_dashboard_stats(self, force_refresh: bool = False) -> dict:
        total_vessels = len(store.vessels)
        all_docs = []
        for node in store.nodes.values():
            if node.get("kind") == "file":
                all_docs.append({
                    "id": node["id"],
                    "name": node["name"],
                    "vessel": "Shared Documents",
                    "type": "Document",
                    "modified": "Today",
                    "modifiedEpoch": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "status": "Valid",
                    "fileSize": "—",
                    "subFolderPath": node.get("path", ""),
                })
        pending_approvals = len([a for a in getattr(store, "approvals", []) if isinstance(a, dict) and a.get("status") == "pending"])
        total_docs = len(all_docs)
        return {
            "total_documents": total_docs,
            "total_vessels": total_vessels,
            "pending_approvals": pending_approvals,
            "expiring_soon_count": 0,
            "expired_count": 0,
            "valid_count": total_docs,
            "recent_documents": all_docs[:10],
            "documents": all_docs,
            "expiry_overview": {
                "total": total_docs,
                "expired": 0,
                "expiring_soon": 0,
                "valid": total_docs,
                "expired_pct": 0,
                "expiring_pct": 0,
                "valid_pct": 100,
            },
        }

    async def stats(self):
        dash = await self.get_dashboard_stats()
        base = store.stats()
        return {
            **base,
            **dash,
        }


    # ------------------------------------------------------------ uploads
    async def upload(self, folder_id, filename, content, content_type, uploaded_by_email, uploaded_by_name, access_token=None, sp_access_token=None):
        """Non-admin uploads stage a pending approval exactly as before.
        SPE Admin uploads are filed immediately and recorded as an activity
        notification instead."""
        node = store.get_node(folder_id)
        if node is None:
            raise NotFound("Folder not found")
        # Allow uploads into any folder
        if node.get("month_driven"):
            target, _ = store.resolve_month_target(folder_id, filename, None)
        elif node["kind"] == "drawing_classifier":
            target, _ = store.resolve_drawing_target(folder_id, filename)
        else:
            target = node

        department = self._resolve_department(target["id"])
        vessel_name = self._resolve_vessel_name_for_node(target["id"])
        dest_path = store.path_of(target["id"])
        display = self._display(uploaded_by_email, uploaded_by_name)

        if self._is_admin(uploaded_by_email):
            path = store.place_file(target["id"], filename, content, content_type)
            approval = store.create_activity(
                action_type="upload",
                requesting_email=uploaded_by_email or "",
                requesting_name=uploaded_by_name,
                department=department,
                vessel_name=vessel_name,
                target_id=target["id"],
                target_description=filename,
                payload={},
                message=(
                    f"SPE Admin ({uploaded_by_email}) uploaded '{filename}' to {dest_path}. "
                    f"No approval was required."
                ),
                filename=filename,
                content_type=content_type,
                destination_folder_id=target["id"],
                destination_path=dest_path,
                final_path=path,
            )
            return _approval_as_job(approval, completed=True)

        approval = store.create_approval(
            target["id"],
            dest_path,
            filename,
            content,
            content_type,
            uploaded_by_email,
            uploaded_by_name,
            department=department,
            vessel_name=vessel_name,
            message=(
                f"{display} ({uploaded_by_email}) is requesting approval to upload "
                f"'{filename}' to {dest_path}."
            ),
        )
        await notify_email(
            settings.admin_emails,
            f"New document pending approval: {filename}",
            f"{uploaded_by_name or uploaded_by_email} uploaded '{filename}' to "
            f"{approval['destination_path']}. Review it in the DMS approvals page.",
        )
        return _approval_as_job(approval)

    async def month_upload(self, folder_id, filename, category, content, content_type, uploaded_by_email, uploaded_by_name):
        node = store.get_node(folder_id)
        if node is None:
            raise NotFound("Folder not found")
        if not node["month_driven"]:
            raise BadRequest("This folder is not a month-driven folder")
        target, detected = store.resolve_month_target(folder_id, filename, category)

        department = self._resolve_department(target["id"])
        vessel_name = self._resolve_vessel_name_for_node(target["id"])
        dest_path = store.path_of(target["id"])
        display = self._display(uploaded_by_email, uploaded_by_name)

        if self._is_admin(uploaded_by_email):
            path = store.place_file(target["id"], filename, content, content_type)
            approval = store.create_activity(
                action_type="upload",
                requesting_email=uploaded_by_email or "",
                requesting_name=uploaded_by_name,
                department=department,
                vessel_name=vessel_name,
                target_id=target["id"],
                target_description=filename,
                payload={},
                message=(
                    f"SPE Admin ({uploaded_by_email}) uploaded '{filename}' to {dest_path}. "
                    f"No approval was required."
                ),
                filename=filename,
                content_type=content_type,
                destination_folder_id=target["id"],
                destination_path=dest_path,
                is_month_upload=True,
                category=category,
                detected_month=detected,
                final_path=path,
            )
            return _approval_as_job(approval, completed=True)

        approval = store.create_approval(
            target["id"],
            dest_path,
            filename,
            content,
            content_type,
            uploaded_by_email,
            uploaded_by_name,
            is_month_upload=True,
            category=category,
            detected_month=detected,
            department=department,
            vessel_name=vessel_name,
            message=(
                f"{display} ({uploaded_by_email}) is requesting approval to upload "
                f"'{filename}' to {dest_path}."
            ),
        )
        await notify_email(
            settings.admin_emails,
            f"New document pending approval: {filename}",
            f"{uploaded_by_name or uploaded_by_email} uploaded '{filename}' to "
            f"{approval['destination_path']}. Review it in the DMS approvals page.",
        )
        return _approval_as_job(approval)

    # ------------------------------------------------------------ folders
    async def create_subfolder(
        self, folder_id: str, name: str, requesting_email=None, requesting_name=None,
    ):
        """Manually create a named sub-folder inside a month_driven folder."""
        cleaned_name = store._validate_subfolder_name(folder_id, name)
        parent_node = store.get_node(folder_id)
        department = self._resolve_department(folder_id)
        vessel_name = self._resolve_vessel_name_for_node(folder_id)
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" for vessel {vessel_name}" if vessel_name else ""
        payload = {
            "parent_folder_id": folder_id,
            "name": cleaned_name,
            "requesting_email": requesting_email,
            "requesting_name": requesting_name,
        }
        return await self._admin_or_pending(
            action_type="create_folder",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_name=vessel_name,
            target_id=folder_id,
            target_description=cleaned_name,
            payload=payload,
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to create the "
                f"folder '{cleaned_name}' inside '{parent_node['name']}'{vessel_clause}."
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) created the folder '{cleaned_name}' "
                f"inside '{parent_node['name']}'{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_create_subfolder(payload),
        )

    async def _execute_create_subfolder(self, payload):
        node = store.create_subfolder(payload["parent_folder_id"], payload["name"])
        parent_path = store.path_of(payload["parent_folder_id"])
        folder_path = f"{parent_path} / {node['name']}" if parent_path else node["name"]
        store.add_folder_alert(
            drive_item_id=node["id"],
            folder_name=node["name"],
            folder_path=folder_path,
            parent_folder_id=payload["parent_folder_id"],
            vessel_name=self._resolve_vessel_name_for_node(payload["parent_folder_id"]),
            department=self._resolve_department(payload["parent_folder_id"]),
            created_by_email=payload.get("requesting_email") or "",
            created_by_name=payload.get("requesting_name") or "",
            alert_type="folder_created",
        )
        return node

    async def delete_folder(
        self, folder_id: str, requesting_email=None, requesting_name=None,
    ):
        node = store.get_node(folder_id)
        folder_name = folder_id
        if node is None:
            # Try finding node by path or name
            clean_name = folder_id.replace("\\", "/").replace(" > ", "/").strip("/").split("/")[-1]
            for n_id, n_data in getattr(store, "_nodes", {}).items():
                if n_data.get("name", "").lower() == clean_name.lower():
                    node = n_data
                    folder_id = n_id
                    break
        if node:
            folder_name = node.get("name", folder_id)
        department = self._resolve_department(folder_id) if node else "Technical & Crewing"
        vessel_name = self._resolve_vessel_name_for_node(folder_id) if node else ""
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel_name}" if vessel_name else ""
        return await self._admin_or_pending(
            action_type="delete_folder",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_name=vessel_name,
            target_id=folder_id,
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
            execute=lambda: self._execute_delete_folder(folder_id),
        )

    async def _execute_delete_folder(self, folder_id):
        if store.get_node(folder_id) is None:
            return {"deleted": True}
        return {"deleted": store.delete_folder(folder_id)}

    async def get_file(self, file_id):
        return store.get_file(file_id)

    async def delete_file(
        self, file_id: str, requesting_email=None, requesting_name=None, reason=None,
    ):
        node = store.get_node(file_id)
        if node is None or node["kind"] != "file":
            raise NotFound("File not found")
        clean_reason = (reason or "").strip()
        if not self._is_admin(requesting_email) and not clean_reason:
            raise BadRequest("A reason for deletion is required")
        department = self._resolve_department(node["parent_id"])
        vessel_name = self._resolve_vessel_name_for_node(file_id)
        display = self._display(requesting_email, requesting_name)
        vessel_clause = f" from vessel {vessel_name}" if vessel_name else ""
        reason_clause = f" Reason: \"{clean_reason}\"" if clean_reason else ""
        return await self._admin_or_pending(
            action_type="delete_document",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_name=vessel_name,
            target_id=file_id,
            target_description=node["name"],
            payload={"reason": clean_reason} if clean_reason else {},
            pending_message=(
                f"{display} ({requesting_email}) is requesting approval to delete the "
                f"document '{node['name']}'{vessel_clause}.{reason_clause}"
            ),
            activity_message=(
                f"SPE Admin ({requesting_email}) deleted the document '{node['name']}'"
                f"{vessel_clause}. No approval was required."
            ),
            execute=lambda: self._execute_delete_file(file_id),
        )

    async def _execute_delete_file(self, file_id):
        node = store.get_node(file_id)
        if node is None or node["kind"] != "file":
            return {"deleted": False}
        return {"deleted": store.delete_file(file_id)}

    async def search(self, q, vessel_id=None):
        return store.search(q, vessel_id)

    async def get_job(self, job_id):
        return store.get_job(job_id)

    def _resolve_item_context(self, item_id, item_name=None, department=None, vessel_name=None):
        """Best-effort name/department/vessel resolution for archive/restore
        actions, using frontend-supplied overrides first (needed for recycle
        bin items that may already be gone from the live tree) and falling
        back to the in-memory node when it's still resolvable."""
        node = store.get_node(item_id)
        resolved_name = item_name or (node["name"] if node else item_id)
        if department:
            resolved_department = department
        elif node:
            parent_for_dept = node["parent_id"] if node["kind"] == "file" else item_id
            resolved_department = self._resolve_department(parent_for_dept)
        else:
            resolved_department = "All Departments"
        resolved_vessel = vessel_name or (self._resolve_vessel_name_for_node(item_id) if node else None)
        return resolved_name, resolved_department, resolved_vessel

    async def archive_item(
        self, item_id: str, item_type: str, requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None, reason=None,
    ):
        name, dept, vessel = self._resolve_item_context(item_id, item_name, department, vessel_name)
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
        store.archive_item(item_id, item_type)
        return {"archived": True}

    async def restore_item(
        self, item_id: str, item_type: str = "folder", requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None,
    ):
        name, dept, vessel = self._resolve_item_context(item_id, item_name, department, vessel_name)
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
        store.restore_item(item_id)
        return {"restored": True}

    async def get_archived_ids(self) -> list[str]:
        return store.get_archived_ids()

    def _stub_original_path(self, node_id: str) -> str:
        path_parts = []
        curr = store.get_node(node_id)
        while curr:
            path_parts.insert(0, curr["name"])
            parent_id = curr.get("parent_id")
            curr = store.get_node(parent_id) if parent_id else None
        return "/".join(path_parts)

    async def get_archived_nodes(self):
        ids = store.get_archived_ids()
        out = []
        for i in ids:
            node = store.get_node(i)
            if node:
                s = store.serialize(node, depth=0)
                original_path = self._stub_original_path(i)
                main_folder = original_path.split("/", 1)[0] if "/" in original_path else original_path
                s["main_folder"] = main_folder
                s["original_path"] = original_path
                s["archived_at"] = node.get("archived_at") or "2025-01-10T14:20:00Z"
                out.append(s)
        return out

    async def get_deleted_ids(self) -> list[str]:
        return store.get_deleted_ids()

    async def get_deleted_nodes(self):
        ids = store.get_deleted_ids()
        out = [
            {key: value for key, value in record.items() if key != "vessel"}
            for record in getattr(store, "deleted_vessel_records", {}).values()
        ]
        deleted_ship_folder_ids = {
            folder_id
            for record in getattr(store, "deleted_vessel_records", {}).values()
            for folder_id in record.get("ship_folder_ids", [])
        }
        for i in ids:
            if i in deleted_ship_folder_ids:
                continue
            node = store.get_node(i)
            if node:
                s = store.serialize(node, depth=0)
                original_path = self._stub_original_path(i)
                main_folder = original_path.split("/", 1)[0] if "/" in original_path else original_path
                s["main_folder"] = main_folder
                s["original_path"] = original_path
                parts = original_path.split("/")
                vessel_name = node.get("vessel") or self._resolve_vessel_name_for_node(i) or ""
                s["vessel_name"] = vessel_name
                s["category"] = parts[-2] if len(parts) > 2 else main_folder
                s["sub_category"] = ""
                s["deleted_at"] = node.get("deleted_at") or "2026-07-20T08:30:00Z"
                ext = node.get("ext", "").upper()
                s["item_type"] = "File folder" if node["kind"] != "file" else (f"{ext} File" if ext else "File")
                out.append(s)
        return out

    async def restore_deleted_item(
        self, item_id: str, item_type: str = "folder", requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None,
    ):
        name, dept, vessel = self._resolve_item_context(item_id, item_name, department, vessel_name)
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
        deleted_vessels = getattr(store, "deleted_vessel_records", {})
        record = deleted_vessels.pop(item_id, None)
        if record:
            vessel = record.get("vessel")
            if vessel and not any(v["id"] == vessel["id"] for v in store.vessels):
                store.vessels.append(vessel)
            for folder_id in record.get("ship_folder_ids", []):
                store.deleted_ids.discard(folder_id)
            store.deleted_vessel_records = deleted_vessels
            return {"restored": True}
        return {"restored": store.restore_deleted_item(item_id)}

    async def permanent_delete_item(
        self, item_id: str, item_type: str, requesting_email=None, requesting_name=None,
        item_name=None, department=None, vessel_name=None,
    ):
        name, dept, vessel = self._resolve_item_context(item_id, item_name, department, vessel_name)
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
        deleted_vessels = getattr(store, "deleted_vessel_records", {})
        if item_id in deleted_vessels:
            record = deleted_vessels.pop(item_id, None)
            for folder_id in (record or {}).get("ship_folder_ids", []):
                store.permanent_delete_item(folder_id, "folder")
            store.deleted_vessel_records = deleted_vessels
            return {"deleted": True}
        return {"deleted": store.permanent_delete_item(item_id, item_type)}

    # -------------------------------------------------------------- approvals
    async def list_approvals(self, status=None, q=None):
        return store.list_approvals(status, q)

    async def list_folder_alerts(self, unread_only=False):
        return store.list_folder_alerts(unread_only=unread_only)

    async def mark_folder_alert_read(self, alert_id: str, read: bool = True):
        return store.mark_alert_read(alert_id, read=read)

    async def mark_all_folder_alerts_read(self):
        return {"marked": store.mark_all_alerts_read()}

    async def get_approval(self, request_id):
        approval = store.get_approval(request_id)
        if approval is None:
            raise NotFound("Approval request not found")
        return approval

    async def get_approval_file(self, request_id):
        return store.get_approval_file(request_id)

    async def approve_request(self, request_id, decided_by_email):
        approval = store.get_approval(request_id)
        if approval is None:
            raise NotFound("Approval request not found")
        if approval["status"] != "pending":
            raise Conflict(f"This request has already been {approval['status']}")
        action_type = approval.get("action_type", "upload")

        if action_type == "upload":
            result = store.approve_approval(request_id, decided_by_email)
            await notify_email(
                approval["uploaded_by_email"],
                f"Your document '{approval['filename']}' was approved",
                f"'{approval['filename']}' has been approved and filed to "
                f"{result['final_path']}.",
            )
            return result

        # Non-upload actions: re-validate the target still exists, execute
        # the deferred mutation, then flip status. If the target vanished in
        # the meantime, resolve the request as rejected rather than erroring.
        payload = approval.get("payload") or {}
        if action_type == "delete_document":
            if store.get_node(approval.get("target_id")) is None:
                return store.mark_rejected(request_id, decided_by_email, "Target no longer exists")
            await self._execute_delete_file(approval["target_id"])
        elif action_type == "delete_folder":
            if store.get_node(approval.get("target_id")) is None:
                return store.mark_rejected(request_id, decided_by_email, "Target no longer exists")
            await self._execute_delete_folder(approval["target_id"])
        elif action_type == "create_folder":
            await self._execute_create_subfolder(payload)
        elif action_type == "create_vessel":
            await self._execute_create_vessel(payload)
        elif action_type == "update_vessel":
            vessel_id = payload.get("vessel_id")
            if not any(v["id"] == vessel_id for v in store.vessels):
                return store.mark_rejected(request_id, decided_by_email, "Target no longer exists")
            await self._execute_update_vessel(payload)
        elif action_type == "delete_vessel":
            vessel_id = payload.get("vessel_id") or approval.get("target_id")
            if not any(v["id"] == vessel_id for v in store.vessels):
                return store.mark_rejected(request_id, decided_by_email, "Target no longer exists")
            await self._execute_delete_vessel(vessel_id)
        elif action_type == "archive_item":
            await self._execute_archive(approval["target_id"], payload.get("item_type", "folder"))
        elif action_type == "restore_item":
            await self._execute_restore(approval["target_id"])
        elif action_type == "restore_from_recycle_bin":
            await self._execute_restore_deleted(approval["target_id"])
        elif action_type == "permanent_delete":
            await self._execute_permanent_delete(approval["target_id"], payload.get("item_type", "folder"))
        else:
            raise BadRequest(f"Unknown action type: {action_type}")

        return store.mark_approved(request_id, decided_by_email)

    async def reject_request(self, request_id, decided_by_email, reason=None):
        approval = store.get_approval(request_id)
        if approval is None:
            raise NotFound("Approval request not found")
        if approval["status"] != "pending":
            raise Conflict(f"This request has already been {approval['status']}")
        action_type = approval.get("action_type", "upload")

        if action_type == "upload":
            result = store.reject_approval(request_id, decided_by_email, reason)
            await notify_email(
                approval["uploaded_by_email"],
                f"Your document '{approval['filename']}' was rejected",
                f"'{approval['filename']}' was rejected"
                + (f" ({reason})" if reason else "")
                + f" and moved to {result['final_path']}.",
            )
            return result

        # Non-upload actions: nothing was staged/created, so rejection is
        # just a state transition.
        return store.mark_rejected(request_id, decided_by_email, reason)

    async def _build_pool_slot(self) -> int:
        """Stub implementation for building a pool slot (no-op)."""
        return 0


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
