import json
import os
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from opsmith.audits import analyzers_for, builtin, external, registered
from opsmith.audits.base import (
    MAX_LINE_CHARS,
    Analyzer,
    AnalyzerError,
    AuditContext,
    RawFinding,
    iter_repo_files,
)
from opsmith.audits.builtin import (
    ComplianceAnalyzer,
    CoverageGapAnalyzer,
    DependencyAnalyzer,
    PythonAstAnalyzer,
    SecretsAnalyzer,
)
from opsmith.audits.external import (
    GitleaksTool,
    OsvScannerTool,
    RuffTool,
    SemgrepTool,
    TrivyTool,
    parse_gitleaks,
    parse_osv_scanner,
    parse_ruff,
    parse_semgrep,
    parse_trivy,
)
from opsmith.gitutil import run_git
from opsmith.models import AuditLog, AuditRun, Finding, Job, Repository
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
    # Output that is not the expected JSON is a failed scan, never "no findings".
    for parser, bad in ((parse_semgrep, "not json"), (parse_semgrep, "[]"), (parse_ruff, "{}")):
        with pytest.raises(ValueError, match="scanner output"):
            parser(bad)


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


# --------------------------------------------------------------------------- hardening
# Repositories are untrusted input to the analyzers and to the external scanners.

FAKE_STRIPE_KEY = "sk_" + "live_" + "9" * 24
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def only_fake_scanners(tmp_path: Path, monkeypatch) -> None:
    """Runner tests must not depend on which scanners the host happens to have installed: an
    external tool counts as available only when a test put a fake one on PATH (``fake_tool``)."""
    fakebin = tmp_path / "fakebin"

    def available(self: external.ExternalTool) -> bool:
        found = shutil.which(self.binary)
        return found is not None and Path(found).parent == fakebin

    monkeypatch.setattr(external.ExternalTool, "available", available)


