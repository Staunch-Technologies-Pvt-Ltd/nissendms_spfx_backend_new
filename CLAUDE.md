# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI control-plane backend for the Vessel Document Management System. It serves the REST API the SPFx web part ([nissendms_spfx_frontend](../nissendms_spfx_frontend)) calls, owns a Postgres cache of the SharePoint folder tree, runs an approval workflow for gated uploads/destructive actions, bridges documents into the external AI BANTO email system, and runs background jobs via APScheduler.

## Commands (run from this directory, in the integrated terminal)

```bash
# Install deps (first time / after requirements.txt changes)
python -m venv .venv
.venv\Scripts\activate          # PowerShell/cmd on Windows
pip install -r requirements.txt

# Run the API with reload (reads .env, picks real/stub backend automatically)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Health check / confirm which backend mode is active (real vs stub)
curl http://localhost:8000/api/health

# Run the pytest/unittest-style suite under tests/ (DB-dependent tests self-skip if Postgres isn't reachable)
python -m unittest discover -s tests -v
# or a single test file/case:
python -m unittest tests.test_vessel_pool -v
# Migration Assistant tests use pytest (per its README)
pytest tests/migration_assistant

# New Alembic migration after changing app/db/models.py
alembic revision --autogenerate -m "describe change"
alembic upgrade head        # normally auto-runs on app startup anyway
```

There is no lint/format/type-check tooling configured in this repo (no ruff/black/mypy config); `pyproject.toml` only carries local Pyrefly interpreter paths for one contributor's machine and isn't a real project config — don't treat it as authoritative.

The root-level `test_*.py` and `verify_*.py` / `check_*.py` / `inspect_*.py` files (e.g. `test_auth.py`, `test_azure.py`, `verify_dms_structure.py`) are standalone manual debugging scripts run directly with `python <file>.py` against a live tenant/DB — they are not part of the `tests/` suite and aren't collected by `unittest discover`.

## Architecture

**One code path, two backends.** `app/services/__init__.py::get_backend()` is the single seam: it returns `RealBackend` (SharePoint Online via Graph + PostgreSQL) when `GRAPH_*`/`DRIVE_ID` and DB env vars are all set, otherwise an in-memory `StubBackend` — both implement the same async interface, so `app/main.py` never branches on mode. Check the active mode via `GET /api/health` (`"mode"` field). When changing backend behavior, mirror the change in both `app/services/real_backend.py` and `app/services/stub_backend.py` unless the change is real-backend-only (e.g. actual Graph calls).

**Everything lives in `app/main.py`** (~10,600 lines, ~125 decorated endpoints) — nearly every REST endpoint is registered there directly rather than split across routers. When looking for a given endpoint's handler, grep `main.py` first rather than assuming a per-feature router file exists. A handful of feature areas *do* have their own API modules split out (`app/tag_config_api.py`, `app/folder_structure_api.py`, `app/filter_settings_api.py`, `app/module_settings_api.py`, `app/settings_tab_api.py`, `app/color_settings_api.py`, `app/vessel_folder_template_api.py`, `app/vessel_roots_api.py`, `app/email_automation.py`, `app/copilot/api.py`) and are mounted via `include_router` at the bottom of `main.py` (each builder takes `require_session`) — check those before assuming everything is in `main.py`.

