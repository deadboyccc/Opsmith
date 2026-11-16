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
