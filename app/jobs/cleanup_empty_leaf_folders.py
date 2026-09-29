"""Delete empty *leaf* folders under a project root in SharePoint Online.

Runs after the folder-provisioning scheduler (e.g. the Kaizen - Knowledge
Bank job) and removes the empty leaf folders it created.

What counts as deletable
------------------------
A folder is deleted only when ALL of these hold:

1. It is a leaf: it has no sub-folders (an empty sub-folder still makes the
   parent a non-leaf).
2. It holds zero items, checked three independent ways:
     - the Graph children listing (all pages) is empty,
     - the driveItem ``folder.childCount`` is 0,
     - the SharePoint roll-up columns ``ItemChildCount`` and
       ``FolderChildCount`` are both 0. These are computed by SharePoint
       itself, so they also count items the children listing may not return
       (hidden/system files, files with no checked-in version).
   If the roll-up columns can't be read the folder is skipped (fail safe),
   unless ``--allow-missing-rollup`` is passed.
3. It passes the ownership guards, so manually created folders are left
   alone:
     - template guard: its path relative to the root is listed in the
       template (the exact tree the scheduler creates), and/or
     - creator guard: its ``createdBy`` identity (user UPN / display name,
       or application id / name) is in the ``--created-by`` allow-list.
   At least one guard is required.
4. Its name doesn't match an exclude pattern (defaults: names starting with
   ``_``, and ``Forms``). Excluded folders are not walked and count as
   content, so their parents are never treated as empty.
5. It hasn't changed between the scan and the delete. The item is re-read
   right before deleting, and the DELETE is sent with ``If-Match: <eTag>``,
   so a file added in the meantime makes it fail with 412 and the folder is
   skipped.

The root folder itself is never deleted. Deleted folders go to the site
recycle bin (Graph DELETE on a driveItem is a recycle, not a purge).

Cascade (optional, ``--cascade``)
---------------------------------
With cascade on, when every sub-folder of a parent has been deleted and the
parent holds no files, the parent becomes a leaf and goes through the same
checks, repeating upward until the root. With cascade off (default), parents
are left in place even if they become empty.

Audit log
---------
One JSON object per line (JSONL), appended to ``--log-file`` (default
``logs/leaf_cleanup_audit.jsonl``) and flushed per record::

    {"ts": "...Z", "run_id": "...", "mode": "apply", "action": "deleted",
     "path": "Kaizen - Knowledge Bank/Lessons Learned", "item_id": "...",
     "reason": "empty leaf", "cascade_level": 0, "created_by": "...",
     "created_at": "..."}

``action`` is one of: deleted, would_delete (dry run), skipped, gone (already
deleted — idempotent re-run), error, summary.

Throttling / paging
-------------------
- Children listings follow ``@odata.nextLink`` to the end.
- The shared GraphClient already honours ``Retry-After`` on 429/503 (up to
  5 retries). On top of that, this job retries 429/503/504 with a longer
  exponential back-off, caps concurrent reads with a semaphore, runs deletes
  one at a time with a delay between them, and sends a decorated User-Agent
  as Microsoft's throttling guidance recommends.

Safety
------
- Dry run by default; nothing is deleted without ``--apply``.
- ``--max-deletes`` (default 500) aborts before deleting anything if the
  scan finds more candidates than expected (catches a wrong root or
  template).
- Idempotent: a second run finds nothing to do; a folder that disappeared
  mid-run is logged as ``gone``.

Usage
-----
    cd backend
    # dry run
    python -m app.jobs.cleanup_empty_leaf_folders \\
        --template app/jobs/cleanup_templates/kaizen_knowledge_bank.json
    # for real, cascading upward
    python -m app.jobs.cleanup_empty_leaf_folders \\
        --template app/jobs/cleanup_templates/kaizen_knowledge_bank.json \\
        --cascade --apply

Exit codes: 0 ok, 1 finished with errors, 2 bad configuration / aborted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import quote

log = logging.getLogger("leaf_cleanup")

USER_AGENT = "NONISV|NissenKaiun|VesselDMS-LeafCleanup/1.0"
DEFAULT_EXCLUDE_PATTERNS = [r"^_", r"^forms$"]
DEFAULT_LOG_FILE = Path("logs") / "leaf_cleanup_audit.jsonl"
_CHILD_SELECT = "id,name,folder,file,package,eTag,createdBy,createdDateTime"
_RETRYABLE = (429, 503, 504)


# --------------------------------------------------------------------------- helpers
def norm_path(path: str) -> str:
    """Case-insensitive, whitespace-normalised, slash-trimmed relative path."""
    parts = [" ".join(p.split()).casefold() for p in (path or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)


def identities(created_by: dict | None) -> set[str]:
    """All identity strings in a Graph ``createdBy`` identitySet, lower-cased."""
    out: set[str] = set()
    for key in ("user", "application", "device"):
        ident = (created_by or {}).get(key) or {}
        for attr in ("id", "email", "displayName", "userPrincipalName"):
            val = ident.get(attr)
            if val:
                out.add(str(val).strip().casefold())
    return out


def _describe_creator(created_by: dict | None) -> str:
    for key in ("user", "application"):
        ident = (created_by or {}).get(key) or {}
        name = ident.get("email") or ident.get("displayName") or ident.get("id")
        if name:
            return f"{key}:{name}"
    return ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- config
@dataclass
class CleanupConfig:
    drive_id: str
    root_path: str
    template_paths: set[str] | None = None      # normalised, relative to root
    created_by: set[str] = field(default_factory=set)  # lower-cased identities
    cascade: bool = False
    apply: bool = False
    log_file: Path = DEFAULT_LOG_FILE
    concurrency: int = 4
    delete_delay: float = 0.25
    max_deletes: int = 500
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    require_rollup_counts: bool = True

    def validate(self) -> None:
        if not self.drive_id:
            raise ValueError("drive_id is required")
        if not norm_path(self.root_path):
            raise ValueError("root_path must be a folder below the library root, not the library root itself")
        if self.template_paths is None and not self.created_by:
            raise ValueError(
                "At least one ownership guard is required: --template and/or --created-by. "
                "Without one, manually created empty folders could be deleted."
            )
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")


def load_template(path: str | Path) -> tuple[str | None, set[str]]:
    """Load a template JSON: {"root": "...", "paths": ["A", "A/B", ...]}.

    Every intermediate path is added automatically, so listing "A/B" also
    covers "A".
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    paths: set[str] = set()
    for raw in data.get("paths", []):
        segs = norm_path(raw).split("/")
        for i in range(1, len(segs) + 1):
            if segs[i - 1]:
                paths.add("/".join(segs[:i]))
    return data.get("root"), paths


