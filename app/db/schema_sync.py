"""Bring an existing database up to the ORM models on every startup.

`Base.metadata.create_all()` creates tables that don't exist yet but never
alters a table that does — so a teammate's database that predates a new
model column breaks with "UndefinedColumn" until someone remembers to run the
right migration. `sync_missing_columns` closes that gap generically: for every
model table that already exists, any column the model has and the table
lacks is added (with its default, so NOT NULL columns can be backfilled),
plus its index. Idempotent and additive only — it never drops, renames or
retypes anything. Alembic migrations still run first; this is the safety net
that makes "start the backend" enough to catch any database up.
"""
from __future__ import annotations

import logging

from sqlalchemy import BigInteger, Integer, MetaData, inspect, text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


def _default_sql_literal(column) -> str | None:
    """SQL literal for a column's default, used so an auto-added NOT NULL
    column can be backfilled on tables that already have rows. Only plain
    scalar defaults are translatable; callables (e.g. datetime.utcnow) and
    JSON/list defaults return None and the column is added nullable."""
    if column.server_default is not None:
        arg = getattr(column.server_default, "arg", None)
        if arg is not None:
            return str(arg.text) if hasattr(arg, "text") else str(arg)
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        val = default.arg
        if isinstance(val, bool):
            return "TRUE" if val else "FALSE"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, str):
            return "'" + val.replace("'", "''") + "'"
    return None


def _needs_bigint_widening(conn: Connection, column, reflected: dict) -> bool:
    """A model column changed from Integer to BigInteger (e.g. file sizes
    over 2 GB) — the one in-place type change that is always safe, so it is
    applied automatically. PostgreSQL only; SQLite integers are 64-bit."""
    if conn.dialect.name != "postgresql":
        return False
    return isinstance(column.type, BigInteger) and not isinstance(reflected["type"], BigInteger) and isinstance(reflected["type"], Integer)


def sync_missing_columns(conn: Connection, metadata: MetaData) -> list[str]:
    """Add every model column missing from an existing table (and widen
    Integer columns the model now declares BigInteger). Returns what changed
    as "table.column" names (empty when already in sync)."""
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    quote = conn.dialect.identifier_preparer.quote
    # SQLite has no "ADD COLUMN IF NOT EXISTS"; the inspector check below
    # already guarantees the column is missing.
    if_not_exists = "" if conn.dialect.name == "sqlite" else "IF NOT EXISTS "
    added: list[str] = []

    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table — create_all() makes it in full
        reflected = {c["name"]: c for c in inspector.get_columns(table.name)}
        have = set(reflected)
        for column in table.columns:
            if column.name in have:
                if _needs_bigint_widening(conn, column, reflected[column.name]):
                    conn.execute(text(
                        f"ALTER TABLE {quote(table.name)} ALTER COLUMN {quote(column.name)} TYPE BIGINT"
                    ))
                    added.append(f"{table.name}.{column.name} (widened to BIGINT)")
                continue
            col_type = column.type.compile(dialect=conn.dialect)
            default_literal = _default_sql_literal(column)
            ddl = f"ALTER TABLE {quote(table.name)} ADD COLUMN {if_not_exists}{quote(column.name)} {col_type}"
            if default_literal is not None:
                ddl += f" DEFAULT {default_literal}"
                if not column.nullable:
                    ddl += " NOT NULL"
            elif not column.nullable:
                logger.warning(
                    "Schema sync: %s.%s is NOT NULL in the model but has no SQL-expressible default — "
                    "added as nullable so existing rows stay valid",
                    table.name, column.name,
                )
            conn.execute(text(ddl))

            if column.unique or column.index:
                index_name = f"ix_{table.name}_{column.name}"
                unique = "UNIQUE " if column.unique else ""
                conn.execute(text(
                    f"CREATE {unique}INDEX IF NOT EXISTS {quote(index_name)} "
                    f"ON {quote(table.name)} ({quote(column.name)})"
                ))
            added.append(f"{table.name}.{column.name}")

    if added:
        logger.warning("Schema sync changed %d column(s): %s", len(added), ", ".join(added))
    return added


def ensure_schema(engine, metadata: MetaData) -> list[str]:
    """create_all + sync_missing_columns in one transaction."""
    with engine.begin() as conn:
        metadata.create_all(bind=conn, checkfirst=True)
        return sync_missing_columns(conn, metadata)
