"""Offline end-to-end demo: a sample service with logs, metrics and a git repo goes through
ingest -> chained inference (lead carried then confirmed) -> audit -> correlation ->
ranked tasks -> human approval -> sandboxed fix -> PR + CI -> human merge -> verification.

Uses the deterministic heuristic provider/agent and the local SCM, so it needs no network
or API key. ``tests/test_e2e.py`` drives the same scenario and asserts the spec's MVP
acceptance criteria.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from opsmith import queue
from opsmith.config import Settings
from opsmith.db import new_id, utcnow
from opsmith.gitutil import run_git
from opsmith.models import Connector, Project, Repository, Service, Task, User
from opsmith.platform import Platform
from opsmith.review import ReviewRequest
from opsmith.scheduler import ensure_project_schedules
from opsmith.schemas import ProjectSettings
from opsmith.scm.local import LocalSCM
from opsmith.security import create_tenant, create_user

# Synthetic, deliberately leaked credential for the audit to find. Built from fragments so
# this source file never contains a full key-shaped literal (keeps secret scanners quiet).
_FAKE_KEY = "sk_" + "live_" + "51HxDemoKeyDoNotUse0000000000"

PAYMENTS_PY = '''"""Payment gateway client."""

import httpx

GATEWAY_URL = "https://payments.example.internal/charge"
API_TOKEN = "__FAKE_KEY__"


def charge(amount_cents, customer_id):
    response = httpx.post(GATEWAY_URL, json={"amount": amount_cents, "customer": customer_id})
    response.raise_for_status()
    return response.json()
'''.replace("__FAKE_KEY__", _FAKE_KEY)

LEDGER_PY = '''"""Ledger helpers."""


def parse_amount(raw):
    try:
        return int(raw)
    except:
        return 0
'''

TEST_PY = """import unittest
from unittest import mock

from app import ledger, payments


class PaymentsTest(unittest.TestCase):
    def test_charge_returns_gateway_json(self):
        fake = mock.Mock()
        fake.json.return_value = {"status": "ok"}
        with mock.patch.object(payments.httpx, "post", return_value=fake):
            self.assertEqual(payments.charge(100, "c1"), {"status": "ok"})

    def test_parse_amount(self):
        self.assertEqual(ledger.parse_amount("42"), 42)
        self.assertEqual(ledger.parse_amount("x"), 0)


if __name__ == "__main__":
    unittest.main()
