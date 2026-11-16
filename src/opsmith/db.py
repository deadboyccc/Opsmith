"""Database engine/session management (SQLAlchemy 2.0).

SQLite is the zero-ops default for development and tests; PostgreSQL is the production
target (``OPSMITH_DATABASE_URL=postgresql+psycopg://...``). All code paths use portable
SQL so both work.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes on every backend (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# The row lock that serializes work on one project or service. ``FOR NO KEY UPDATE`` on
# PostgreSQL: it excludes another holder exactly like ``FOR UPDATE``, but not the
# ``FOR KEY SHARE`` a foreign-key check takes, so rows that reference the locked one can still
# be inserted while it is held (ingestion and reviews go on during a long model call).
# SQLite, which has no row locks, ignores it.
SERIALIZE: dict[str, bool] = {"key_share": True}


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
        datetime: UTCDateTime,
    }


def make_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        """Bring the schema to the newest revision (``opsmith.migrations``, ADR-002)."""
        from opsmith.migrations import upgrade  # noqa: PLC0415 - alembic loads lazily

        upgrade(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> Session:
        return self._factory()
