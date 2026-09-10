"""Database engine, session factory, and FastAPI dependency."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from etl_agent.settings import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=settings.is_dev,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def get_db() -> Generator[Session]:
    """FastAPI dependency: yield a database session for the request lifecycle."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
