"""Database engine and session factory for this project's own database.

Defaults to a local SQLite file so the app runs with zero external setup;
point `DATABASE_URL` at Postgres/etc. for a shared deployment. Tables are
created at startup via `Base.metadata.create_all()` — no migration framework
yet (see README "Future enhancements").
"""
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models.db_models import Base

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _upgrade_existing_schema()


def _upgrade_existing_schema() -> None:
    """Apply small idempotent upgrades for databases created before the ORM
    model gained new nullable columns. ``create_all`` does not alter tables
    that already exist.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    item_columns = {column["name"] for column in inspector.get_columns("migration_items")}
    if "classification_result" not in item_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE migration_items ADD COLUMN classification_result TEXT")
            )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
