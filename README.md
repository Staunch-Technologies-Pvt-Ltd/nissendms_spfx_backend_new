# Vessel DMS — Backend (FastAPI + Microsoft Graph + PostgreSQL)

The control plane for the Vessel Document Management System. A single FastAPI
service that:

- serves the REST API the SPFx web part calls (auth, vessels, folders, files,
  approvals, recycle bin, archive, sessions, email),
- owns the **folder-tree cache** of the SharePoint document library so the UI
  never has to walk Graph on every request,
- runs the **approval workflow** (gated uploads and destructive actions),
- bridges documents into the external **AI BANTO** email system,
- runs background jobs (APScheduler) for month-folder pre-creation and
  vessel-folder pool replenishment.

One code path sits over two backends (`app/services/__init__.py`): the **real**
backend (SharePoint Online / Graph + PostgreSQL) when configured, or an
**in-memory stub** for dependency-free local development.

## Tech stack

- **FastAPI** + Pydantic v2, Uvicorn
- **Microsoft Graph** via MSAL (app-only + delegated user tokens); TLS via `truststore`
- **PostgreSQL** with SQLAlchemy 2.0 and **Alembic** migrations (auto-run on startup)
- **APScheduler** for background jobs
- **PaddleOCR / PyMuPDF** for OCR date + drawing-category extraction
- `python-docx` / `openpyxl` for month detection inside Office documents

## Layout

```
app/
  main.py               # Every REST endpoint lives here (auth, vessels, folders, …)
  config.py             # Multi-env .env resolver (APP_ENV=local|dev|prod)
  database.py           # Legacy SQLAlchemy setup
  db/
    base.py             # Engine + SessionLocal
    models.py           # All ORM models (Vessel, Folder, ApprovalRequest, UserSession, …)
  graph/
    client.py           # MSAL token + Graph HTTP client
    drive.py            # SharePoint drive helpers (create folder, upload, move, download)
    http.py             # TLS verification helper
  services/
    real_backend.py     # SharePoint Online Graph + Postgres implementation
    stub_backend.py     # In-memory implementation
    session_service.py  # Server-side sessions (create/validate/logout/revoke/audit)
    classify.py         # Vessel vs normal-folder classification
    anomaly_detector.py # Folder-placement anomaly scan
    notify.py           # Email notifications
  ocr/                  # dates.py, drawing_category.py, office.py, extract.py
  email_automation.py   # AI BANTO email router (/send-email, /email-logs, /bento-email/*)
  scheduler.py          # APScheduler jobs (precreate_next_month, pool replenishment)
  store.py              # In-memory store helpers
alembic/                # Versioned migrations
requirements.txt
```

## Getting started

### 1. Python environment & deps

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate   |  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment file

The whole configuration lives in one `.env` file (see `app/config.py`). Three
blocks share the file, selected by `APP_ENV`:

```
APP_ENV=dev        # local | dev | prod
# Then the matching prefixed block is used, with un-prefixed keys as fallback:
#   DEV_*, PROD_*, LOCAL_*  (e.g. DEV_AZURE_TENANT_ID, DEV_GRAPH_CLIENT_ID, …)
```

Key groups:

| Area | Keys |
|---|---|
| Entra / Graph | `AZURE_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` |
| SharePoint Online | `DRIVE_ID` |
| PostgreSQL | `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` |
| CORS | `ALLOWED_ORIGINS` (comma-separated) |
| Email (AI BANTO) | `GRAPH_SENDER_MAILBOX`, `AI_BANTO_RECIPIENT` |
| Approvals | `ADMIN_EMAILS` (comma-separated allow-list) |
| Sessions | `SESSION_IDLE_TIMEOUT_MINUTES`, `SESSION_MAX_LIFETIME_HOURS`, `SESSION_REVALIDATION_INTERVAL_MINUTES` |
| Other | `MONTH_FOLDER_FORMAT` (default `%B %Y`), `TRUSTED_PROXY_HOPS`, `GRAPH_VERIFY_SSL` |

When `GRAPH_*` + `DRIVE_ID` **and** DB keys are all set, the app boots in
`real` mode; otherwise it falls back to the `stub` backend so local development
works with no Azure/Postgres. Check the active mode with `/api/health` (`"mode"`).

### 3. PostgreSQL

Docker is the quickest way to a local instance:

```bash
docker run --name vessel-dms-postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=vessel_dms_dev \
  -p 5432:5432 -d postgres:16
```

On startup the app:

1. creates the database if it doesn't exist,
2. runs `alembic upgrade head` (and stamps `head` if it detects tables created
   earlier without Alembic tracking),
3. runs a `create_all(checkfirst=True)` safety net.

For schema changes, author a new revision:

```bash
alembic revision --autogenerate -m "describe change"
```

### 4. Run it

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify: `curl http://localhost:8000/api/health`

```json
{"status": "ok", "env": "dev", "mode": "real", "graph_configured": true, "db_configured": true}
```

The interactive API docs are at `http://localhost:8000/docs`.

## Core API surface

All endpoints are under `/api/*`. Highlights:

