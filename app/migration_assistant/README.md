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
- Own Entra app registration (`Sites.Selected`, app-only) — kept separate from
  the DMS's SharePoint Embedded credentials. Configure in `backend/.env.migration`
  (see `migration.env.example`; the old standalone `.env` can be copied as-is).
- Layout: `config.py`, `db.py`, `api.py`, `graph/`, `services/`, `classifier/`,
  `document_parser/`, `models/`. Internal imports are relative.
- Tests: `backend/tests/migration_assistant/` (`pytest tests/migration_assistant`).
- No new dependencies — everything it needs is already in `requirements.txt`.
