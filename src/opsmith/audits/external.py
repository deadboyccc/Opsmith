"""Wrappers for mature external scanners. Each is used only when its binary is on PATH;
unavailable tools are recorded as such in ``AuditRun.tool_versions`` (never silently).

Parsers are pure functions over the tools' JSON output so they are unit-testable without
the binaries installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from opsmith.audits.base import Analyzer, AnalyzerError, AuditContext, RawFinding, register

# Scanners read configuration from the directory they run in or scan. The audited repository is
# untrusted, so a scanner is given the platform's own configuration, or run from an empty
# directory, whenever its configuration could change what is reported or where.
GITLEAKS_CONFIG = Path(__file__).resolve().parent / "gitleaks.toml"

_SEV = {
    "ERROR": "high",
    "WARNING": "medium",
    "INFO": "low",
    "CRITICAL": "critical",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
    "UNKNOWN": "low",
}

# File names passed to one invocation, in bytes. Far below the kernel's argument limit, so a
# repository with tens of thousands of files is scanned in several runs instead of failing.
_ARGV_BUDGET = 100_000
# What a scanner needs to reach its rule/vulnerability database through a proxy. Nothing else
# of the worker's environment (API keys, SCM tokens, database URL) is handed to a process
# that reads an untrusted repository.
_ENV_PASSTHROUGH = (
    "TMPDIR",
    "XDG_CACHE_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)


def tool_env() -> dict[str, str]:
    """Scrubbed environment for scanner subprocesses (same idea as ``gitutil.git_env``)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME") or tempfile.gettempdir(),
        "LANG": "C.UTF-8",
    }
    env.update({name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ})
    return env