| Area | Endpoints |
|---|---|
| Auth / sessions | `POST /api/auth/check-email`, `POST /api/auth/login`, `POST /api/auth/bypass-login`, `POST /api/auth/logout`, `GET /api/profile`, `PATCH /api/profile`, `GET /api/sessions`, `DELETE /api/sessions/{id}`, `GET /api/sessions/audit` |
| Vessels | `GET/POST /api/vessels`, `PATCH/DELETE /api/vessels/{id}`, `GET /api/vessels/flat-tree`, `POST /api/vessels/{id}/provision` (+ `/provision-status`, `/reprovision`), `POST /api/vessels/repair-links` |
| Folders / files | `GET /api/mains`, `GET /api/folders/{id}[/children]`, `POST /api/folders/{id}/upload`, `POST /api/folders/{id}/month-upload`, `POST /api/folders/upload-by-path`, `POST /api/folders/{id}/subfolder`, `GET /api/files/{id}/content`, `GET /api/search`, `GET /api/stats` |
| Approvals | `GET /api/my-approvals`, `GET /api/approvals`, `GET /api/approvals/{id}[/preview]`, `POST /api/approvals/{id}/approve|reject` |
| Recycle bin / archive | `GET /api/recycle-bin/*`, `POST /api/recycle-bin/restore/{id}`, `DELETE /api/recycle-bin/{id}`, `POST /api/archive/{id}`, `POST /api/restore/{id}` |
| Anomalies | `GET/POST /api/normal-folders`, `GET/PATCH /api/anomalies`, `POST /api/anomalies/scan` |
| AI BANTO email | `POST /api/send-email`, `POST /api/bento/dispatch`, `POST /api/bento-email/upload`, `GET /api/bento-email/suggest-tag`, `GET /api/datasource-tags`, `GET /api/email-logs`, `POST/DELETE /api/email-logs/{id}` |
| Ops | `GET /api/health` |

**Auth convention:** the web part authenticates with MSAL in the browser, then
obtains a server-side `session_id` via `bypass-login` and sends it on every call
as `X-Session-ID` (or `Authorization: Bearer <session_id>`). Admin-only endpoints
additionally require the acting user's email via `X-User-Email` and membership of
`ADMIN_EMAILS`.

## Approval workflow

Uploads into gated folders and destructive actions are staged as
`approval_requests` rows rather than applied immediately:

- **Non-admins** — the mutation is deferred; the row starts `status=pending` and
  the response is `202`. An SPE admin approves (`POST /api/approvals/{id}/approve`)
  or rejects (with a reason) and the deferred action then executes.
- **SPE admins** — the mutation runs immediately and the row is written as an
  `activity` entry purely for the audit trail.
- Admins also get a notifications feed by combining `/api/approvals` (admin-scope)
  and `/api/my-approvals` (self-scope).

Staged upload files are held in a temporary "Pending Approvals" area in SharePoint;
the admin preview endpoint serves them without attaching auth headers so they can
render in `<img>/<iframe>`.

## AI BANTO email bridge

`email_automation.py` lets the web part send a tagged email into AI BANTO:

1. Collect `datasource_tag`, vessel name, subject text, body, optional attachments.
2. The tag is validated against the DataSource table. Invalid/missing tags fall
   back to `mail` (or are auto-detected from the filename when no tag is given).
3. Subject is built as `[DataSource:TAG] Vessel Name / Subject`.
4. Sent via Graph `sendMail`:
   - with a **delegated user token** → `POST /me/sendMail` (no app-level Mail.Send
     permission needed),
   - otherwise app-only token → `POST /users/{sender}/sendMail` using
     `GRAPH_SENDER_MAILBOX`.
5. Every attempt is logged to `email_log` (attachments stored as `bytea`), with
   resend (`POST /api/email-logs/{id}/resend`) and status `pending/completed/failed`.

The compose/upload flows in the web part call `/api/bento/dispatch` (multipart,
supports direct file upload or a SharePoint `file_id` reference) and
`/api/bento-email/upload` (auto-tag). Browse tags at `GET /api/datasource-tags`.

## Background jobs (scheduler.py)

- **Month-folder pre-creation** — runs after the 20th to pre-create next month's
  folders (`MONTH_FOLDER_FORMAT`) for month-driven leaves.
- **Vessel pool replenishment** — keeps a pool of pre-provisioned, unassigned
  vessel folder trees (`pool_slots`) so new vessels can be claimed instantly
  without a slow from-scratch build; a reconciliation check resumes crashed builds.

## Deployment

See the repo-root [`DEPLOY.md`](../DEPLOY.md) for the full dev/prod runbook
(env files, `gunicorn`/Uvicorn invocation, Nginx reverse proxy, `TRUSTED_PROXY_HOPS`,
CORS values). Key production pointers:

- Run with a process manager, e.g. `gunicorn app.main:app -k uvicorn.workers.UvicornWorker --workers 4 --bind 0.0.0.0:8000`.
- Keep secrets out of source control — use App Service Application Settings or a
  secrets manager, not `.env`, in production.
- `TRUSTED_PROXY_HOPS=1` behind Nginx so `get_client_ip()` reads `X-Forwarded-For`.
- CORS defaults to `*`; set `ALLOWED_ORIGINS` to the SharePoint site in production.

## Security notes / known trade-offs

- Several Graph checks **fail open** on network/transient errors (e.g.
  `check-email`, session validation when the DB is mid-migration) so sign-in is
  never blocked — audit these if they're relied on as a security boundary.
- Approval authorization is an **email allow-list** (`ADMIN_EMAILS`), not a
  full role/claim model — see the "TEMPORARY bypass" comments in `app/main.py`.
- Email attachments are stored raw in Postgres (`bytea`); move to blob storage
  for very large or high-volume payloads.
- `Mail.Send` as an app permission can send as any mailbox — scope it down with an
  Exchange **ApplicationAccessPolicy** if you run the app-only path (see the
  notes in `app/email_automation.py` and `app/config.py`).
