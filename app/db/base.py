"""Database engine, session factory, and declarative base."""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import settings


class Base(DeclarativeBase):
    pass


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