# --------------------------------------------------------------------------- Graph access
class DriveAPI(Protocol):
    async def resolve_path(self, path: str) -> dict | None: ...
    async def list_children(self, item_id: str) -> list[dict]: ...
    async def get_item(self, item_id: str) -> dict | None: ...
    async def get_rollup_counts(self, item_id: str) -> tuple[int | None, int | None]: ...
    async def delete_item(self, item_id: str, etag: str | None) -> str: ...


class GraphDriveAPI:
    """Thin adapter over the backend's GraphClient with an outer throttling retry."""

    def __init__(self, client, drive_id: str, max_retries: int = 5):
        self._client = client
        self._drive = drive_id
        self._max_retries = max_retries

    async def _request(self, method: str, path: str, headers: dict | None = None):
        from ..graph.client import GraphError

        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        for attempt in range(self._max_retries + 1):
            try:
                return await self._client.request(method, path, headers=hdrs)
            except GraphError as exc:
                if exc.status in _RETRYABLE and attempt < self._max_retries:
                    delay = min(30 * (2 ** attempt), 300) + random.random() * 5
                    log.warning("Throttled (%s) on %s %s; backing off %.0fs", exc.status, method, path, delay)
                    await asyncio.sleep(delay)
                    continue
                raise

    async def _get_json(self, path: str) -> dict | None:
        from ..graph.client import GraphError

        try:
            return (await self._request("GET", path)).json()
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    async def resolve_path(self, path: str) -> dict | None:
        encoded = quote(path.strip("/"), safe="/")
        return await self._get_json(f"/drives/{self._drive}/root:/{encoded}?$select={_CHILD_SELECT}")

    async def list_children(self, item_id: str) -> list[dict]:
        items: list[dict] = []
        url: str | None = f"/drives/{self._drive}/items/{item_id}/children?$top=200&$select={_CHILD_SELECT}"
        while url:
            page = await self._get_json(url)
            if page is None:
                break
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
        return items

    async def get_item(self, item_id: str) -> dict | None:
        return await self._get_json(f"/drives/{self._drive}/items/{item_id}?$select={_CHILD_SELECT}")

    async def get_rollup_counts(self, item_id: str) -> tuple[int | None, int | None]:
        data = await self._get_json(
            f"/drives/{self._drive}/items/{item_id}/listItem"
            "?$select=id&$expand=fields($select=ItemChildCount,FolderChildCount)"
        )
        fields = (data or {}).get("fields") or {}
        return _to_int(fields.get("ItemChildCount")), _to_int(fields.get("FolderChildCount"))

    async def delete_item(self, item_id: str, etag: str | None) -> str:
        from ..graph.client import GraphError

        headers = {"If-Match": etag} if etag else None
        try:
            await self._request("DELETE", f"/drives/{self._drive}/items/{item_id}", headers=headers)
            return "deleted"
        except GraphError as exc:
            if exc.status == 404:
                return "gone"
            if exc.status == 412:
                return "changed"
            raise


