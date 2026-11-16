"""Regression tests for the server-rendered UI (and the demo that drives it): form-post origin
checks, logout, error reporting, link rendering, and review-form round trips."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from opsmith.api import ui
from opsmith.api.app import create_app
from opsmith.demo import run_demo
from opsmith.models import ExecutionRun, Review, Task

APPROACH = "1. add a timeout\n2. add a retry"


@pytest.fixture
def client(platform, seed) -> TestClient:
    return TestClient(create_app(platform))


def login(client: TestClient, seed, role: str = "reviewer", **headers: str) -> str:
    resp = client.post(
        "/ui/login", data={"api_key": seed.keys[role]}, headers=headers, follow_redirects=False
    )
    assert (resp.status_code, resp.headers["location"]) == (303, "/ui")
    return resp.headers["set-cookie"].lower()


def make_task(platform, seed, **fields) -> str:
    values = {
        "title": "Fix gateway timeouts",
        "category": "fix",
        "status": "in_review",
        "proposed_approach": APPROACH,
        "branch_name": "opsmith/fix-gateway-timeouts",
        **fields,
    }
    with platform.db.session() as s:
        task = Task(tenant_id=seed.tenant.id, project_id=seed.project.id, **values)
        s.add(task)
        s.flush()
        return task.id


def review_form(platform, task_id: str, **fields: str) -> dict[str, str]:
    """What a browser posts for an untouched review form: textarea newlines become CRLF."""
    with platform.db.session() as s:
        task = s.get(Task, task_id)
        return {
            "decision": "approve",
            "expected_version": str(task.version),
            "reason_code": "",
            "edit_title": task.title,
            "edit_approach": task.proposed_approach.replace("\n", "\r\n"),
            "edit_priority": task.priority,
            "investigation": "",
            "comment": "",
            **fields,
        }


# --------------------------------------------------------------------------- pages


def test_status_filter_heading_is_not_shadowed_by_the_status_macro(client, platform, seed) -> None:
    make_task(platform, seed)
    login(client, seed)
    tasks = client.get("/ui/tasks").text
    assert "<h1>Tasks</h1>" in tasks
    assert "Macro" not in tasks
    assert 'class="chip s-warn">in_review</span>' in tasks  # the macro itself still renders
    assert "<h1>Tasks · in_review</h1>" in client.get("/ui/tasks?status=in_review").text
    assert "<h1>Jobs</h1>" in client.get("/ui/jobs").text
    assert "<h1>Jobs · dead</h1>" in client.get("/ui/jobs?status=dead").text


@pytest.mark.parametrize(
    ("pr_url", "linked"),
    [
        ("https://github.com/acme/payments/pull/7", True),
        ("HTTP://scm.internal/acme/payments/pull/7", True),
        ("javascript:alert(document.domain)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("local://payments-api/pull/7", False),
    ],
)
def test_pr_url_is_a_link_only_when_it_is_http(
    client, platform, seed, pr_url: str, linked: bool
) -> None:
    task_id = make_task(platform, seed, status="pr_open", pr_url=pr_url, pr_number=7)
    with platform.db.session() as s:
        s.add(
            ExecutionRun(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                task_id=task_id,
                review_id="rev_x",
                status="succeeded",
                pr_url=pr_url,
                pr_number=7,
            )
        )
    login(client, seed)
    scheme = pr_url.split(":", 1)[0]
    for page in ("/ui/tasks", f"/ui/tasks/{task_id}", "/ui/executions"):
        html = client.get(page).text
        assert "#7" in html, page  # the PR is still shown
        assert (f'href="{scheme}:' in html) is linked, page


def test_login_page_shows_a_fixed_message_not_the_query_text(client) -> None:
    spoof = "Session expired. Re-enter your key at https://evil.example/login"
    page = client.get("/ui/login", params={"error": spoof}).text
    assert "evil.example" not in page
    assert '<div class="error">Invalid API key</div>' in page
    assert 'class="error"' not in client.get("/ui/login").text
    bad = client.post("/ui/login", data={"api_key": "nope"}, follow_redirects=False)
    assert bad.headers["location"] == "/ui/login?error=1"


# --------------------------------------------------------------------------- error reporting


def test_validation_errors_name_the_failing_field(client, platform, seed) -> None:
    login(client, seed, "admin")
    url = f"/ui/projects/{seed.project.id}/settings"
    resp = client.post(url, data={"settings_json": '{"lead_max_carry": "abc"}'})
    assert resp.status_code == 200  # redirected back to the config page
    assert '<div class="error">lead_max_carry: Input should be a valid integer' in resp.text
    resp = client.post(url, data={"settings_json": '{"nope": 1, "also_nope": 2}'})
    assert "nope: Extra inputs are not permitted (+1 more)" in resp.text
    resp = client.post(url, data={"settings_json": "{not json"})
    assert "Expecting property name" in resp.text

    task_id = make_task(platform, seed)
    resp = client.post(f"/ui/tasks/{task_id}/review", data={"decision": "bogus"})
    assert '<div class="error">decision: Input should be' in resp.text


def test_error_text_survives_the_redirect_query(client, platform, seed) -> None:
    text = "use a&b=c #1 + 100% of café"
    query = ui._error_query(ValueError(text + "\nsecond line is dropped"))
    assert query == "error=use%20a%26b%3Dc%20%231%20%2B%20100%25%20of%20caf%C3%A9"
    task_id = make_task(platform, seed)
    login(client, seed)
    page = client.get(f"/ui/tasks/{task_id}?{query}").text
    assert '<div class="error">use a&amp;b=c #1 + 100% of café</div>' in page


# --------------------------------------------------------------------------- review form


def test_untouched_review_form_stays_a_plain_approval(client, platform, seed) -> None:
    task_id = make_task(platform, seed, title="Fix  gateway timeouts ")
    login(client, seed)
    resp = client.post(
        f"/ui/tasks/{task_id}/review",
        data=review_form(platform, task_id),
        follow_redirects=False,
    )
    assert resp.headers["location"] == f"/ui/tasks/{task_id}"  # no error
    with platform.db.session() as s:
        review = s.scalars(select(Review).where(Review.task_id == task_id)).one()
        task = s.get(Task, task_id)
        assert review.decision == "approve"
        assert not review.edits
        assert (task.title, task.proposed_approach) == ("Fix  gateway timeouts ", APPROACH)
        assert task.status == "approved"


def test_edited_review_form_is_recorded_as_edits_with_normalized_newlines(
    client, platform, seed
) -> None:
    task_id = make_task(platform, seed)
    login(client, seed)
    form = review_form(platform, task_id, edit_approach="1. add a timeout\r\n2. add a breaker\r\n")
    client.post(f"/ui/tasks/{task_id}/review", data=form)
    with platform.db.session() as s:
        review = s.scalars(select(Review).where(Review.task_id == task_id)).one()
        assert review.decision == "approve_with_edits"
        assert set(review.edits["after"]) == {"proposed_approach"}
        assert s.get(Task, task_id).proposed_approach == "1. add a timeout\n2. add a breaker"


# --------------------------------------------------------------------------- origin, logout


def test_origin_check_compares_the_host_so_a_tls_proxy_works(client, seed) -> None:
    # TLS terminated in front: the app is reached over http, the browser's origin is https.
    assert "secure" in login(client, seed, origin="https://testserver")
    assert "secure" not in login(client, seed, origin="http://testserver")
    assert "secure" not in login(client, seed)  # no Origin header at all


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://testserver.evil.example",
        "https://testserver:8443",  # same host, another port: another origin
        "null",
        "",
        "ftp://testserver",
    ],
)
def test_form_posts_from_another_origin_are_rejected(client, platform, seed, origin: str) -> None:
    task_id = make_task(platform, seed)
    assert (
        client.post(
            "/ui/login", data={"api_key": seed.keys["reviewer"]}, headers={"origin": origin}
        ).status_code
        == 403
    )
    login(client, seed)
    resp = client.post(
        f"/ui/tasks/{task_id}/review", data={"decision": "approve"}, headers={"origin": origin}
    )
    assert resp.status_code == 403
    assert client.post("/ui/logout", headers={"origin": origin}).status_code == 403
    with platform.db.session() as s:
        assert s.get(Task, task_id).status == "in_review"
    assert client.get("/ui", follow_redirects=False).status_code == 200  # still signed in


def test_logout_is_a_post_form(client, seed) -> None:
    login(client, seed)
    page = client.get("/ui").text
    assert '<form class="inline" method="post" action="/ui/logout">' in page
    assert 'href="/ui/logout"' not in page
    # A link, image or redirect on another site can only issue a GET: it no longer signs out.
    assert client.get("/ui/logout", follow_redirects=False).status_code == 405
    assert client.get("/ui", follow_redirects=False).status_code == 200
    resp = client.post(
        "/ui/logout", headers={"origin": "http://testserver"}, follow_redirects=False
    )
    assert (resp.status_code, resp.headers["location"]) == (303, "/ui/login")
    assert client.get("/ui", follow_redirects=False).headers["location"] == "/ui/login"


# --------------------------------------------------------------------------- demo


def test_demo_runs_twice_into_the_same_directory(tmp_path: Path) -> None:
    """Its job keys are scoped to the demo tenant; fixed keys made a second run reuse the first
    run's finished jobs and stop with no tasks."""
    first = run_demo(tmp_path / "demo", verbose=False)
    second = run_demo(tmp_path / "demo", verbose=False)
    assert (first.final_status, second.final_status) == ("verified", "verified")
    assert second.approved_task_id != first.approved_task_id
