"""MVP acceptance criteria (spec section 11), asserted against the full offline scenario."""

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from opsmith.config import Settings
from opsmith.demo import run_demo
from opsmith.gitutil import run_git
from opsmith.models import (
    AuditLog,
    ExecutionRun,
    Finding,
    InferenceRun,
    Job,
    Lead,
    Review,
    Task,
)
from opsmith.platform import Platform
from opsmith.provenance import task_provenance

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory):
    base: Path = tmp_path_factory.mktemp("demo")
    result = run_demo(base, verbose=False)
    # OPSMITH_DATABASE_URL lets CI run the same acceptance suite against PostgreSQL.
    platform = Platform(
        Settings(
            data_dir=result.data_dir,
            log_json=False,
            database_url=os.environ.get("OPSMITH_DATABASE_URL", ""),
            sandbox_backend="local",
            local_root=base,
        )
    )
    return result, platform


def test_1_logs_metrics_repo_produce_ranked_tasks_without_intervention(demo) -> None:
    result, platform = demo
    with platform.db.session() as s:
        tasks = s.scalars(
            select(Task).where(Task.project_id == result.project_id).order_by(Task.score.desc())
        ).all()
        assert len(tasks) >= 3
        assert [t.score for t in tasks] == sorted((t.score for t in tasks), reverse=True)
        assert {t.priority for t in tasks} <= {"P0", "P1", "P2", "P3"}
        kinds = {e["kind"] for t in tasks for e in t.evidence}
        assert kinds == {"runtime", "static"}
        # Runtime + static evidence were correlated into one task.
        assert any({e["kind"] for e in t.evidence} == {"runtime", "static"} for t in tasks)


def test_2_lead_from_run_n_is_input_to_run_n_plus_1(demo) -> None:
    _, platform = demo
    with platform.db.session() as s:
        runs = {r.slot: r for r in s.scalars(select(InferenceRun))}
        assert runs["run-2"].previous_run_id == runs["run-1"].id
        leads = s.scalars(select(Lead).where(Lead.originating_run_id == runs["run-1"].id)).all()
        assert leads
        carried_ids = {lead["id"] for lead in runs["run-2"].input_leads}
        assert {lead.id for lead in leads} <= carried_ids
        states = {lead.state for lead in leads}
        assert "confirmed" in states  # timeouts confirmed with carried context
        assert states <= {"confirmed", "carried", "refuted", "expired"}


def test_3_duplicates_and_rejections_do_not_reappear(demo) -> None:
    result, platform = demo
    with platform.db.session() as s:
        fps = [
            f.fingerprint
            for f in s.scalars(
                select(Finding).where(
                    Finding.project_id == result.project_id, Finding.status != "suppressed"
                )
            )
        ]
        tasks = s.scalars(select(Task)).all()
        titles = [t.title for t in tasks]
        assert len(titles) == len(set(titles))  # no duplicate tasks
        assert len(fps) >= len(tasks)
    # Re-auditing the same commit must not create new tasks.
    with platform.db.session() as s:
        before = len(s.scalars(select(Task)).all())
        platform.audits.run(s, s.scalars(select(Task)).first().repository_id, mode="full")
    platform.worker().drain()
    with platform.db.session() as s:
        assert len(s.scalars(select(Task)).all()) == before


def test_4_no_code_change_without_recorded_human_approval(demo) -> None:
    _, platform = demo
    with platform.db.session() as s:
        for run in s.scalars(select(ExecutionRun)):
            review = s.get(Review, run.review_id)
            assert review is not None
            assert review.decision in {"approve", "approve_with_edits"}
            assert review.reviewer_role in {"reviewer", "admin"}
            assert review.created_at <= run.started_at
        unapproved = s.scalars(select(Task).where(Task.status == "in_review")).all()
        assert unapproved
        assert all(t.pr_url is None for t in unapproved)


def test_5_approved_task_results_in_pr_on_new_branch_with_ci(demo) -> None:
    result, platform = demo
    assert result.pr_url == "local://payments-api/pull/1"
    with platform.db.session() as s:
        task = s.get(Task, result.approved_task_id)
        assert task.ci_status == "success"
        assert task.branch_name.startswith("opsmith/")
        branches = run_git(["branch", "--list"], result.repo_path)
        assert task.branch_name in branches
        log = run_git(["log", "--merges", "--format=%an"], result.repo_path)
        assert "Human Reviewer" in log  # merged by a human, never by Opsmith


def test_6_every_pr_traces_back_to_evidence_and_runs(demo) -> None:
    result, platform = demo
    with platform.db.session() as s:
        task = s.get(Task, result.approved_task_id)
        prov = task_provenance(s, task)
        assert prov["executions"][0]["pr_url"] == result.pr_url
        assert prov["reviews"][0]["decision"] == "approve"
        sources = {f["source"] for f in prov["findings"]}
        assert "inference" in sources
        assert any(x.startswith("audit:") for x in sources)
        assert prov["inference_runs"]
        assert prov["audit_runs"]
        assert prov["bundles"]
        assert prov["leads"]
        refs = [r for f in prov["findings"] for r in f["evidence_refs"]]
        assert any(r.startswith("bundle:") for r in refs)
        assert any(r.startswith("obj:") for r in refs)  # raw event in object storage
        raw_key = next(r for r in refs if r.startswith("obj:"))[4:].split("#")[0]
        assert platform.store.exists(raw_key)


def test_7_no_silent_drops(demo) -> None:
    result, platform = demo
    with platform.db.session() as s:
        dead = s.scalars(select(Job).where(Job.status == "dead")).all()
        assert dead == []
        assert result.final_status == "verified"
        actions = {a.action for a in s.scalars(select(AuditLog))}
        assert {
            "task.in_review",
            "task.approved",
            "task.executing",
            "task.pr_open",
            "task.merged",
            "task.verified",
            "lead.confirmed",
            "inference.completed",
            "audit.completed",
            "aggregation.completed",
        } <= actions
