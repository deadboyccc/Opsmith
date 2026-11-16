"""Shared fixtures. Every test gets an isolated data dir + SQLite database."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsmith.config import Settings
from opsmith.db import utcnow
from opsmith.demo import make_repo
from opsmith.models import Connector, Project, Repository, Service, Tenant, User
from opsmith.platform import Platform
from opsmith.scheduler import ensure_project_schedules
from opsmith.schemas import ProjectSettings
from opsmith.security import create_tenant, create_user

# Synthetic AWS-style key id, assembled so no full key-shaped literal is committed.
FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_json=False, sandbox_backend="local")


@pytest.fixture
def platform(settings: Settings) -> Platform:
    return Platform(settings)


@dataclass
class Seed:
    tenant: Tenant
    admin: User
    reviewer: User
    viewer: User
    service_user: User
    keys: dict[str, str]
    project: Project
    repo: Repository
    service: Service
    log_connector: Connector
    metric_connector: Connector
    repo_path: Path
    logs: Path
    metrics: Path


@pytest.fixture
def seed(platform: Platform, tmp_path: Path) -> Seed:
    repo_path = make_repo(tmp_path / "repo")
    logs = tmp_path / "signals" / "logs.jsonl"
    metrics = tmp_path / "signals" / "metrics.jsonl"
    logs.parent.mkdir(parents=True)
    logs.touch()
    metrics.touch()
    with platform.db.session() as s:
        tenant = create_tenant(s, "acme")
        admin, admin_key = create_user(s, tenant, "Ada Admin", "admin")
        reviewer, reviewer_key = create_user(s, tenant, "Rui Reviewer", "reviewer")
        viewer, viewer_key = create_user(s, tenant, "Vic Viewer", "viewer")
        svc_user, svc_key = create_user(s, tenant, "collector", "service")
        project = Project(
            tenant_id=tenant.id,
            name="payments",
            settings=ProjectSettings(
                executor_agent="heuristic", lead_max_carry=2, verification_delay_hours=0
            ).model_dump(mode="json"),
        )
        s.add(project)
        s.flush()
        repo = Repository(
            tenant_id=tenant.id,
            project_id=project.id,
            name="payments-api",
            scm_provider="local",
            url=str(repo_path),
            test_command=f"{sys.executable} -m unittest discover -s tests -q",
        )
        s.add(repo)
        s.flush()
        service = Service(
            tenant_id=tenant.id,
            project_id=project.id,
            name="payments-api",
            repository_id=repo.id,
            path_prefixes=["app/"],
        )
        s.add(service)
        s.flush()
        start = (utcnow() - timedelta(hours=24)).isoformat()
        log_conn = Connector(
            tenant_id=tenant.id,
            project_id=project.id,
            name="logs",
            type="file_logs",
            kind="logs",
            config={"path": str(logs)},
            service_id=service.id,
            interval_seconds=6 * 3600,
            cursor={"last_end": start},
        )
        metric_conn = Connector(
            tenant_id=tenant.id,
            project_id=project.id,
            name="metrics",
            type="file_metrics",
            kind="metrics",
            config={"path": str(metrics), "baseline_seconds": 6 * 3600},
            service_id=service.id,
            interval_seconds=6 * 3600,
            cursor={"last_end": start},
        )
        s.add_all([log_conn, metric_conn])
        s.flush()
        ensure_project_schedules(s, project)
    return Seed(
        tenant,
        admin,
        reviewer,
        viewer,
        svc_user,
        {"admin": admin_key, "reviewer": reviewer_key, "viewer": viewer_key, "service": svc_key},
        project,
        repo,
        service,
        log_conn,
        metric_conn,
        repo_path,
        logs,
        metrics,
    )


def write_logs(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=str) + "\n")


def error_records(
    start: datetime,
    count: int,
    message: str = "upstream timeout calling payment gateway after {i}ms",
    *,
    path: str = "app/payments.py",
    line: int = 10,
    service: str = "payments-api",
    span: timedelta = timedelta(hours=5),
) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": (start + span * (i + 0.5) / max(count, 1)).isoformat(),
            "level": "error",
            "service": service,
            "message": message.format(i=30000 + i),
            "stack": f'Traceback:\n  File "{path}", line {line}, in charge',
        }
        for i in range(count)
    ]


def metric_records(
    start: datetime,
    n: int,
    value: float,
    name: str = "latency_ms_p95",
    service: str = "payments-api",
) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
            "name": name,
            "value": value + (i % 5) * 2,
            "service": service,
        }
        for i in range(n)
    ]


@pytest.fixture
def drain(platform: Platform) -> Callable[[], list[Any]]:
    def _drain() -> list[Any]:
        return platform.worker().drain()

    return _drain
