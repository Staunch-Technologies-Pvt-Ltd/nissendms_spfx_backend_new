"""Database engine, session factory, and declarative base."""
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria

from ..config import get_active_drive_id, settings


class Base(DeclarativeBase):
    pass


def _active_site_id() -> str:
    return get_active_drive_id()


@event.listens_for(Session, "do_orm_execute")
def _scope_folder_queries(execute_state):
    """Keep logical folder paths isolated when multiple sites share a DB."""
    if execute_state.is_select and not execute_state.is_column_load:
        from .models import Folder
        site_id = _active_site_id()
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(Folder, lambda folder: folder.site_id == site_id, include_aliases=True)
        )


# Engine is created when a DATABASE_URL is configured. In stub mode this
# stays None and the app uses the in-memory store instead.
engine = (
    create_engine(settings.database_url_resolved, pool_pre_ping=True, future=True)
    if settings.db_configured
    else None
)

_session_factory = (
    sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    if engine is not None
    else None
)


def SessionLocal() -> Session:
    global engine, _session_factory
    if not settings.db_configured:
        raise RuntimeError("Database not configured (DATABASE_URL is empty).")
    if engine is None or _session_factory is None:
        engine = create_engine(settings.database_url_resolved, pool_pre_ping=True, future=True)
        _session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return _session_factory()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

