import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from opsmith import queue
from opsmith.audit_log import transition_task
from opsmith.db import utcnow
from opsmith.models import AnalysisBundle, Job, Repository, Task
from opsmith.scm import GitHubSCM, SCMError
from opsmith.scm.local import LocalSCM
from opsmith.tracking import MAX_VERIFY_DEFERRALS
from tests.test_executor import EditingAgent, _use_agent, approve, make_task


def gh_repo(**kw) -> Repository:
    fields = {
        "id": "repo_1",
        "tenant_id": "t",
        "project_id": "p",
        "name": "api",
        "scm_provider": "github",
        "url": "https://github.com/acme/api.git",
        "credentials_ref": "env:GH_TEST_TOKEN",
        "default_branch": "main",
    }
    return Repository(**{**fields, **kw})


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


def test_a_task_merged_again_after_a_regression_is_verified_again(platform, seed, drain) -> None:
    task_id = make_task(platform, seed, fp="fp-static-2")
    scm = platform.scm.for_provider("local")
    assert isinstance(scm, LocalSCM)
    for merge in (1, 2):
        approve(platform, seed, task_id)
        drain()
        with platform.db.session() as s:
            t = s.get(Task, task_id)
            assert t.status == "pr_open", t.execution_report
            repo, number = s.get(Repository, seed.repo.id), t.pr_number
        scm.human_merge(repo, number)
        with platform.db.session() as s:
            assert platform.tracker.run(s, seed.project.id)["updated"] == {task_id: "merged"}
        if merge == 1:  # the verifier found a regression; the aggregator reopens the task
            with platform.db.session() as s:
                t = s.get(Task, task_id)
                transition_task(s, t, "regressed")
                transition_task(s, t, "in_review")
            _use_agent(platform, seed, EditingAgent("app/second_fix.py", "RETRIES = 3\n"))
    with platform.db.session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "verify").order_by(Job.created_at)).all()
        assert len({j.idempotency_key for j in jobs}) == 2  # one per merge
        assert jobs[-1].status == "queued"  # the second merge will be verified


def test_verification_without_post_merge_data_fails_loudly(platform, seed) -> None:
    """Waiting is fine; giving up without telling anyone is not (no silent drops)."""
    task_id = make_task(platform, seed, fp="fp-static-2")
    with platform.db.session() as s:
        t = s.get(Task, task_id)
        for state in ("approved", "executing", "pr_open", "merged"):
            transition_task(s, t, state)
        t.merged_at = utcnow()
    with platform.db.session() as s, pytest.raises(queue.RetryLater):
        platform.verifier.run(s, task_id, attempt=MAX_VERIFY_DEFERRALS - 1)
    with platform.db.session() as s, pytest.raises(queue.PermanentFailure, match="by hand"):
        platform.verifier.run(s, task_id, attempt=MAX_VERIFY_DEFERRALS)
    assert MAX_VERIFY_DEFERRALS >= 26  # a daily audit always falls inside the wait


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


@pytest.mark.parametrize(("late", "expected"), [("failure", "failure"), (None, "pending")])
def test_github_ci_reads_every_page_of_check_runs(gh_token, late, expected) -> None:
    """GitHub returns check runs 30 to a page. Only the first page was read, so a commit whose
    failing or unfinished run was listed later was reported as passing."""
    runs = [{"status": "completed", "conclusion": "success"} for _ in range(149)]
    runs.append({"status": "completed" if late else "in_progress", "conclusion": late})
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"state": "open", "head": {"sha": "abc"}})
        size = int(request.url.params.get("per_page", 30))
        page = int(request.url.params.get("page", 1))
        pages.append(page)
        chunk = runs[(page - 1) * size : page * size]
        return httpx.Response(200, json={"total_count": len(runs), "check_runs": chunk})

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert scm.get_pull_request(gh_repo(), 7).ci == expected
    assert pages == [1, 2]


# --------------------------------------------------------------------------- github provider


