import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from opsmith.audits import analyzers_for, registered
from opsmith.audits.base import AuditContext, iter_repo_files
from opsmith.audits.builtin import (
    ComplianceAnalyzer,
    CoverageGapAnalyzer,
    DependencyAnalyzer,
    PythonAstAnalyzer,
    SecretsAnalyzer,
)
from opsmith.audits.external import (
    parse_gitleaks,
    parse_osv_scanner,
    parse_ruff,
    parse_semgrep,
    parse_trivy,
)
from opsmith.gitutil import run_git
from opsmith.models import AuditRun, Finding, Job, Repository
from opsmith.schemas import PolicyRule, ProjectSettings
from tests.conftest import FAKE_AWS_KEY

BUGGY = """import subprocess, yaml, requests

def f(items=[]):
    try:
        x = eval(items[0])
    except:
        pass
    subprocess.run("ls " + x, shell=True)
    yaml.load(open("a"))
    requests.get("http://x")
    cursor.execute(f"SELECT * FROM t WHERE id = {x}")
    for i in items:
        db.query(i)
    try:
        g()
    except Exception:
        pass
"""


def ctx_for(root: Path, settings: ProjectSettings | None = None, files=None) -> AuditContext:
    all_files = iter_repo_files(root)
    return AuditContext(
        repo_path=root,
        repository_id="repo",
        files=files or all_files,
        all_files=all_files,
        settings=settings or ProjectSettings(),
    )


def test_python_ast_analyzer_rules(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(BUGGY)
    (tmp_path / "broken.py").write_text("def (:\n")
    rules = {f.rule_id for f in PythonAstAnalyzer().analyze(ctx_for(tmp_path))}
    assert {
        "py.mutable-default",
        "py.eval",
        "py.bare-except",
        "py.subprocess-shell",
        "py.yaml-load",
        "py.http-no-timeout",
        "py.sql-injection",
        "py.n-plus-one",
        "py.swallowed-exception",
        "py.syntax-error",
    } <= rules


def test_secrets_analyzer_masks_value(tmp_path: Path) -> None:
    (tmp_path / "cfg.py").write_text('TOKEN = "ghp_' + "b" * 36 + '"\nok = 1\n')
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(FAKE_AWS_KEY)
    findings = SecretsAnalyzer().analyze(ctx_for(tmp_path))
    assert len(findings) == 1
    assert findings[0].line == 1
    assert "b" * 36 not in findings[0].snippet


def test_dependency_analyzer_and_osv(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask\nrequests==2.19.0  # old\n-e .\n")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"left-pad": "*"}}))

    def handler(request: httpx.Request) -> httpx.Response:
        queries = json.loads(request.content)["queries"]
        assert queries == [
            {"package": {"name": "requests", "ecosystem": "PyPI"}, "version": "2.19.0"}
        ]
        return httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-x84v"}]}]})

    ctx = ctx_for(tmp_path, ProjectSettings(osv_online=True))
    ctx.http = httpx.Client(transport=httpx.MockTransport(handler))
    findings = DependencyAnalyzer().analyze(ctx)
    rules = sorted(f.rule_id for f in findings)
    assert rules == ["dep.unpinned", "dep.unpinned", "dep.vuln.GHSA-x84v"]


def test_compliance_license_and_policy_rules(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"license": "AGPL-3.0"}))
    (tmp_path / "svc.py").write_text("# SPDX-License-Identifier: GPL-3.0\nprint('debug')\n")
    settings = ProjectSettings(
        policy_rules=[
            PolicyRule(
                id="no-print",
                description="use logging",
                path_glob="*.py",
                forbid_regex=r"\bprint\(",
            ),
            PolicyRule(
                id="header", description="needs header", path_glob="*.py", require_regex="Copyright"
            ),
        ]
    )
    rules = [f.rule_id for f in ComplianceAnalyzer().analyze(ctx_for(tmp_path, settings))]
    assert "license.missing" in rules
    assert rules.count("license.not-allowed") == 2
    assert "policy.no-print" in rules
    assert "policy.header" in rules


