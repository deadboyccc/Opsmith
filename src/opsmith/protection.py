"""Protected-path classification (spec 8): ``elevated`` paths need an admin approval with an
explicit elevated acknowledgement; ``excluded`` paths are never changed autonomously."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable

from opsmith.schemas import ProtectedPath

_RANK = {"none": 0, "elevated": 1, "excluded": 2}


def normalize_pattern(pattern: str) -> str:
    """A pattern relative to the repository root, however it was written: ``/infra/**``,
    ``./infra/**`` and ``infra/`` all mean the ``infra`` directory at the root."""
    pattern = pattern.strip()
    while pattern.startswith(("./", "/")):
        pattern = pattern[2:] if pattern.startswith("./") else pattern[1:]
    return pattern.rstrip("/")


def path_matches(path: str, pattern: str) -> bool:
    """Whether ``pattern`` covers ``path``. A pattern covers what it matches as a glob and,
    like a ``.gitignore`` entry, everything below a directory it names: ``infra`` covers
    ``infra/main.tf``. The patterns guard paths, so a spelling that looks like it protects a
    directory must not silently match nothing."""
    path = path.removeprefix("./")
    pattern = normalize_pattern(pattern)
    if not pattern:
        return False
    candidates = [pattern, f"{pattern}/*"]  # ``*`` also matches ``/``: everything below
    if pattern.startswith("**/"):
        candidates += [pattern[3:], f"{pattern[3:]}/*"]
    return any(fnmatch.fnmatch(path, candidate) for candidate in candidates)


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
