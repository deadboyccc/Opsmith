"""Check the Markdown documentation.

* Every relative link must point at a file that exists, and every ``#anchor`` at a heading
  that exists in the target file.
* Every test the security pages cite as proof of a guarantee must exist in ``tests/``.
* Everything the code offers must be documented: environment variables, project settings,
  API endpoints, notification events, connector types, analyzers, task and lead states, model
  providers, coding agents, CLI commands and job kinds.

    uv run python scripts/check_docs.py

Exits non-zero and lists the problems if any are found. Runs in CI.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "opsmith-demo",
    "data",
    ".pytest_cache",
    "build",  # the assembled documentation site
    "site",  # its landing page; the site build checks those links (mkdocs --strict)
}
# The original specification and the outside review are kept as they were written.
SKIP_FILES = {"docs/development/independent-project-review.md"}
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|\!\[[^\]]*\]\(([^)\s]+)\)")
HTML_SRC = re.compile(r"<(?:img|a)\b[^>]*?\b(?:src|href)=\"([^\"]+)\"")
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


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def undocumented() -> tuple[list[str], dict[str, int]]:
    """Compare what the code offers with what the documentation describes.

    Each check takes its list from the running code (settings models, the API router, the
    connector registry, the CLI parser, ...) and requires every item to be named in the page
    that documents that area. A new setting, endpoint or event without documentation fails
    here. Returns the problems and how many items each check covered; a check that covers
    nothing is itself a problem, because it would pass without checking anything.
    """
    from opsmith import audit_log, cli  # noqa: PLC0415 - only needed for this check
    from opsmith.api import routes  # noqa: PLC0415
    from opsmith.audits import AUDIT_TYPES, registered  # noqa: PLC0415
    from opsmith.config import Settings  # noqa: PLC0415
    from opsmith.connectors import available_types  # noqa: PLC0415
    from opsmith.llm import default_gateway  # noqa: PLC0415
    from opsmith.platform import Platform, default_agent_factories  # noqa: PLC0415
    from opsmith.schemas import Budgets, LLMSettings, ProjectSettings, StageModel  # noqa: PLC0415

    problems: list[str] = []
    covered: dict[str, int] = {}

    def require(items: set[str], what: str, *pages: str) -> None:
        covered[what] = covered.get(what, 0) + len(items)
        text = "".join(_read(page) for page in pages)
        for item in sorted(items):
            if item not in text:
                problems.append(f"{pages[0]}: {what} is not documented: {item}")

    source = "".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "src" / "opsmith").rglob("*.py")
    )

    # Environment variables the processes read: documented, and in the example file.
    env_vars = {f"OPSMITH_{n}" for n in re.findall(r'_env(?:_list)?\(\s*"([A-Z_]+)"', source)}
    env_vars |= set(re.findall(r'environ(?:\.get)?[\[(]\s*"(OPSMITH_[A-Z_]+)"', source))
    require(env_vars, "environment variable", "docs/guide/deployment.md")
    require(env_vars, "example environment variable", ".env.example")
    for name in sorted(set(re.findall(r"`(OPSMITH_[A-Z_]+)`", _read("docs/guide/deployment.md")))):
        if name not in env_vars:
            problems.append(f"docs/guide/deployment.md: documents a variable nothing reads: {name}")

    # Project settings, wherever they are explained.
    for model in (ProjectSettings, Budgets, StageModel, LLMSettings):
        require(
            set(model.model_fields),
            "project setting",
            "docs/guide/configuration.md",
            "docs/guide/models.md",
        )

    # REST API: every route must be findable by its method and its last path segment.
    api = _read("docs/guide/api.md")
    endpoints = 0
    for route in routes.router.routes:
        path = str(getattr(route, "path", ""))
        segments = path.rstrip("/").split("/")
        tail = "/" + segments[-1]
        if segments[-1].startswith("{"):  # /projects/{project_id} is written /projects/{id}
            tail = f"/{segments[-2]}/{{id}}"
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            endpoints += 1
            if not re.search(rf"`{method} [^`]*{re.escape(tail)}[`?]", api) and not (
                method in api and f"`{tail}`" in api
            ):
                problems.append(f"docs/guide/api.md: endpoint is not documented: {method} {path}")
    covered["API endpoint"] = endpoints

    # Notification events, connector types, audit types, analyzers.
    notified = set()
    for block in source.split("notifications.notify(")[1:] + source.split("\n    notify(")[1:]:
        notified |= set(re.findall(r'"((?:task|lead|job|budget|inference)\.[a-z_]+)"', block[:400]))
    require(notified, "notification event", "docs/guide/configuration.md")
    require(set(available_types()), "connector type", "docs/guide/configuration.md")
    require(set(AUDIT_TYPES), "audit type", "docs/guide/configuration.md")
    require(set(registered()), "analyzer", "docs/guide/concepts.md")

    # Task and lead states, in the lifecycle diagrams.
    require(set(audit_log.TASK_TRANSITIONS), "task state", "docs/guide/concepts.md")
    require(set(audit_log.LEAD_TRANSITIONS), "lead state", "docs/guide/concepts.md")

    # Model providers and coding agents.
    settings = Settings(sandbox_backend="local")
    gateway = default_gateway(settings)
    require({f"`{name}`" for name in gateway.providers}, "model provider", "docs/guide/models.md")
    agents = default_agent_factories(settings, gateway)
    require({f"`{name}`" for name in agents}, "coding agent", "docs/guide/models.md")

    # Command line and job kinds.
    commands = {
        name
        for action in cli.build_parser()._actions
        if isinstance(getattr(action, "choices", None), dict)
        for name in action.choices
    }
    readme = _read("README.md")
    covered["CLI command"] = len(commands)
    for name in sorted(commands):
        if not re.search(rf"`(?:opsmith )?{re.escape(name)}\b", readme):
            problems.append(f"README.md: command is not documented: opsmith {name}")
    with tempfile.TemporaryDirectory() as tmp:
        job_kinds = set(Platform(Settings(data_dir=Path(tmp), sandbox_backend="local")).handlers())
    require({f"`{kind}`" for kind in job_kinds}, "job kind", "docs/development/architecture.md")

    for what, count in covered.items():
        if count == 0:
            problems.append(f"check_docs: the {what!r} check found nothing to compare")
    return problems, covered


def main() -> int:
    undocumented_items, covered = undocumented()
    problems: list[str] = missing_tests() + undocumented_items
    cache: dict[Path, set[str]] = {}
    files = markdown_files()
    for path in files:
        rel = path.relative_to(ROOT)
        for line in strip_code(path.read_text(encoding="utf-8")):
            targets = [m.group(1) or m.group(2) for m in LINK.finditer(line)]
            targets += HTML_SRC.findall(line)  # <img src="..."> and <a href="...">
            for target in targets:
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
    summary = ", ".join(f"{count} {what}s" for what, count in covered.items())
    print(f"compared with the code: {summary}")
    print(f"checked {len(files)} files: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
