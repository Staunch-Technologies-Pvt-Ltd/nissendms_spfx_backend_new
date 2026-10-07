# Migration Assistant (merged module)

Formerly the standalone `SharePoint-AI-Migration-Assistant` project (own FastAPI
app on port 8020). It now lives inside the Vessel DMS backend and starts and
stops with it — **no second process**.

- Routes: `/api/migration-assistant/*` (health, `migration/*`, `site-to-site/*`,
  `tag-existing/*`, `vessel-export/excel`). All require a DMS session
  (`require_session`), like every other DMS router.
- Mounted from `app/main.py` with one line: `_setup_migration_assistant(app, require_session)`.
  On startup it creates its own tables and marks scans orphaned by a restart as failed.
  A failure there is logged and never stops the DMS from booting.
- Own database: `backend/migration_assistant.db` (SQLite) by default, separate
  from the DMS Postgres. To keep your old job history, copy the old
  `migration_assistant.db` over it while the backend is stopped.
- **One `.env`.** It uses the DMS's own Entra app credentials (same app
  registration) and keeps its few options in `backend/.env` with a
  `MIGRATION_` prefix: `MIGRATION_SITE_HOSTNAME`, `MIGRATION_SITE_PATH`,
  `MIGRATION_DESTINATION_ROOT`, `MIGRATION_ALLOWED_SITES`, term-set ids, ...
  (see `.env.example`). An old `backend/.env.migration` with un-prefixed
  names is still read if present, but is no longer needed.
- Layout: `config.py`, `db.py`, `api.py`, `graph/`, `services/`, `classifier/`,
  `document_parser/`, `models/`. Internal imports are relative.
- Tests: `backend/tests/migration_assistant/` (`pytest tests/migration_assistant`).
- No new dependencies — everything it needs is already in `requirements.txt`.

## Site-to-Site copy jobs

- **Confirm runs in the background.** `POST site-to-site/jobs/{id}/confirm`
  (body: `conflict_policy` skip|replace|rename|fail, `copy_permissions`,
  `copy_versions`) returns `202` straight away; the copy engine is
  `services/site_to_site_mover.py`.
- **Live progress:** `GET site-to-site/jobs/{id}/stream` is an NDJSON feed
  (files/bytes done, speed, time left, per-item events) ending with a
  `{"type": "done", "job": ...}` line. Live counters are kept in memory by
  `services/site_to_site_progress.py`; with several worker processes, a worker
  that doesn't own the run falls back to counts from the database.
- **Control:** `pause`, `resume`, `cancel`. Every item's result is saved as it
  finishes, so a cancelled job — or one interrupted by a server restart
  (marked `interrupted` on startup) — resumes without copying anything twice.
- **Verification** runs automatically after each copy (`verify` re-runs it):
  every copied item is re-read on both sides and compared by quickXorHash or
  size. Office files whose bytes SharePoint rewrote while setting metadata are
  reported as `changed_by_sharepoint`, not as failures.
  `GET site-to-site/jobs/{id}/report` downloads an Excel report.
- **Site picking:** the pickers list every site in the DMS's
  **Sites → Site Management** (`services/dms_sites.py`, keys `dms:<site_key>`,
  hidden/removed sites skipped) — add a site there and it appears here with
  the same name. `ALLOWED_SITES` entries that aren't in Site Management are
  listed after them (and keep resolving for old jobs / DESTINATION_SITE_KEY).
  `GET site-to-site/sites/search?q=` and `POST site-to-site/sites/resolve`
  find any other site (`url:https://host/sites/x` keys); use
  `GET site-to-site/drives?site_key=` for libraries, since keys can contain
  slashes.
- **Version history** uses Graph's `includeAllVersionHistory` copy option; if
  the tenant rejects it, the current version is copied and the item is
  flagged. **Permissions** re-grants unique (non-inherited) user/group access
  without sending invitations; sharing links and SharePoint groups are listed
  in the report as skipped.
- New columns are added to existing tables automatically on startup
  (`db._upgrade_existing_schema`, SQLite and Postgres).