def _chunks(files: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for name in files:
        cost = len(os.fsencode(name)) + 1
        if batch and size + cost > _ARGV_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(name)
        size += cost
    if batch:
        yield batch


class ExternalTool(Analyzer):
    binary: ClassVar[str]
    timeout: ClassVar[int] = 900
    # Exit statuses that mean "ran to completion". ``findings_exit_code`` is the status a tool
    # uses for "ran and found something"; it only counts when there is a report to parse.
    # Every other status is a failure of the tool, never an empty result.
    clean_exit_codes: ClassVar[tuple[int, ...]] = (0,)
    findings_exit_code: ClassVar[int | None] = None
    # False: run from an empty directory instead of the checkout, for tools that read
    # configuration from the working directory (and take the checkout as an absolute path).
    run_in_checkout: ClassVar[bool] = True

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def version(self) -> str:
        try:
            proc = subprocess.run(
                [self.binary, "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                check=False,
                env=tool_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        lines = (proc.stdout or proc.stderr).strip().splitlines()
        return lines[0][:80] if lines else "unknown"

    def command(self, ctx: AuditContext) -> list[str]:
        raise NotImplementedError

    def commands(self, ctx: AuditContext) -> list[list[str]]:
        """The invocations for one audit. Tools that take file arguments return one command per
        chunk of files (``_chunks``) and put ``--`` in front of them: file names come from the
        repository and must never be read as options."""
        return [self.command(ctx)]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        raise NotImplementedError

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        if not ctx.files:
            return []
        out: list[RawFinding] = []
        try:
            for cmd in self.commands(ctx):
                out.extend(self._run(cmd, ctx))
        except Exception as exc:
            raise AnalyzerError(str(exc), _ordered(out)) from exc
        return _ordered(out)

    def _run(self, cmd: list[str], ctx: AuditContext) -> list[RawFinding]:
        if self.run_in_checkout:
            return self._run_in(cmd, ctx, ctx.repo_path)
        with tempfile.TemporaryDirectory(prefix="opsmith-scan-") as neutral:
            return self._run_in(cmd, ctx, Path(neutral))

    def _run_in(self, cmd: list[str], ctx: AuditContext, cwd: Path) -> list[RawFinding]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
                check=False,
                env=tool_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{self.binary} timed out after {self.timeout}s") from exc
        has_report = bool(proc.stdout.strip())
        if proc.returncode in self.clean_exit_codes:
            return self.parse(proc.stdout, ctx) if has_report else []
        if proc.returncode == self.findings_exit_code and has_report:
            return self.parse(proc.stdout, ctx)
        raise RuntimeError(
            f"{self.binary} exited with status {proc.returncode}: {proc.stderr.strip()[-500:]}"
        )


def _ordered(findings: list[RawFinding]) -> list[RawFinding]:
    # Scanners report in no guaranteed order; position in the repository is stable, which
    # keeps the runner's numbering of colliding fingerprints the same from run to run.
    return sorted(
        findings,
        key=lambda f: (str(f.path or ""), f.line if isinstance(f.line, int) else 0, f.rule_id),
    )


@register
class SemgrepTool(ExternalTool):
    name = "semgrep"
    binary = "semgrep"
    audit_types = ("security", "bugs", "performance")
    findings_exit_code = 1

    def commands(self, ctx: AuditContext) -> list[list[str]]:
        base = ["semgrep", "scan", "--json", "--quiet", "--config", "auto", "--"]
        return [[*base, *chunk] for chunk in _chunks(ctx.files)]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        return parse_semgrep(output)


@register
class GitleaksTool(ExternalTool):
    name = "gitleaks"
    binary = "gitleaks"
    audit_types = ("security",)
    findings_exit_code = 1

    def command(self, ctx: AuditContext) -> list[str]:
        return [
            "gitleaks",
            "detect",
            "--no-git",
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            "-",  # stdout; gitleaks 8.30 writes nothing to "/dev/stdout"
            "--config",
            str(GITLEAKS_CONFIG),
            "--source",
            ".",
        ]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        scope = set(ctx.files)
        return [f for f in parse_gitleaks(output) if f.path in scope]


@register
class TrivyTool(ExternalTool):
    name = "trivy"
    binary = "trivy"
    audit_types = ("security", "dependency", "compliance")
    # trivy exits 0 with or without findings; 1 is a fatal error (e.g. no vulnerability DB).
    # trivy reads trivy.yaml and .trivyignore from its working directory; a trivy.yaml in the
    # checkout could send the report to a file of its choosing and leave stdout empty.
    run_in_checkout = False

    def command(self, ctx: AuditContext) -> list[str]:
        target = str(Path(ctx.repo_path).resolve())
        return [
            "trivy",
            "fs",
            "--quiet",
            "--format",
            "json",
            "--scanners",
            "vuln,misconfig",
            target,
        ]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        return parse_trivy(output)


@register
class OsvScannerTool(ExternalTool):
    name = "osv-scanner"
    binary = "osv-scanner"
    audit_types = ("dependency", "security")
    clean_exit_codes = (0, 128)  # 128: no package sources found in the repository
    findings_exit_code = 1

    def command(self, ctx: AuditContext) -> list[str]:
        return ["osv-scanner", "--format", "json", "-r", "."]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        return parse_osv_scanner(output)


@register
class RuffTool(ExternalTool):
    name = "ruff"
    binary = "ruff"
    audit_types = ("bugs",)
    # --exit-zero: findings do not change the status, so anything but 0 is a failure.

    def commands(self, ctx: AuditContext) -> list[list[str]]:
        # --isolated: the checkout's own ruff.toml/pyproject.toml must not be able to switch
        # the rules off (per-file-ignores, exclude) for the code it ships.
        base = [
            "ruff",
            "check",
            "--isolated",
            "--output-format",
            "json",
            "--select",
            "E9,F,B",
            "--exit-zero",
            "--",
        ]
        py = [f for f in ctx.files if f.endswith(".py")]
        return [[*base, *chunk] for chunk in _chunks(py)]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        findings = parse_ruff(output)
        root = str(ctx.repo_path.resolve())
        for f in findings:  # ruff reports absolute paths
            if f.path and os.path.isabs(f.path):
                f.path = os.path.relpath(f.path, root)
        return findings


# --------------------------------------------------------------------------- parsers


def _loads(output: str, expect: type[dict[str, Any]] | type[list[Any]]) -> Any:
    """Parse a scanner's JSON report. Output that is not the expected JSON is an error, never
    an empty result: a scanner that printed a crash message has not found "nothing"."""
    try:
        data = json.loads(output)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"unparseable scanner output: {exc}") from exc
    if data is None:
        return expect()
    if not isinstance(data, expect):
        raise ValueError(f"unexpected scanner output: a JSON {type(data).__name__}")
    return data


def parse_semgrep(output: str) -> list[RawFinding]:
    data = _loads(output, dict)
    out = []
    for r in data.get("results", []):
        extra = r.get("extra", {})
        meta = extra.get("metadata", {})
        cats = " ".join(
            meta.get("category", [])
            if isinstance(meta.get("category"), list)
            else [str(meta.get("category", ""))]
        )
        category = (
            "security"
            if "security" in cats or meta.get("cwe")
            else ("performance" if "performance" in cats else "fix")
        )
        out.append(
            RawFinding(
                "semgrep",
                f"semgrep.{r.get('check_id', 'rule')}",
                category,
                extra.get("message", r.get("check_id", "semgrep finding"))[:300],
                extra.get("message", ""),
                _SEV.get(str(extra.get("severity", "")).upper(), "medium"),
                0.75,
                r.get("path"),
                r.get("start", {}).get("line"),
                extra.get("lines", "")[:200],
            )
        )
    return out


def parse_gitleaks(output: str) -> list[RawFinding]:
    data = _loads(output, list)
    return [
        RawFinding(
            "gitleaks",
            f"secret.gitleaks.{r.get('RuleID', 'generic')}",
            "security",
            f"Secret detected by gitleaks ({r.get('RuleID')}) in {r.get('File')}",
            r.get("Description", "Hardcoded secret"),
            "high",
            0.85,
            r.get("File"),
            r.get("StartLine"),
            # Neither the secret nor gitleaks' own fingerprint (file:rule:line, which changes
            # whenever a line is added above) goes into ours. Several leaks of one rule in a
            # file are told apart by their order (see the runner's collision numbering).
            "redacted",
        )
        for r in data
        if isinstance(r, dict)
    ]


def parse_trivy(output: str) -> list[RawFinding]:
    data = _loads(output, dict)
    out = []
    for res in data.get("Results", []) or []:
        target = res.get("Target")
        for v in res.get("Vulnerabilities", []) or []:
            vid = v.get("VulnerabilityID", "CVE")
            out.append(
                RawFinding(
                    "trivy",
                    f"dep.vuln.{vid}",
                    "security",
                    f"{v.get('PkgName')} {v.get('InstalledVersion')} is affected by {vid}",
                    (v.get("Title") or vid)
                    + (f" Fixed in {v['FixedVersion']}." if v.get("FixedVersion") else ""),
                    _SEV.get(str(v.get("Severity", "")).upper(), "medium"),
                    0.9,
                    target,
                    None,
                    f"{v.get('PkgName')}@{v.get('InstalledVersion')} {vid}",
                )
            )
        for m in res.get("Misconfigurations", []) or []:
            out.append(
                RawFinding(
                    "trivy",
                    f"iac.{m.get('ID', 'misconfig')}",
                    "security",
                    f"IaC misconfiguration {m.get('ID')}: {m.get('Title')}"[:300],
                    m.get("Description", ""),
                    _SEV.get(str(m.get("Severity", "")).upper(), "medium"),
                    0.8,
                    target,
                    (m.get("CauseMetadata") or {}).get("StartLine"),
                    str(m.get("ID")),
                )
            )
    return out


def parse_osv_scanner(output: str) -> list[RawFinding]:
    data = _loads(output, dict)
    out = []
    for res in data.get("results", []) or []:
        path = (res.get("source") or {}).get("path")
        for pkg in res.get("packages", []) or []:
            info = pkg.get("package", {})
            for vuln in pkg.get("vulnerabilities", []) or []:
                vid = vuln.get("id", "OSV")
                out.append(
                    RawFinding(
                        "osv-scanner",
                        f"dep.vuln.{vid}",
                        "security",
                        f"{info.get('name')} {info.get('version')} is affected by {vid}",
                        vuln.get("summary", vid),
                        "high",
                        0.9,
                        path,
                        None,
                        f"{info.get('name')}@{info.get('version')} {vid}",
                    )
                )
    return out


def parse_ruff(output: str) -> list[RawFinding]:
    data = _loads(output, list)
    return [
        RawFinding(
            "ruff",
            f"ruff.{r.get('code')}",
            "fix",
            f"{r.get('code')}: {r.get('message')}",
            r.get("message", ""),
            "low" if str(r.get("code", "")).startswith("F4") else "medium",
            0.8,
            r.get("filename"),
            (r.get("location") or {}).get("row"),
            str(r.get("code")),
        )
        for r in data
        if isinstance(r, dict)
    ]
