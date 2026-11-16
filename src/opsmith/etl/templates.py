"""Log template extraction (a compact Drain-style clusterer) and code-location extraction.

Variable tokens (numbers, ids, hashes, paths-with-numbers, quoted values) are masked, then
messages are clustered by token count + leading token with a similarity threshold. The
fingerprint of a template is stable across windows, which is what lets leads and tasks
follow the same signal over time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[REDACTED:[a-z_0-9]+\]"), "<redacted>"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
        "<ts>",
    ),
    (re.compile(r"(?<=[=:])\s*\"[^\"]*\""), "<str>"),
    (re.compile(r"(?<=[=:])\s*'[^']*'"), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|us|ns|kb|mb|gb|%)?\b", re.IGNORECASE), "<num>"),
)
_WILDCARD = "<*>"
MAX_LOCATION_PATH_CHARS = 500  # the size of the columns a path ends up in
# Line numbers are at most seven digits: ``int()`` refuses a number of several thousand
# digits, and no source file has ten million lines.
_LOCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d{1,7})'),  # Python traceback
    re.compile(r"\bat [\w.$]+\((?P<path>[\w./-]+\.(?:java|kt|scala)):(?P<line>\d{1,7})\)"),  # JVM
    # Generic ``dir/file.ext:line``. The look-behind pins a match to the start of a path and
    # the repetitions are bounded, so a long token costs time linear in its length.
    re.compile(
        r"(?<![\w./-])/?(?P<path>(?:[\w.-]{1,255}/){1,64}[\w.-]{1,255}"
        r"\.(?:py|js|ts|tsx|jsx|go|rb|rs|php|cs)):(?P<line>\d{1,7})"
    ),
)


def mask(message: str) -> str:
    text = message.strip()
    for pattern, token in _MASKS:
        text = pattern.sub(token, text)
    return text


def tokenize(message: str) -> list[str]:
    return mask(message).split()


def fingerprint_of(service: str, template: str) -> str:
    raw = f"{service}|{template}".encode(errors="replace")  # stored text may hold a surrogate
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


def _repo_relative(path: str) -> str:
    """Drop leading ``./`` and ``../`` segments and the root of an absolute path.

    Only whole segments are removed, so ``.github/x.py`` keeps its dot."""
    while path.startswith(("./", "../")):
        path = path.split("/", 1)[1]
    return path.lstrip("/")


def extract_locations(text: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for pattern in _LOCATION_PATTERNS:
        for match in pattern.finditer(text):
            loc = (_repo_relative(match.group("path")), int(match.group("line")))
            if len(loc[0]) > MAX_LOCATION_PATH_CHARS:
                continue  # not a path anyone can open, and it would fill the bundle
            if loc not in found:
                found.append(loc)
    return found


@dataclass
class Cluster:
    tokens: list[str]
    count: int = 0
    member_indices: list[int] = field(default_factory=list)

    @property
    def template(self) -> str:
        return " ".join(self.tokens)


class TemplateMiner:
    def __init__(self, similarity: float = 0.5, max_clusters_per_leaf: int = 200) -> None:
        self.similarity = similarity
        self.max_clusters = max_clusters_per_leaf
        self._leaves: dict[tuple[int, str], list[Cluster]] = {}

    def add(self, message: str, index: int) -> Cluster:
        tokens = tokenize(message) or ["<empty>"]
        head = tokens[0] if not tokens[0].startswith("<") else _WILDCARD
        leaf = self._leaves.setdefault((len(tokens), head), [])
        best, best_sim = None, -1.0
        for cluster in leaf:
            sim = self._sim(cluster.tokens, tokens)
            if sim > best_sim:
                best, best_sim = cluster, sim
        if best is None or best_sim < self.similarity:
            if len(leaf) >= self.max_clusters:
                best = leaf[-1]
            else:
                best = Cluster(tokens=list(tokens))
                leaf.append(best)
        if best.tokens != tokens:
            best.tokens = [
                a if a == b else _WILDCARD for a, b in zip(best.tokens, tokens, strict=True)
            ]
        best.count += 1
        best.member_indices.append(index)
        return best

    @staticmethod
    def _sim(template: list[str], tokens: list[str]) -> float:
        same = sum(1 for a, b in zip(template, tokens, strict=True) if a in (b, _WILDCARD))
        return same / len(tokens)

    def clusters(self) -> list[Cluster]:
        out = [c for leaf in self._leaves.values() for c in leaf]
        return sorted(out, key=lambda c: c.count, reverse=True)
