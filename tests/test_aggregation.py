from datetime import timedelta

from sqlalchemy import select

from opsmith.aggregation.scoring import noisy_or, priority_for, score
from opsmith.audit_log import transition_task
from opsmith.db import utcnow
from opsmith.models import Finding, Notification, Task, User
from opsmith.review import ReviewRequest


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
