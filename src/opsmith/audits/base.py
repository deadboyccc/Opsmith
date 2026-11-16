"""Audit analyzer interface + registry."""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, ClassVar

import httpx

from opsmith.schemas import ProjectSettings

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 1_000_000
# Regexes are only ever run against this much of one line. Repository content is untrusted and
# several detectors backtrack quadratically on a long line, so a single-line megabyte file
# would otherwise occupy the worker for minutes. Anything past the cap is not scanned.
MAX_LINE_CHARS = 10_240
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
AUDIT_TYPES = ("security", "bugs", "compliance", "performance", "dependency", "coverage")


_NEWLINE = re.compile(r"\r\n|\r|\n")


def source_lines(text: str) -> list[str]:
    """The lines of a source file as editors, compilers and git number them: ended by LF,
    CRLF or CR and nothing else. ``str.splitlines`` also breaks at form feeds and the Unicode
    separators, which shifts every later line number in a file that contains one."""
    lines = _NEWLINE.split(text)
    if lines and not lines[-1]:
        lines.pop()  # the text ended with a newline; there is no line after it
    return lines


def printable(text: str) -> str:
    """Text that can be stored and sent. A file name that is not UTF-8 reaches Python with its
    odd bytes as lone surrogates, which neither a database nor JSON over UTF-8 accepts; they
    become U+FFFD here."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _printable_value(value: Any) -> Any:
    if isinstance(value, str):
        return printable(value)
    if isinstance(value, dict):
        return {_printable_value(k): _printable_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_printable_value(v) for v in value]
    return value


@dataclass
class RawFinding:
    analyzer: str
    rule_id: str
    category: str
    title: str
    description: str
    severity: str
    confidence: float
    path: str | None = None
    line: int | None = None
    snippet: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def cleaned(self) -> RawFinding:
        """The same finding with every string made storable (see ``printable``)."""
        return RawFinding(
            analyzer=self.analyzer,
            rule_id=printable(self.rule_id),
            category=self.category,
            title=printable(self.title),
            description=printable(self.description or ""),
            severity=self.severity,
            confidence=self.confidence,
            path=printable(self.path) if self.path else self.path,
            line=self.line,
            snippet=printable(self.snippet),
            extra=_printable_value(self.extra),
        )

    def fingerprint(self, repository_id: str, occurrence: int = 0) -> str:
        """Identity of the finding across commits. ``occurrence`` numbers further findings in
        one run that would otherwise share it (the same rule on identical lines of one file):
        the first keeps the plain fingerprint, each later one gets its own."""
        # Line numbers shift between commits; the normalized snippet does not.
        norm = " ".join(self.snippet.split())[:200]
        raw = f"{repository_id}|{self.rule_id}|{self.path or ''}|{norm}"
        if occurrence:
            raw += f"|#{occurrence}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class AuditContext:
    repo_path: Path
    repository_id: str
    files: list[str]  # files in scope (all files on full runs, changed files on incremental)
    all_files: list[str]
    settings: ProjectSettings
    commit_sha: str | None = None
    http: httpx.Client | None = None

    def read(self, rel: str) -> str | None:
        path = self.repo_path / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:4096]:
            return None  # binary
        return data.decode("utf-8", errors="replace")

    def iter_text(self, suffixes: tuple[str, ...] | None = None) -> Iterator[tuple[str, str]]:
        for rel in self.files:
            if suffixes and not rel.endswith(suffixes):
                continue
            text = self.read(rel)
            if text is not None:
                yield rel, text


class AnalyzerError(RuntimeError):
    """An analyzer failed part-way. ``findings`` holds what it had produced until then, so the
    runner keeps those and still records the failure on the run (nothing is dropped, and the
    failure is not mistaken for a clean result)."""

    def __init__(self, message: str, findings: list[RawFinding] | None = None) -> None:
        super().__init__(message)
        self.findings: list[RawFinding] = findings or []


class Analyzer(ABC):
    name: ClassVar[str]
    audit_types: ClassVar[tuple[str, ...]]
    deterministic: ClassVar[bool] = True

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "builtin-1"

    @abstractmethod
    def analyze(self, ctx: AuditContext) -> list[RawFinding]: ...


_REGISTRY: dict[str, type[Analyzer]] = {}
_EP_LOADED = False


def register(cls: type[Analyzer]) -> type[Analyzer]:
    _REGISTRY[cls.name] = cls
    return cls


def analyzers_for(types: list[str]) -> list[Analyzer]:
    global _EP_LOADED  # noqa: PLW0603
    if not _EP_LOADED:
        _EP_LOADED = True
        for ep in entry_points(group="opsmith.analyzers"):
            try:
                register(ep.load())
            except Exception:
                log.exception("failed to load analyzer plugin", extra={"plugin": ep.name})
    wanted = set(types)
    return [cls() for cls in _REGISTRY.values() if wanted & set(cls.audit_types)]


def registered() -> dict[str, tuple[str, ...]]:
    return {name: cls.audit_types for name, cls in sorted(_REGISTRY.items())}


AnalyzerFactory = Callable[[list[str]], list[Analyzer]]


def iter_repo_files(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.is_symlink():
            continue
        out.append(rel.as_posix())
    return out
