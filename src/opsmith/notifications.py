"""Notifications (review queue items, escalations, dead jobs, PR updates).

``notify`` writes an outbox row and enqueues a ``notify`` job, so delivery inherits the
queue's retries and dead-lettering. Channels: ``log`` and ``webhook`` (Slack-compatible
``{"text": ...}`` JSON body).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from opsmith import queue
from opsmith.connectors.base import ConnectorError, Resolver, check_host_reachable
from opsmith.models import Job, Notification, Project
from opsmith.schemas import NotificationChannel, ProjectSettings

log = logging.getLogger(__name__)


def notify(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    event: str,
    subject: str,
    body: str = "",
    link: str | None = None,
) -> Notification:
    note = Notification(
        tenant_id=tenant_id,
        project_id=project_id,
        event=event,
        subject=subject[:300],  # column size; titles come from model output and can be long
        body=body,
        link=link[:500] if link else None,
    )
    session.add(note)
    session.flush()
    queue.enqueue(
        session,
        tenant_id=tenant_id,
        kind="notify",
        payload={"notification_id": note.id},
        idempotency_key=f"notify:{note.id}",
    )
    return note


def _channels(session: Session, note: Notification) -> list[NotificationChannel]:
    if note.project_id is None:
        return [NotificationChannel(type="log")]
    project = session.get(Project, note.project_id)
    if project is None:
        return [NotificationChannel(type="log")]
    settings = ProjectSettings.model_validate(project.settings)
    return [
        ch
        for ch in settings.notifications
        if any(fnmatch.fnmatch(note.event, pattern) for pattern in ch.events)
    ]


def deliver(
    session: Session,
    note: Notification,
    http: httpx.Client | None = None,
    resolver: Resolver | None = None,
) -> list[dict[str, Any]]:
    deliveries: list[dict[str, Any]] = []
    failures = 0
    for channel in _channels(session, note):
        if channel.type == "log":
            log.info("notification", extra={"event": note.event, "subject": note.subject})
            deliveries.append({"channel": "log", "ok": True})
        elif channel.type == "webhook" and channel.url:
            try:
                # A webhook URL comes from project settings; it must not be able to reach
                # cloud metadata endpoints any more than a connector can.
                check_host_reachable(urlsplit(channel.url).hostname or "", resolver)
            except ConnectorError as exc:
                log.warning("webhook refused", extra={"error": str(exc)})
                deliveries.append({"channel": "webhook", "ok": False, "error": "refused"})
                continue  # not a failure to retry: no retry can make this URL acceptable
            client = http or httpx.Client(timeout=10)
            try:
                text = f"*{note.subject}*\n{note.body}" + (f"\n{note.link}" if note.link else "")
                resp = client.post(channel.url, json={"text": text, "event": note.event})
                ok = resp.status_code < 300
            except httpx.HTTPError as exc:
                ok = False
                log.warning("webhook delivery failed", extra={"error": type(exc).__name__})
            finally:
                if http is None:
                    client.close()
            failures += 0 if ok else 1
            deliveries.append({"channel": "webhook", "ok": ok})
    note.deliveries = deliveries
    refused = any(d.get("error") == "refused" for d in deliveries)
    note.status = "failed" if failures or refused else "sent"
    if failures:
        raise RuntimeError(f"{failures} notification channel(s) failed for {note.id}")
    return deliveries


def handle_notify_job(session: Session, job: Job) -> dict[str, Any]:
    note = session.get(Notification, job.payload["notification_id"])
    if note is None:
        raise queue.PermanentFailure("notification vanished")
    return {"deliveries": deliver(session, note)}
