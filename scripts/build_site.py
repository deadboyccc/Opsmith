"""Assemble the documentation site's source and, with ``--build``, build it with MkDocs.

The site publishes every Markdown page in the repository (README, the root guides, everything
under ``docs/`` and ``reports/``) without moving or editing any of them: they are copied into
``build/site-src/`` with their relative layout kept, so the links between them keep working,
and MkDocs builds from there. The copies are adjusted for the web only:

* the root ``README.md`` becomes ``overview.md`` (``index.md`` is the landing page in
  ``site/``, and MkDocs would otherwise drop one of the two);
* a link to a file that is not part of the site (``LICENSE``, a script, an example
  configuration) points at that file on GitHub instead;
* images below the fold load lazily;
* the hero image gets a compressed WebP copy next to the original, which is not touched.

    uv run --group docs python scripts/build_site.py           # assemble build/site-src
    uv run --group docs python scripts/build_site.py --build   # ... and build build/site
    uv run --group docs python scripts/build_site.py --serve   # ... and serve with reload

Runs in CI (``.github/workflows/docs.yml``) with ``mkdocs build --strict``, so a broken link,
anchor or missing page fails the build.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_EXTRAS = ROOT / "site"  # landing page, styles and theme overrides
STAGE = ROOT / "build" / "site-src"
REPO_URL = "https://github.com/deadboyccc/Opsmith"
ROOT_PAGES = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "ARCHITECTURE_DECISIONS.md",
)
RENAMED = {"README.md": "overview.md"}
COPIED_DIRS = ("docs", "reports")
HERO_IMAGE = "docs/images/brand/social-preview.png"

# The target part of any Markdown link or image, ``](target "title")``: this also finds the
# outer link of a badge, ``[![alt](image)](target)``, whose text holds brackets.
_MD_TARGET = re.compile(r"\]\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)")
_MD_IMAGE = re.compile(r"(\!\[[^\]]*\]\([^)\s]+(?:\s+\"[^\"]*\")?\))(?!\{)")
_HTML_LINK = re.compile(r"(<(?:a|img|source)\b[^>]*?\b(?:href|src)=\")([^\"]+)(\")")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _is_external(target: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I)) or target.startswith("#")


def _published(rel: str) -> bool:
    """Whether the repository path ``rel`` is part of the staged site."""
    return rel in ROOT_PAGES or rel.split("/", 1)[0] in COPIED_DIRS


def _rewrite_target(target: str, page: str) -> str:
    """The link ``target`` as written in the repository page ``page``, as the site needs it."""
    if _is_external(target):
        return target
    path, _, anchor = target.partition("#")
    if not path:
        return target
    resolved = (ROOT / page).parent / path
    try:
        rel = resolved.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return target
    suffix = f"#{anchor}" if anchor else ""
    if not _published(rel):
        kind = "tree" if resolved.is_dir() else "blob"
        return f"{REPO_URL}/{kind}/main/{rel}{suffix}"
    if resolved.is_dir():  # a link to a folder of pages: its index
        return f"{path.rstrip('/')}/README.md{suffix}"
    if rel in RENAMED:
        depth = page.count("/")
        return "../" * depth + RENAMED[rel] + suffix
    return target


def _html_target(target: str, page: str) -> str:
    """A link in raw HTML (``<img src>``, ``<a href>``). MkDocs serves ``guide/x.md`` as
    ``guide/x/``, one directory deeper than the file, and adjusts Markdown links for that but
    not raw HTML; so a relative one gets the extra ``../`` here. ``index.md`` and
    ``README.md`` are served as their own directory and need none."""
    target = _rewrite_target(target, page)
    if _is_external(target) or target.startswith("/"):
        return target
    served = RENAMED.get(page, page)
    if Path(served).name in {"index.md", "README.md"}:
        return target
    path, sep, anchor = target.partition("#")
    if path.endswith(".md"):  # a page: its directory URL
        path = path.removesuffix("README.md").removesuffix("index.md")
        path = path.removesuffix(".md") + "/" if path.endswith(".md") else path
    return f"../{path}{sep}{anchor}"


def _adjust(text: str, page: str) -> str:
    out: list[str] = []
    fenced = False
    for raw in text.splitlines(keepends=True):
        if _FENCE.match(raw):
            fenced = not fenced
        if fenced or _FENCE.match(raw):
            out.append(raw)
            continue
        line = _MD_TARGET.sub(lambda m: f"]({_rewrite_target(m.group(1), page)}{m.group(2)})", raw)
        line = _HTML_LINK.sub(
            lambda m: m.group(1) + _html_target(m.group(2), page) + m.group(3), line
        )
        # Screenshots and the demo GIF are below the fold: load them when they are reached.
        # The hero image is not, and loads at once.
        line = _MD_IMAGE.sub(r'\1{ loading="lazy" }', line)
        if Path(HERO_IMAGE).name not in line:
            line = re.sub(
                r"<img (?![^>]*\bloading=)", '<img loading="lazy" decoding="async" ', line
            )
        out.append(line)
    return "".join(out)


def _webp(source: Path, dest: Path, width: int) -> None:
    from PIL import Image  # noqa: PLC0415 - only the site build needs Pillow

    with Image.open(source) as image:
        height = round(image.height * width / image.width)
        image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).save(
            dest, "WEBP", quality=82, method=6
        )


def assemble() -> Path:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for name in COPIED_DIRS:
        shutil.copytree(ROOT / name, STAGE / name)
    for page in [
        *ROOT_PAGES,
        *(p.relative_to(ROOT).as_posix() for d in COPIED_DIRS for p in (ROOT / d).rglob("*.md")),
    ]:
        text = (ROOT / page).read_text(encoding="utf-8")
        target = STAGE / RENAMED.get(page, page)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_adjust(text, page), encoding="utf-8")
    shutil.copytree(SITE_EXTRAS, STAGE, dirs_exist_ok=True)
    # The hero image, compressed, at two widths. The original stays as it is, and is the
    # fallback for a browser without WebP.
    hero = ROOT / HERO_IMAGE
    brand = STAGE / "assets" / "brand"
    brand.mkdir(parents=True, exist_ok=True)
    for width in (640, 1280):
        _webp(hero, brand / f"social-preview-{width}.webp", width)
    return STAGE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--build", action="store_true", help="build the site (strict)")
    group.add_argument("--serve", action="store_true", help="serve it with live reload")
    args = parser.parse_args()
    stage = assemble()
    print(f"assembled {sum(1 for _ in stage.rglob('*.md'))} pages in {stage.relative_to(ROOT)}")
    if args.build:
        return subprocess.call([sys.executable, "-m", "mkdocs", "build", "--strict"], cwd=ROOT)
    if args.serve:
        return subprocess.call([sys.executable, "-m", "mkdocs", "serve"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