def test_coverage_gaps(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "tested.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "lonely.py").write_text("y = 2\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("from pkg import tested\n")
    found = [f.snippet for f in CoverageGapAnalyzer().analyze(ctx_for(tmp_path))]
    assert found == ["lonely"]


def test_external_tool_parsers() -> None:
    semgrep = json.dumps(
        {
            "results": [
                {
                    "check_id": "python.sqli",
                    "path": "a.py",
                    "start": {"line": 3},
                    "extra": {
                        "message": "SQLi",
                        "severity": "ERROR",
                        "metadata": {"category": "security"},
                    },
                }
            ]
        }
    )
    assert parse_semgrep(semgrep)[0].category == "security"
    gl = json.dumps([{"RuleID": "aws", "File": "k.txt", "StartLine": 2, "Fingerprint": "f"}])
    assert parse_gitleaks(gl)[0].rule_id == "secret.gitleaks.aws"
    trivy = json.dumps(
        {
            "Results": [
                {
                    "Target": "req.txt",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-1",
                            "PkgName": "x",
                            "InstalledVersion": "1",
                            "Severity": "HIGH",
                            "FixedVersion": "2",
                        }
                    ],
                    "Misconfigurations": [{"ID": "DS001", "Title": "root", "Severity": "MEDIUM"}],
                }
            ]
        }
    )
    parsed = parse_trivy(trivy)
    assert [f.rule_id for f in parsed] == ["dep.vuln.CVE-1", "iac.DS001"]
    osv = json.dumps(
        {
            "results": [
                {
                    "source": {"path": "go.mod"},
                    "packages": [
                        {
                            "package": {"name": "p", "version": "1"},
                            "vulnerabilities": [{"id": "GO-1"}],
                        }
                    ],
                }
            ]
        }
    )
    assert parse_osv_scanner(osv)[0].path == "go.mod"
    ruff = json.dumps(
        [{"code": "F401", "message": "unused", "filename": "/r/a.py", "location": {"row": 1}}]
    )
    assert parse_ruff(ruff)[0].severity == "low"
    assert parse_semgrep("not json") == []


def test_unavailable_external_tools_are_recorded(platform, seed, drain) -> None:
    with platform.db.session() as s:
        run = platform.audits.run(s, seed.repo.id, mode="full")
        assert run.tool_versions["gitleaks"] == "unavailable"
        assert run.tool_versions["secrets"] == "builtin-1"
        assert run.status == "succeeded"
        assert {"python-ast", "secrets", "semgrep"} <= set(registered())


def test_audit_run_full_then_incremental_with_triage(platform, seed) -> None:
    with platform.db.session() as s:
        run = platform.audits.run(s, seed.repo.id, mode="auto")
        assert run.mode == "full"
        rules = {
            f.rule_id for f in s.scalars(select(Finding).where(Finding.source_run_id == run.id))
        }
        assert {"secret.stripe_key", "py.http-no-timeout", "py.bare-except"} <= rules
        assert s.scalar(select(Job).where(Job.kind == "aggregate"))
        first_sha = run.commit_sha

    (seed.repo_path / "tests" / "fixtures").mkdir()
    (seed.repo_path / "tests" / "fixtures" / "creds.py").write_text(
        'password = "example-placeholder-pass"\n'
    )
    (seed.repo_path / "app" / "new.py").write_text("def g(a={}):\n    return a\n")
    run_git(["add", "-A"], seed.repo_path)
    run_git(["commit", "-m", "more"], seed.repo_path, author=("d", "d@example.com"))
    with platform.db.session() as s:
        run = platform.audits.run(s, seed.repo.id, mode="auto")
        assert run.mode == "incremental"
        assert run.base_sha == first_sha
        assert run.files_analyzed == 2
        findings = s.scalars(select(Finding).where(Finding.source_run_id == run.id)).all()
        by_rule = {f.rule_id: f for f in findings}
        assert by_rule["py.mutable-default"].status == "new"
        fixture = by_rule["secret.credential_assignment"]
        assert fixture.status == "suppressed"  # triage: placeholder in test fixture
        assert fixture.status_reason.startswith("triage:")
        assert s.get(Repository, seed.repo.id).last_audited_sha == run.commit_sha


def test_one_broken_analyzer_does_not_sink_the_run(platform, seed, monkeypatch) -> None:
    def boom(self, ctx):
        raise RuntimeError("tool crashed")

    monkeypatch.setattr(SecretsAnalyzer, "analyze", boom)
    with platform.db.session() as s:
        run = platform.audits.run(s, seed.repo.id, mode="full")
        assert run.tool_versions["secrets"].startswith("error: tool crashed")
        assert run.findings_count > 0
        assert s.get(AuditRun, run.id).status == "succeeded"


@pytest.mark.parametrize("types", [["security"], ["coverage"]])
def test_analyzer_selection(types: list[str]) -> None:
    names = {a.name for a in analyzers_for(types)}
    if types == ["coverage"]:
        assert names == {"coverage-gaps"}
    else:
        assert {"secrets", "python-ast", "dependencies"} <= names
