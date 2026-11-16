from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from opsmith.api.app import create_app
from opsmith.models import Task
from opsmith.security import create_tenant, create_user
from tests.test_aggregation import add_finding, aggregate


@pytest.fixture
def client(platform, seed) -> TestClient:
    return TestClient(create_app(platform))


def h(seed, role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {seed.keys[role]}"}


@pytest.fixture
def task_id(platform, seed) -> str:
    with platform.db.session() as s:
        add_finding(s, seed)
    aggregate(platform, seed)
    with platform.db.session() as s:
        return s.scalars(select(Task)).one().id


def test_health_metrics_and_security_headers(client) -> None:
    resp = client.get("/healthz")
    assert resp.json()["status"] == "ok"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert "opsmith_http_seconds" in client.get("/metrics").text


def test_auth_required_and_roles_enforced(client, seed) -> None:
    assert client.get("/api/v1/projects").status_code == 401
    assert (
        client.get("/api/v1/projects", headers={"Authorization": "Bearer osk_nope"}).status_code
        == 401
    )
    assert client.get("/api/v1/me", headers=h(seed, "viewer")).json()["role"] == "viewer"
    resp = client.post("/api/v1/projects", json={"name": "x"}, headers=h(seed, "viewer"))
    assert resp.status_code == 403


def test_tenant_isolation(client, platform, seed, task_id) -> None:
    with platform.db.session() as s:
        other = create_tenant(s, "globex")
        _, other_key = create_user(s, other, "Mallory", "admin")
    hdr = {"Authorization": f"Bearer {other_key}"}
    assert client.get("/api/v1/projects", headers=hdr).json() == []
    assert client.get(f"/api/v1/tasks/{task_id}", headers=hdr).status_code == 404
    assert client.get(f"/api/v1/projects/{seed.project.id}/tasks", headers=hdr).status_code == 404
    resp = client.post(f"/api/v1/tasks/{task_id}/review", json={"decision": "approve"}, headers=hdr)
    assert resp.status_code == 404  # other tenants' tasks are indistinguishable from missing
    assert client.get(f"/api/v1/tasks/{task_id}", headers=h(seed)).status_code == 200


def test_configuration_crud(client, seed) -> None:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "checkout", "settings": {"inference_interval_hours": 2}},
        headers=h(seed),
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]
    assert (
        client.post("/api/v1/projects", json={"name": "checkout"}, headers=h(seed)).status_code
        == 409
    )
    bad = client.patch(
        f"/api/v1/projects/{pid}/settings", json={"settings": {"bogus": 1}}, headers=h(seed)
    )
    assert bad.status_code == 422
    ok = client.patch(
        f"/api/v1/projects/{pid}/settings",
        json={"settings": {"lead_max_carry": 5}},
        headers=h(seed),
    )
    assert ok.json()["settings"]["lead_max_carry"] == 5
    repo = client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={
            "name": "checkout-api",
            "url": "https://github.com/acme/checkout.git",
            "full_name": "acme/checkout",
            "credentials_ref": "env:GITHUB_TOKEN",
        },
        headers=h(seed),
    )
    assert repo.status_code == 201
    svc = client.post(
        f"/api/v1/projects/{pid}/services",
        json={
            "name": "checkout-api",
            "repository_id": repo.json()["id"],
            "inference_interval_hours": 1,
        },
        headers=h(seed),
    )
    assert svc.status_code == 201
    conn = client.post(
        f"/api/v1/projects/{pid}/connectors",
        json={
            "name": "loki",
            "type": "loki",
            "config": {"url": "http://loki", "query": "{}"},
            "service_id": svc.json()["id"],
        },
        headers=h(seed),
    )
    assert conn.json()["kind"] == "logs"
    assert (
        client.post(
            f"/api/v1/projects/{pid}/connectors",
            json={"name": "x", "type": "kafka"},
            headers=h(seed),
        ).status_code
        == 422
    )
    schedules = client.get(f"/api/v1/projects/{pid}/schedules", headers=h(seed)).json()
    kinds = {s["job_kind"]: s for s in schedules}
    assert set(kinds) == {"ingest", "inference", "audit", "track_prs"}
    assert kinds["inference"]["interval_seconds"] == 3600  # per-service override
    upd = client.patch(
        f"/api/v1/schedules/{kinds['audit']['id']}", json={"cron": "0 4 * * 1"}, headers=h(seed)
    )
    assert upd.json()["cron"] == "0 4 * * 1"
    assert (
        client.patch(
            f"/api/v1/schedules/{kinds['audit']['id']}", json={"cron": "bad"}, headers=h(seed)
        ).status_code
        == 422
    )
    assert "file_logs" in client.get("/api/v1/catalog", headers=h(seed)).json()["connectors"]