**Schema is repaired at startup, not just migrated.** The `startup` handler in `main.py` (~lines 970–1075) runs Alembic `upgrade head` (stamping `head` first if tables exist but `alembic_version` doesn't), then `app/db/schema_sync.py::ensure_schema` (`create_all` + adds any model column an existing table lacks), then a hand-written guard for `vessels.is_provisioned`. So a new ORM column in `app/db/models.py` reaches a stale DB even without a migration — but still write the Alembic migration so fresh/stamped DBs stay consistent. Migrations 0017/0018 merged multiple heads; keep a single head.

**Legacy top-level modules.** `app/database.py`, `app/db_models.py`, `app/graph_client.py`, `app/template.py(.bak)` and `app/models.py` predate the `app/db/` and `app/graph/` packages; the live ORM is `app/db/models.py` and the live Graph client is `app/graph/client.py`. `app/store.py` is only the in-memory store behind `StubBackend`. Don't add new code to the legacy files. Likewise `scratch/` and the root-level `_*.py` / `fix_*.py` / `provision_*.py` files are one-off ops scripts, not app code.

**Config is one `.env` file for all three environments**, resolved by `app/config.py` via `APP_ENV=local|dev|prod`. Each key can be set with an env-specific prefix (`DEV_*`, `PROD_*`, `LOCAL_*`) or unprefixed as a shared fallback (`_prefixed()`/`_prefixed_int()` helpers implement the precedence). There's also a multi-site concept (`ACTIVE_SITE`, `_SITE_CONFIGS_CACHE`) for serving more than one SharePoint site config from the same deployment — check `app/config.py` before assuming a single global `settings` object covers every site.

**Auth is session-based, not JWT.** The web part does MSAL sign-in client-side, then calls `POST /api/auth/bypass-login` to mint a server-side session via `app/services/session_service.py`; every subsequent request carries `X-Session-ID` (or `Authorization: Bearer <session_id>`). Admin-only endpoints additionally require `X-User-Email` to be present in the `ADMIN_EMAILS` allow-list — this is an email allow-list, not a role/claim model (see "TEMPORARY bypass" comments in `main.py`).

**Approval workflow gates mutations, not requests.** Uploads into gated folders and destructive actions are staged as `approval_requests` rows instead of applying immediately: non-admins get `202` + a `pending` row for an SPE admin to approve/reject; admins' own actions execute immediately and are logged as `activity` for audit only. Staged upload files sit in a temporary "Pending Approvals" SharePoint location; the admin preview endpoint intentionally serves them without auth headers so they render directly in `<img>/<iframe>`.

**Folder tree is cached, not live.** `app/db/models.py` (`Vessel`, `Folder`, `ApprovalRequest`, `UserSession`, `PoolSlot`, …) mirrors the SharePoint folder structure in Postgres so the UI's tree/list views are single DB queries (see `GET /api/vessels/flat-tree`) instead of walking Graph per request. `app/services/vessel_sync.py` and the pool-slot machinery (`PoolSlot`, `_link_claimed_slot`, `_ensure_slot_matches_template`) keep a pool of pre-provisioned, unassigned vessel folder trees so new-vessel creation is a fast claim-and-rename rather than a slow from-scratch Graph build; `app/scheduler.py` reconciles crashed/stuck builds and pre-creates next month's month-folders.

**Background jobs** (`app/scheduler.py`, APScheduler, started with the app): `ensure_template_month_folders` (cron + once at startup), `session_sweep` (15 min, also revalidates Graph accounts), `reconcile_pool` (5 min), `reconcile_vessel_folders` (10 min), `sync_folder_table` (2 min, incremental Graph delta per drive into the `Folder` table), `refresh_dashboard_stats_cache`, `reconcile_native_deletions`. One-off cleanup jobs live in `app/jobs/`. Behavior that "just happens" in the DB/SharePoint without a request usually originates here.

**Folder Structure Mode** (`app/services/folder_structure.py`, API in `app/folder_structure_api.py`): four admin-selectable modes (`empty_pool`, `full_template`, `adopt_existing`, `adopt_create`) deciding what a vessel gets on top of its pooled slot. Never renames/moves/deletes; a DB row is only written after its SharePoint folder exists. There is deliberately no second provisioning path — reuse `graph.drive.ensure_folder` and `RealBackend._upsert`.

**Per-site vessel roots** (`app/services/vessel_roots.py`): admins choose which folders in a site's library contain vessel folders (stored in `app_settings`, key `vessel_roots`, keyed by drive id). Vessel discovery, dashboard counts and SharePoint vessel sync only look inside these; a library with no entry keeps the old automatic behavior.

**Migration Assistant** (`app/migration_assistant/`, routes `/api/migration-assistant/*`): formerly a separate service, now mounted into this app via `_setup_migration_assistant`. Has its own SQLite DB (`migration_assistant.db`, separate from Postgres), `MIGRATION_*` env keys, and its own tests in `tests/migration_assistant/`. Read its `README.md` before changing it (site-to-site copy jobs run in the background with NDJSON progress streaming). The frontend's `migrationAssistant/` module is its UI.

**Graph choke points**: all Graph/SharePoint traffic goes through `app/graph/client.py` (`GraphClient.request` / `sp_request`) and `app/graph/drive.py`. `app/graph/guard.py` (protected-site block) is currently inert — it only activates if `PROTECTED_SITE_PATTERNS` / `PROTECTED_DRIVE_IDS` are set.

**OCR classification pipeline** (`app/ocr/`) extracts dates and drawing categories from uploaded files (PaddleOCR/PyMuPDF for scans, `python-docx`/`openpyxl` for Office docs) to auto-classify uploads into the right month/category folder; `app/services/classify.py` and `app/services/anomaly_detector.py` handle vessel-vs-normal-folder classification and flag folder-placement anomalies for review.

**AI BANTO email bridge** (`app/email_automation.py`): tags an outgoing email with a validated `DataSource` tag, builds the subject as `[DataSource:TAG] Vessel Name / Subject`, and sends via Graph — delegated user token (`/me/sendMail`) when available, otherwise app-only token against `GRAPH_SENDER_MAILBOX`. Every send is logged to `email_log` (attachments as `bytea`) with resend support. The compose/upload UI hits `/api/bento/dispatch` and `/api/bento-email/upload`.

**Documents Copilot** (`app/copilot/`, routes `GET /api/copilot/status` and `POST /api/copilot/query`): a natural-language front-end over the cached per-site **dashboard document index** — *not* GitHub Copilot, and not a chat/RAG system. It reads no document contents and writes nothing. Pipeline in `service.ask()`:
1. **Load the index**: `get_backend().get_dashboard_documents(force_refresh=False, site_key=…)` (the same cache the Dashboard/Vessels pages use; `site_key` `None`/`"all"` = every site). If a site is selected it also loads the all-sites index so a question can name a different site. Vessel names, site names and people for matching are derived from that index. If the scan is still running and nothing is cached, it returns a "still indexing" answer with `scan_pending: true`.
2. **Parse** (`_parse`, rule-based, always runs): regex vocabularies extract site, vessel (longest known name), time period (`_period`: today / last week / "in March 2025" / "last 30 days"…), person (`by <name>`), group (`drawings|manuals|to_be_classified`, via `services/doc_groups.py`), file type, and intent (`list|count|latest|oldest|largest|overview|sites`); leftover non-stopword tokens become `keywords`.
3. **Optional AI parse**: only in `ai` mode *and* only when the rules understood nothing (no filter, intent still `list`) but keywords remain — `_llm_parse` asks Azure OpenAI for filters constrained to the known vessel list, merged by `_merge_llm`.
4. **Follow-ups**: the client echoes the last `filters` back as `context`; if the question matches `_FOLLOW_UP` ("only drawings", "those…"), missing vessel/group/file_type/site are inherited.
5. **Filter + rank** in memory (`_doc_matches`, `_keyword_score`: name hit = 3, path hit = 1; all keywords required, relaxed to any-keyword if that yields nothing → "closest results"). Sort depends on intent; top 25 returned (`_MAX_RESULTS`) with exact `total`, `breakdown`, `top_vessels`, `by_site`, `suggestions`.
6. **Answer**: a rule-based sentence is always built; in `ai` mode `_llm_answer` rewrites it from a JSON `DATA` blob (≤12k chars, last 4 history turns) under a "use ONLY the DATA" system prompt. Any Azure failure at either step logs a warning and silently falls back to the rule-based result — so `mode` is `ai` whenever Azure is *configured*, even if that request fell back.

Config is its own tiny `app/copilot/config.py` reading `AZURE_OPENAI_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT` / `_API_VERSION` (company Azure only, by policy — no external AI) and `COPILOT_M365_URL` straight from `.env` (not via `app/config.py`'s multi-site `Settings`; values are read once at import, so restart after editing). Questions about document *contents* aren't answered here; the UI links out to Microsoft 365 Copilot (`m365_url` from `/status`). Auth is just `require_session`, no admin check; `QueryIn` caps the question at 500 chars and history at the last 6 turns. Router is mounted near the bottom of `main.py`. Tests: `pytest tests/test_copilot.py` (stub backend feeding `ask()` directly; the AI path is tested by monkeypatching `_chat`). Frontend: `CopilotSearchPanel.tsx`.

## Known trade-offs worth knowing before touching related code

- Several Graph/auth checks (e.g. `check-email`, session validation mid-migration) **fail open** on network/transient errors so sign-in is never blocked — don't assume they're a hard security boundary.
- `ADMIN_EMAILS` is a flat allow-list, not real RBAC.
- Email attachments are stored raw in Postgres `bytea` — fine for now, but don't assume it scales to large/high-volume attachments without revisiting.
