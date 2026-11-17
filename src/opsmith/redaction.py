"""PII and secret redaction.

Applied in ETL (before anything is persisted for analysis) and again, defensively, in the
LLM gateway right before a prompt leaves the process. Redaction is deterministic so the
same input always yields the same masked output (keeps log-template clustering stable).

The built-in patterns are written to run in time linear in the input: every unbounded
repetition either starts at a position a look-behind pins to the start of its run, or is
bounded. ``redaction_extra_patterns`` are compiled as given; they are written by a project
admin and are trusted not to backtrack catastrophically.
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


def _card_shaped(number: str) -> bool:
    """Whether a Luhn-valid number can be a payment card at all. Card numbers start with 2 to
    6 (the networks' ranges), and a 13-digit one with 4. Without this, one 13-digit number in
    ten is a "card": an epoch timestamp in milliseconds starts with 1 and passes Luhn that
    often."""
    digits = re.sub(r"\D", "", number)
    return digits[:1] in "23456" and (len(digits) > 13 or digits[:1] == "4")


def _is_card(number: str) -> bool:
    return _luhn_ok(number) and _card_shaped(number)


# Names whose value is a credential, as they appear at the end of a key: ``password``,
# ``DB_PASSWORD``, ``"apiKey"``, ``AWS_SECRET_ACCESS_KEY``, ``x-auth-token`` ...
# A bare ``pass`` counts only where it is a word of its own (``DB_PASS``, ``--pass``), not
# inside ``bypass`` or ``compass``.
_CREDENTIAL_KEY = (
    r"(?:pass(?:word|wd|phrase)|(?<![A-Za-z])pass|pwd|secret(?:[_-]?access)?(?:[_-]?key)?|"
    r"token|api[_-]?key|access[_-]?key|private[_-]?key)"
)
# ``key = `` / ``"key": `` up to the value; the key may be quoted (JSON, dict reprs), and the
# quote may be escaped (JSON carried inside a JSON string).
_ASSIGN = _CREDENTIAL_KEY + r"\\?[\"']?\s*[=:]\s*"
# Start of a URL scheme, pinned so a long dotted or hyphenated run is scanned once.
_SCHEME = r"(?<![A-Za-z0-9+.\-])[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://"
# A structured field with one of these names holds a credential whatever its value looks like.
_SENSITIVE_FIELD = re.compile(rf"(?i)(?:{_CREDENTIAL_KEY}|authorization|cookie)s?$")
_REDACTED_ONLY = re.compile(r"\[REDACTED:[a-z_0-9]+\]")
_DIGITS = re.compile(r"\d+")

# Order matters: specific credential formats first, generic patterns last.
DETECTORS: tuple[Detector, ...] = (
    # The body stops at the next BEGIN, so text with many headers is still scanned once. A
    # block cut off by the end of the text (a truncated message) is masked as far as it goes.
    Detector(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY(?: BLOCK)?-----"
            r"(?:(?!-----BEGIN )[\s\S]){0,16384}?"
            r"(?:-----END (?:[A-Z ]+ )?PRIVATE KEY(?: BLOCK)?-----|\Z)"
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
    Detector(
        "openai_style_key",
        re.compile(r"\bsk-(?:[A-Za-z0-9]{32,200}|(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,200})\b"),
        secret=True,
    ),
    Detector("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), secret=True),
    Detector("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,99}\b"), secret=True),
    # A header that starts mid-run (after a "-") is length-bounded, so a long hyphenated run
    # of "eyJ..." fragments is not rescanned from every fragment.
    Detector(
        "jwt",
        re.compile(
            r"(?:(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}|\beyJ[A-Za-z0-9_\-]{8,1024})"
            r"\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"
        ),
        secret=True,
    ),
    # scheme://user:password@host. The user may be empty (redis://:pw@host) and the
    # password runs to the last "@" of the authority, so a password containing "@" is
    # masked whole.
    Detector(
        "url_credentials",
        re.compile("(" + _SCHEME + r"[^\s:/]{0,256}:)([^\s/]{1,256})(@)"),
        group=2,
        secret=True,
    ),
    # A password containing "/" ends the authority early for a URL parser, but what follows
    # "user:" is not a port, so the text up to the last "@host" can only be a password.
    Detector(
        "url_credentials",
        re.compile(
            "(" + _SCHEME + r"[^\s:/@\[]{0,256}:)(?!\d{1,5}(?:[/?#\s]|$))"
            r"([^\s]{1,256})(@)(?=[A-Za-z0-9\[])"
        ),
        group=2,
    ),
    Detector(
        "bearer_token",
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-.=+/]{16,})"),
        group=2,
        secret=True,
    ),
    Detector(
        "basic_auth",
        re.compile(r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?basic\s+)([A-Za-z0-9+/=_\-]{8,})"),
        group=2,
    ),
    # Any other Authorization scheme (Token, Digest, a raw value) and cookies, as headers are
    # written in logs. The value is one token; a scheme word may come before it.
    Detector(
        "authorization_header",
        re.compile(
            r"(?i)\b((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:[A-Za-z]{2,20}\s+)?)"
            r"([A-Za-z0-9_\-.=+/]{8,})"
        ),
        group=2,
    ),
    Detector(
        "cookie",
        re.compile(
            r"(?i)\b((?:set-)?cookie[\"']?\s*[:=]\s*[\"']?[^\s=;\"']{1,64}=)([^\s;\"']{8,})"
        ),
        group=2,
    ),
    # Quoted values first, so a value with spaces is masked whole rather than word by word.
    Detector(
        "credential_assignment",
        re.compile(rf"(?i)({_ASSIGN}\\?\")([^\"\n]{{6,512}}?)(?=\\?\")"),
        group=2,
    ),
    Detector(
        "credential_assignment",
        re.compile(rf"(?i)({_ASSIGN}')([^'\n]{{6,512}})(?=')"),
        group=2,
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
    # The same idea for logs: any key *ending* in a credential name (DB_PASSWORD,
    # AWS_SECRET_ACCESS_KEY, "apiKey", token, passphrase). Too eager for source code, where
    # `token = next_token()` is ordinary, so the secrets audit keeps the stricter one above.
    Detector(
        "credential_assignment",
        re.compile(rf"(?i)({_ASSIGN})([^\s\"',;&]{{6,}})"),
        group=2,
    ),
    Detector(
        "email",
        re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    Detector("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # A run of digit groups; the card numbers inside it are found by `_mask_cards`.
    Detector("credit_card", re.compile(r"\b\d(?:[ -]?\d){12,}\b")),
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


def _mask_cards(run: str, counts: Counter[str]) -> str:
    """Mask the card numbers inside a run of digit groups (``4111 1111 1111 1111 123``).

    A card rarely stands alone: a CVV, an expiry, an amount or an order number next to it
    makes the run as a whole fail the Luhn check. So every window of whole groups holding
    13-19 digits is checked, longest first. Windows never split a group, and apart from the
    whole run they must look like a card (groups of 4+ digits, the last one 3+), which keeps
    ordinary number lists and long ids intact.
    """
    groups = [m.span() for m in _DIGITS.finditer(run)]
    sizes = [end - start for start, end in groups]
    last = len(groups) - 1
    whole_run_fits = sum(sizes) <= 19
    out: list[str] = []
    pos = i = 0
    while i <= last:
        hit: int | None = None
        digits = 0
        for j in range(i, last + 1):
            digits += sizes[j]
            if digits > 19:
                break
            whole_run = i == 0 and j == last
            card_like = sizes[j] >= 3  # every earlier group has 4+ digits, or we left the loop
            candidate = run[groups[i][0] : groups[j][1]]
            if digits >= 13 and (whole_run or card_like) and _is_card(candidate):
                hit = j  # keep looking: a longer window wins
            if sizes[j] < 4 and not (i == 0 and whole_run_fits):
                break  # no card-shaped window continues past a short group
        if hit is None:
            i += 1
            continue
        counts["credit_card"] += 1
        out.append(run[pos : groups[i][0]] + "[REDACTED:credit_card]")
        pos = groups[hit][1]
        i = hit + 1
    out.append(run[pos:])
    return "".join(out)


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
            if det.name == "credit_card":
                return _mask_cards(match.group(0), counts)
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

    def redact(
        self, value: Any, counts: Counter[str] | None = None, *, key: str | None = None
    ) -> Any:
        """Recursively redact strings inside dicts/lists. Keys are left untouched.

        ``key`` is the name the value is stored under, when there is one. The value of a
        field named like a credential (``password``, ``api_key``, ``authorization`` ...) is
        masked whole: structured logs carry no ``password=`` text for a pattern to find.
        Integers that are card numbers are masked too.
        """
        counts = counts if counts is not None else Counter()
        sensitive = key is not None and _SENSITIVE_FIELD.search(key) is not None
        if isinstance(value, dict):
            # Everything under a credential-named key is masked, at any depth:
            # {"password": {"new": "..."}} must not escape because "new" is harmless.
            return {
                k: self.redact(v, counts, key=key if sensitive else str(k))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact(v, counts, key=key) for v in value]
        if isinstance(value, bool) or value is None:
            return value
        if sensitive and isinstance(value, (str, int, float)):
            if value == "" or (isinstance(value, str) and _REDACTED_ONLY.fullmatch(value)):
                return value
            counts["credential_assignment"] += 1
            return "[REDACTED:credential_assignment]"
        if isinstance(value, str):
            redacted, c = self.redact_text(value)
            counts.update(c)
            return redacted
        if isinstance(value, int) and _is_card(str(abs(value))):
            counts["credit_card"] += 1
            return "[REDACTED:credit_card]"
        return value


DEFAULT_REDACTOR = Redactor()


def redact_text(text: str) -> str:
    return DEFAULT_REDACTOR.redact_text(text)[0]