def test_review_flow_and_provenance(client, seed, task_id) -> None:
    resp = client.post(
        f"/api/v1/tasks/{task_id}/review", json={"decision": "approve"}, headers=h(seed, "viewer")
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/api/v1/tasks/{task_id}/review", json={"decision": "reject"}, headers=h(seed, "reviewer")
    )
    assert resp.status_code == 409
    resp = client.post(
        f"/api/v1/tasks/{task_id}/review",
        json={"decision": "approve", "comment": "ok"},
        headers=h(seed, "reviewer"),
    )
    assert resp.status_code == 200
    assert resp.json()["task"]["status"] == "approved"
    prov = client.get(f"/api/v1/tasks/{task_id}/provenance", headers=h(seed)).json()
    assert prov["reviews"][0]["reviewer_name"] == "Rui Reviewer"
    assert prov["findings"][0]["rule_id"] == "py.http-no-timeout"
    assert [a["to_state"] for a in prov["audit_log"]][-2:] == ["in_review", "approved"]
    tasks = client.get(
        f"/api/v1/projects/{seed.project.id}/tasks?status=approved", headers=h(seed)
    ).json()
    assert [t["id"] for t in tasks] == [task_id]
    summary = client.get(f"/api/v1/projects/{seed.project.id}/summary", headers=h(seed)).json()
    assert summary["tasks"]["approved"] == 1


def test_push_ingestion_requires_service_role(client, platform, seed) -> None:
    now = datetime.now().astimezone()
    records = [
        {
            "timestamp": (now - timedelta(minutes=i)).isoformat(),
            "service": "payments-api",
            "severity": "error",
            "message": f"boom {i} for a@b.co",
        }
        for i in range(5)
    ]
    url = f"/api/v1/connectors/{seed.log_connector.id}/push"
    assert client.post(url, json=records, headers=h(seed, "reviewer")).status_code == 403
    resp = client.post(url, json=records, headers=h(seed, "service"))
    assert resp.status_code == 202
    assert resp.json()["records"] == 5
    assert client.post(url, json=[{"bad": 1}], headers=h(seed, "service")).status_code == 422


def test_trigger_and_dead_job_retry(client, platform, seed) -> None:
    resp = client.post(
        f"/api/v1/projects/{seed.project.id}/trigger",
        json={"job": "audit", "target_id": seed.repo.id, "mode": "full"},
        headers=h(seed),
    )
    assert resp.status_code == 202
    jobs = client.get("/api/v1/jobs?status=queued", headers=h(seed)).json()
    assert any(j["kind"] == "audit" for j in jobs)
    assert (
        client.post(f"/api/v1/jobs/{resp.json()['job_id']}/retry", headers=h(seed)).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/projects/{seed.project.id}/trigger",
            json={"job": "ingest", "target_id": "con_missing"},
            headers=h(seed),
        ).status_code
        == 404
    )


