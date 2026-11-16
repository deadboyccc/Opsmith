from datetime import timedelta

import pytest
from sqlalchemy import select

from opsmith.aggregation.scoring import noisy_or, priority_for, score
from opsmith.audit_log import transition_task
from opsmith.db import utcnow
from opsmith.models import (
    Finding,
    Notification,
    Repository,
    Service,
    Task,
    TaskFingerprint,
    User,
)
from opsmith.review import ReviewError, ReviewRequest


def add_finding(
    s,
    seed,
    *,
    source="audit:python-ast",
    category="performance",
    path="app/payments.py",
    line=10,
    fp="fp-static",
    severity="medium",
    confidence=0.7,
    strength=1.0,
    rule="py.http-no-timeout",
    signal=None,
) -> Finding:
    f = Finding(
        tenant_id=seed.tenant.id,
        project_id=seed.project.id,
        source=source,
        source_run_id="run_x",
        category=category,
        rule_id=rule,
        title=f"{rule} in {path}",
        description="d",
        severity=severity,
        confidence=confidence,
        evidence_refs=["ref"],
        repository_id=seed.repo.id,
        service_id=seed.service.id if source == "inference" else None,
        path=path,
        line=line,
        fingerprint=fp,
        evidence_strength=strength,
        signal=signal,
    )
    s.add(f)
    s.flush()
    return f


def aggregate(platform, seed) -> dict[str, int]:
    with platform.db.session() as s:
        return platform.aggregator.run(s, seed.project.id)


def tasks(platform) -> list[Task]:
    with platform.db.session() as s:
        return list(s.scalars(select(Task).order_by(Task.created_at)).all())


def test_runtime_and_static_findings_merge_with_higher_confidence(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed, confidence=0.6)
        add_finding(
            s,
            seed,
            source="inference",
            rule=None,
            fp="fp-runtime",
            confidence=0.6,
            signal="log:abc",
        )
        add_finding(s, seed, category="security", rule="secret.x", line=6, fp="fp-secret")
    stats = aggregate(platform, seed)
    assert stats["created"] == 2
    merged = next(t for t in tasks(platform) if len(t.evidence) == 2)
    assert {e["kind"] for e in merged.evidence} == {"runtime", "static"}
    assert merged.confidence == noisy_or([0.6, 0.6]) == 0.84
    assert merged.status == "in_review"
    assert merged.branch_name.startswith("opsmith/performance/")
    assert merged.provenance["audit_run_ids"] == ["run_x"]
    assert merged.verification["signals"] == ["log:abc"]
    with platform.db.session() as s:
        assert s.scalar(select(Notification).where(Notification.event == "task.in_review"))


def test_runtime_evidence_arriving_later_attaches_to_existing_task(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed)
    aggregate(platform, seed)
    with platform.db.session() as s:
        add_finding(s, seed, source="inference", rule=None, fp="fp-runtime", line=11)
    stats = aggregate(platform, seed)
    assert stats["merged"] == 1
    assert stats["created"] == 0
    [task] = tasks(platform)
    assert {e["kind"] for e in task.evidence} == {"runtime", "static"}
    assert task.occurrences == 2


def test_repeat_static_detection_does_not_inflate_evidence(platform, seed) -> None:
    for _ in range(3):
        with platform.db.session() as s:
            add_finding(s, seed)
        aggregate(platform, seed)
    [task] = tasks(platform)
    assert task.occurrences == 1
    assert len(task.evidence) == 1


def test_recently_closed_tasks_suppress_duplicates(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed)
    aggregate(platform, seed)
    with platform.db.session() as s:
        task = s.scalars(select(Task)).one()
        transition_task(s, task, "closed")
        add_finding(s, seed)
    stats = aggregate(platform, seed)
    assert stats["suppressed"] == 1
    with platform.db.session() as s:
        latest = s.scalars(select(Finding).order_by(Finding.created_at.desc())).first()
        assert latest.status == "suppressed"
        assert latest.status_reason.startswith("recently_closed")
        task = s.scalars(select(Task)).one()
        task.updated_at = utcnow() - timedelta(days=30)
        add_finding(s, seed)
    assert aggregate(platform, seed)["created"] == 1  # long-closed issue resurfacing = new task


def _reject(platform, seed, reason: str) -> str:
    with platform.db.session() as s:
        task = s.scalars(select(Task)).one()
        reviewer = s.get(User, seed.reviewer.id)
        platform.reviews.review(
            s, task, reviewer, ReviewRequest(decision="reject", reason_code=reason)
        )
        return task.id


