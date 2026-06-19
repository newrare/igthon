"""SQLAlchemy database engine and session configuration.

Supports both SQLite (local dev) and PostgreSQL (production).
The driver is auto-detected from DATABASE_URL:
  - sqlite:///./file.db       → sqlite+aiosqlite:///./file.db
  - postgresql://...          → postgresql+asyncpg://...
  - Already has async driver  → used as-is
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.core.config import Settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


def _to_async_url(url: str) -> str:
    """Convert a sync database URL to its async equivalent.

    Examples:
        sqlite:///./db.sqlite3       → sqlite+aiosqlite:///./db.sqlite3
        postgresql://user:pass@host  → postgresql+asyncpg://user:pass@host
        postgresql+asyncpg://...     → unchanged
        sqlite+aiosqlite:///...      → unchanged
    """
    if "aiosqlite" in url or "asyncpg" in url:
        return url
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def create_engine(settings: Settings):
    """Create an async SQLAlchemy engine from settings."""
    url = _to_async_url(settings.database_url)

    # SQLite doesn't support pool_size or NullPool the same way
    if "sqlite" in url:
        return create_async_engine(
            url, echo=False, connect_args={"check_same_thread": False}
        )

    return create_async_engine(url, echo=False)


def create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory."""
    engine = create_engine(settings)
    return async_sessionmaker(engine, expire_on_commit=False)
