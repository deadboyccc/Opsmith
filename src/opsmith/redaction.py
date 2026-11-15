"""PII and secret redaction.

Applied in ETL (before anything is persisted for analysis) and again, defensively, in the
LLM gateway right before a prompt leaves the process. Redaction is deterministic so the
same input always yields the same masked output (keeps log-template clustering stable).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Detector:
    name: str
    pattern: re.Pattern[str]
    group: int = 0  # which group to mask (0 = whole match)
    secret: bool = False  # True -> also reported by the secrets audit analyzer


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        value = digit * 2 if i % 2 == 1 else digit
        total += value - 9 if value > 9 else value
    return total % 10 == 0


# Order matters: specific credential formats first, generic patterns last.
DETECTORS: tuple[Detector, ...] = (
    Detector(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:[A-Z ]+ )?PRIVATE KEY-----"
        ),
        secret=True,
    ),
    Detector("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), secret=True),
    Detector(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b"),
        secret=True,
    ),
    Detector("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,200}\b"), secret=True),
    Detector("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,200}\b"), secret=True),
    Detector("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{32,200}\b"), secret=True),
    Detector("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), secret=True),
    Detector("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,99}\b"), secret=True),
    Detector(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
        secret=True,
    ),
    Detector(
        "url_credentials",
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)"),
        group=2,
        secret=True,
    ),
    Detector(
        "bearer_token",
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-.=+/]{16,})"),
        group=2,
        secret=True,
    ),
    Detector(
        "credential_assignment",
        re.compile(
            r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|private[_-]?key)\s*[=:]\s*[\"']?)([^\s\"',;]{6,})"
        ),
        group=2,
        secret=True,
    ),
    Detector("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    Detector("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    Detector("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    Detector(
        "phone",
        re.compile(r"(?<![\w.])\+?\d{1,3}[\s.\-]?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
    ),
    Detector(
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    ),
)

SECRET_DETECTORS: tuple[Detector, ...] = tuple(d for d in DETECTORS if d.secret)


@dataclass
class Redactor:
    extra_patterns: Iterable[str] = ()
    detectors: tuple[Detector, ...] = DETECTORS
    _extra: tuple[Detector, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        self._extra = tuple(
            Detector(f"custom_{i}", re.compile(p)) for i, p in enumerate(self.extra_patterns)
        )

    def redact_text(self, text: str) -> tuple[str, Counter[str]]:
        counts: Counter[str] = Counter()
        for det in (*self.detectors, *self._extra):
            text = self._apply(det, text, counts)
        return text, counts

    @staticmethod
    def _apply(det: Detector, text: str, counts: Counter[str]) -> str:
        def repl(match: re.Match[str]) -> str:
            if det.name == "credit_card" and not _luhn_ok(match.group(0)):
                return match.group(0)
            if "[REDACTED:" in match.group(0):
                return match.group(0)
            counts[det.name] += 1
            token = f"[REDACTED:{det.name}]"
            if det.group == 0:
                return token
            start, end = match.span(det.group)
            base = match.start(0)
            whole = match.group(0)
            return whole[: start - base] + token + whole[end - base :]

        return det.pattern.sub(repl, text)

    def redact(self, value: Any, counts: Counter[str] | None = None) -> Any:
        """Recursively redact strings inside dicts/lists. Keys are left untouched."""
        counts = counts if counts is not None else Counter()
        if isinstance(value, str):
            redacted, c = self.redact_text(value)
            counts.update(c)
            return redacted
        if isinstance(value, dict):
            return {k: self.redact(v, counts) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v, counts) for v in value]
        return value


DEFAULT_REDACTOR = Redactor()


def redact_text(text: str) -> str:
    return DEFAULT_REDACTOR.redact_text(text)[0]
