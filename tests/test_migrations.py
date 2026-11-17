"""Schema migrations (ADR-002): the revisions match the models, and upgrades are safe.

Run on SQLite by default and on PostgreSQL when ``OPSMITH_TEST_DATABASE_URL`` is set, as in CI.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text

from opsmith.cli import main
from opsmith.config import Settings
from opsmith.db import Base, make_engine
from opsmith.migrations import (
    BASELINE,
    SchemaError,
    _config,
    current_revision,
    head_revision,
    script_directory,
    upgrade,
)
from opsmith.models import Tenant
from opsmith.platform import Platform
from tests.conftest import fresh_database


@pytest.fixture
def engine(tmp_path: Path, database_url: str) -> Iterator[Engine]:
    engine = make_engine(database_url or f"sqlite:///{tmp_path / 'opsmith.db'}")
    try:
        yield engine
    finally:
        engine.dispose()


def _drift(engine: Engine) -> list[object]:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))


def _revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return current_revision(conn)


def test_the_migrations_create_exactly_the_schema_of_the_models(engine: Engine) -> None:
    """A model change without a migration fails here. Write one with
    ``uv run alembic revision --autogenerate`` (see docs/development/architecture.md)."""
    upgrade(engine)
    assert _revision(engine) == head_revision()
    assert _drift(engine) == []


def test_there_is_one_line_of_revisions() -> None:
    # Two revisions written in parallel branches both revise the same parent; merge them.
    assert len(script_directory().get_heads()) == 1


def test_every_revision_downgrades_and_upgrades_again(engine: Engine) -> None:
    upgrade(engine)
    with engine.begin() as conn:
        command.downgrade(_config(conn), "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    upgrade(engine)
    assert _drift(engine) == []


def test_upgrading_again_changes_nothing_and_keeps_data(engine: Engine) -> None:
    upgrade(engine)
    with engine.begin() as conn:
        conn.execute(
            Tenant.__table__.insert().values(
                id="ten_1", name="acme", created_at=text("CURRENT_TIMESTAMP")
            )
        )
    upgrade(engine)
    with engine.connect() as conn:
        assert conn.scalar(select(Tenant.__table__.c.name)) == "acme"


def test_a_database_created_by_opsmith_1_0_0_is_adopted(engine: Engine) -> None:
    # 1.0.0 created its tables with create_all and had no version table.
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            Tenant.__table__.insert().values(
                id="ten_1", name="acme", created_at=text("CURRENT_TIMESTAMP")
            )
        )
    upgrade(engine)
    assert _revision(engine) == head_revision()
    assert _drift(engine) == []
    with engine.connect() as conn:
        assert conn.scalar(select(Tenant.__table__.c.name)) == "acme"


def test_a_database_from_before_1_0_0_is_refused_and_left_alone(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # 0.1.0 had no schedules.custom (added in 1.0.0)
        conn.execute(text("ALTER TABLE schedules DROP COLUMN custom"))
    with pytest.raises(SchemaError, match=r"before 1\.0\.0.*schedules\.custom"):
        upgrade(engine)
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_a_database_written_by_a_newer_release_is_refused(engine: Engine) -> None:
    upgrade(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'from_the_future'"))
    with pytest.raises(SchemaError, match="newer release"):
        upgrade(engine)


def test_processes_starting_together_migrate_once(database_url: str, tmp_path: Path) -> None:
    """The API, worker and scheduler start together, as processes (Compose) or as threads
    of one process (``opsmith run``). On a fresh SQLite database they all created the schema
    at once: "table alembic_version already exists", and a half-made schema after that."""
    database_url = database_url or f"sqlite:///{tmp_path / 'fresh.db'}"
    go = time.time() + 5  # every process starts its upgrade at the same moment
    script = (
        "import sys, time\n"
        "from opsmith.db import make_engine\n"
        "from opsmith.migrations import upgrade\n"
        "engine = make_engine(sys.argv[1])\n"
        "time.sleep(max(0.0, float(sys.argv[2]) - time.time()))\n"
        "upgrade(engine)\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, database_url, str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(4)
    ]
    outputs = []
    for proc in procs:
        try:
            out, _ = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            out = "timed out (deadlock?)\n" + out
        outputs.append((proc.returncode, out))
    assert [code for code, _ in outputs] == [0, 0, 0, 0], outputs
    engine = make_engine(database_url)
    try:
        assert _revision(engine) == head_revision()
        assert _drift(engine) == []
    finally:
        engine.dispose()


def test_the_first_connection_to_a_new_sqlite_file_waits_for_a_writer(tmp_path: Path) -> None:
    """Switching a file to WAL ignores the busy timeout: while another connection writes,
    SQLite says "database is locked" at once. Two processes starting on a new file did that."""
    path = tmp_path / "new.db"
    writer = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    writer.execute("CREATE TABLE early (id INTEGER)")
    writer.execute("BEGIN IMMEDIATE")  # another process is writing (rollback-journal mode)
    done = threading.Timer(0.5, writer.execute, args=("COMMIT",))
    done.start()
    engine = make_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    finally:
        done.join()
        writer.close()
        engine.dispose()


def test_a_start_at_the_newest_revision_does_not_wait_for_a_writer(tmp_path: Path) -> None:
    """On SQLite the upgrade takes the write lock only when there is something to do, so a
    process that restarts while another holds the lock (a long job) starts at once."""
    path = tmp_path / "busy.db"
    engine = make_engine(f"sqlite:///{path}")
    try:
        upgrade(engine)
        writer = sqlite3.connect(path, timeout=1)
        try:
            writer.execute("BEGIN IMMEDIATE")  # another process is writing
            started = time.monotonic()
            upgrade(engine)
            assert time.monotonic() - started < 5
        finally:
            writer.rollback()
            writer.close()
    finally:
        engine.dispose()


def test_the_platform_migrates_when_it_starts(tmp_path: Path) -> None:
    with fresh_database() as url:
        platform = Platform(Settings(data_dir=tmp_path / "data", log_json=False, database_url=url))
        try:
            assert _revision(platform.db.engine) == head_revision()
        finally:
            platform.db.engine.dispose()


def test_the_command_line_reports_a_refused_schema_without_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("OPSMITH_DATA_DIR", str(data))
    monkeypatch.setenv("OPSMITH_LOG_JSON", "false")
    monkeypatch.delenv("OPSMITH_DATABASE_URL", raising=False)
    assert main(["init", "--tenant", "acme"]) == 0
    engine = make_engine(Settings(data_dir=data).resolved_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'from_the_future'"))
    finally:
        engine.dispose()
    with pytest.raises(SystemExit, match=r"database schema: .*newer release"):
        main(["scheduler", "--once"])


def test_the_baseline_is_the_first_revision() -> None:
    assert script_directory().get_base() == BASELINE
