"""Database engine and session factory for the Migration Assistant.

Its tables live in the main DMS PostgreSQL database — the same engine as
everything else (`app.db.base.engine`), so there is one database to run, back
up and inspect. The tables are this module's own (`models/db_models.py`, its
own metadata) and never collide with DMS table names.

Fallbacks:
- `MIGRATION_DATABASE_URL` set in the process environment overrides it (tests
  point it at a throwaway SQLite file).
- With no DMS database configured (stub mode) it uses its own SQLite file
  (`DATABASE_URL` from `.env.migration`, default backend/migration_assistant.db).

Tables and any missing columns are created on startup (`init_db`). When the
module first starts on PostgreSQL and its tables are still empty, job history
from that legacy SQLite file is copied across once.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ..db.schema_sync import ensure_schema
from .config import _BACKEND_DIR, settings
from .models.db_models import Base

logger = logging.getLogger("migration_assistant")


def _sqlite_engine(url: str):
    return create_engine(url, pool_pre_ping=True, future=True, connect_args={"check_same_thread": False})


def _build_engine():
    if os.environ.get("MIGRATION_DATABASE_URL"):
        url = settings.database_url
        return _sqlite_engine(url) if url.startswith("sqlite") else create_engine(url, pool_pre_ping=True, future=True)
    from ..db.base import engine as dms_engine

    if dms_engine is not None:
        return dms_engine
    return _sqlite_engine(_legacy_sqlite_url() or settings.database_url)


def _legacy_sqlite_url() -> str | None:
    """The module's own SQLite file (from `.env.migration`'s DATABASE_URL),
    with a relative path resolved against the backend folder rather than
    whatever directory the server happened to be started from."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    path = Path(make_url(url).database or "")
    if not path.is_absolute():
        path = (_BACKEND_DIR / path).resolve()
    return f"sqlite:///{path.as_posix()}"


engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Tables whose rows mean "this database is already in use" — the legacy
# import only runs while all of them are empty.
_JOB_TABLES = ("migration_scan_jobs", "site_to_site_jobs", "existing_file_tag_scan_jobs")


def init_db() -> None:
    ensure_schema(engine, Base.metadata)
    if engine.dialect.name != "sqlite":
        try:
            _import_legacy_sqlite()
        except Exception as e:
            # Keep the log readable — DB errors here embed every row's values.
            logger.error("Importing Migration Assistant history from SQLite failed — continuing without it: %s",
                         str(e).splitlines()[0][:500])


def _import_legacy_sqlite() -> None:
    """One-time copy of the standalone/SQLite-era job history into the main
    database. Runs only while the target tables are empty, so it can never
    duplicate or overwrite anything; the SQLite file itself is left as-is."""
    src_url = _legacy_sqlite_url()
    if not src_url or not Path(make_url(src_url).database).exists():
        return
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    with engine.connect() as conn:
        if any(conn.execute(select(func.count()).select_from(tables[name])).scalar() for name in _JOB_TABLES):
            return

    src = _sqlite_engine(src_url)
    try:
        from sqlalchemy import inspect

        src_inspector = inspect(src)
        src_tables = set(src_inspector.get_table_names())
        copied: dict[str, int] = {}
        with src.connect() as src_conn, engine.begin() as dst:
            for table in Base.metadata.sorted_tables:  # parents before children
                if table.name not in src_tables:
                    continue
                src_cols = {c["name"] for c in src_inspector.get_columns(table.name)}
                cols = [c for c in table.columns if c.name in src_cols]
                # Selecting through the model's own column types converts
                # SQLite's text dates / 0-1 booleans into real Python values.
                rows = [
                    # OCR'd text excerpts can carry NUL characters, which
                    # SQLite stores but PostgreSQL text columns reject.
                    {k: (v.replace("\x00", "") if isinstance(v, str) else v) for k, v in r._mapping.items()}
                    for r in src_conn.execute(select(*cols))
                ]
                for start in range(0, len(rows), 500):
                    dst.execute(table.insert(), rows[start : start + 500])
                copied[table.name] = len(rows)
                if rows and engine.dialect.name == "postgresql" and "id" in table.c:
                    # Rows were inserted with their original ids — move the
                    # id sequence past them so new jobs don't collide.
                    dst.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"(SELECT MAX(id) FROM {table.name}))"
                    ))
    finally:
        src.dispose()
    if any(copied.values()):
        logger.warning(
            "Imported Migration Assistant history from %s: %s",
            src_url, ", ".join(f"{k}={v}" for k, v in copied.items() if v),
        )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
