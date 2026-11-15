"""Protected-path classification (spec 8): ``elevated`` paths need an admin approval with an
explicit elevated acknowledgement; ``excluded`` paths are never changed autonomously."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable

from opsmith.schemas import ProtectedPath

_RANK = {"none": 0, "elevated": 1, "excluded": 2}


def path_matches(path: str, pattern: str) -> bool:
    path = path.removeprefix("./")
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def classify(paths: Iterable[str], rules: Iterable[ProtectedPath]) -> str:
    level = "none"
    rules = list(rules)
    for path in paths:
        for rule in rules:
            if path_matches(path, rule.pattern) and _RANK[rule.mode] > _RANK[level]:
                level = rule.mode
    return level


def protected_hits(paths: Iterable[str], rules: Iterable[ProtectedPath]) -> list[tuple[str, str]]:
    rules = list(rules)
    return [(p, r.mode) for p in paths for r in rules if path_matches(p, r.pattern)]
