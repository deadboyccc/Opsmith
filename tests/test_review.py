from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from opsmith.models import Job, Lead, Review, SuppressionRule, Task, User
from opsmith.review import ReviewError, ReviewRequest
from tests.conftest import error_records, write_logs
from tests.test_aggregation import add_finding, aggregate


@pytest.fixture
def task_id(platform, seed) -> str:
    with platform.db.session() as s:
        add_finding(s, seed, source="inference", rule=None, signal="log:sig")
    aggregate(platform, seed)
    with platform.db.session() as s:
        return s.scalars(select(Task)).one().id


def review(platform, user_id: str, task_id: str, **kw):
    with platform.db.session() as s:
        task = s.get(Task, task_id)
        user = s.get(User, user_id)
        return platform.reviews.review(s, task, user, ReviewRequest(**kw))


def status(platform, task_id: str) -> str:
    with platform.db.session() as s:
        return s.get(Task, task_id).status


def test_viewers_cannot_review(platform, seed, task_id) -> None:
    with pytest.raises(ReviewError, match="only reviewers"):
        review(platform, seed.viewer.id, task_id, decision="approve")
    with pytest.raises(ReviewError):
        review(platform, seed.service_user.id, task_id, decision="approve")
    assert status(platform, task_id) == "in_review"


def test_approve_records_identity_and_enqueues_execution(platform, seed, task_id) -> None:
    rev = review(platform, seed.reviewer.id, task_id, decision="approve", comment="lgtm")
    with platform.db.session() as s:
        stored = s.get(Review, rev.id)
        assert stored.reviewer_name == "Rui Reviewer"
        assert stored.created_at is not None
        job = s.scalar(select(Job).where(Job.kind == "execute"))
        assert job.payload == {"task_id": task_id, "review_id": rev.id}
    assert status(platform, task_id) == "approved"
    with pytest.raises(ReviewError, match="only in_review"):
        review(platform, seed.reviewer.id, task_id, decision="approve")


def test_approve_with_edits_validates_and_applies(platform, seed, task_id) -> None:
    with pytest.raises(ReviewError, match="not editable"):
        review(
            platform,
            seed.reviewer.id,
            task_id,
            decision="approve_with_edits",
            edits={"status": "merged"},
        )
    with pytest.raises(ReviewError, match="priority"):
        review(
            platform,
            seed.reviewer.id,
            task_id,
            decision="approve_with_edits",
            edits={"priority": "P9"},
        )
    with pytest.raises(ReviewError, match="approve_with_edits"):
        review(platform, seed.reviewer.id, task_id, decision="approve", edits={"title": "x"})
    edits = {
        "title": "Add timeout to gateway call",
        "priority": "P0",
        "paths": [".github/workflows/ci.yml"],
    }
    # Moving the scope onto a protected path re-evaluates protection: a reviewer can no
    # longer approve it, an admin can with an explicit acknowledgement.
    with pytest.raises(ReviewError, match="elevated"):
        review(platform, seed.reviewer.id, task_id, decision="approve_with_edits", edits=edits)
    rev = review(
        platform,
        seed.admin.id,
        task_id,
        decision="approve_with_edits",
        edits=edits,
        elevated_ack=True,
    )
    assert rev.edits["after"]["priority"] == "P0"
    with platform.db.session() as s:
        task = s.get(Task, task_id)
        assert task.title == "Add timeout to gateway call"
        assert task.protection == "elevated"
        assert task.status == "approved"


def test_reject_requires_reason_and_creates_suppression(platform, seed, task_id) -> None:
    with pytest.raises(ReviewError, match="reason_code"):
        review(platform, seed.reviewer.id, task_id, decision="reject")
    review(platform, seed.reviewer.id, task_id, decision="reject", reason_code="false_positive")
    with platform.db.session() as s:
        rule = s.scalars(select(SuppressionRule)).one()
        assert rule.reason_code == "false_positive"
        assert rule.sources_at_rejection == ["runtime"]
        assert s.get(Task, task_id).rejection_reason == "false_positive"


def test_defer_and_resubmit(platform, seed, task_id) -> None:
    review(platform, seed.reviewer.id, task_id, decision="defer")
    assert status(platform, task_id) == "deferred"
    with platform.db.session() as s:
        platform.reviews.resubmit(s, s.get(Task, task_id), s.get(User, seed.reviewer.id))
    assert status(platform, task_id) == "in_review"


def test_request_investigation_runs_through_next_inference(platform, seed, task_id) -> None:
    review(
        platform,
        seed.reviewer.id,
        task_id,
        decision="request_investigation",
        investigation="Is this tied to the gateway deploy?",
    )
    assert status(platform, task_id) == "investigating"
    with platform.db.session() as s:
        lead = s.scalars(select(Lead)).one()
        assert lead.source == "review_request"
        assert lead.task_id == task_id
    start = datetime.fromisoformat(seed.log_connector.cursor["last_end"])
    write_logs(seed.logs, error_records(start, 3))
    with platform.db.session() as s:
        platform.ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        platform.bundles.build_for_project(s, seed.project.id)
        run = platform.inference.run(s, seed.service.id, "inv")
        assert lead.id in [lead["id"] for lead in run.input_leads]
    with platform.db.session() as s:
        assert s.get(Lead, lead.id).state == "confirmed"
        task = s.get(Task, task_id)
        assert task.status == "in_review"
        assert task.evidence[-1]["type"] == "investigation"


def test_protected_paths_need_elevated_admin_approval(platform, seed, task_id) -> None:
    with platform.db.session() as s:
        s.get(Task, task_id).protection = "elevated"
    with pytest.raises(ReviewError, match="elevated"):
        review(platform, seed.reviewer.id, task_id, decision="approve")
    with pytest.raises(ReviewError, match="elevated"):
        review(platform, seed.admin.id, task_id, decision="approve")
    review(platform, seed.admin.id, task_id, decision="approve", elevated_ack=True)
    assert status(platform, task_id) == "approved"


def test_excluded_paths_cannot_be_approved(platform, seed, task_id) -> None:
    with platform.db.session() as s:
        s.get(Task, task_id).protection = "excluded"
    with pytest.raises(ReviewError, match="excluded"):
        review(platform, seed.admin.id, task_id, decision="approve", elevated_ack=True)


def test_stale_version_is_rejected(platform, seed, task_id) -> None:
    with pytest.raises(ReviewError, match="changed since"):
        review(platform, seed.reviewer.id, task_id, decision="approve", expected_version=999)


def test_edits_of_the_wrong_type_are_refused_not_crashed(platform, seed, task_id) -> None:
    """A JSON list or object where a string belongs used to raise ``TypeError`` (a 500):
    the allowed values are kept in a set, and membership needs a hashable value."""
    for edits in ({"priority": ["P0"]}, {"effort_estimate": {"size": "S"}}, {"category": ["fix"]}):
        with pytest.raises(ReviewError):
            review(platform, seed.reviewer.id, task_id, decision="approve_with_edits", edits=edits)
    assert status(platform, task_id) == "in_review"


def test_reason_code_is_bounded_to_its_column() -> None:
    """Only ``reject`` checked the reason code. Any other decision stored it unchecked, which
    PostgreSQL refuses for more than 50 characters (a 500 instead of a validation error)."""
    with pytest.raises(ValueError, match="reason_code"):
        ReviewRequest(decision="defer", reason_code="x" * 51)
    assert ReviewRequest(decision="defer", reason_code="too_risky").reason_code == "too_risky"
