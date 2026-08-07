"""
PostgreSQL connection setup via SQLAlchemy.
DATABASE_URL example: postgresql+psycopg://postgres:Kamal%40146@localhost:5432/EmailDetails
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:Kamal%40146@localhost:5432/EmailDetails",
)

# pool_pre_ping avoids stale-connection errors after DB restarts/idle timeouts
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a session, always closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
