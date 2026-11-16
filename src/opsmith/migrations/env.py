"""Alembic environment for Opsmith's schema migrations (ADR-002).

Opsmith applies migrations itself when a process starts (``opsmith.migrations.upgrade``),
which hands this module an open connection. Contributors use the ``alembic`` command with
``alembic.ini`` at the repository root to write new revisions; that path connects to
``OPSMITH_DATABASE_URL``.
"""

from __future__ import annotations

from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection

from opsmith import models  # noqa: F401 - importing registers every table on Base.metadata
from opsmith.db import Base, UTCDateTime, make_engine

config = context.config


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Write portable SQLAlchemy types into revisions instead of Opsmith's own.

    A revision is frozen once it is released; it must not change meaning when an application
    type is renamed or altered later.
    """
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        # SQLite cannot ALTER most things; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    if context.is_offline_mode():
        raise SystemExit(
            "offline (--sql) migrations are not supported; run them against a database"
        )
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    from opsmith.config import Settings  # noqa: PLC0415 - only the alembic command needs it

    settings = Settings.from_env()
    settings.ensure_dirs()
    engine = make_engine(settings.resolved_database_url)
    try:
        with engine.connect() as conn:
            _run(conn)
            conn.commit()
    finally:
        engine.dispose()


run()
