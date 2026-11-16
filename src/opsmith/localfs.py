"""Confinement for components that read the worker's own filesystem.

File connectors and the ``local`` SCM provider take a path from project configuration, which
a tenant admin controls. Left alone, such a path could point at another tenant's stored
signals, the database file or anything else the worker can read. Every such path must
therefore resolve inside one operator-chosen directory (``OPSMITH_LOCAL_ROOT``) and outside
the platform's own data directory. With no root configured, these components are disabled.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


class LocalAccessError(ValueError):
    """A configured path points outside the directory local components may use."""


def confine(path: Path | str, root: Path | None, *, what: str, deny: Iterable[Path] = ()) -> Path:
    """Return ``path`` resolved (symlinks followed), or raise if it is not allowed."""
    if root is None:
        raise LocalAccessError(
            f"{what} reads the worker's filesystem, which is disabled; set OPSMITH_LOCAL_ROOT "
            "to the directory it may use"
        )
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise LocalAccessError(f"{what}: {str(path)!r} is outside OPSMITH_LOCAL_ROOT")
    for denied in deny:
        if resolved.is_relative_to(denied.resolve()):
            raise LocalAccessError(f"{what}: {str(path)!r} is inside the platform data directory")
    return resolved
