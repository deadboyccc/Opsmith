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
from typing import Any, ClassVar

from opsmith.audits.base import Analyzer, AuditContext, RawFinding, register

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


class ExternalTool(Analyzer):
    binary: ClassVar[str]
    timeout: ClassVar[int] = 900

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def version(self) -> str:
        try:
            proc = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return (
            (proc.stdout or proc.stderr).strip().splitlines()[0][:80]
            if (proc.stdout or proc.stderr)
            else "unknown"
        )

    def command(self, ctx: AuditContext) -> list[str]:
        raise NotImplementedError

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        raise NotImplementedError

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        if not ctx.files:
            return []
        proc = subprocess.run(
            self.command(ctx),
            cwd=ctx.repo_path,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if not proc.stdout.strip():
            if proc.returncode not in (0, 1):
                raise RuntimeError(f"{self.binary} failed: {proc.stderr[-500:]}")
            return []
        return self.parse(proc.stdout, ctx)


@register
class SemgrepTool(ExternalTool):
    name = "semgrep"
    binary = "semgrep"
    audit_types = ("security", "bugs", "performance")

    def command(self, ctx: AuditContext) -> list[str]:
        return ["semgrep", "scan", "--json", "--quiet", "--config", "auto", *ctx.files]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        return parse_semgrep(output)


@register
class GitleaksTool(ExternalTool):
    name = "gitleaks"
    binary = "gitleaks"
    audit_types = ("security",)

    def command(self, ctx: AuditContext) -> list[str]:
        return [
            "gitleaks",
            "detect",
            "--no-git",
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            "/dev/stdout",
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

    def command(self, ctx: AuditContext) -> list[str]:
        return ["trivy", "fs", "--quiet", "--format", "json", "--scanners", "vuln,misconfig", "."]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        return parse_trivy(output)


@register
class OsvScannerTool(ExternalTool):
    name = "osv-scanner"
    binary = "osv-scanner"
    audit_types = ("dependency", "security")

    def command(self, ctx: AuditContext) -> list[str]:
        return ["osv-scanner", "--format", "json", "-r", "."]

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        return parse_osv_scanner(output)


@register
class RuffTool(ExternalTool):
    name = "ruff"
    binary = "ruff"
    audit_types = ("bugs",)

    def command(self, ctx: AuditContext) -> list[str]:
        py = [f for f in ctx.files if f.endswith(".py")]
        return (
            ["ruff", "check", "--output-format", "json", "--select", "E9,F,B", "--exit-zero", *py]
            if py
            else ["ruff", "--version"]
        )

    def parse(self, output: str, ctx: AuditContext) -> list[RawFinding]:
        findings = parse_ruff(output)
        root = str(ctx.repo_path.resolve())
        for f in findings:  # ruff reports absolute paths
            if f.path and os.path.isabs(f.path):
                f.path = os.path.relpath(f.path, root)
        return findings


# --------------------------------------------------------------------------- parsers


def _loads(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def parse_semgrep(output: str) -> list[RawFinding]:
    data = _loads(output) or {}
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
    data = _loads(output) or []
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
            "redacted:" + str(r.get("Fingerprint", ""))[:120],
        )
        for r in data
        if isinstance(r, dict)
    ]


def parse_trivy(output: str) -> list[RawFinding]:
    data = _loads(output) or {}
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
    data = _loads(output) or {}
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
    data = _loads(output) or []
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