# --------------------------------------------------------------------------- model
@dataclass(eq=False)
class FolderNode:
    id: str
    name: str
    rel_path: str                    # display path relative to root ("" for root)
    parent: "FolderNode | None"
    etag: str | None = None
    created_by: dict | None = None
    created_at: str | None = None
    listed_child_count: int | None = None
    child_folders: list["FolderNode"] = field(default_factory=list)
    content_count: int = 0           # files, packages, excluded folders, unknown items
    depth: int = 0


@dataclass
class CleanupReport:
    run_id: str
    mode: str
    folders_scanned: int = 0
    candidates: int = 0
    deleted: int = 0
    would_delete: int = 0
    skipped: int = 0
    gone: int = 0
    errors: int = 0
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.aborted


class AuditLog:
    def __init__(self, path: Path, run_id: str, mode: str, root_path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._run_id, self._mode = run_id, mode
        self._root = root_path.strip("/")

    def full_path(self, rel: str) -> str:
        return f"{self._root}/{rel}" if rel else self._root

    def write(self, action: str, node: FolderNode | None = None, reason: str = "", **extra) -> None:
        rec: dict[str, Any] = {"ts": _utc_now(), "run_id": self._run_id, "mode": self._mode, "action": action}
        if node is not None:
            rec.update({
                "path": self.full_path(node.rel_path),
                "item_id": node.id,
                "created_by": _describe_creator(node.created_by),
                "created_at": node.created_at,
            })
        if reason:
            rec["reason"] = reason
        rec.update(extra)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        level = logging.ERROR if action == "error" else logging.INFO
        log.log(level, "%s %s %s", action.upper(), rec.get("path", ""), reason)

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- job
class LeafFolderCleanup:
    def __init__(self, cfg: CleanupConfig, api: DriveAPI):
        cfg.validate()
        self.cfg = cfg
        self.api = api
        self._excludes = [re.compile(p, re.IGNORECASE) for p in cfg.exclude_patterns]
        self._sem = asyncio.Semaphore(cfg.concurrency)

    # ---- scan ---------------------------------------------------------
    def _excluded(self, name: str) -> bool:
        return any(p.search(name or "") for p in self._excludes)

    async def _scan(self, root_item: dict) -> list[FolderNode]:
        root = FolderNode(
            id=root_item["id"], name=root_item.get("name", ""), rel_path="", parent=None,
            etag=root_item.get("eTag"), created_by=root_item.get("createdBy"),
            created_at=root_item.get("createdDateTime"),
            listed_child_count=_to_int((root_item.get("folder") or {}).get("childCount")),
        )
        all_nodes = [root]
        level = [root]
        while level:
            async def expand(node: FolderNode) -> list[FolderNode]:
                async with self._sem:
                    children = await self.api.list_children(node.id)
                new: list[FolderNode] = []
                for ch in children:
                    is_folder = ch.get("folder") is not None and ch.get("package") is None
                    if not is_folder or self._excluded(ch.get("name", "")):
                        node.content_count += 1
                        continue
                    child = FolderNode(
                        id=ch["id"], name=ch.get("name", ""),
                        rel_path=f"{node.rel_path}/{ch.get('name', '')}".strip("/"),
                        parent=node, etag=ch.get("eTag"), created_by=ch.get("createdBy"),
                        created_at=ch.get("createdDateTime"),
                        listed_child_count=_to_int((ch.get("folder") or {}).get("childCount")),
                        depth=node.depth + 1,
                    )
                    node.child_folders.append(child)
                    new.append(child)
                return new

            results = await asyncio.gather(*(expand(n) for n in level))
            level = [c for batch in results for c in batch]
            all_nodes.extend(level)
        return all_nodes

    # ---- checks -------------------------------------------------------
    def _guard_failure(self, node: FolderNode) -> str | None:
        """Why this folder may not be touched, or None if the ownership guards pass."""
        if node.parent is None:
            return "project root is never deleted"
        if self.cfg.template_paths is not None and norm_path(node.rel_path) not in self.cfg.template_paths:
            return "not in template (treated as manually created)"
        if self.cfg.created_by and not (identities(node.created_by) & self.cfg.created_by):
            return f"creator not in allow-list ({_describe_creator(node.created_by) or 'unknown'})"
        return None

    @staticmethod
    def _is_empty_leaf(node: FolderNode) -> bool:
        return (
            not node.child_folders
            and node.content_count == 0
            and (node.listed_child_count in (0, None))
        )

    async def _verify_live(
        self, node: FolderNode, simulated_gone: set[str] | None = None
    ) -> tuple[bool, str, str | None]:
        """Re-read the folder right before deleting. Returns (ok, reason, etag).

        ``simulated_gone`` holds sub-folder ids a dry run pretended to delete,
        so a dry-run cascade can still evaluate their parent.
        """
        simulated_gone = simulated_gone or set()
        async with self._sem:
            item = await self.api.get_item(node.id)
            if item is None:
                return False, "gone", None
            if item.get("folder") is None:
                return False, "no longer a folder", None
            children = await self.api.list_children(node.id)
            real = [c for c in children if c.get("id") not in simulated_gone]
            n_sim = len(children) - len(real)
            child_count = _to_int(item["folder"].get("childCount"))
            if child_count is not None and child_count - n_sim != 0:
                return False, f"childCount={child_count}", None
            if real:
                return False, f"children listing returned {len(real)} item(s)", None
            items_count, folders_count = await self.api.get_rollup_counts(node.id)
        if folders_count is not None:
            folders_count -= n_sim
        if items_count is None or folders_count is None:
            if self.cfg.require_rollup_counts:
                return False, "ItemChildCount/FolderChildCount unavailable (fail-safe skip)", None
        elif items_count or folders_count:
            return False, f"ItemChildCount={items_count}, FolderChildCount={folders_count} (hidden/system items)", None
        return True, "empty leaf", item.get("eTag") or node.etag

    # ---- run ----------------------------------------------------------
    async def run(self) -> CleanupReport:
        cfg = self.cfg
        mode = "apply" if cfg.apply else "dry-run"
        report = CleanupReport(run_id=uuid.uuid4().hex[:12], mode=mode)
        audit = AuditLog(cfg.log_file, report.run_id, mode, cfg.root_path)
        try:
            root_item = await self.api.resolve_path(cfg.root_path)
            if root_item is None or root_item.get("folder") is None:
                audit.write("error", reason=f"root folder not found: {cfg.root_path}", path=cfg.root_path)
                report.errors += 1
                return report

            nodes = await self._scan(root_item)
            report.folders_scanned = len(nodes)

            leaves = [n for n in nodes if n.parent is not None and self._is_empty_leaf(n)]
            report.candidates = len(leaves)
            if len(leaves) > cfg.max_deletes:
                report.aborted = True
                audit.write(
                    "error",
                    reason=f"{len(leaves)} empty leaves exceeds --max-deletes={cfg.max_deletes}; nothing deleted",
                    path=cfg.root_path,
                )
                return report

            # Deepest first so cascade sees children removed before parents.
            queue = sorted(leaves, key=lambda n: n.depth, reverse=True)
            queued = {id(n) for n in queue}
            level_of = {id(n): 0 for n in queue}
            deletes_done = 0
            simulated_gone: set[str] = set()  # dry run only

            while queue:
                node = queue.pop(0)
                cascade_level = level_of.get(id(node), 0)
                guard = self._guard_failure(node)
                if guard:
                    report.skipped += 1
                    audit.write("skipped", node, guard, cascade_level=cascade_level)
                    continue

                try:
                    ok, reason, etag = await self._verify_live(node, simulated_gone)
                except Exception as exc:  # noqa: BLE001 — log and continue with the rest
                    report.errors += 1
                    audit.write("error", node, f"verify failed: {exc}", cascade_level=cascade_level)
                    continue
                if not ok:
                    if reason == "gone":
                        report.gone += 1
                        audit.write("gone", node, "already deleted", cascade_level=cascade_level)
                        removed = True
                    else:
                        report.skipped += 1
                        audit.write("skipped", node, reason, cascade_level=cascade_level)
                        continue
                else:
                    removed = False
                    if cfg.apply:
                        if deletes_done >= cfg.max_deletes:
                            report.aborted = True
                            audit.write("error", node, f"--max-deletes={cfg.max_deletes} reached; stopping")
                            break
                        try:
                            outcome = await self.api.delete_item(node.id, etag)
                        except Exception as exc:  # noqa: BLE001
                            report.errors += 1
                            audit.write("error", node, f"delete failed: {exc}", cascade_level=cascade_level)
                            continue
                        if outcome == "deleted":
                            deletes_done += 1
                            report.deleted += 1
                            removed = True
                            audit.write("deleted", node, reason if not cascade_level else "empty leaf (cascade)",
                                        cascade_level=cascade_level)
                        elif outcome == "gone":
                            report.gone += 1
                            removed = True
                            audit.write("gone", node, "already deleted", cascade_level=cascade_level)
                        else:  # changed (412)
                            report.skipped += 1
                            audit.write("skipped", node, "folder changed since scan (eTag mismatch)",
                                        cascade_level=cascade_level)
                        if cfg.delete_delay:
                            await asyncio.sleep(cfg.delete_delay)
                    else:
                        report.would_delete += 1
                        removed = True  # simulate, so a dry run shows the cascade too
                        simulated_gone.add(node.id)
                        audit.write("would_delete", node, reason if not cascade_level else "empty leaf (cascade)",
                                    cascade_level=cascade_level)

                if removed and cfg.cascade:
                    parent = node.parent
                    if parent is not None:
                        parent.child_folders = [c for c in parent.child_folders if c is not node]
                        if (
                            parent.parent is not None
                            and not parent.child_folders
                            and parent.content_count == 0
                            and id(parent) not in queued
                        ):
                            queued.add(id(parent))
                            level_of[id(parent)] = cascade_level + 1
                            queue.append(parent)
                            queue.sort(key=lambda n: n.depth, reverse=True)
            return report
        except Exception as exc:  # noqa: BLE001
            report.errors += 1
            audit.write("error", reason=f"run failed: {exc}", path=cfg.root_path)
            log.exception("Leaf cleanup failed")
            return report
        finally:
            audit.write("summary", reason="run finished", **{
                k: getattr(report, k) for k in (
                    "folders_scanned", "candidates", "deleted", "would_delete",
                    "skipped", "gone", "errors", "aborted",
                )
            })
            audit.close()


async def run_cleanup(cfg: CleanupConfig, api: DriveAPI | None = None, site: str | None = None) -> CleanupReport:
    """Programmatic entry point, e.g. to chain after a scheduler job:

        report = await run_cleanup(CleanupConfig(...))
    """
    owned_client = None
    if api is None:
        from ..config import Settings
        from ..graph.client import GraphClient, graph

        if site:
            owned_client = GraphClient(site_config=Settings.load_site_config(site))
            client = owned_client
        else:
            client = graph()
        api = GraphDriveAPI(client, cfg.drive_id)
    try:
        return await LeafFolderCleanup(cfg, api).run()
    finally:
        if owned_client is not None:
            await owned_client.aclose()


# --------------------------------------------------------------------------- CLI
def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m app.jobs.cleanup_empty_leaf_folders",
        description="Delete empty leaf folders under a project root in SharePoint Online (dry run by default).",
    )
    p.add_argument("--template", help="Template JSON listing the folders the scheduler creates (template guard).")
    p.add_argument("--root", help="Project root folder path in the library, e.g. 'Kaizen - Knowledge Bank'. "
                                  "Defaults to the template's 'root'.")
    p.add_argument("--site", help="Configured site key (e.g. dev, prod). Defaults to the active site.")
    p.add_argument("--drive-id", help="Document library drive id. Defaults to the site's configured drive.")
    p.add_argument("--created-by", action="append", default=[],
                   help="Creator allow-list (UPN, display name, or app id/name). Repeatable.")
    p.add_argument("--cascade", action="store_true", help="Also delete parents that become empty leaves.")
    p.add_argument("--apply", action="store_true", help="Actually delete. Without this it is a dry run.")
    p.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="JSONL audit log path (appended).")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel folder listings (default 4).")
    p.add_argument("--delete-delay", type=float, default=0.25, help="Seconds between deletes (default 0.25).")
    p.add_argument("--max-deletes", type=int, default=500,
                   help="Abort without deleting if more empty leaves than this are found (default 500).")
    p.add_argument("--exclude", action="append", default=None,
                   help="Regex on folder name to never walk or delete. Repeatable. "
                        "Replaces the defaults ('^_', '^forms$') when given.")
    p.add_argument("--allow-missing-rollup", action="store_true",
                   help="Delete even if ItemChildCount/FolderChildCount can't be read (less safe).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(list(argv) if argv is not None else None)


def build_config(args: argparse.Namespace) -> tuple[CleanupConfig, str | None]:
    template_root, template_paths = (None, None)
    if args.template:
        template_root, template_paths = load_template(args.template)
    root = args.root or template_root
    if not root:
        raise ValueError("--root is required when the template has no 'root'")

    drive_id = args.drive_id
    if not drive_id:
        from ..config import Settings, settings

        drive_id = (Settings.load_site_config(args.site) if args.site else settings).drive_id

    return CleanupConfig(
        drive_id=drive_id,
        root_path=root,
        template_paths=template_paths,
        created_by={c.strip().casefold() for c in args.created_by if c.strip()},
        cascade=args.cascade,
        apply=args.apply,
        log_file=Path(args.log_file),
        concurrency=args.concurrency,
        delete_delay=args.delete_delay,
        max_deletes=args.max_deletes,
        exclude_patterns=args.exclude if args.exclude is not None else list(DEFAULT_EXCLUDE_PATTERNS),
        require_rollup_counts=not args.allow_missing_rollup,
    ), args.site


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg, site = build_config(args)
        cfg.validate()
    except Exception as exc:  # noqa: BLE001
        log.error("Configuration error: %s", exc)
        return 2

    report = asyncio.run(run_cleanup(cfg, site=site))
    log.info(
        "Done (%s): scanned=%d candidates=%d deleted=%d would_delete=%d skipped=%d gone=%d errors=%d%s",
        report.mode, report.folders_scanned, report.candidates, report.deleted, report.would_delete,
        report.skipped, report.gone, report.errors, " ABORTED" if report.aborted else "",
    )
    if report.aborted:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