def fake_tool(tmp_path: Path, monkeypatch, name: str, body: str) -> None:
    """Put an executable shell script called ``name`` first on PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def run_audit(platform, repository_id: str, mode: str = "auto"):
    with platform.db.session() as s:
        run = platform.audits.run(s, repository_id, mode=mode)
        findings = s.scalars(select(Finding).where(Finding.source_run_id == run.id)).all()
        return run, list(findings), s.get(Repository, repository_id).last_audited_sha


def test_file_names_are_never_read_as_scanner_options(tmp_path: Path, monkeypatch) -> None:
    # A directory called "--output-file=" makes a repo-relative path that is a valid ruff option.
    repo = tmp_path / "repo"
    (repo / "--output-file=" / "x").mkdir(parents=True)
    (repo / "--output-file=" / "x" / "owned.py").write_text("import os\n")
    (repo / "a.py").write_text("x = 1\n")
    calls = tmp_path / "calls"
    calls.mkdir()
    fake_tool(
        tmp_path,
        monkeypatch,
        "ruff",
        f'n=$(ls {calls} | wc -l)\nprintf "%s\\n" "$@" > {calls}/$n.argv\n'
        f"env > {calls}/$n.env\necho '[]'\n",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "worker-only-credential")
    ctx = ctx_for(repo)

    assert RuffTool().analyze(ctx) == []
    argv = (calls / "0.argv").read_text().splitlines()
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["--output-file=/x/owned.py", "a.py"]
    assert "--isolated" in argv[:sep]  # the checkout's own ruff config is not honoured
    assert "worker-only-credential" not in (calls / "0.env").read_text()  # scrubbed environment

    for cmd in SemgrepTool().commands(ctx):
        assert cmd[cmd.index("--") + 1 :] == ["--output-file=/x/owned.py", "a.py"]


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is not installed")
def test_real_ruff_accepts_the_hardened_command_line(tmp_path: Path) -> None:
    """The one check against a real scanner (skipped without it): the flags added for safety
    are ones ruff actually accepts, and they do what the fake-based tests assume."""
    repo = tmp_path / "repo"
    outside = tmp_path / "outside" / "owned.py"
    planted = repo / f"--output-file={outside}"
    planted.parent.mkdir(parents=True)
    planted.write_text("import os\n")
    (repo / "m.py").write_text("import os\ndef f(a=[]):\n    return undefined_name\n")
    # The repository tries to switch the rules off for itself.
    (repo / "ruff.toml").write_text('[lint.per-file-ignores]\n"*" = ["F", "B", "E9"]\n')
    findings = RuffTool().analyze(ctx_for(repo))
    assert sorted({f.rule_id for f in findings if f.path == "m.py"}) == [
        "ruff.B006",
        "ruff.F401",
        "ruff.F821",
    ]
    assert any(f.path.startswith("--output-file=") for f in findings)
    assert not outside.exists()  # the planted name was scanned as a file, not obeyed as an option


def test_file_lists_are_chunked_below_the_argv_limit(tmp_path: Path) -> None:
    files = [f"pkg_{'x' * 60}/module_{i:05d}.py" for i in range(32_000)]  # ~2.6 MB of names
    ctx = AuditContext(tmp_path, "repo", files, files, ProjectSettings())
    for tool in (RuffTool(), SemgrepTool()):
        cmds = tool.commands(ctx)
        assert len(cmds) > 1
        assert all(sum(len(a) + 1 for a in cmd) < 128_000 for cmd in cmds)
        assert [a for cmd in cmds for a in cmd[cmd.index("--") + 1 :]] == files
    assert (
        RuffTool().commands(AuditContext(tmp_path, "r", ["a.txt"], ["a.txt"], ProjectSettings()))
        == []
    )


def test_secrets_every_hit_is_masked_and_reported(tmp_path: Path) -> None:
    second_aws = FAKE_AWS_KEY[:4] + "Z" * 16
    (tmp_path / "cfg.py").write_text(
        f'KEYS = ("{FAKE_AWS_KEY}", "{FAKE_STRIPE_KEY}")\n'
        f'A = "{FAKE_AWS_KEY}"; B = "{second_aws}"; C = "{FAKE_AWS_KEY}"\n'
        'db = "postgres://u:hunter2secret@h/db"; password = "TopSecretPassw0rd"\n'
        f'api_key = "{FAKE_STRIPE_KEY}"\n'
    )
    findings = SecretsAnalyzer().analyze(ctx_for(tmp_path))
    by_line = {n: [f for f in findings if f.line == n] for n in (1, 2, 3, 4)}
    assert [f.rule_id for f in by_line[1]] == ["secret.aws_access_key", "secret.stripe_key"]
    # Two different keys of one kind are two findings; the repeated one is not a third.
    assert [f.rule_id for f in by_line[2]] == ["secret.aws_access_key"] * 2
    assert by_line[2][0] != by_line[2][1]
    assert [f.rule_id for f in by_line[3]] == [
        "secret.url_credentials",
        "secret.credential_assignment",
    ]
    # A specific detector's value is not reported again by the generic assignment rule.
    assert [f.rule_id for f in by_line[4]] == ["secret.stripe_key"]
    stored = " ".join(f.snippet + json.dumps(f.extra) for f in findings)
    for raw in (FAKE_AWS_KEY, second_aws, FAKE_STRIPE_KEY, "hunter2secret", "TopSecretPassw0rd"):
        assert raw not in stored
    assert by_line[1][0].snippet == 'KEYS = ("AKI***", "sk_***")'


def test_secrets_short_values_show_no_prefix(tmp_path: Path) -> None:
    (tmp_path / "a.env").write_text("DATABASE_URL=postgres://app:pw1@db/prod\n")
    (finding,) = SecretsAnalyzer().analyze(ctx_for(tmp_path))
    assert "pw1" not in finding.snippet
    assert finding.snippet == "DATABASE_URL=postgres://app:***@db/prod"


def test_secrets_detects_pem_private_keys(tmp_path: Path) -> None:
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"
    (tmp_path / "id_rsa").write_text(f"{PEM_BEGIN}\n{body}\n{body}\n{PEM_END}\n")
    (tmp_path / "sa.json").write_text(
        '{"private_key": "' + PEM_BEGIN + "\\n" + body + "\\n" + PEM_END + '\\n", "x": 1}\n'
    )
    (tmp_path / "wrapped.txt").write_text(f"key={PEM_BEGIN}{body}\n")  # no END on the line
    findings = SecretsAnalyzer().analyze(ctx_for(tmp_path))
    assert [(f.path, f.rule_id, f.line) for f in findings] == [
        ("id_rsa", "secret.private_key", 1),
        ("sa.json", "secret.private_key", 1),
        ("wrapped.txt", "secret.private_key", 1),
    ]
    assert all(body not in f.snippet for f in findings)  # a one-line key is masked whole


def test_regexes_only_see_a_bounded_part_of_a_line(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "LICENSE").write_text("MIT")
    # 900 KB on one line: detectors that backtrack quadratically would run for minutes on it.
    (tmp_path / "bundle.js").write_text(f"k='{FAKE_AWS_KEY}';" + "a." * 450_000 + "print(1)")
    seen: list[int] = []
    real_scan = builtin.scan_line

    def recording_scan(line: str):
        seen.append(len(line))
        return real_scan(line)

    monkeypatch.setattr(builtin, "scan_line", recording_scan)
    secrets = SecretsAnalyzer().analyze(ctx_for(tmp_path))
    assert max(seen) == MAX_LINE_CHARS
    assert [f.rule_id for f in secrets] == ["secret.aws_access_key"]  # the start is still scanned

    rule = PolicyRule(id="no-print", description="d", path_glob="*.js", forbid_regex=r"print\(")
    (tmp_path / "ok.js").write_text("print(1)\n")
    policy = ComplianceAnalyzer().analyze(ctx_for(tmp_path, ProjectSettings(policy_rules=[rule])))
    assert [f.path for f in policy if f.rule_id == "policy.no-print"] == ["ok.js"]


def test_default_policy_glob_covers_root_level_files(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("MIT")
    (tmp_path / "root.py").write_text("print('x')\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "nested.py").write_text("print('y')\n")
    rule = PolicyRule(id="no-print", description="use logging", forbid_regex=r"\bprint\(")
    assert rule.path_glob == "**/*"
    findings = ComplianceAnalyzer().analyze(ctx_for(tmp_path, ProjectSettings(policy_rules=[rule])))
    assert sorted(f.path for f in findings) == ["pkg/nested.py", "root.py"]


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("package.json", "[]"),
        ("package.json", json.dumps({"dependencies": ["a"], "devDependencies": 3})),
        ("package.json", "[" * 100_000),
        ("pyproject.toml", 'project = "x"\n'),
        ("pyproject.toml", "[project]\ndependencies = [1, {a = 1}]\n"),
    ],
)
def test_malformed_manifest_does_not_sink_dependency_analysis(
    tmp_path: Path, name: str, content: str
) -> None:
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / name).write_text(content)
    findings = DependencyAnalyzer().analyze(ctx_for(tmp_path))
    assert [(f.rule_id, f.snippet) for f in findings] == [("dep.unpinned", "flask")]


@pytest.mark.parametrize("license_value", [{"type": "GPL-3.0", "url": "x"}, ["GPL-3.0"], 7])
def test_non_string_package_license_does_not_sink_compliance(tmp_path: Path, license_value) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"license": license_value}))
    (tmp_path / "svc.py").write_text("print('debug')\n")
    rule = PolicyRule(id="no-print", description="d", path_glob="*.py", forbid_regex=r"print\(")
    rules = [
        f.rule_id
        for f in ComplianceAnalyzer().analyze(
            ctx_for(tmp_path, ProjectSettings(policy_rules=[rule]))
        )
    ]
    assert "policy.no-print" in rules  # the rest of the analyzer still ran
    # The legacy object form names a license and is checked; other shapes carry none.
    assert rules.count("license.not-allowed") == (1 if isinstance(license_value, dict) else 0)


def test_python_ast_survives_pathological_files(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("def f(a=[]):\n    pass\n")
    (tmp_path / "deep_binop.py").write_text("x = 1" + "+1" * 100_000 + "\n")
    (tmp_path / "deep_unary.py").write_text("x = " + "-" * 200_000 + "1\n")
    # Too deep for ast.unparse, yet still recognised as SQL built by concatenation.
    terms = "+".join(["a"] * 3000)
    (tmp_path / "deep_sql.py").write_text(f"c.execute('select 1 where a = ' + {terms})\n")
    by_path = {f.path: f.rule_id for f in PythonAstAnalyzer().analyze(ctx_for(tmp_path))}
    assert by_path == {
        "ok.py": "py.mutable-default",
        "deep_binop.py": "py.unanalyzable",
        "deep_unary.py": "py.unanalyzable",
        "deep_sql.py": "py.sql-injection",
    }


def test_failed_analyzer_does_not_advance_the_audited_commit(platform, seed, monkeypatch) -> None:
    _, _, first_sha = run_audit(platform, seed.repo.id)
    assert first_sha
    (seed.repo_path / "app" / "new.py").write_text("def g(a={}):\n    return a\n")
    run_git(["add", "-A"], seed.repo_path)
    run_git(["commit", "-m", "more"], seed.repo_path, author=("d", "d@example.com"))

    with monkeypatch.context() as broken:
        broken.setattr(
            PythonAstAnalyzer, "analyze", lambda self, ctx: (_ for _ in ()).throw(OSError("boom"))
        )
        run, findings, sha = run_audit(platform, seed.repo.id)
    assert run.mode == "incremental"
    assert run.status == "succeeded"
    assert run.error == "analyzers failed: python-ast"  # visible on the finished run
    assert run.tool_versions["python-ast"] == "error: boom"
    assert "py.mutable-default" not in {f.rule_id for f in findings}
    assert sha == first_sha  # not advanced: the changed file was not analyzed by python-ast

    run, findings, sha = run_audit(platform, seed.repo.id)
    assert (run.mode, run.base_sha, run.error) == ("incremental", first_sha, None)
    assert "py.mutable-default" in {f.rule_id for f in findings}  # picked up, not skipped
    assert sha == run.commit_sha != first_sha


SCANNER_CASES = [
    # tool, script body, expected: number of findings or None for "must be reported as failed"
    (TrivyTool, "echo 'FATAL failed to download vulnerability DB' >&2; exit 1", None),
    (TrivyTool, "echo '{\"Results\": []}'", 0),
    (GitleaksTool, 'echo \'[{"RuleID": "aws", "File": "k.txt", "StartLine": 2}]\'; exit 1', 1),
    (GitleaksTool, "echo 'config error' >&2; exit 1", None),  # status 1 without a report
    (GitleaksTool, "exit 126", None),
    (SemgrepTool, "echo 'Traceback (most recent call last):'; exit 0", None),  # not JSON
    (SemgrepTool, "echo '{\"results\": []}'; exit 2", None),
    (OsvScannerTool, "echo 'No package sources found' >&2; exit 128", 0),
    (OsvScannerTool, "exit 127", None),
    (RuffTool, "echo 'error: something broke' >&2; exit 2", None),
]


@pytest.mark.parametrize(("tool", "script", "expected"), SCANNER_CASES)
def test_scanner_failure_is_not_a_clean_result(
    tmp_path: Path, monkeypatch, tool: type[external.ExternalTool], script: str, expected
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "k.txt").write_text("x\n")
    (repo / "a.py").write_text("x = 1\n")
    fake_tool(tmp_path, monkeypatch, tool.binary, script + "\n")
    if expected is None:
        with pytest.raises(AnalyzerError):
            tool().analyze(ctx_for(repo))
    else:
        assert len(tool().analyze(ctx_for(repo))) == expected


def test_scanner_failure_is_recorded_on_the_run(
    platform, seed, tmp_path: Path, monkeypatch
) -> None:
    fake_tool(
        tmp_path,
        monkeypatch,
        "trivy",
        'if [ "$1" = "--version" ]; then echo "Version: 0.50.0"; exit 0; fi\n'
        "echo 'FATAL failed to download vulnerability DB' >&2\nexit 1\n",
    )
    run, findings, sha = run_audit(platform, seed.repo.id, mode="full")
    assert run.tool_versions["trivy"].startswith("error: trivy exited with status 1")
    assert "trivy" in run.error
    assert findings  # the other analyzers' results are kept
    assert sha is None


@pytest.mark.parametrize("failure", ["http-500", "network", "not-json", "wrong-shape"])
def test_osv_lookup_failure_is_surfaced_and_keeps_offline_findings(
    tmp_path: Path, failure: str
) -> None:
    (tmp_path / "requirements.txt").write_text("flask\nrequests==2.19.0\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError("down")
        if failure == "http-500":
            return httpx.Response(500)
        if failure == "not-json":
            return httpx.Response(200, text="<html>proxy login</html>")
        return httpx.Response(200, json={"results": []})  # one query, zero results

    ctx = ctx_for(tmp_path, ProjectSettings(osv_online=True))
    ctx.http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AnalyzerError, match="OSV lookup failed") as err:
        DependencyAnalyzer().analyze(ctx)
    assert [f.rule_id for f in err.value.findings] == ["dep.unpinned"]


def test_partial_findings_of_a_failed_analyzer_are_kept(platform, seed) -> None:
    class Partial(Analyzer):
        name = "partial"
        audit_types = ("security",)

        def analyze(self, ctx: AuditContext) -> list[RawFinding]:
            found = RawFinding(
                self.name, "x.rule", "security", "t", "d", "low", 0.9, "a.py", 1, "s"
            )
            raise AnalyzerError("lookup failed", [found])

    platform.audits.analyzer_factory = lambda types: [Partial()]
    run, findings, sha = run_audit(platform, seed.repo.id, mode="full")
    assert [f.rule_id for f in findings] == ["x.rule"]
    assert run.tool_versions == {"partial": "error: lookup failed"}
    assert sha is None


def test_colliding_fingerprints_keep_every_distinct_finding(platform, seed) -> None:
    def raw(line: int, tool: str = "fake", **extra) -> RawFinding:
        return RawFinding(
            tool, "x.rule", "fix", f"{tool} title", "d", "low", 0.9, "a.py", line, "s", extra=extra
        )

    class Fake(Analyzer):
        name = "fake"
        audit_types = ("bugs",)

        def analyze(self, ctx: AuditContext) -> list[RawFinding]:
            # Line 9 is reported twice, the second time by another tool: still one finding.
            # Line 20 holds two hits that only their column tells apart (two secrets).
            return [raw(3), raw(9), raw(9, tool="other"), raw(20, column=5), raw(20, column=40)]

    platform.audits.analyzer_factory = lambda types: [Fake()]
    run, findings, _ = run_audit(platform, seed.repo.id, mode="full")
    assert sorted(f.line for f in findings) == [3, 9, 20, 20]
    fps = {(f.line, f.extra.get("column")): f.fingerprint for f in findings}
    assert len(set(fps.values())) == 4
    # The first keeps the fingerprint it always had, so existing dedup and suppression hold.
    assert fps[(3, None)] == raw(3).fingerprint(seed.repo.id)
    assert run.findings_count == 4
    # Stable: the same report yields the same fingerprints on the next run.
    _, again, _ = run_audit(platform, seed.repo.id, mode="full")
    assert {(f.line, f.extra.get("column")): f.fingerprint for f in again} == fps
    with platform.db.session() as s:
        details = s.scalars(
            select(AuditLog).where(AuditLog.action == "audit.completed").order_by(AuditLog.id)
        ).first()
        assert (details.details["raw"], details.details["kept"]) == (5, 4)


def test_repeated_rule_in_one_file_is_reported_per_occurrence(tmp_path: Path) -> None:
    src = "try:\n    f()\nexcept:\n    pass\ntry:\n    g()\nexcept:\n    pass\n"
    (tmp_path / "a.py").write_text(src)
    found = [
        f for f in PythonAstAnalyzer().analyze(ctx_for(tmp_path)) if f.rule_id == "py.bare-except"
    ]
    assert [f.line for f in found] == [3, 7]
    assert found[0].fingerprint("r") == found[1].fingerprint("r")  # hence the runner's numbering
    assert len({found[0].fingerprint("r"), found[1].fingerprint("r", occurrence=1)}) == 2


def test_gitleaks_fingerprint_does_not_depend_on_line_numbers() -> None:
    def report(line: int) -> str:
        row = {"RuleID": "aws", "File": "k.txt", "StartLine": line, "Secret": FAKE_AWS_KEY}
        return json.dumps([{**row, "Fingerprint": f"k.txt:aws:{line}"}])

    before, after = parse_gitleaks(report(2))[0], parse_gitleaks(report(40))[0]
    assert before.fingerprint("r") == after.fingerprint("r")
    assert FAKE_AWS_KEY not in before.snippet


def test_over_long_values_are_clipped_to_the_finding_columns(platform, seed) -> None:
    long_path = "/".join(["d" * 200] * 4) + "/secrets.py"  # 814 characters

    class Long(Analyzer):
        name = "long"
        audit_types = ("security",)

        def analyze(self, ctx: AuditContext) -> list[RawFinding]:
            title = f"Hardcoded credential in {long_path}"
            return [
                RawFinding(
                    self.name, "r." + "x" * 200, "security", title, "d", "low", 0.9, long_path
                )
            ]

    platform.audits.analyzer_factory = lambda types: [Long()]
    _, (finding,), _ = run_audit(platform, seed.repo.id, mode="full")
    assert (len(finding.title), len(finding.path), len(finding.rule_id)) == (300, 500, 120)


def test_analyzers_run_before_the_audit_writes_anything(platform, seed, monkeypatch) -> None:
    """The checkout and analyzers can take minutes; no write transaction may be open meanwhile
    (on SQLite it would block every other writer, including the queue's lease heartbeat)."""
    if platform.db.engine.dialect.name != "sqlite":
        pytest.skip("checks SQLite's single-writer lock")
    database = platform.db.engine.url.database
    real_analyze = SecretsAnalyzer.analyze
    outcome: list[str] = []

    def analyze(self, ctx):
        other = sqlite3.connect(database, timeout=0.2)
        try:
            other.execute("UPDATE projects SET name = name")
            other.commit()
            outcome.append("written")
        except sqlite3.OperationalError as exc:
            outcome.append(str(exc))
        finally:
            other.close()
        return real_analyze(self, ctx)

    monkeypatch.setattr(SecretsAnalyzer, "analyze", analyze)
    run, _, _ = run_audit(platform, seed.repo.id, mode="full")
    assert outcome == ["written"]
    assert run.started_at <= run.finished_at
