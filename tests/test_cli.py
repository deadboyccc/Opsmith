from pathlib import Path

import pytest
from sqlalchemy import select

from opsmith.cli import main
from opsmith.config import Settings
from opsmith.models import Connector, Project, Schedule, Service
from opsmith.platform import Platform

TOML = """
tenant = "acme"

[[projects]]
name = "payments"
[projects.settings]
inference_interval_hours = 4
audit_cron = "30 1 * * *"

[[projects.repositories]]
name = "payments-api"
scm_provider = "github"
url = "https://github.com/acme/payments-api.git"
full_name = "acme/payments-api"
credentials_ref = "env:GITHUB_TOKEN"
test_command = "pytest -q"

[[projects.services]]
name = "payments-api"
repository = "payments-api"
path_prefixes = ["app/"]

[[projects.connectors]]
name = "loki"
type = "loki"
service = "payments-api"
config = { url = "http://loki:3100", query = '{app="payments"}' }

[[projects.connectors]]
name = "prom"
type = "prometheus"
service = "payments-api"
config = { url = "http://prom:9090", queries = [{ name = "latency_p95", promql = "x" }] }
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("OPSMITH_DATA_DIR", str(data))
    monkeypatch.setenv("OPSMITH_LOG_JSON", "false")
    for name in ("OPSMITH_DATABASE_URL", "OPSMITH_SANDBOX_BACKEND", "OPSMITH_LOCAL_ROOT"):
        monkeypatch.delenv(name, raising=False)  # the CLI tests always use their own SQLite file
    return data


def test_init_create_user_and_apply(env: Path, tmp_path: Path, capsys) -> None:
    assert main(["init", "--tenant", "acme", "--admin", "Ada"]) == 0
    assert "osk_" in capsys.readouterr().out
    assert main(["init", "--tenant", "acme"]) == 1
    assert main(["create-user", "--tenant", "acme", "--name", "Rui", "--role", "reviewer"]) == 0
    assert "reviewer" in capsys.readouterr().out
    cfg = tmp_path / "opsmith.toml"
    cfg.write_text(TOML)
    assert main(["apply", str(cfg)]) == 0
    assert main(["apply", str(cfg)]) == 0  # idempotent
    platform = Platform(Settings(data_dir=env, log_json=False))
    with platform.db.session() as s:
        project = s.scalars(select(Project)).one()
        assert project.settings["inference_interval_hours"] == 4
        assert len(s.scalars(select(Connector)).all()) == 2
        svc = s.scalars(select(Service)).one()
        assert svc.repository_id is not None
        kinds = sorted(sch.job_kind for sch in s.scalars(select(Schedule)))
        assert kinds == ["audit", "inference", "ingest", "ingest", "track_prs"]
        audit = s.scalar(select(Schedule).where(Schedule.job_kind == "audit"))
        assert audit.cron == "30 1 * * *"


def test_apply_rejects_bad_config_without_writing_anything(
    env: Path, tmp_path: Path, capsys
) -> None:
    main(["init", "--tenant", "acme"])
    cfg = tmp_path / "bad.toml"
    bad_configs = {
        "unknown keys": '[[projects.connectors]]\nname = "c"\ntype = "loki"\ncolour = "red"\n',
        "interval_seconds": '[[projects.connectors]]\nname = "c"\ntype = "loki"\n'
        "interval_seconds = 0\n",
        "reserved for the platform": '[[projects.connectors]]\nname = "c"\ntype = "loki"\n'
        'credentials_ref = "env:OPSMITH_DATABASE_URL"\n',
        "outside the secrets directory": '[[projects.repositories]]\nname = "r"\nurl = "x"\n'
        'credentials_ref = "file:/etc/passwd"\n',
        "audit_cron": '[projects.settings]\naudit_cron = "0 0 31 2 *"\n',
        "needs a name": "[[projects.services]]\nowner = 1\n",
    }
    for expected, body in bad_configs.items():
        cfg.write_text(f'[[projects]]\nname = "p"\n{body}')
        assert main(["apply", str(cfg), "--tenant", "acme"]) == 1, expected
        assert expected in capsys.readouterr().err
    cfg.write_text("this is = not [valid toml")
    assert main(["apply", str(cfg), "--tenant", "acme"]) == 1
    assert main(["apply", str(tmp_path / "missing.toml"), "--tenant", "acme"]) == 1
    platform = Platform(Settings(data_dir=env, log_json=False))
    with platform.db.session() as s:
        assert s.scalars(select(Project)).all() == []  # every failed apply rolled back


def test_invalid_process_settings_exit_cleanly(env: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPSMITH_SANDBOX_BACKEND", "chroot")
    with pytest.raises(SystemExit, match="OPSMITH_"):
        main(["scheduler", "--once"])


def test_worker_and_scheduler_once(env: Path) -> None:
    main(["init", "--tenant", "acme"])
    assert main(["scheduler", "--once"]) == 0
    assert main(["worker", "--once"]) == 0


def test_version(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "opsmith" in capsys.readouterr().out


def test_init_does_not_print_the_database_password(env: Path, monkeypatch, capsys) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://opsmith:s3cr3t-pw@db.internal:5432/opsmith"
    )
    assert "s3cr3t-pw" not in settings.database_display
    assert "db.internal" in settings.database_display
    assert main(["init", "--tenant", "acme"]) == 0  # SQLite here: the path is shown as is
    assert "database: sqlite:///" in capsys.readouterr().out


# --------------------------------------------------------------------------- regressions (1.2.1)


def test_apply_reports_values_of_the_wrong_type(env: Path, tmp_path: Path, capsys) -> None:
    """``apply`` checked far less than the API does. A value of the wrong type either ended in
    a traceback (``TypeError``, a database error) or was stored: ``path_prefixes = "app/"``
    became a string that later code read one character at a time."""
    main(["init", "--tenant", "acme"])
    repo = '[[projects.repositories]]\nname = "r"\nurl = "/srv/r"\n'
    bad_configs = {
        "audit_types": repo + "audit_types = 5\n",
        "list of strings": repo + 'audit_types = [["security"]]\n',
        "test_command": repo + 'test_command = ["pytest"]\n',
        "default_branch": repo + "default_branch = 7\n",
        "url": '[[projects.repositories]]\nname = "r"\nurl = 5\n',
        "path_prefixes": '[[projects.services]]\nname = "s"\npath_prefixes = "app/"\n',
        "environment": '[[projects.services]]\nname = "s"\nenvironment = 3\n',
        "enabled": '[[projects.connectors]]\nname = "c"\ntype = "loki"\nenabled = "no"\n',
        "unknown type": '[[projects.connectors]]\nname = "c"\ntype = ["loki"]\n',
    }
    cfg = tmp_path / "bad.toml"
    for expected, body in bad_configs.items():
        cfg.write_text(f'[[projects]]\nname = "p"\n{body}')
        assert main(["apply", str(cfg), "--tenant", "acme"]) == 1, expected
        assert expected in capsys.readouterr().err, expected
    platform = Platform(Settings(data_dir=env, log_json=False))
    with platform.db.session() as s:
        assert s.scalars(select(Project)).all() == []


def test_apply_links_a_connector_to_the_service_in_the_named_environment(
    env: Path, tmp_path: Path
) -> None:
    """Services were looked up by name alone, so with ``api`` in two environments every
    connector was linked to whichever came last in the file."""
    main(["init", "--tenant", "acme"])
    cfg = tmp_path / "two.toml"
    cfg.write_text(
        '[[projects]]\nname = "p"\n'
        '[[projects.services]]\nname = "api"\n'
        '[[projects.services]]\nname = "api"\nenvironment = "staging"\n'
        '[[projects.connectors]]\nname = "prod-logs"\ntype = "push_logs"\nservice = "api"\n'
        '[[projects.connectors]]\nname = "stg-logs"\ntype = "push_logs"\nservice = "api"\n'
        'service_environment = "staging"\n'
    )
    assert main(["apply", str(cfg), "--tenant", "acme"]) == 0
    platform = Platform(Settings(data_dir=env, log_json=False))
    with platform.db.session() as s:
        linked = {
            conn.name: s.get(Service, conn.service_id).environment
            for conn in s.scalars(select(Connector))
        }
    assert linked == {"prod-logs": "production", "stg-logs": "staging"}


def test_an_unknown_log_level_is_a_configuration_error(env: Path, monkeypatch) -> None:
    """It reached ``logging`` as it was and every command ended in a traceback."""
    monkeypatch.setenv("OPSMITH_LOG_LEVEL", "verbose")
    with pytest.raises(SystemExit, match="OPSMITH_"):
        main(["scheduler", "--once"])
    monkeypatch.setenv("OPSMITH_LOG_LEVEL", "debug")
    assert main(["scheduler", "--once"]) == 0


def test_apply_gives_a_connector_without_an_interval_the_projects(
    env: Path, tmp_path: Path
) -> None:
    main(["init", "--tenant", "acme"])
    cfg = tmp_path / "interval.toml"
    cfg.write_text(
        '[[projects]]\nname = "p"\n[projects.settings]\ningest_interval_seconds = 300\n'
        '[[projects.connectors]]\nname = "default"\ntype = "push_logs"\n'
        '[[projects.connectors]]\nname = "own"\ntype = "push_logs"\ninterval_seconds = 120\n'
    )
    assert main(["apply", str(cfg), "--tenant", "acme"]) == 0
    platform = Platform(Settings(data_dir=env, log_json=False))
    with platform.db.session() as s:
        intervals = {c.name: c.interval_seconds for c in s.scalars(select(Connector))}
    assert intervals == {"default": 300, "own": 120}
