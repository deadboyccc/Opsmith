"""Built-in analyzers: secrets, Python AST bug/security/performance checks, dependency
health (+ optional OSV lookups), license/policy-as-code compliance, and test-coverage gaps.

They are dependency-free so every install can audit something; external scanners
(Semgrep, gitleaks, Trivy, OSV-Scanner, ruff) plug in through ``opsmith.audits.external``.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import tomllib
from pathlib import PurePosixPath
from typing import Any

import httpx

from opsmith.audits.base import Analyzer, AuditContext, RawFinding, register
from opsmith.redaction import SECRET_DETECTORS

_SECRET_SKIP = (".lock", ".min.js", ".svg", ".map")
# Judged on the raw value before masking, so triage never needs to see the secret itself.
_PLACEHOLDER_VALUE = re.compile(
    r"(?i)example|dummy|placeholder|changeme|xxxx|fake|sample|your[_-]|<[^>]+>|^\$\{"
)


@register
class SecretsAnalyzer(Analyzer):
    name = "secrets"
    audit_types = ("security",)

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        out: list[RawFinding] = []
        for rel, text in ctx.iter_text():
            if rel.endswith(_SECRET_SKIP):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for det in SECRET_DETECTORS:
                    match = det.pattern.search(line)
                    if not match:
                        continue
                    secret = match.group(det.group)
                    masked = line.replace(secret, secret[:3] + "***") if secret else line
                    placeholder = bool(_PLACEHOLDER_VALUE.search(secret or ""))
                    out.append(
                        RawFinding(
                            analyzer=self.name,
                            rule_id=f"secret.{det.name}",
                            category="security",
                            title=f"Hardcoded credential ({det.name}) in {rel}",
                            description=(
                                f"A value matching the `{det.name}` detector is committed in "
                                f"{rel}:{lineno}. Move it to the secret store and rotate it."
                            ),
                            severity="high" if det.name != "credential_assignment" else "medium",
                            confidence=0.8 if det.name != "credential_assignment" else 0.55,
                            path=rel,
                            line=lineno,
                            snippet=masked.strip()[:200],
                            extra={"placeholder": placeholder},
                        )
                    )
                    break  # one finding per line
        return out


_DB_RECEIVERS = re.compile(r"(?i)(db|session|cursor|conn|connection|repo|client|orm|query)")
_DB_METHODS = {
    "execute",
    "query",
    "get",
    "filter",
    "filter_by",
    "fetchone",
    "fetchall",
    "scalar",
    "scalars",
    "find",
    "find_one",
    "select",
    "first",
    "all",
}
_SQL_KEYWORDS = re.compile(r"(?i)\b(select|insert|update|delete)\b")


@register
class PythonAstAnalyzer(Analyzer):
    """AST checks across bugs, security and performance."""

    name = "python-ast"
    audit_types = ("bugs", "security", "performance")

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        out: list[RawFinding] = []
        for rel, text in ctx.iter_text((".py",)):
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:
                out.append(
                    self._f(
                        "py.syntax-error",
                        "fix",
                        f"Syntax error in {rel}",
                        str(exc),
                        "high",
                        0.9,
                        rel,
                        exc.lineno or 1,
                        "",
                    )
                )
                continue
            lines = text.splitlines()
            out.extend(self._visit(tree, rel, lines))
        return out

    def _f(
        self,
        rule: str,
        category: str,
        title: str,
        desc: str,
        sev: str,
        conf: float,
        path: str,
        line: int,
        snippet: str,
    ) -> RawFinding:
        return RawFinding(
            self.name, rule, category, title, desc, sev, conf, path, line, snippet.strip()[:200]
        )

    def _visit(self, tree: ast.AST, rel: str, lines: list[str]) -> list[RawFinding]:
        out: list[RawFinding] = []

        def src(node: ast.AST) -> str:
            lineno = getattr(node, "lineno", 1)
            return lines[lineno - 1] if 0 < lineno <= len(lines) else ""

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    out.append(
                        self._f(
                            "py.bare-except",
                            "fix",
                            f"Bare `except:` in {rel}",
                            "A bare except also catches KeyboardInterrupt/SystemExit and hides "
                            "bugs.",
                            "medium",
                            0.85,
                            rel,
                            node.lineno,
                            src(node),
                        )
                    )
                elif (
                    len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)
                    and isinstance(node.type, ast.Name)
                    and node.type.id in {"Exception", "BaseException"}
                ):
                    out.append(
                        self._f(
                            "py.swallowed-exception",
                            "observability",
                            f"Exception swallowed silently in {rel}",
                            "`except Exception: pass` drops errors without logging.",
                            "low",
                            0.7,
                            rel,
                            node.lineno,
                            src(node),
                        )
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        out.append(
                            self._f(
                                "py.mutable-default",
                                "fix",
                                f"Mutable default argument in `{node.name}` ({rel})",
                                "Mutable defaults are shared between calls.",
                                "medium",
                                0.9,
                                rel,
                                node.lineno,
                                src(node),
                            )
                        )
                        break
            elif isinstance(node, ast.Call):
                out.extend(self._call(node, rel, src(node)))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                out.extend(self._loop(node, rel, src))
        return out

    def _call(self, node: ast.Call, rel: str, line: str) -> list[RawFinding]:
        out: list[RawFinding] = []
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else "")
        )
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if name in {"eval", "exec"} and isinstance(func, ast.Name):
            out.append(
                self._f(
                    "py.eval",
                    "security",
                    f"Use of `{name}` in {rel}",
                    f"`{name}` on dynamic input enables code injection.",
                    "high",
                    0.7,
                    rel,
                    node.lineno,
                    line,
                )
            )
        shell = kw.get("shell")
        if isinstance(shell, ast.Constant) and shell.value is True:
            out.append(
                self._f(
                    "py.subprocess-shell",
                    "security",
                    f"subprocess with shell=True in {rel}",
                    "shell=True with interpolated input allows command injection.",
                    "high",
                    0.75,
                    rel,
                    node.lineno,
                    line,
                )
            )
        if (
            name == "load"
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "yaml"
            and "Loader" not in kw
        ):
            out.append(
                self._f(
                    "py.yaml-load",
                    "security",
                    f"Unsafe yaml.load in {rel}",
                    "Use yaml.safe_load; yaml.load can construct arbitrary objects.",
                    "high",
                    0.85,
                    rel,
                    node.lineno,
                    line,
                )
            )
        if (
            name in {"get", "post", "put", "delete", "patch", "request"}
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in {"requests", "httpx"}
            and "timeout" not in kw
        ):
            out.append(
                self._f(
                    "py.http-no-timeout",
                    "performance",
                    f"HTTP call without timeout in {rel}",
                    "Calls without a timeout can hang workers indefinitely.",
                    "medium",
                    0.8,
                    rel,
                    node.lineno,
                    line,
                )
            )
        if name in {"execute", "executemany"} and node.args:
            arg = node.args[0]
            dynamic = (
                isinstance(arg, ast.JoinedStr)
                or (isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Mod, ast.Add)))
                or (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "format"
                )
            )
            if dynamic and _SQL_KEYWORDS.search(ast.unparse(arg)):
                out.append(
                    self._f(
                        "py.sql-injection",
                        "security",
                        f"SQL built from string formatting in {rel}",
                        "Use parameterized queries instead of string formatting.",
                        "high",
                        0.8,
                        rel,
                        node.lineno,
                        line,
                    )
                )
        return out

    def _loop(self, node: ast.AST, rel: str, src: Any) -> list[RawFinding]:
        body = getattr(node, "body", [])
        for stmt in body:
            for inner in ast.walk(stmt):
                if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                    continue
                if inner.func.attr not in _DB_METHODS:
                    continue
                receiver = ast.unparse(inner.func.value)
                if _DB_RECEIVERS.search(receiver):
                    return [
                        self._f(
                            "py.n-plus-one",
                            "performance",
                            f"Query inside loop (N+1) in {rel}",
                            f"`{receiver}.{inner.func.attr}(...)` runs once per iteration; "
                            "batch it.",
                            "medium",
                            0.6,
                            rel,
                            getattr(inner, "lineno", 1),
                            src(inner),
                        )
                    ]
        return []


_REQ_LINE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(==|>=|<=|~=|!=|>|<)?\s*([^;#\s]*)"
)


def parse_python_requirements(text: str) -> list[tuple[str, str | None, str | None]]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = _REQ_LINE.match(line)
        if m:
            out.append((m.group(1).lower(), m.group(3), m.group(4) or None))
    return out


@register
class DependencyAnalyzer(Analyzer):
    """Dependency health (unpinned deps) + known vulnerabilities via OSV (opt-in online)."""

    name = "dependencies"
    audit_types = ("dependency", "security")

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        deps: list[tuple[str, str, str | None, str]] = []  # ecosystem, name, version, file
        out: list[RawFinding] = []
        for rel in ctx.files:
            base = PurePosixPath(rel).name
            text = None
            if fnmatch.fnmatch(base, "requirements*.txt"):
                text = ctx.read(rel)
                for name, op, ver in parse_python_requirements(text or ""):
                    deps.append(("PyPI", name, ver if op == "==" else None, rel))
                    if op is None:
                        out.append(self._unpinned(name, rel))
            elif base == "pyproject.toml":
                text = ctx.read(rel)
                try:
                    data = tomllib.loads(text or "")
                except tomllib.TOMLDecodeError:
                    continue
                for spec in data.get("project", {}).get("dependencies", []):
                    for name, op, ver in parse_python_requirements(spec):
                        deps.append(("PyPI", name, ver if op == "==" else None, rel))
            elif base == "package.json":
                text = ctx.read(rel)
                try:
                    data = json.loads(text or "{}")
                except json.JSONDecodeError:
                    continue
                for section in ("dependencies", "devDependencies"):
                    for name, ver in (data.get(section) or {}).items():
                        exact = ver if re.fullmatch(r"\d+\.\d+\.\d+", str(ver)) else None
                        deps.append(("npm", name, exact, rel))
                        if str(ver) in {"*", "latest", ""}:
                            out.append(self._unpinned(name, rel))
        if ctx.settings.osv_online and deps:
            out.extend(self._osv(ctx, deps))
        return out

    def _unpinned(self, name: str, rel: str) -> RawFinding:
        return RawFinding(
            self.name,
            "dep.unpinned",
            "dependency",
            f"Unpinned dependency `{name}` in {rel}",
            "Unpinned dependencies make builds non-reproducible and can pull in breaking or "
            "vulnerable releases.",
            "low",
            0.7,
            rel,
            None,
            name,
        )

    def _osv(
        self, ctx: AuditContext, deps: list[tuple[str, str, str | None, str]]
    ) -> list[RawFinding]:
        pinned = [(eco, n, v, f) for eco, n, v, f in deps if v]
        if not pinned:
            return []
        client = ctx.http or httpx.Client(timeout=20)
        queries = [
            {"package": {"name": n, "ecosystem": eco}, "version": v} for eco, n, v, _ in pinned
        ]
        try:
            resp = client.post("https://api.osv.dev/v1/querybatch", json={"queries": queries})
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError):
            return []
        out = []
        for (eco, name, ver, rel), res in zip(pinned, results, strict=False):
            for vuln in res.get("vulns", []) or []:
                vid = vuln.get("id", "OSV")
                out.append(
                    RawFinding(
                        self.name,
                        f"dep.vuln.{vid}",
                        "security",
                        f"{name}=={ver} is affected by {vid}",
                        f"OSV reports {vid} for {eco} package {name} {ver}. Upgrade to a fixed "
                        f"version.",
                        "high",
                        0.9,
                        rel,
                        None,
                        f"{name}=={ver} {vid}",
                        {"osv_id": vid},
                    )
                )
        return out


_SPDX = re.compile(r"SPDX-License-Identifier:\s*([A-Za-z0-9.\-+]+)")


@register
class ComplianceAnalyzer(Analyzer):
    """License checks and policy-as-code rules from project settings."""

    name = "compliance"
    audit_types = ("compliance",)

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        out: list[RawFinding] = []
        if not any(
            PurePosixPath(f).name.upper().startswith(("LICENSE", "COPYING")) for f in ctx.all_files
        ):
            out.append(
                RawFinding(
                    self.name,
                    "license.missing",
                    "compliance",
                    "Repository has no LICENSE file",
                    "Add a LICENSE file so usage terms are explicit.",
                    "low",
                    0.9,
                    "LICENSE",
                    None,
                    "missing",
                )
            )
        allow = set(ctx.settings.license_allowlist)
        for rel, text in ctx.iter_text():
            if PurePosixPath(rel).name == "package.json":
                try:
                    lic = json.loads(text).get("license")
                except (json.JSONDecodeError, AttributeError):
                    lic = None
                if lic and lic not in allow:
                    out.append(self._license(rel, lic))
            for m in _SPDX.finditer(text[:2000]):
                if m.group(1) not in allow:
                    out.append(self._license(rel, m.group(1)))
        for rule in ctx.settings.policy_rules:
            out.extend(self._policy(ctx, rule))
        return out

    def _license(self, rel: str, lic: str) -> RawFinding:
        return RawFinding(
            self.name,
            "license.not-allowed",
            "compliance",
            f"License `{lic}` in {rel} is not on the allowlist",
            "Confirm the license is acceptable or replace the component.",
            "medium",
            0.8,
            rel,
            None,
            lic,
        )

    def _policy(self, ctx: AuditContext, rule: Any) -> list[RawFinding]:
        out: list[RawFinding] = []
        forbid = re.compile(rule.forbid_regex) if rule.forbid_regex else None
        require = re.compile(rule.require_regex) if rule.require_regex else None
        for rel, text in ctx.iter_text():
            if not fnmatch.fnmatch(rel, rule.path_glob):
                continue
            if forbid:
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if forbid.search(line):
                        out.append(
                            RawFinding(
                                self.name,
                                f"policy.{rule.id}",
                                "compliance",
                                f"Policy `{rule.id}` violated in {rel}",
                                rule.description,
                                rule.severity,
                                0.85,
                                rel,
                                lineno,
                                line.strip()[:200],
                            )
                        )
            if require and not require.search(text):
                out.append(
                    RawFinding(
                        self.name,
                        f"policy.{rule.id}",
                        "compliance",
                        f"Policy `{rule.id}` not satisfied by {rel}",
                        rule.description,
                        rule.severity,
                        0.85,
                        rel,
                        None,
                        f"missing:{rule.require_regex}",
                    )
                )
        return out


@register
class CoverageGapAnalyzer(Analyzer):
    """Modules that no test file references (cheap proxy for coverage gaps)."""

    name = "coverage-gaps"
    audit_types = ("coverage",)

    def analyze(self, ctx: AuditContext) -> list[RawFinding]:
        tests = [f for f in ctx.all_files if re.search(r"(^|/)tests?/|test_[^/]+\.py$", f)]
        corpus = "\n".join(ctx.read(t) or "" for t in tests)
        out: list[RawFinding] = []
        for rel in ctx.files:
            if not rel.endswith(".py") or rel in tests or PurePosixPath(rel).name.startswith("_"):
                continue
            module = PurePosixPath(rel).stem
            if module not in corpus:
                out.append(
                    RawFinding(
                        self.name,
                        "coverage.untested-module",
                        "refactor",
                        f"No tests reference `{module}` ({rel})",
                        "Add tests for this module before changing it.",
                        "low",
                        0.5,
                        rel,
                        None,
                        module,
                    )
                )
        return out
