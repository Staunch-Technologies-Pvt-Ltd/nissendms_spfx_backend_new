"""In-memory store for the stub backend.

Builds the folder tree from the declarative template, supports adding vessels
(which clones the per-ship sub-tree under all three main folders), and fakes the
upload + month-folder behaviour so the UI can be built against realistic data.
"""
import itertools
import re
from datetime import date, datetime

from . import template
from .ocr.drawing_category import classify_drawing_category

from .services.errors import Conflict, DuplicateFile, InternalServerError  # noqa: E402  (re-exported for callers)

_ids = itertools.count(1)


def _new_id():
    return str(next(_ids))


class Store:
    def __init__(self):
        # Flat map: id -> node dict.
        self.nodes = {}
        # Ordered list of vessel ids.
        self.vessels = []
        # id -> job dict.
        self.jobs = {}
        self._job_ids = itertools.count(1)
        self.archived_ids = set()
        self.deleted_ids = set()
        # id -> pending/approved/rejected approval request dict.
        self.approvals = {}
        self._approval_ids = itertools.count(1)
        # Top-header alert bell: folder-creation alerts (in-memory for stub mode).
        self.folder_alerts = []
        self._alert_ids = itertools.count(1)
        self._build_roots()

    # ------------------------------------------------------------------ build
    def _make_node(self, name, kind, parent_id, *, month_children=None, ext=None):
        node = {
            "id": _new_id(),
            "name": name,
            "kind": kind,
            "parent_id": parent_id,
            "children": [],
            "upload": kind in ("leaf", "month_driven", "month", "drawing_classifier"),
            "month_driven": kind == "month_driven",
        }
        if month_children is not None:
            node["month_children"] = month_children
        if ext is not None:
            node["ext"] = ext  # for file nodes
        self.nodes[node["id"]] = node
        if parent_id is not None:
            self.nodes[parent_id]["children"].append(node["id"])
        return node

    def _build_subtree(self, spec, parent_id):
        node = self._make_node(
            spec["name"],
            spec["kind"],
            parent_id,
            month_children=spec.get("month_children"),
        )
        for child in spec.get("children", []):
            self._build_subtree(child, node["id"])
        return node

    def _build_roots(self):
        self.roots = []
        self.main_folders = {}  # name -> node

        # Top-level main folders at Documents root
        for name in template.MAIN_FOLDERS:
            main = self._make_node(name, "main", None)
            self.roots.append(main["id"])
            self.main_folders[name] = main
            # Insurance uses a different folder name for its common area
            common_name = (
                template.INSURANCE_COMMON_FOLDER_NAME
                if name == "Insurance"
                else template.COMMON_SHIPS_ROOT
            )
            common = self._make_node(common_name, "common", main["id"])
            for spec in template.COMMON_TEMPLATE[name]:
                self._build_subtree(spec, common["id"])

        # Kaizen - Knowledge Bank at Documents root
        kaizen_name = template.FLAT_MAIN_FOLDERS[0]
        kaizen = self._make_node(kaizen_name, "main", None)
        self.roots.append(kaizen["id"])
        self.main_folders[kaizen_name] = kaizen
        for spec in template.FLAT_TEMPLATE[kaizen_name]:
            self._build_subtree(spec, kaizen["id"])

        self._seed_sample_archive_and_recycle_bin()

    def _seed_sample_archive_and_recycle_bin(self):
        tech_main = self.main_folders.get("Technical & Crewing")
        comm_main = self.main_folders.get("Commercial & Chartering")
        parent_id = tech_main["id"] if tech_main else (self.roots[0] if self.roots else None)
        comm_id = comm_main["id"] if comm_main else parent_id

        if not parent_id:
            return

        # Seed Archived Items
        a1 = self._make_node("Expired_Class_Certificate_2024.pdf", "file", parent_id, ext="pdf")
        a1["size"] = 1258291
        a1["modified"] = "2024-12-15T09:00:00Z"
        a1["archived_at"] = "2025-01-10T14:20:00Z"
        self.archived_ids.add(a1["id"])

        a2 = self._make_node("Old_Drydock_Report_2023.pdf", "file", parent_id, ext="pdf")
        a2["size"] = 3565158
        a2["modified"] = "2023-11-20T14:30:00Z"
        a2["archived_at"] = "2024-02-01T11:15:00Z"
        self.archived_ids.add(a2["id"])

        if comm_id:
            a3 = self._make_node("Prior_Charter_Agreement_2022.pdf", "file", comm_id, ext="pdf")
            a3["size"] = 870400
            a3["modified"] = "2022-08-05T10:00:00Z"
            a3["archived_at"] = "2023-01-15T16:45:00Z"
            self.archived_ids.add(a3["id"])

        # Seed Deleted / Recycle Bin Items
        d1 = self._make_node("Draft_Inspection_Notes_v1.docx", "file", parent_id, ext="docx")
        d1["size"] = 460800
        d1["modified"] = "2026-07-10T11:15:00Z"
        d1["deleted_at"] = "2026-07-20T08:30:00Z"
        self.deleted_ids.add(d1["id"])

        d2 = self._make_node("Temp_Crew_List_June.xlsx", "file", parent_id, ext="xlsx")
        d2["size"] = 122880
        d2["modified"] = "2026-06-28T16:45:00Z"
        d2["deleted_at"] = "2026-07-25T13:40:00Z"
        self.deleted_ids.add(d2["id"])

        d3 = self._make_node("Unverified_Drawing_v2.dwg", "file", parent_id, ext="dwg")
        d3["size"] = 2202009
        d3["modified"] = "2026-05-14T09:20:00Z"
        d3["deleted_at"] = "2026-07-28T17:10:00Z"
        self.deleted_ids.add(d3["id"])

    # ----------------------------------------------------------------- vessels
    def add_vessel(self, name, imo=None, shipyard=None, hull_number=None, vessel_type=None):
        ship_folder_ids = {}
        for main_name in template.MAIN_FOLDERS:
            if main_name in template.FLAT_MAIN_FOLDERS:
                continue
            parent_main = self.main_folders.get(main_name)
            if parent_main:
                ship_node = self._make_node(name, "ship", parent_main["id"])
                ship_node["vessel"] = name
                for spec in template.SHIP_TEMPLATE[main_name]:
                    self._build_subtree(spec, ship_node["id"])
                ship_folder_ids[main_name] = ship_node["id"]
        vessel = {
            "id": _new_id(),
            "name": name,
            "imo": imo,
            "shipyard": shipyard,
            "hull_number": hull_number,
            "vessel_type": vessel_type,
            "ship_folders": ship_folder_ids,
        }
        self.vessels.append(vessel)
        # Pre-seed the current + next month folders to showcase scheduled creation.
        today = date.today()
        for ship_id in ship_folder_ids.values():
            for md in self._descendant_month_driven(ship_id):
                self.ensure_month_folder(md["id"], today.year, today.month)
                nm_year, nm_month = _next_month(today.year, today.month)
                self.ensure_month_folder(md["id"], nm_year, nm_month)
        return vessel

    def update_vessel(self, vessel_id, name=None, imo=None, shipyard=None, hull_number=None, vessel_type=None):
        vessel = next((v for v in self.vessels if v["id"] == vessel_id), None)
        if not vessel:
            return None
        
        if name is not None:
            new_name = name.strip()
            if new_name:
                old_name = vessel["name"]
                vessel["name"] = new_name
                # Rename the ship root node under Specific Vessels
                for cid in self.nodes.get(self._specific_vessels_id, {}).get("children", []):
                    node = self.nodes.get(cid)
                    if node and node.get("kind") == "ship" and node.get("vessel") == old_name:
                        node["name"] = new_name
                        node["vessel"] = new_name
                        break

        if imo is not None:
            vessel["imo"] = imo.strip() or None
        if shipyard is not None:
            vessel["shipyard"] = shipyard.strip() or None
        if hull_number is not None:
            vessel["hull_number"] = hull_number.strip() or None
        if vessel_type is not None:
            vessel["vessel_type"] = vessel_type.strip() or None
            
        return vessel

    def _descendant_month_driven(self, root_id):
        out = []
        stack = [root_id]
        while stack:
            nid = stack.pop()
            node = self.nodes[nid]
            if node["month_driven"]:
                out.append(node)
            stack.extend(node["children"])
        return out

    # ----------------------------------------------------------------- folders
    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def serialize(self, node, depth=1):
        """Return a node with `depth` levels of nested children (depth<0 = all)."""
        out = {
            "id": node["id"],
            "name": node["name"],
            "kind": node["kind"],
            "upload": node["upload"],
            "month_driven": node["month_driven"],
            "has_children": bool(node["children"]),
        }
        if "ext" in node:
            out["ext"] = node["ext"]
        if node["kind"] == "file":
            out["size"] = node.get("size")
            out["modified"] = node.get("modified")
        if node["month_driven"]:
            out["categories"] = [c["name"] for c in node.get("month_children", [])]
        if depth != 0:
            out["children"] = [
                self.serialize(self.nodes[c], depth - 1) for c in node["children"]
            ]
        return out

    def tree(self):
        return [self.serialize(self.nodes[r], depth=-1) for r in self.roots]

    def mains(self):
        return [self.serialize(self.nodes[r], depth=0) for r in self.roots]

    def stats(self):
        files = months = month_driven = 0
        for node in self.nodes.values():
            if node["kind"] == "file":
                files += 1
            elif node["kind"] == "month":
                months += 1
            if node["month_driven"]:
                month_driven += 1
        return {
            "vessels": len(self.vessels),
            "main_folders": len(self.roots),
            "month_driven": month_driven,
            "months": months,
            "documents": files,
        }

    def children(self, node_id):
        node = self.nodes[node_id]
        return [self.serialize(self.nodes[c], depth=1) for c in node["children"] if c not in self.deleted_ids]

    # ------------------------------------------------------------ month folders
    def ensure_month_folder(self, month_driven_id, year, month):
        """Create `{Month YYYY}` (+ its category children) under a month_driven
        folder if absent; return the month folder node."""
        md = self.nodes[month_driven_id]
        label = f"{_MONTHS[month - 1]} {year}"
        for cid in md["children"]:
            if self.nodes[cid]["name"] == label:
                return self.nodes[cid]
        month_node = self._make_node(label, "month", md["id"])
        month_node["is_month"] = True
        for cat in md.get("month_children", []):
            self._build_subtree(cat, month_node["id"])
        return month_node

    # ----------------------------------------------------------------- uploads
    def _validate_subfolder_name(self, parent_id: str, name: str) -> str:
        """Validate + clean a proposed sub-folder name without creating it.
        Split out of create_subfolder so callers can validate up front
        (before an approval decision is made) and create later."""
        from .services.errors import BadRequest, Conflict
        parent = self.nodes.get(parent_id)
        if parent is None:
            from .services.errors import NotFound
            raise NotFound("Parent folder not found")
        if not parent["month_driven"]:
            raise BadRequest("Can only create sub-folders inside month-driven folders")
        from .services.normalize import clean_folder_name
        name = clean_folder_name(name)
        if not name:
            raise BadRequest("Folder name is required")
        if not any(c.isalpha() for c in name):
            raise BadRequest("Folder name must contain alphabetic characters (letters)")

        # Replace slashes/backslashes/colons with hyphens, and * ? " < > | with underscores
        name = name.replace("/", "-").replace("\\", "-").replace(":", "-")
        for c in '*?"<>|':
            name = name.replace(c, "_")
        name = name.strip(" .")
        matching_name = self._has_child_named(parent_id, name)
        if matching_name:
            raise Conflict(f"A folder with a similar name '{matching_name}' already exists here (ignoring casing, spaces, and special characters)")
        return name

    def create_subfolder(self, parent_id: str, name: str):
        """Manually create a named sub-folder inside a month_driven folder,
        including its category children."""
        name = self._validate_subfolder_name(parent_id, name)
        parent = self.nodes[parent_id]
        month_node = self._make_node(name, "month", parent_id)
        month_node["is_month"] = True
        for cat in parent.get("month_children", []):
            self._build_subtree(cat, month_node["id"])
        return self.serialize(month_node, depth=1)

    # ----------------------------------------------------------------- uploads
    def _add_file(self, parent_id, filename, content=b"", content_type=""):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        node = self._make_node(filename, "file", parent_id, ext=ext)
        node["content"] = content
        node["content_type"] = content_type or "application/octet-stream"
        node["size"] = len(content)
        node["modified"] = datetime.now().isoformat()
        return node

    def _has_child_named(self, parent_id, name):
        from .services.normalize import normalize_folder_name
        normalized_name = normalize_folder_name(name)
        for c in self.nodes[parent_id]["children"]:
            child_node = self.nodes[c]
            if child_node["kind"] != "file":
                if normalize_folder_name(child_node["name"]) == normalized_name:
                    return child_node["name"]
        return None


    def _find_file_globally(self, filename: str, exclude_folder_id: str | None = None):
        """Return (node, folder_path) if a file with the same name already exists
        in ANY folder other than `exclude_folder_id`, or (None, None) otherwise."""
        name_lc = filename.lower()
        for node in self.nodes.values():
            if node["kind"] != "file":
                continue
            if node["name"].lower() != name_lc:
                continue
            parent_id = node["parent_id"]
            if exclude_folder_id and parent_id == exclude_folder_id:
                continue
            return node, self._path_of(parent_id)
        return None, None

    def _path_of(self, node_id):
        parts = []
        nid = node_id
        while nid is not None:
            n = self.nodes[nid]
            parts.append(n["name"])
            nid = n["parent_id"]
        return " / ".join(reversed(parts))

    def path_of(self, node_id):
        return self._path_of(node_id)

    def find_by_path(self, path):
        """Resolve a '/'-joined logical folder path (e.g.
        'Folder-3 Insurance/Bow Fighter') to a node id by walking name
        segments from the roots. An optional leading 'Vessel Management'
        segment is ignored, since that root isn't itself a stored node."""
        segments = [seg.strip() for seg in (path or "").split("/") if seg.strip()]
        if segments and segments[0] == "Vessel Management":
            segments = segments[1:]
        if not segments:
            return None
        candidates = [self.nodes[r] for r in self.roots]
        node = None
        for seg in segments:
            node = next((n for n in candidates if n["name"] == seg), None)
            if node is None:
                return None
            candidates = [self.nodes[c] for c in node["children"]]
        return node["id"] if node is not None else None

    def place_file(self, folder_id, filename, content=b"", content_type=""):
        """Actually add a file node under folder_id. Raises DuplicateFile on collision.

        This is the low-level primitive shared by the approval workflow's
        approve step (whichever folder was ultimately resolved for a request).
        """
        if self._has_child_named(folder_id, filename):
            raise DuplicateFile(f"'{filename}' already exists in this folder")
        _, existing_path = self._find_file_globally(filename, exclude_folder_id=folder_id)
        if existing_path:
            parts = [p.strip() for p in existing_path.split(" / ") if p.strip()]
            if len(parts) >= 2:
                main_folder = parts[0]
                vessel_name = parts[1]
                leaf_folder = parts[-1]
                msg = (
                    f"Duplicate files upload, file already exists in folder '{leaf_folder}' "
                    f"under main folder '{main_folder}' and vessel '{vessel_name}'"
                )
            else:
                msg = f"Duplicate files upload, file already exists in folder: {existing_path}"
            raise DuplicateFile(msg)
        self._add_file(folder_id, filename, content, content_type)
        return self._path_of(folder_id)

    def resolve_month_target(self, month_driven_id, filename, category=None):
        """Fake OCR month detection + ensure the month/category folders exist;
        return (target_node, detected_month_label) without placing any file."""
        # 1. Check duplicate first globally before anything else
        _, existing_path = self._find_file_globally(filename)
        if existing_path:
            parts = [p.strip() for p in existing_path.split(" / ") if p.strip()]
            if len(parts) >= 2:
                main_folder = parts[0]
                vessel_name = parts[1]
                leaf_folder = parts[-1]
                msg = (
                    f"Duplicate files upload, file already exists in folder '{leaf_folder}' "
                    f"under main folder '{main_folder}' and vessel '{vessel_name}'"
                )
            else:
                msg = f"Duplicate files upload, file already exists in folder: {existing_path}"
            raise DuplicateFile(msg)

        # 2. Check fitz (PyMuPDF) and paddleocr \u2014 if missing, fall back to To be Classified
        try:
            # pyrefly: ignore [missing-import]
            import fitz  # noqa: F401
            # pyrefly: ignore [missing-import]
            from paddleocr import PaddleOCR  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            # OCR libraries not installed \u2014 still allow upload, route to To be Classified
            pass

        # 3. Detect month
        year, month = _detect_month(filename)
        md = self.nodes[month_driven_id]
        if year is None:
            # No confident date -> month-agnostic "To be Classified".
            target = self.ensure_to_be_classified(md["id"])
            detected = None
        else:
            month_node = self.ensure_month_folder(month_driven_id, year, month)
            target = month_node
            cat_name = category or "To be Classified"
            for cid in month_node["children"]:
                if self.nodes[cid]["name"] == cat_name:
                    target = self.nodes[cid]
                    break
            detected = f"{_MONTHS[month - 1]} {year}"

        return target, detected

    def resolve_drawing_target(self, node_id, filename):
        """Fake OCR drawing-category detection by keyword-matching the
        filename (stub has no real OCR); route to the matched category leaf,
        or "Other Drawings" if nothing matches. Never routes automatically to
        "To be Classified" — that's reserved for documents that aren't even
        identified as belonging to the Drawings category."""
        node = self.nodes[node_id]
        category = classify_drawing_category(filename)
        target_name = category or "Other Drawings"
        for cid in node["children"]:
            if self.nodes[cid]["name"].lower() == target_name.lower():
                return self.nodes[cid], category
        return node, category

    def reject_target_for(self, destination_folder_id):
        """The sibling fallback folder for a rejected upload — found inside
        the same parent as the originally-selected destination. Reuses
        whichever fallback-named leaf already exists there ("To be
        Classified", "Other Drawings", or "Other Manuals"); if the
        destination itself already is one of those, reuse it as-is."""
        node = self.nodes[destination_folder_id]
        if node["name"].strip().lower() in template.FALLBACK_LEAF_NAMES:
            return destination_folder_id
        return self.ensure_to_be_classified(node["parent_id"])["id"]

    def ensure_to_be_classified(self, parent_id):
        for cid in self.nodes[parent_id]["children"]:
            if self.nodes[cid]["name"].strip().lower() in template.FALLBACK_LEAF_NAMES:
                return self.nodes[cid]
        return self._make_node("To be Classified", "leaf", parent_id)

    # ----------------------------------------------------------- files / search
    def get_file(self, node_id):
        node = self.nodes.get(node_id)
        if not node or node["kind"] != "file":
            return None
        return node["content"], node["content_type"], node["name"]

    def delete_folder(self, folder_id):
        """Soft delete a folder by adding its ID to deleted_ids."""
        if folder_id not in self.nodes:
            return False
        self.deleted_ids.add(folder_id)
        return True

    def delete_file(self, node_id):
        """Soft delete a file by adding its ID to deleted_ids."""
        if node_id not in self.nodes or self.nodes[node_id]["kind"] != "file":
            return False
        self.deleted_ids.add(node_id)
        return True


    def restore_deleted_item(self, item_id):
        if item_id in self.deleted_ids:
            self.deleted_ids.discard(item_id)
            return True
        return False

    def permanent_delete_item(self, item_id, item_type):
        self.deleted_ids.discard(item_id)
        if item_type == "folder":
            node = self.nodes.get(item_id)
            if not node:
                return False
            # Recursively collect all descendant IDs
            to_remove = []
            stack = [item_id]
            while stack:
                nid = stack.pop()
                n = self.nodes.get(nid)
                if n:
                    to_remove.append(nid)
                    stack.extend(n.get("children", []))
            # Remove from parent's children list
            pid = node.get("parent_id")
            if pid and pid in self.nodes:
                self.nodes[pid]["children"] = [
                    c for c in self.nodes[pid]["children"] if c != item_id
                ]
            # Delete all collected nodes
            for nid in to_remove:
                self.nodes.pop(nid, None)
                self.archived_ids.discard(nid)
                self.deleted_ids.discard(nid)
            return True
        else:
            node = self.nodes.get(item_id)
            if not node or node["kind"] != "file":
                return False
            pid = node["parent_id"]
            if pid is not None and item_id in self.nodes[pid]["children"]:
                self.nodes[pid]["children"].remove(item_id)
            self.nodes.pop(item_id, None)
            self.archived_ids.discard(item_id)
            return True

    def get_deleted_ids(self):
        return list(self.deleted_ids)

    def search(self, query, vessel_id=None):
        """Search folders + files by name. When `vessel_id` is given, walk
        only that vessel's own ship folders (one per main folder) instead of
        the full tree — other vessels' folders, and the shared "Common for
        all ships" areas, are never visited."""
        ql = query.lower().strip()
        if not ql:
            return []
        roots = self.roots
        if vessel_id is not None:
            vessel = next((v for v in self.vessels if v["id"] == vessel_id), None)
            if vessel is not None:
                roots = list(vessel["ship_folders"].values())
        results = []

        def walk(nid, trail):
            if nid in self.deleted_ids:
                return
            node = self.nodes[nid]
            t2 = trail + [{"id": nid, "name": node["name"]}]
            if node["kind"] != "main" and ql in node["name"].lower():
                results.append(
                    {
                        "id": nid,
                        "name": node["name"],
                        "kind": node["kind"],
                        "trail": t2,
                        "path": self._path_of(nid),
                    }
                )
            for c in node["children"]:
                walk(c, t2)

        for r in roots:
            walk(r, [])
        return results[:50]

    # ------------------------------------------------------------- approvals
    def _base_entry(
        self,
        *,
        entry_kind,
        action_type,
        status,
        requesting_email,
        requesting_name=None,
        department=None,
        vessel_id=None,
        vessel_name=None,
        target_id=None,
        target_description=None,
        payload=None,
        changes=None,
        message=None,
        filename=None,
        content=b"",
        content_type=None,
        destination_folder_id=None,
        destination_path=None,
        is_month_upload=False,
        category=None,
        detected_month=None,
        decided_by_email=None,
        decided_at=None,
        final_path=None,
    ):
        now = datetime.now().isoformat()
        req = {
            "id": str(next(self._approval_ids)),
            "filename": filename,
            "content": content,
            "content_type": content_type,
            "size": len(content) if content else 0,
            "uploaded_by_email": requesting_email,
            "uploaded_by_name": requesting_name or "",
            "uploaded_at": now,
            "destination_folder_id": destination_folder_id,
            "destination_path": destination_path,
            "is_month_upload": is_month_upload,
            "category": category,
            "detected_month": detected_month,
            "drive_item_id": None,
            "status": status,
            "decided_by_email": decided_by_email,
            "decided_at": decided_at,
            "rejection_reason": None,
            "final_path": final_path,
            "created_at": now,
            "entry_kind": entry_kind,
            "action_type": action_type,
            "department": department,
            "vessel_id": vessel_id,
            "vessel_name": vessel_name,
            "target_id": target_id,
            "target_description": target_description,
            "payload": payload or {},
            "changes": changes or [],
            "message": message,
        }
        self.approvals[req["id"]] = req
        return req

    def create_approval(
        self,
        destination_id,
        destination_path,
        filename,
        content,
        content_type,
        uploaded_by_email,
        uploaded_by_name,
        *,
        is_month_upload=False,
        category=None,
        detected_month=None,
        department=None,
        vessel_name=None,
        message=None,
    ):
        """Create a pending upload-approval request (entry_kind='approval',
        action_type='upload')."""
        for a in self.approvals.values():
            if (
                a["status"] == "pending"
                and a["destination_folder_id"] == destination_id
                and (a.get("filename") or "").lower() == filename.lower()
            ):
                raise Conflict(
                    f"A request for '{filename}' in this folder is already pending approval"
                )
        if self._has_child_named(destination_id, filename):
            raise DuplicateFile(filename)
        req = self._base_entry(
            entry_kind="approval",
            action_type="upload",
            status="pending",
            requesting_email=uploaded_by_email,
            requesting_name=uploaded_by_name,
            department=department,
            vessel_name=vessel_name,
            message=message,
            filename=filename,
            content=content,
            content_type=content_type or "application/octet-stream",
            destination_folder_id=destination_id,
            destination_path=destination_path,
            is_month_upload=is_month_upload,
            category=category,
            detected_month=detected_month,
        )
        return self.public_approval(req)

    def create_activity(
        self,
        *,
        action_type,
        requesting_email,
        requesting_name=None,
        department=None,
        vessel_id=None,
        vessel_name=None,
        target_id=None,
        target_description=None,
        payload=None,
        changes=None,
        message=None,
        filename=None,
        content_type=None,
        destination_folder_id=None,
        destination_path=None,
        is_month_upload=False,
        category=None,
        detected_month=None,
        final_path=None,
    ):
        """Record an already-completed SPE Admin action (entry_kind='activity',
        status='completed') — the mutation itself has already run by the time
        this is called; this is purely the audit-trail row."""
        now = datetime.now().isoformat()
        req = self._base_entry(
            entry_kind="activity",
            action_type=action_type,
            status="completed",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=target_id,
            target_description=target_description,
            payload=payload,
            changes=changes,
            message=message,
            filename=filename,
            content_type=content_type,
            destination_folder_id=destination_folder_id,
            destination_path=destination_path,
            is_month_upload=is_month_upload,
            category=category,
            detected_month=detected_month,
            decided_by_email=requesting_email,
            decided_at=now,
            final_path=final_path,
        )
        return self.public_approval(req)

    def create_pending_action(
        self,
        *,
        action_type,
        requesting_email,
        requesting_name=None,
        department=None,
        vessel_id=None,
        vessel_name=None,
        target_id=None,
        target_description=None,
        payload=None,
        changes=None,
        message=None,
    ):
        """Create a pending approval for a non-upload action (delete/create
        folder, create/update vessel). No mutation has happened yet — it's
        deferred until an admin approves."""
        req = self._base_entry(
            entry_kind="approval",
            action_type=action_type,
            status="pending",
            requesting_email=requesting_email,
            requesting_name=requesting_name,
            department=department,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            target_id=target_id,
            target_description=target_description,
            payload=payload,
            changes=changes,
            message=message,
        )
        return self.public_approval(req)

    def list_approvals(self, status=None, q=None):
        items = list(self.approvals.values())
        if status and status != "all":
            items = [a for a in items if a["status"] == status]
        if q:
            ql = q.lower().strip()
            def _match(a):
                haystacks = (
                    a.get("filename"), a.get("uploaded_by_email"), a.get("destination_path"),
                    a.get("target_description"), a.get("vessel_name"), a.get("message"),
                )
                return any(ql in (h or "").lower() for h in haystacks)
            items = [a for a in items if _match(a)]
        items.sort(key=lambda a: a["uploaded_at"], reverse=True)
        return [self.public_approval(a) for a in items]

    def get_approval(self, request_id):
        req = self.approvals.get(request_id)
        return self.public_approval(req) if req else None

    def get_approval_file(self, request_id):
        req = self.approvals.get(request_id)
        if not req:
            return None
        return req["content"], req["content_type"], req["filename"]

    def approve_approval(self, request_id, decided_by_email):
        """Upload-only: move the staged file to its destination."""
        req = self.approvals.get(request_id)
        if req is None:
            return None
        if req["status"] != "pending":
            raise Conflict(f"This request has already been {req['status']}")
        req["status"] = "approved"
        req["decided_by_email"] = decided_by_email
        req["decided_at"] = datetime.now().isoformat()
        try:
            path = self.place_file(
                req["destination_folder_id"], req["filename"], req["content"], req["content_type"]
            )
        except Exception:
            req["status"] = "pending"
            req["decided_by_email"] = None
            req["decided_at"] = None
            raise
        req["final_path"] = path
        req["content"] = b""  # release staged bytes once filed
        return self.public_approval(req)

    def reject_approval(self, request_id, decided_by_email, reason=None):
        """Upload-only: move the staged file to the sibling fallback folder
        ("To be Classified", or "Other Drawings"/"Other Manuals" as applicable)."""
        req = self.approvals.get(request_id)
        if req is None:
            return None
        if req["status"] != "pending":
            raise Conflict(f"This request has already been {req['status']}")
        req["status"] = "rejected"
        req["decided_by_email"] = decided_by_email
        req["decided_at"] = datetime.now().isoformat()
        req["rejection_reason"] = reason
        try:
            target_id = self.reject_target_for(req["destination_folder_id"])
            path = self.place_file(target_id, req["filename"], req["content"], req["content_type"])
        except Exception:
            req["status"] = "pending"
            req["decided_by_email"] = None
            req["decided_at"] = None
            req["rejection_reason"] = None
            raise
        req["final_path"] = path
        req["content"] = b""
        return self.public_approval(req)

    def mark_approved(self, request_id, decided_by_email):
        """Non-upload actions: flip status only — the caller has already
        executed (or is about to execute) the deferred mutation itself."""
        req = self.approvals.get(request_id)
        if req is None:
            return None
        req["status"] = "approved"
        req["decided_by_email"] = decided_by_email
        req["decided_at"] = datetime.now().isoformat()
        return self.public_approval(req)

    def mark_rejected(self, request_id, decided_by_email, reason=None):
        """Non-upload actions: nothing was staged/created, so rejecting is a
        pure state transition — no compensating action needed."""
        req = self.approvals.get(request_id)
        if req is None:
            return None
        req["status"] = "rejected"
        req["decided_by_email"] = decided_by_email
        req["decided_at"] = datetime.now().isoformat()
        req["rejection_reason"] = reason
        return self.public_approval(req)

    @staticmethod
    def public_approval(req):
        return {k: v for k, v in req.items() if k != "content"}

    # -------------------------------------------------------------------- jobs
    def _make_job(self, filename, status, dest_path, detected_month):
        job = {
            "id": str(next(self._job_ids)),
            "filename": filename,
            "status": "processing",  # flips to `status` after first poll
            "final_status": status,
            "destination": dest_path,
            "detected_month": detected_month,
            "polls": 0,
        }
        self.jobs[job["id"]] = job
        return self.public_job(job)

    def get_job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return None
        # Simulate async processing: first poll still "processing", then done.
        job["polls"] += 1
        if job["polls"] >= 2:
            job["status"] = job["final_status"]
        return self.public_job(job)

    @staticmethod
    def public_job(job):
        return {
            "id": job["id"],
            "filename": job["filename"],
            "status": job["status"],
            "destination": job["destination"],
            "detected_month": job["detected_month"],
        }

    def archive_item(self, item_id, item_type):
        self.archived_ids.add(item_id)

    def restore_item(self, item_id):
        self.archived_ids.discard(item_id)

    def get_archived_ids(self):
        return list(self.archived_ids)

    # ----------------------------------------------------------- folder alerts
    def add_folder_alert(
        self, *,
        drive_item_id=None, folder_name, folder_path, parent_folder_id=None,
        vessel_name=None, department="All Departments",
        created_by_email="", created_by_name="", alert_type="folder_created",
    ):
        now = datetime.now().isoformat()
        alert = {
            "id": str(next(self._alert_ids)),
            "drive_item_id": drive_item_id,
            "folder_name": folder_name,
            "folder_path": folder_path,
            "parent_folder_id": parent_folder_id,
            "vessel_name": vessel_name,
            "department": department,
            "created_by_email": created_by_email or "",
            "created_by_name": created_by_name or "",
            "alert_type": alert_type,
            "read": False,
            "created_at": now,
        }
        self.folder_alerts.insert(0, alert)
        return dict(alert)

    def list_folder_alerts(self, unread_only=False):
        items = self.folder_alerts
        if unread_only:
            items = [a for a in items if not a["read"]]
        return [dict(a) for a in items]

    def mark_alert_read(self, alert_id, read=True):
        for a in self.folder_alerts:
            if a["id"] == alert_id:
                a["read"] = read
                return dict(a)
        return None

    def mark_all_alerts_read(self):
        for a in self.folder_alerts:
            a["read"] = True
        return len(self.folder_alerts)


# --------------------------------------------------------------------- helpers
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_LOOKUP = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_LOOKUP.update({m[:3].lower(): i + 1 for i, m in enumerate(_MONTHS)})


def _next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _detect_month(filename):
    """Fake the PaddleOCR step by parsing a month/year out of the filename.

    Recognises `2026-07`, `2026_07`, `07-2026`, and month names (`July 2026`,
    `Jul-2026`). Returns (year, month) or (None, None)."""
    name = filename.lower()
    m = re.search(r"(20\d{2})[-_.](0[1-9]|1[0-2])", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(0[1-9]|1[0-2])[-_.](20\d{2})", name)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]?(20\d{2})", name)
    if m:
        return int(m.group(2)), _MONTH_LOOKUP[m.group(1)]
    return None, None


store = Store()