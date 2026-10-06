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

# New Alembic migration after changing app/db/models.py
alembic revision --autogenerate -m "describe change"
alembic upgrade head        # normally auto-runs on app startup anyway
```

There is no lint/format/type-check tooling configured in this repo (no ruff/black/mypy config); `pyproject.toml` only carries local Pyrefly interpreter paths for one contributor's machine and isn't a real project config — don't treat it as authoritative.

The root-level `test_*.py` and `verify_*.py` / `check_*.py` / `inspect_*.py` files (e.g. `test_auth.py`, `test_azure.py`, `verify_dms_structure.py`) are standalone manual debugging scripts run directly with `python <file>.py` against a live tenant/DB — they are not part of the `tests/` suite and aren't collected by `unittest discover`.

## Architecture

**One code path, two backends.** `app/services/__init__.py::get_backend()` is the single seam: it returns `RealBackend` (SharePoint Online via Graph + PostgreSQL) when `GRAPH_*`/`DRIVE_ID` and DB env vars are all set, otherwise an in-memory `StubBackend` — both implement the same async interface, so `app/main.py` never branches on mode. Check the active mode via `GET /api/health` (`"mode"` field). When changing backend behavior, mirror the change in both `app/services/real_backend.py` and `app/services/stub_backend.py` unless the change is real-backend-only (e.g. actual Graph calls).

**Everything lives in `app/main.py`** (~10,500 lines) — every REST endpoint is registered there directly rather than split across routers. When looking for a given endpoint's handler, grep `main.py` first rather than assuming a per-feature router file exists. A handful of feature areas *do* have their own API modules split out (`app/tag_config_api.py`, `app/folder_structure_api.py`, `app/filter_settings_api.py`, `app/module_settings_api.py`, `app/settings_tab_api.py`, `app/color_settings_api.py`, `app/copilot/api.py`) and are mounted into the main app — check those before assuming everything is in `main.py`.

**Config is one `.env` file for all three environments**, resolved by `app/config.py` via `APP_ENV=local|dev|prod`. Each key can be set with an env-specific prefix (`DEV_*`, `PROD_*`, `LOCAL_*`) or unprefixed as a shared fallback (`_prefixed()`/`_prefixed_int()` helpers implement the precedence). There's also a multi-site concept (`ACTIVE_SITE`, `_SITE_CONFIGS_CACHE`) for serving more than one SharePoint site config from the same deployment — check `app/config.py` before assuming a single global `settings` object covers every site.

**Auth is session-based, not JWT.** The web part does MSAL sign-in client-side, then calls `POST /api/auth/bypass-login` to mint a server-side session via `app/services/session_service.py`; every subsequent request carries `X-Session-ID` (or `Authorization: Bearer <session_id>`). Admin-only endpoints additionally require `X-User-Email` to be present in the `ADMIN_EMAILS` allow-list — this is an email allow-list, not a role/claim model (see "TEMPORARY bypass" comments in `main.py`).

**Approval workflow gates mutations, not requests.** Uploads into gated folders and destructive actions are staged as `approval_requests` rows instead of applying immediately: non-admins get `202` + a `pending` row for an SPE admin to approve/reject; admins' own actions execute immediately and are logged as `activity` for audit only. Staged upload files sit in a temporary "Pending Approvals" SharePoint location; the admin preview endpoint intentionally serves them without auth headers so they render directly in `<img>/<iframe>`.

**Folder tree is cached, not live.** `app/db/models.py` (`Vessel`, `Folder`, `ApprovalRequest`, `UserSession`, `PoolSlot`, …) mirrors the SharePoint folder structure in Postgres so the UI's tree/list views are single DB queries (see `GET /api/vessels/flat-tree`) instead of walking Graph per request. `app/services/vessel_sync.py` and the pool-slot machinery (`PoolSlot`, `_link_claimed_slot`, `_ensure_slot_matches_template`) keep a pool of pre-provisioned, unassigned vessel folder trees so new-vessel creation is a fast claim-and-rename rather than a slow from-scratch Graph build; `app/scheduler.py` reconciles crashed/stuck builds and pre-creates next month's month-folders.

**OCR classification pipeline** (`app/ocr/`) extracts dates and drawing categories from uploaded files (PaddleOCR/PyMuPDF for scans, `python-docx`/`openpyxl` for Office docs) to auto-classify uploads into the right month/category folder; `app/services/classify.py` and `app/services/anomaly_detector.py` handle vessel-vs-normal-folder classification and flag folder-placement anomalies for review.

**AI BANTO email bridge** (`app/email_automation.py`): tags an outgoing email with a validated `DataSource` tag, builds the subject as `[DataSource:TAG] Vessel Name / Subject`, and sends via Graph — delegated user token (`/me/sendMail`) when available, otherwise app-only token against `GRAPH_SENDER_MAILBOX`. Every send is logged to `email_log` (attachments as `bytea`) with resend support. The compose/upload UI hits `/api/bento/dispatch` and `/api/bento-email/upload`.

## Known trade-offs worth knowing before touching related code

- Several Graph/auth checks (e.g. `check-email`, session validation mid-migration) **fail open** on network/transient errors so sign-in is never blocked — don't assume they're a hard security boundary.
- `ADMIN_EMAILS` is a flat allow-list, not real RBAC.
- Email attachments are stored raw in Postgres `bytea` — fine for now, but don't assume it scales to large/high-volume attachments without revisiting.
