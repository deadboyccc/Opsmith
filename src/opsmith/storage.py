"""Object storage for raw/derived artifacts.

The MVP ships a local-filesystem backend; the interface is deliberately tiny so an S3/GCS
backend is a drop-in (see ARCHITECTURE_DECISIONS.md, ADR-005).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol


class ObjectStore(Protocol):
    def put_jsonl(self, key: str, records: Iterable[dict[str, Any]]) -> int: ...
    def iter_jsonl(self, key: str) -> Iterator[dict[str, Any]]: ...
    def put_text(self, key: str, text: str) -> None: ...
    def get_text(self, key: str) -> str: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"object key escapes the store: {key!r}")
        return path

    def put_jsonl(self, key: str, records: Iterable[dict[str, Any]]) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
                    count += 1
                fh.flush()
                os.fsync(fh.fileno())  # on disk before it is published under its name
            tmp.replace(path)  # atomic publish
        except BaseException:
            # A write that failed half-way must not leave its partial file in the store:
            # nothing would ever clean it up.
            tmp.unlink(missing_ok=True)
            raise
        return count

    def iter_jsonl(self, key: str) -> Iterator[dict[str, Any]]:
        with self._path(key).open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def put_text(self, key: str, text: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def get_text(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        """Remove an object if it exists (used to undo a write whose transaction rolled back)."""
        self._path(key).unlink(missing_ok=True)
