"""Database schema migrations (Alembic; ADR-002).

Every Opsmith process calls :func:`upgrade` when it starts, so a new release brings its
database up to date without a separate step. Revisions live in ``versions/`` and ship with
the package.

- On PostgreSQL the upgrade holds a transaction-scoped advisory lock and the DDL is
  transactional, so the API, worker and scheduler can start together: one migrates, the
  others wait and then find nothing to do.
- A database created by Opsmith 1.0.0 (``create_all``, no ``alembic_version`` table) is checked
  against the baseline revision and adopted by stamping it. A database from before 1.0.0 is
  refused: its schema differs and there is nothing to migrate it from.
- A database that is at a revision this code does not know (written by a newer Opsmith) is
  refused instead of being run against a schema the code does not understand.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import Connection

log = logging.getLogger("opsmith.migrations")

BASELINE = "0001"
VERSION_TABLE = "alembic_version"
# Arbitrary constant: the key of the PostgreSQL advisory lock that serializes upgrades.
_LOCK_KEY = 0x6F70736D  # "opsm"
_DIR = Path(__file__).resolve().parent
# Tables every Opsmith database has had since 0.1.0. Finding any of them without a version
# table means the database was created before migrations existed. Kept literal so that later
# model changes cannot alter it.
_MARKER_TABLES = frozenset({"tenants", "users", "projects", "jobs", "audit_log"})


class SchemaError(RuntimeError):
    """The database cannot be brought to the schema this version of Opsmith needs."""


def _config(connection: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(_DIR))
    return ScriptDirectory.from_config(cfg)


def head_revision() -> str:
    head = script_directory().get_current_head()
    if head is None:  # pragma: no cover - the baseline always exists
        raise SchemaError("no migrations found")
    return head


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _baseline_columns() -> dict[str, set[str]]:
    """Tables and columns the baseline revision creates, read from a scratch database."""
    scratch = create_engine("sqlite://")
    try:
        with scratch.begin() as conn:
            command.upgrade(_config(conn), BASELINE)
            insp = inspect(conn)
            return {
                t: {c["name"] for c in insp.get_columns(t)}
                for t in insp.get_table_names()
                if t != VERSION_TABLE
            }
    finally:
        scratch.dispose()


def _adopt_unversioned(connection: Connection, tables: set[str]) -> None:
    """Stamp a database that Opsmith 1.0.0 created with ``create_all`` as the baseline."""
    expected = _baseline_columns()
    insp = inspect(connection)
    missing = sorted(t for t in expected if t not in tables)
    missing += sorted(
        f"{t}.{c}"
        for t in expected
        if t in tables
        for c in expected[t] - {col["name"] for col in insp.get_columns(t)}
    )
    if missing:
        raise SchemaError(
            "the database was created by an Opsmith release before 1.0.0 and cannot be "
            f"upgraded (missing: {', '.join(missing[:8])}"
            f"{', ...' if len(missing) > 8 else ''}). Back it up and start from a fresh "
            "database; see the 1.0.0 upgrade notes in CHANGELOG.md."
        )
    command.stamp(_config(connection), BASELINE)
    log.info("adopted an Opsmith 1.0.0 database at schema revision %s", BASELINE)


def upgrade(engine: Engine) -> None:
    """Bring the database to the newest schema revision. Safe to call from every process."""
    with engine.begin() as conn:
        # Processes (and the threads of ``opsmith run``) that start together migrate one at
        # a time: the first holds the lock, the others wait for it and then find the schema
        # current. Everything below runs again under the lock.
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        elif conn.dialect.name == "sqlite" and current_revision(conn) != head_revision():
            # The sqlite3 driver begins no transaction before DDL, so ``engine.begin()`` alone
            # locks nothing. Take the write lock (waiting up to the busy timeout), and only
            # when there is work: a start at the newest revision never waits for a writer.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        tables = set(inspect(conn).get_table_names())
        if VERSION_TABLE not in tables and tables & _MARKER_TABLES:
            _adopt_unversioned(conn, tables)
        before = current_revision(conn)
        if before is not None:
            try:
                script_directory().get_revision(before)
            except CommandError:
                raise SchemaError(
                    f"the database is at schema revision {before!r}, which this version of "
                    "Opsmith does not know: it was written by a newer release. Run that "
                    "release, or restore the backup taken before the upgrade."
                ) from None
        head = head_revision()
        if before == head:
            return
        command.upgrade(_config(conn), "head")
        log.info("database schema upgraded from %s to %s", before or "empty", head)
