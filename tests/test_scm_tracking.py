import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from opsmith import queue
from opsmith.audit_log import transition_task
from opsmith.db import utcnow
from opsmith.models import AnalysisBundle, Job, Repository, Task
from opsmith.scm import GitHubSCM, SCMError
from opsmith.scm.local import LocalSCM
from tests.test_executor import approve, make_task


def gh_repo(**kw) -> Repository:
    return Repository(
        id="repo_1",
        tenant_id="t",
        project_id="p",
        name="api",
        scm_provider="github",
        url="https://github.com/acme/api.git",
        credentials_ref="env:GH_TEST_TOKEN",
        default_branch="main",
        **kw,
    )


@pytest.fixture
def gh_token(monkeypatch) -> str:
    monkeypatch.setenv("GH_TEST_TOKEN", "ghs_testtoken")
    return "ghs_testtoken"


def test_github_create_pr_and_idempotent_existing(gh_token) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.params.get("head")))
        assert request.headers["authorization"] == f"Bearer {gh_token}"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["base"] == "main"
            assert body["head"] == "opsmith/fix/x"
            if len(calls) == 1:
                return httpx.Response(201, json={"number": 7, "html_url": "https://gh/pr/7"})
            return httpx.Response(422, json={"message": "A pull request already exists"})
        return httpx.Response(200, json=[{"number": 7, "html_url": "https://gh/pr/7"}])

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(handler)))
    pr = scm.create_pull_request(gh_repo(), branch="opsmith/fix/x", title="t", body="b")
    assert (pr.number, pr.url) == (7, "https://gh/pr/7")
    again = scm.create_pull_request(gh_repo(), branch="opsmith/fix/x", title="t", body="b")
    assert again.number == 7
    assert calls[-1] == ("GET", "/repos/acme/api/pulls", "acme:opsmith/fix/x")


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([{"status": "completed", "conclusion": "success"}], "success"),
        ([{"status": "in_progress", "conclusion": None}], "pending"),
        (
            [
                {"status": "completed", "conclusion": "failure"},
                {"status": "completed", "conclusion": "success"},
            ],
            "failure",
        ),
    ],
)
def test_github_pr_status_and_ci(gh_token, runs, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={
                    "state": "closed",
                    "merged": True,
                    "head": {"sha": "abc"},
                    "review_comments": 2,
                },
            )
        return httpx.Response(200, json={"check_runs": runs})

    status = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(handler))).get_pull_request(
        gh_repo(), 7
    )
    assert status.state == "merged"
    assert status.ci == expected
    assert status.review_comments == 2


def test_github_refuses_default_branch_and_missing_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GH_TEST_TOKEN", "x")
    with pytest.raises(SCMError, match="default branch"):
        GitHubSCM().push_branch(gh_repo(), tmp_path, "main")
    monkeypatch.delenv("GH_TEST_TOKEN")
    with pytest.raises(SCMError, match="no token"):
        GitHubSCM().push_branch(gh_repo(), tmp_path, "opsmith/x")


def test_scm_interface_has_no_merge_capability() -> None:
    assert not any("merge" in name for name in dir(GitHubSCM) if not name.startswith("_"))


def test_pr_tracking_merge_and_verification(platform, seed, drain) -> None:
    task_id_static = make_task(platform, seed, fp="fp-static-2")
    approve(platform, seed, task_id_static)
    drain()
    with platform.db.session() as s:
        t = s.get(Task, task_id_static)
        assert t.status == "pr_open"
        repo = s.get(Repository, seed.repo.id)
        number = t.pr_number
    scm = platform.scm.for_provider("local")
    assert isinstance(scm, LocalSCM)
    with platform.db.session() as s:
        assert platform.tracker.run(s, seed.project.id)["updated"] == {}
        assert s.get(Task, task_id_static).ci_status == "success"
    scm.human_merge(repo, number)
    with platform.db.session() as s:
        res = platform.tracker.run(s, seed.project.id)
        assert res["updated"] == {task_id_static: "merged"}
        assert s.scalar(select(Job).where(Job.kind == "verify"))
    # No post-merge data yet -> wait; then a later audit no longer reports it -> verified.
    with platform.db.session() as s, pytest.raises(queue.RetryLater):
        platform.verifier.run(s, task_id_static)
    with platform.db.session() as s:
        platform.audits.run(s, seed.repo.id, mode="full")
    with platform.db.session() as s:
        assert platform.verifier.run(s, task_id_static)["outcome"] == "verified"
        assert s.get(Task, task_id_static).status == "verified"


def test_verification_detects_regression(platform, seed) -> None:
    task_id = make_task(platform, seed, source="inference", rule=None, signal="log:sig")
    merged_at = utcnow() - timedelta(hours=2)
    with platform.db.session() as s:
        t = s.get(Task, task_id)
        for state in ("approved", "executing", "pr_open", "merged"):
            transition_task(s, t, state)
        t.merged_at = merged_at
        s.add(
            AnalysisBundle(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                service_id=seed.service.id,
                window_start=merged_at,
                window_end=utcnow(),
                log_templates=[
                    {
                        "ref": "bundle:b#tpl:sig",
                        "signal": "log:sig",
                        "severities": {"error": 9},
                        "last_seen": utcnow().isoformat(),
                    }
                ],
            )
        )
    with platform.db.session() as s:
        assert platform.verifier.run(s, task_id)["outcome"] == "regressed"


def test_closed_pr_closes_task(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    with platform.db.session() as s:
        t = s.get(Task, task_id)
        LocalSCM(platform.settings.data_dir / "local_prs").human_close(
            s.get(Repository, seed.repo.id), t.pr_number
        )
        platform.tracker.run(s, seed.project.id)
        assert t.status == "closed"