def test_rejected_findings_stay_suppressed_unless_evidence_materially_changes(platform, seed):
    with platform.db.session() as s:
        add_finding(s, seed, strength=1.0)
    aggregate(platform, seed)
    task_id = _reject(platform, seed, "insufficient_evidence")

    with platform.db.session() as s:
        add_finding(s, seed, strength=1.0)  # same evidence again
    assert aggregate(platform, seed)["suppressed"] == 1
    with platform.db.session() as s:
        assert s.get(Task, task_id).status == "rejected"
        assert (
            s.scalars(select(Finding).where(Finding.status == "suppressed")).first().status_reason
            == "previously_rejected:insufficient_evidence"
        )

    with platform.db.session() as s:
        add_finding(s, seed, strength=2.5)  # >= 2x the evidence at rejection
    assert aggregate(platform, seed)["reopened"] == 1
    with platform.db.session() as s:
        task = s.get(Task, task_id)
        assert task.status == "in_review"
        assert task.rejection_reason is None


def test_wont_fix_only_reopens_on_severity_increase(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed)
    aggregate(platform, seed)
    _reject(platform, seed, "wont_fix")
    with platform.db.session() as s:
        add_finding(s, seed, strength=50)
        add_finding(s, seed, source="inference", rule=None, fp="fp-rt", line=10)
    assert aggregate(platform, seed)["suppressed"] >= 1
    with platform.db.session() as s:
        add_finding(s, seed, severity="critical")
    assert aggregate(platform, seed)["reopened"] == 1


def test_low_confidence_and_task_cap(platform, seed) -> None:
    with platform.db.session() as s:
        project = s.get(type(seed.project), seed.project.id)
        project.settings = {**project.settings, "max_tasks_per_aggregation": 2}
        add_finding(s, seed, fp="weak", confidence=0.1, path="a.py")
        for i in range(4):
            add_finding(s, seed, fp=f"f{i}", path=f"m{i}.py")
    stats = aggregate(platform, seed)
    assert stats["created"] == 2
    assert stats["deferred"] == 2
    assert stats["suppressed"] == 1
    assert aggregate(platform, seed)["created"] == 2  # deferred groups are not dropped


def test_scoring_orders_by_severity_and_evidence() -> None:
    base = {
        "confidence": 0.8,
        "evidence_strength": 1.0,
        "occurrences": 1,
        "services_affected": 1,
        "source_kinds": 1,
        "effort": "M",
        "category": "fix",
    }
    assert score(severity="critical", **base) > score(severity="medium", **base)
    stronger = {**base, "evidence_strength": 8.0, "source_kinds": 2}
    assert score(severity="medium", **stronger) > score(severity="medium", **base)
    stale = score(severity="high", last_seen=utcnow() - timedelta(days=60), **base)
    assert stale < score(severity="high", last_seen=utcnow(), **base)
    assert [priority_for(v) for v in (30, 15, 6, 1)] == ["P0", "P1", "P2", "P3"]


# --------------------------------------------------------------------------- hardening


def _review(platform, seed, decision: str, **kw) -> None:
    with platform.db.session() as s:
        task = s.scalars(select(Task)).one()
        platform.reviews.review(
            s, task, s.get(User, seed.reviewer.id), ReviewRequest(decision=decision, **kw)
        )


def test_approved_task_is_not_changed_by_later_evidence(platform, seed) -> None:
    """Between approval and the end of execution the task is frozen. New evidence (here with
    a model-chosen path under a protected directory and a higher severity) must wait."""
    with platform.db.session() as s:
        add_finding(s, seed, source="inference", rule=None, fp="fp-rt", signal="log:abc")
    aggregate(platform, seed)
    _review(platform, seed, "approve_with_edits", edits={"priority": "P3"})
    [before] = tasks(platform)
    with platform.db.session() as s:
        add_finding(
            s,
            seed,
            source="inference",
            rule=None,
            fp="fp-rt",
            signal="log:abc",
            path=".github/workflows/deploy.yml",
            severity="critical",
        )
    stats = aggregate(platform, seed)
    assert stats["deferred"] == 1
    assert stats["merged"] == 0
    [after] = tasks(platform)
    assert after.status == "approved"
    for field in ("paths", "severity", "priority", "evidence", "protection", "version", "score"):
        assert getattr(after, field) == getattr(before, field), field
    with platform.db.session() as s:
        late = s.scalars(select(Finding).order_by(Finding.created_at.desc())).first()
        assert late.status == "new"  # not dropped: it attaches once the execution is over
        assert late.task_id is None
        task = s.get(Task, after.id)
        transition_task(s, task, "executing")
        transition_task(s, task, "failed")
        transition_task(s, task, "in_review")
    assert aggregate(platform, seed)["merged"] == 1
    [reopened] = tasks(platform)
    assert ".github/workflows/deploy.yml" in reopened.paths
    assert reopened.protection == "elevated"  # the grown scope is re-classified
    assert reopened.severity == "critical"


