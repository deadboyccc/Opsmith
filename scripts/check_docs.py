"""Check the Markdown documentation.

* Every relative link must point at a file that exists, and every ``#anchor`` at a heading
  that exists in the target file.
* Every test the security pages cite as proof of a guarantee must exist in ``tests/``.

    python scripts/check_docs.py

Exits non-zero and lists the problems if any are found. Runs in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules", "opsmith-demo", "data", ".pytest_cache"}
# The original specification and the outside review are kept as they were written.
SKIP_FILES = {"docs/development/independent-project-review.md"}
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|\!\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not (set(path.relative_to(ROOT).parts) & SKIP_DIRS)
        and path.relative_to(ROOT).as_posix() not in SKIP_FILES
    )


def strip_code(text: str) -> list[str]:
    """Lines outside fenced code blocks, with inline code spans blanked."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(re.sub(r"`[^`]*`", "``", line))
    return out


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # link text only
    text = re.sub(r"[`*_]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            base = slug(match.group(2))
            name, n = base, 0
            while name in found:  # GitHub numbers repeated headings
                n += 1
                name = f"{base}-{n}"
            found.add(name)
    return found


# Pages whose claims are backed by named tests.
CITES_TESTS = ("docs/guide/security.md", "docs/development/review-response.md")


def missing_tests() -> list[str]:
    defined: set[str] = set()
    names: set[str] = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        names.add(path.name)
        defined |= set(re.findall(r"^def (test_\w+)", path.read_text(encoding="utf-8"), re.M))
    problems = []
    for rel in CITES_TESTS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for name in sorted(set(re.findall(r"`(test_[a-z0-9_]+)`", text))):
            if name not in defined:
                problems.append(f"{rel}: cites a test that does not exist: {name}")
        for name in sorted(set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", text))):
            if name not in names:
                problems.append(f"{rel}: cites a test file that does not exist: {name}")
    return problems


def main() -> int:
    problems: list[str] = missing_tests()
    cache: dict[Path, set[str]] = {}
    files = markdown_files()
    for path in files:
        rel = path.relative_to(ROOT)
        for line in strip_code(path.read_text(encoding="utf-8")):
            for match in LINK.finditer(line):
                target = match.group(1) or match.group(2)
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # http:, mailto:, ...
                    continue
                file_part, _, anchor = target.partition("#")
                dest = path if not file_part else (path.parent / file_part).resolve()
                if not dest.exists():
                    problems.append(f"{rel}: link to missing file: {target}")
                    continue
                if anchor and dest.suffix == ".md":
                    if dest not in cache:
                        cache[dest] = anchors(dest)
                    if anchor.lower() not in cache[dest]:
                        problems.append(f"{rel}: no heading for anchor: {target}")
    for problem in problems:
        print(problem)
    print(f"checked {len(files)} files: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