def test_ui_login_pages_and_review_form(client, seed, task_id) -> None:
    assert client.get("/ui", follow_redirects=False).headers["location"] == "/ui/login"
    bad = client.post("/ui/login", data={"api_key": "nope"}, follow_redirects=False)
    assert "error" in bad.headers["location"]
    ok = client.post("/ui/login", data={"api_key": seed.keys["reviewer"]}, follow_redirects=False)
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    for page in (
        "/ui",
        "/ui/review",
        "/ui/tasks",
        "/ui/leads",
        "/ui/executions",
        "/ui/config",
        "/ui/jobs",
        "/ui/jobs?status=dead",
        f"/ui/tasks/{task_id}",
    ):
        resp = client.get(page)
        assert resp.status_code == 200, page
        assert "<title>" in resp.text
    assert "Submit review" in client.get(f"/ui/tasks/{task_id}").text
    assert "py.http-no-timeout in app/payments.py" in client.get("/ui/review").text
    evil = client.post(
        f"/ui/tasks/{task_id}/review",
        data={"decision": "approve"},
        headers={"origin": "https://evil.example"},
    )
    assert evil.status_code == 403
    resp = client.post(
        f"/ui/tasks/{task_id}/review",
        data={"decision": "approve", "edit_priority": "P0", "comment": "ship"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get(f"/ui/tasks/{task_id}").text
    assert "approve_with_edits" in page
    assert "Rui Reviewer" in page


def test_ui_settings_editor_is_admin_only(client, seed) -> None:
    client.post("/ui/login", data={"api_key": seed.keys["reviewer"]})
    url = f"/ui/projects/{seed.project.id}/settings"
    assert client.post(url, data={"settings_json": "{}"}).status_code == 403
    client.post("/ui/login", data={"api_key": seed.keys["admin"]})
    resp = client.post(url, data={"settings_json": "{not json"}, follow_redirects=False)
    assert "error" in resp.headers["location"]
    resp = client.post(url, data={"settings_json": '{"lead_max_carry": 4}'}, follow_redirects=False)
    assert resp.status_code == 303
    page = client.get(f"/ui/config?project_id={seed.project.id}").text
    assert "lead_max_carry&#34;: 4" in page  # rendered (escaped) inside the editor


# --------------------------------------------------------------------------- regressions (1.2.1)


@pytest.mark.parametrize(
    "timestamp",
    ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00", "9999-12-31T23:59:59.5Z"],
)
def test_a_pushed_timestamp_at_the_edge_of_the_calendar_is_a_422(client, seed, timestamp) -> None:
    """Converting it to UTC (or adding the second that closes the window) overflowed, which
    was an unhandled 500: a status log shippers retry forever."""
    url = f"/api/v1/connectors/{seed.log_connector.id}/push"
    record = {"timestamp": timestamp, "service": "payments-api", "message": "m"}
    resp = client.post(url, json=[record], headers=h(seed, "service"))
    assert resp.status_code == 422
    assert "out of range" in resp.text


@pytest.mark.parametrize("level", ["²", "9" * 5000])
def test_a_pushed_severity_that_only_looks_numeric_is_accepted(client, seed, level) -> None:
    url = f"/api/v1/connectors/{seed.log_connector.id}/push"
    record = {
        "timestamp": "2026-11-16T00:00:00Z",
        "service": "payments-api",
        "message": "m",
        "severity": level,
    }
    resp = client.post(url, json=[record], headers=h(seed, "service"))
    assert (resp.status_code, resp.json()["records"]) == (202, 1)


def test_a_write_is_not_acknowledged_before_it_is_committed(platform, seed, monkeypatch) -> None:
    """The request's transaction was committed after the response had been sent, so a commit
    that failed (a dropped connection, a constraint checked at commit) still answered 201
    with the id of a row that was never stored."""
    from sqlalchemy.orm import Session  # noqa: PLC0415

    client = TestClient(create_app(platform), raise_server_exceptions=False)
    real_commit = Session.commit
    armed = {"on": True}

    def failing_commit(self: Session) -> None:
        if armed["on"]:
            raise RuntimeError("connection lost at commit")
        real_commit(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    resp = client.post("/api/v1/projects", json={"name": "lost"}, headers=h(seed))
    armed["on"] = False
    assert resp.status_code == 500
    names = [p["name"] for p in client.get("/api/v1/projects", headers=h(seed)).json()]
    assert "lost" not in names


def test_values_longer_than_their_columns_are_refused_when_saved(client, seed) -> None:
    """PostgreSQL enforces column sizes; SQLite does not. These were accepted and then failed
    inside a later request or job."""
    long_cron = ",".join(str(m) for m in range(60)) + " * * * *"  # valid, and 177 characters
    assert len(long_cron) > 100
    settings_url = f"/api/v1/projects/{seed.project.id}/settings"
    for settings in (
        {"audit_cron": long_cron},
        {"executor_agent": "a" * 51},
        {"llm": {"default": {"provider": "openai", "model": "m" * 101}}},
        {"llm": {"default": {"provider": "p" * 51, "model": "m"}}},
    ):
        resp = client.patch(settings_url, json={"settings": settings}, headers=h(seed))
        assert resp.status_code == 422, settings
    schedules = client.get(f"/api/v1/projects/{seed.project.id}/schedules", headers=h(seed))
    schedule_id = schedules.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/schedules/{schedule_id}", json={"cron": long_cron}, headers=h(seed)
    )
    assert resp.status_code == 422


def test_an_id_with_a_nul_is_not_found(client, seed) -> None:
    """PostgreSQL cannot compare text with a NUL in it; the lookup was a 500 there."""
    assert client.get("/api/v1/tasks/task%00x", headers=h(seed)).status_code == 404
    assert client.get("/api/v1/projects/a%00b", headers=h(seed)).status_code == 404


def test_the_api_reference_page_is_allowed_to_load_its_assets(client) -> None:
    """``/docs`` was served with the UI's strict policy, which blocks the script and style
    sheet Swagger UI loads from its CDN: the page documented in the API guide was blank."""
    docs = client.get("/docs")
    assert docs.status_code == 200
    script_host = "https://cdn.jsdelivr.net"
    assert script_host in docs.text
    assert script_host in docs.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in docs.headers["content-security-policy"]
    # Everything else keeps the strict policy.
    for path in ("/ui/login", "/healthz", "/api/v1/me"):
        policy = client.get(path).headers["content-security-policy"]
        assert "cdn.jsdelivr.net" not in policy
        assert "unsafe-inline" not in policy