def test_evidence_attached_during_review_bumps_the_version(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed)
    aggregate(platform, seed)
    [loaded] = tasks(platform)
    with platform.db.session() as s:
        add_finding(s, seed, source="inference", rule=None, fp="fp-rt", path="app/payments.py")
    assert aggregate(platform, seed)["merged"] == 1
    [changed] = tasks(platform)
    assert changed.version > loaded.version
    with platform.db.session() as s, pytest.raises(ReviewError, match="changed since"):
        platform.reviews.review(
            s,
            s.get(Task, loaded.id),
            s.get(User, seed.reviewer.id),
            ReviewRequest(decision="approve", expected_version=loaded.version),
        )


def test_reopening_on_higher_severity_records_it_and_does_not_loop(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed, severity="medium")
    aggregate(platform, seed)
    _reject(platform, seed, "wont_fix")
    with platform.db.session() as s:
        add_finding(s, seed, severity="high")  # the same static finding, re-rated
    assert aggregate(platform, seed)["reopened"] == 1
    [task] = tasks(platform)
    assert task.severity == "high"
    assert task.occurrences == 1  # still the same evidence
    _reject(platform, seed, "wont_fix")
    with platform.db.session() as s:
        add_finding(s, seed, severity="high")
    stats = aggregate(platform, seed)
    assert stats["reopened"] == 0
    assert stats["suppressed"] == 1


def test_identical_static_findings_in_one_batch_count_once(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed, strength=1.0)
    aggregate(platform, seed)
    _reject(platform, seed, "insufficient_evidence")
    with platform.db.session() as s:
        for _ in range(3):
            add_finding(s, seed, strength=1.0)  # three audits re-reporting the same defect
    stats = aggregate(platform, seed)
    assert stats["reopened"] == 0  # 3 x the same evidence is not 3 x the evidence
    with platform.db.session() as s:
        for _ in range(3):
            add_finding(s, seed, fp="fp-fresh", path="other.py", strength=1.0)
    aggregate(platform, seed)
    fresh = tasks(platform)[-1]
    assert fresh.occurrences == 1
    assert fresh.evidence_strength == 1.0
    assert len(fresh.evidence) == 1


def test_runtime_evidence_does_not_attach_to_another_repository(platform, seed) -> None:
    with platform.db.session() as s:
        other = Repository(
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            name="other-repo",
            scm_provider="local",
            url=str(seed.repo_path),
        )
        s.add(other)
        s.flush()
        static = add_finding(s, seed, path="app/main.py", line=12)
        static.repository_id = other.id
    aggregate(platform, seed)
    with platform.db.session() as s:
        add_finding(s, seed, source="inference", rule=None, fp="fp-rt", path="app/main.py", line=12)
    stats = aggregate(platform, seed)
    assert stats["merged"] == 0
    assert stats["created"] == 1


def test_open_task_wins_over_a_newer_closed_one(platform, seed) -> None:
    with platform.db.session() as s:
        add_finding(s, seed, fp="fp-a", path="a.py")
    aggregate(platform, seed)
    with platform.db.session() as s:
        add_finding(s, seed, fp="fp-b", path="b.py")
    aggregate(platform, seed)
    older, newer = tasks(platform)
    with platform.db.session() as s:
        transition_task(s, s.get(Task, newer.id), "closed")
        # One group that carries both fingerprints (a runtime finding ties them together).
        s.add(
            TaskFingerprint(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                task_id=newer.id,
                fingerprint="fp-a",
            )
        )
        add_finding(s, seed, source="inference", rule=None, fp="fp-a", path="a.py", line=10)
    stats = aggregate(platform, seed)
    assert stats["merged"] == 1
    assert stats["suppressed"] == 0
    with platform.db.session() as s:
        latest = s.scalars(select(Finding).order_by(Finding.created_at.desc())).first()
        assert latest.task_id == older.id


def test_attaching_evidence_keeps_blast_radius_and_confidence_consistent(platform, seed) -> None:
    with platform.db.session() as s:
        services = []
        for name in ("checkout", "billing"):
            svc = Service(tenant_id=seed.tenant.id, project_id=seed.project.id, name=name)
            s.add(svc)
            s.flush()
            services.append(svc.id)
        for service_id in (seed.service.id, *services):
            f = add_finding(s, seed, source="inference", rule=None, fp="fp-rt", confidence=0.3)
            f.service_id = service_id
    aggregate(platform, seed)
    [created] = tasks(platform)
    for _ in range(5):  # the same kind of evidence, one batch at a time
        with platform.db.session() as s:
            add_finding(s, seed, source="inference", rule=None, fp="fp-rt", confidence=0.3)
        aggregate(platform, seed)
    [task] = tasks(platform)
    assert task.confidence == created.confidence == 0.3  # repeats of one kind do not compound
    assert task.score > created.score  # more occurrences, and still three services affected