"""


@dataclass
class DemoResult:
    data_dir: Path
    repo_path: Path
    api_keys: dict[str, str]
    project_id: str
    steps: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    approved_task_id: str | None = None
    pr_url: str | None = None
    final_status: str | None = None


def make_repo(path: Path) -> Path:
    (path / "app").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(exist_ok=True)
    (path / "app" / "__init__.py").write_text("")
    (path / "app" / "payments.py").write_text(PAYMENTS_PY)
    (path / "app" / "ledger.py").write_text(LEDGER_PY)
    (path / "tests" / "__init__.py").write_text("")
    (path / "tests" / "test_app.py").write_text(TEST_PY)
    (path / "LICENSE").write_text("MIT License\n")
    (path / "requirements.txt").write_text("httpx==0.28.1\n")
    run_git(["init", "-b", "main"], path)
    run_git(["add", "-A"], path)
    run_git(["commit", "-m", "initial"], path, author=("Demo Dev", "dev@example.com"))
    return path


def write_signals(
    logs: Path,
    metrics: Path,
    start: datetime,
    hours: float,
    *,
    timeout_errors: int,
    cache_errors: int,
    latency_ms: float,
) -> None:
    """Append one window of synthetic production telemetry."""
    span = timedelta(hours=hours)
    with logs.open("a") as fh:
        for i in range(200):
            ts = start + span * (i / 200)
            fh.write(
                json.dumps(
                    {
                        "timestamp": ts.isoformat(),
                        "level": "info",
                        "service": "payments-api",
                        "message": f"POST /charge 200 in {80 + i % 40}ms user=u{i}@example.com",
                    }
                )
                + "\n"
            )
        for i in range(timeout_errors):
            ts = start + span * ((i + 0.5) / max(timeout_errors, 1))
            fh.write(
                json.dumps(
                    {
                        "timestamp": ts.isoformat(),
                        "level": "error",
                        "service": "payments-api",
                        "message": f"upstream timeout calling payment gateway after {30000 + i}ms",
                        "stack": 'Traceback (most recent call last):\n  File "app/payments.py", '
                        "line 10, in charge\nhttpx.ReadTimeout: timed out",
                        "trace_id": f"t{i:06d}",
                    }
                )
                + "\n"
            )
        for i in range(cache_errors):
            ts = start + span * ((i + 0.3) / max(cache_errors, 1))
            fh.write(
                json.dumps(
                    {
                        "timestamp": ts.isoformat(),
                        "level": "error",
                        "service": "payments-api",
                        "message": f"cache miss storm for key session:{i}",
                    }
                )
                + "\n"
            )
    with metrics.open("a") as fh:
        for i in range(int(hours * 12)):
            ts = start + timedelta(minutes=5 * i)
            fh.write(
                json.dumps(
                    {
                        "timestamp": ts.isoformat(),
                        "name": "http_request_duration_ms_p95",
                        "value": latency_ms + (i % 5) * 3,
                        "service": "payments-api",
                    }
                )
                + "\n"
            )


def _drain(platform: Platform) -> list[queue.WorkResult]:
    return platform.worker().drain()


def run_demo(base: Path, *, verbose: bool = True, database_url: str | None = None) -> DemoResult:
    def say(msg: str) -> None:
        result.steps.append(msg)
        if verbose:
            print(f"==> {msg}")

    base.mkdir(parents=True, exist_ok=True)
    if database_url is None:
        database_url = os.environ.get("OPSMITH_DATABASE_URL", "")
    # The demo executes a repository it has just generated itself, on the local SCM provider,
    # so the unconfined development sandbox is acceptable here (and needs no Docker).
    settings = Settings(
        data_dir=base / "data",
        log_json=False,
        database_url=database_url,
        sandbox_backend="local",
        local_root=base,
    )
    platform = Platform(settings)
    repo_path = make_repo(base / "repos" / "payments-api")
    logs, metrics = base / "signals" / "logs.jsonl", base / "signals" / "metrics.jsonl"
    logs.parent.mkdir(parents=True, exist_ok=True)
    now = utcnow().replace(microsecond=0)
    t0 = now - timedelta(hours=18)

    with platform.db.session() as s:
        # The random part keeps the name unique when the demo runs twice within one second
        # against the same database (tenant names are unique).
        tenant = create_tenant(s, f"demo-{now:%H%M%S}-{new_id('x')[-6:]}")
        admin, admin_key = create_user(s, tenant, "Demo Admin", "admin")
        reviewer, reviewer_key = create_user(s, tenant, "Riley Reviewer", "reviewer")
        project_settings = ProjectSettings(
            executor_agent="heuristic",
            verification_delay_hours=0.0,
            inference_interval_hours=6,
            lead_max_carry=2,
        )
        project = Project(
            tenant_id=tenant.id, name="payments", settings=project_settings.model_dump(mode="json")
        )
        s.add(project)
        s.flush()
        repo = Repository(
            tenant_id=tenant.id,
            project_id=project.id,
            name="payments-api",
            scm_provider="local",
            url=str(repo_path),
            default_branch="main",
            test_command=f"{sys.executable} -m unittest discover -s tests -q",
            audit_types=["security", "bugs", "performance", "dependency", "compliance"],
        )
        s.add(repo)
        s.flush()
        svc = Service(
            tenant_id=tenant.id,
            project_id=project.id,
            name="payments-api",
            repository_id=repo.id,
            path_prefixes=["app/"],
            owner="team-payments",
        )
        s.add(svc)
        s.flush()
        for name, ctype, path in (
            ("app-logs", "file_logs", logs),
            ("app-metrics", "file_metrics", metrics),
        ):
            s.add(
                Connector(
                    tenant_id=tenant.id,
                    project_id=project.id,
                    name=name,
                    type=ctype,
                    kind="logs" if ctype == "file_logs" else "metrics",
                    config={"path": str(path), "baseline_seconds": 6 * 3600},
                    service_id=svc.id,
                    interval_seconds=6 * 3600,
                    cursor={"last_end": t0.isoformat()},
                )
            )
        s.flush()
        ensure_project_schedules(s, project)
        ids = {
            "project": project.id,
            "service": svc.id,
            "repo": repo.id,
            "reviewer": reviewer.id,
            "admin": admin.id,
        }
        connectors = [
            c.id for c in s.scalars(select(Connector).where(Connector.project_id == project.id))
        ]
    result = DemoResult(
        data_dir=settings.data_dir,
        repo_path=repo_path,
        api_keys={"admin": admin_key, "reviewer": reviewer_key},
        project_id=ids["project"],
    )

    def ingest_and_infer(window_end: datetime, slot: str) -> None:
        with platform.db.session() as s:
            for cid in connectors:
                platform.ingestion.run_connector(s, cid, end=window_end)
        _drain(platform)  # build bundles
        with platform.db.session() as s:
            queue.enqueue(
                s,
                tenant_id=tenant.id,
                kind="inference",
                payload={"service_id": ids["service"], "slot": slot},
                idempotency_key=f"demo:{tenant.id}:inference:{slot}",
            )
        _drain(platform)  # inference + aggregation + notifications

    # Baseline window, then window 1: a modest error burst -> leads, not tasks yet.
    write_signals(logs, metrics, t0, 6, timeout_errors=0, cache_errors=0, latency_ms=120)
    write_signals(
        logs, metrics, t0 + timedelta(hours=6), 6, timeout_errors=30, cache_errors=6, latency_ms=125
    )
    ingest_and_infer(t0 + timedelta(hours=6), "run-0")
    ingest_and_infer(t0 + timedelta(hours=12), "run-1")
    say("run-1: error bursts became open leads (carried into the next run)")

    # Window 2: timeouts keep growing, latency regresses -> lead confirmed, findings.
    write_signals(
        logs,
        metrics,
        t0 + timedelta(hours=12),
        6,
        timeout_errors=80,
        cache_errors=0,
        latency_ms=900,
    )
    ingest_and_infer(now, "run-2")
    say("run-2: timeout lead confirmed from carried context; latency regression found")

    with platform.db.session() as s:
        queue.enqueue(
            s,
            tenant_id=tenant.id,
            kind="audit",
            payload={"repository_id": ids["repo"], "mode": "full"},
            idempotency_key=f"demo:{tenant.id}:audit:1",
        )
    _drain(platform)
    say("audit: secrets, bug and performance analyzers + triage; findings correlated")

    with platform.db.session() as s:
        tasks = s.scalars(
            select(Task).where(Task.project_id == ids["project"]).order_by(Task.score.desc())
        ).all()
        result.task_ids = [t.id for t in tasks]
        if verbose:
            print("\n   Ranked tasks awaiting review:")
            for t in tasks:
                print(
                    f"   [{t.priority} {t.score:6.2f}] {t.status:10} {t.category:12} "
                    f"{t.title[:70]}  paths={t.paths}"
                )
            print()
        correlated = next(
            (t for t in tasks if {e.get("kind") for e in t.evidence} >= {"runtime", "static"}), None
        )
        target = correlated or tasks[0]
        reviewer_user = s.get(User, ids["reviewer"])
        assert reviewer_user is not None
        platform.reviews.review(
            s,
            target,
            reviewer_user,
            ReviewRequest(
                decision="approve", comment="Runtime timeouts match the audit finding; ship it."
            ),
        )
        result.approved_task_id = target.id
    say(f"human review: approved {result.approved_task_id}")

    _drain(platform)  # execute -> PR (+ local CI)
    with platform.db.session() as s:
        task = s.get(Task, result.approved_task_id)
        assert task is not None
        result.pr_url = task.pr_url
        say(f"executor: sandboxed fix, checks passed, PR opened {task.pr_url} ({task.status})")
        queue.enqueue(
            s,
            tenant_id=tenant.id,
            kind="track_prs",
            payload={"project_id": ids["project"]},
            idempotency_key=f"demo:{tenant.id}:track:1",
        )
    _drain(platform)

    with platform.db.session() as s:
        task = s.get(Task, result.approved_task_id)
        repo_row = s.get(Repository, ids["repo"])
        assert task is not None
        assert repo_row is not None
        say(f"PR CI status: {task.ci_status}")
        if task.status == "pr_open" and task.pr_number:
            scm = platform.scm.for_provider("local")
            assert isinstance(scm, LocalSCM)
            scm.human_merge(repo_row, task.pr_number)
            say("a human merged the PR (Opsmith itself has no merge capability)")
        queue.enqueue(
            s,
            tenant_id=tenant.id,
            kind="track_prs",
            payload={"project_id": ids["project"]},
            idempotency_key=f"demo:{tenant.id}:track:2",
        )
    _drain(platform)

    # Post-merge window with healthy telemetry -> verification.
    after = utcnow() + timedelta(minutes=1)
    write_signals(logs, metrics, after, 1, timeout_errors=0, cache_errors=0, latency_ms=118)
    with platform.db.session() as s:
        for cid in connectors:
            platform.ingestion.run_connector(s, cid, end=after + timedelta(hours=1, minutes=1))
    _drain(platform)
    with platform.db.session() as s:
        # The scheduled verify job waits for post-merge data; run the check now.
        platform.verifier.run(s, result.approved_task_id or "", attempt=99)
    with platform.db.session() as s:
        task = s.get(Task, result.approved_task_id)
        assert task is not None
        result.final_status = task.status
    say(f"post-merge verification: task is {result.final_status}")
    return result


def summarize(result: DemoResult) -> dict[str, Any]:
    return {
        "data_dir": str(result.data_dir),
        "repo": str(result.repo_path),
        "approved_task": result.approved_task_id,
        "pr": result.pr_url,
        "final_status": result.final_status,
        "api_keys": result.api_keys,
    }