def test_github_checkout_clones_then_fetches_over_https_only(
    gh_token, tmp_path, monkeypatch
) -> None:
    """What the provider asks git to do, recorded instead of run (tests stay offline): the
    token goes in a transient header, options end before the address, and git may use https
    and nothing else, for the address and for anything it redirects to."""
    from opsmith.scm import github  # noqa: PLC0415

    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(github, "run_git", lambda args, *a, **kw: calls.append((args, kw)) or "")
    scm = GitHubSCM()
    dest = tmp_path / "checkout"
    assert scm.checkout(gh_repo(), dest) == dest
    clone, options = calls[0]
    assert clone == [
        "clone", "--depth", "200", "--branch", "main", "--",
        "https://github.com/acme/api.git", str(dest),
    ]  # fmt: skip
    assert (options["token"], options["protocols"]) == (gh_token, ("https",))
    (dest / ".git").mkdir(parents=True)
    calls.clear()
    scm.checkout(gh_repo(), dest)
    assert [args[0] for args, _ in calls] == ["fetch", "reset", "clean"]
    fetch, options = calls[0]
    assert fetch[-3:] == ["--", "https://github.com/acme/api.git", "refs/heads/main"]
    assert options["protocols"] == ("https",)


def test_github_push_goes_only_to_the_opsmith_branch(gh_token, tmp_path, monkeypatch) -> None:
    from opsmith.scm import github  # noqa: PLC0415

    calls: list[list[str]] = []
    monkeypatch.setattr(github, "run_git", lambda args, *a, **kw: calls.append(args) or "")
    GitHubSCM().push_branch(gh_repo(), tmp_path, "opsmith/fix/x")
    assert calls == [
        [
            "push",
            "--force",
            "--",
            "https://github.com/acme/api.git",
            "HEAD:refs/heads/opsmith/fix/x",
        ]
    ]


def test_github_token_problems_are_reported(monkeypatch) -> None:
    monkeypatch.delenv("GH_TEST_TOKEN", raising=False)
    with pytest.raises(SCMError, match="no token resolvable"):
        GitHubSCM()._token(gh_repo())
    with pytest.raises(SCMError, match="reserved for the platform"):
        GitHubSCM()._token(gh_repo(credentials_ref="env:OPSMITH_DATABASE_URL"))


def test_github_owner_and_name_come_from_the_url_when_not_given(gh_token) -> None:
    assert GitHubSCM._full_name(gh_repo()) == "acme/api"
    assert GitHubSCM._full_name(gh_repo(full_name="other/name")) == "other/name"
    with pytest.raises(SCMError, match="cannot derive owner/repo"):
        GitHubSCM._full_name(gh_repo(url="https://github.example.com/acme/team/api.git"))


def test_github_api_failures_surface_as_scm_errors(gh_token) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(SCMError, match="GitHub API GET"):
        scm.get_pull_request(gh_repo(), 7)

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed: head is invalid"})
        return httpx.Response(404, json={"message": "Not Found"})

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(SCMError, match=r"create PR failed \(422\)"):
        scm.create_pull_request(gh_repo(), branch="opsmith/fix/x", title="t", body="b")
    with pytest.raises(SCMError, match=r"get PR failed \(404\)"):
        scm.get_pull_request(gh_repo(), 7)


def test_an_existing_pull_request_that_cannot_be_found_is_an_error(gh_token) -> None:
    """GitHub says the pull request already exists, then does not list it (it was closed in
    between): that is a failed creation, not a success with no pull request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(422, json={"message": "A pull request already exists"})
        return httpx.Response(200, json=[])

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SCMError, match="create PR failed"):
        scm.create_pull_request(gh_repo(), branch="opsmith/fix/x", title="t", body="b")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"total_count": 1, "state": "success"}, "success"),
        ({"total_count": 2, "state": "failure"}, "failure"),
        ({"total_count": 1, "state": "error"}, "failure"),
        ({"total_count": 1, "state": "pending"}, "pending"),
        ({"total_count": 0, "state": "pending"}, "none"),
    ],
)
def test_github_ci_falls_back_to_commit_statuses(gh_token, status, expected) -> None:
    """A repository whose CI reports commit statuses (not check runs)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"state": "open", "head": {"sha": "abc"}})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        return httpx.Response(200, json=status)

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert scm.get_pull_request(gh_repo(), 7).ci == expected


def test_a_pull_request_without_a_head_commit_has_no_ci(gh_token) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"state": "closed", "merged": False})

    scm = GitHubSCM(http=httpx.Client(transport=httpx.MockTransport(handler)))
    status = scm.get_pull_request(gh_repo(), 7)
    assert (status.state, status.ci, status.head_sha) == ("closed", "none", None)
    assert calls == ["/repos/acme/api/pulls/7"]
