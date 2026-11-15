import json

import httpx
import pytest
from sqlalchemy import select

from opsmith.models import Notification, Project
from opsmith.notifications import deliver, notify


def _channels(platform, seed, channels) -> None:
    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        project.settings = {**project.settings, "notifications": channels}


def test_webhook_delivery_filters_events(platform, seed) -> None:
    _channels(
        platform,
        seed,
        [
            {"type": "webhook", "url": "https://hooks.example/x", "events": ["task.*"]},
            {"type": "log"},
        ],
    )
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with platform.db.session() as s:
        note = notify(
            s,
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            event="task.in_review",
            subject="Review me",
            link="/ui/tasks/1",
        )
        assert [d["channel"] for d in deliver(s, note, http)] == ["webhook", "log"]
        other = notify(
            s,
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            event="lead.escalated",
            subject="x",
        )
        assert [d["channel"] for d in deliver(s, other, http)] == ["log"]
    assert sent == [{"text": "*Review me*\n\n/ui/tasks/1", "event": "task.in_review"}]


def test_failed_webhook_raises_for_queue_retry(platform, seed) -> None:
    _channels(platform, seed, [{"type": "webhook", "url": "https://hooks.example/x"}])
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with platform.db.session() as s:
        note = notify(
            s, tenant_id=seed.tenant.id, project_id=seed.project.id, event="job.dead", subject="x"
        )
        with pytest.raises(RuntimeError, match="failed"):
            deliver(s, note, http)
        assert note.status == "failed"
        assert s.scalars(select(Notification)).all()
