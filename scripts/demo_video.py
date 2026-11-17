"""Record a demonstration video of the offline demo, with a human approving in the UI.

The demo runs in a background thread and is held at its approval step. The browser logs in as
the demo's reviewer, reads the top task and approves it through the review form. The demo then
executes the task in the sandbox, opens the pull request, merges it as a human would and
verifies the fix, and the recording shows the result. Captions explain each step.

Everything shown is synthetic: the demo generates its own repository, logs and metrics.

    uvx --from playwright playwright install chromium         # once
    uv run --with playwright --with imageio-ffmpeg python scripts/demo_video.py

Writes ``docs/images/demo.mp4`` (H.264) and ``docs/images/demo.gif`` (a short, small preview
for the README).

**Narration.** Each beat has a spoken line (``NARRATION`` below, one per caption). When the
recordings exist in ``docs/images/narration/<key>.mp3``, every beat is held until its line has
been spoken, the moment each line starts is noted, and the lines are mixed into the MP4 at
those moments, with the loudness normalized for speech. Without them the video is recorded
silent, as before. The recordings were made with ElevenLabs (voice "Tommy", model
``eleven_v4``); to change a line, replace its text here and its recording there, and record
the video again. The GIF is always silent.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import uvicorn
from playwright.sync_api import Page, sync_playwright
from sqlalchemy import select
from ui_screenshots import _hide_host_paths  # same directory as this script

from opsmith import demo
from opsmith.api.app import create_app
from opsmith.config import Settings
from opsmith.models import Review, Task
from opsmith.platform import Platform
from opsmith.review import ReviewService

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
NARRATION_DIR = OUT / "narration"
# What the narrator says on each beat. The captions on screen say the same thing in fewer
# words, so the video still works with the sound off.
NARRATION: dict[str, str] = {
    "intro": "Here's Opsmith, start to finish, in about a minute.",
    "login": (
        "This is the offline demo. Everything you're about to see is synthetic: a generated"
        " repository, made-up logs, and made-up metrics."
    ),
    "ranked": (
        "Opsmith has already pulled in the logs and metrics, run its analysis, audited the"
        " code, and ranked everything it found as tasks."
    ),
    "queue": (
        "And here's the part that matters most. Nothing touches your code until a person"
        " approves it."
    ),
    "task": (
        "Each task ties what production saw to a specific line of code: the problem, the fix"
        " it proposes, and how we'll know it worked."
    ),
    "provenance": (
        "Scroll down, and you can trace it all the way back, from the raw log lines to the"
        " finding in the code."
    ),
    "approve": (
        "Our reviewer reads it, leaves a quick note, and approves. That approval covers"
        " exactly what they saw, and nothing more."
    ),
    "execute": (
        "Now a sandboxed agent writes the fix, runs the tests, and opens a pull request."
        " Merging it, though, is always up to a human. Opsmith can't merge."
    ),
    "verified": (
        "After the merge, fresh telemetry comes in, and the errors are gone. So the task is"
        " marked verified."
    ),
    "record": (
        "And every step is on the record: the run, the review, the findings, and the"
        " analysis behind them."
    ),
    "executions": "Executions, checks, and pull requests, all in one place.",
    "cta": (
        "Want to try it yourself? Just run the demo. It's free, it runs offline, and it takes"
        " about a minute."
    ),
}
PAUSE_AFTER_LINE = 0.45  # seconds of silence after a line before the next beat
BANNER = ROOT / "docs" / "images" / "brand" / "social-preview.html"
SIZE = {"width": 1280, "height": 720}

CAPTION_JS = """
(text) => {
  let el = document.getElementById('__caption');
  if (!el) {
    el = document.createElement('div');
    el.id = '__caption';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);'
      + 'max-width:980px;padding:14px 22px;border-radius:14px;background:rgba(11,17,32,.92);'
      + 'color:#F8FAFC;font:600 20px/1.4 Inter,system-ui,sans-serif;text-align:center;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35);border:1px solid rgba(249,115,22,.55);'
      + 'z-index:2147483647;transition:opacity .25s';
    document.body.appendChild(el);
  }
  el.textContent = text;
}
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HeldDemo:
    """Runs the demo in a thread and holds it at the review step until the UI has approved."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.keys: dict[str, str] = {}
        self.task_id = ""
        self.at_review = threading.Event()
        self.approved = threading.Event()
        self.result: demo.DemoResult | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        create_user = demo.create_user
        review = ReviewService.review
        held = self

        def capture_user(session: Any, tenant: Any, name: str, role: str) -> Any:
            user, key = create_user(session, tenant, name, role)
            held.keys[role] = key
            return user, key

        def hold_review(self: ReviewService, session: Any, task: Task, *args: Any) -> Any:
            if threading.current_thread() is not held.thread:
                return review(self, session, task, *args)  # the UI's own review
            held.task_id = task.id
            session.commit()  # release the demo's read transaction so the UI can write
            held.at_review.set()
            if not held.approved.wait(timeout=300):
                raise TimeoutError("nobody approved the task in the UI")
            return session.scalars(
                select(Review).where(Review.task_id == task.id).order_by(Review.created_at.desc())
            ).first()

        demo.create_user = capture_user  # type: ignore[assignment]
        ReviewService.review = hold_review  # type: ignore[method-assign]
        try:
            self.result = demo.run_demo(self.base, verbose=False)
        except BaseException as exc:
            self.error = exc
            self.at_review.set()
        finally:
            demo.create_user = create_user  # type: ignore[assignment]
            ReviewService.review = review  # type: ignore[method-assign]


class Narrator:
    """Knows how long each recorded line is, and notes when each one starts in the video."""

    def __init__(self) -> None:
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.lengths: dict[str, float] = {}
        for key in NARRATION:
            path = NARRATION_DIR / f"{key}.mp3"
            if path.is_file():
                self.lengths[key] = self._length(path)
        self.enabled = len(self.lengths) == len(NARRATION)
        self.started = 0.0
        self.cues: list[tuple[str, float]] = []

    def _length(self, path: Path) -> float:
        probe = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        found = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
        if found is None:
            raise RuntimeError(f"cannot read the length of {path}")
        hours, minutes, seconds = found.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def start(self) -> None:
        """The video starts when its page is created: line start times count from here."""
        self.started = time.monotonic()

    def hold(self, key: str | None, seconds: float, overlap: float = 0.0) -> float:
        """How long a beat stays on screen: its own time, or long enough for its line.
        ``overlap`` is how much of what follows (typing, a click) may run under the line."""
        if not self.enabled or key is None:
            return seconds
        self.cues.append((key, time.monotonic() - self.started))
        return max(seconds, self.lengths[key] + PAUSE_AFTER_LINE - overlap)


NARRATOR = Narrator()


def caption(
    page: Page, text: str, hold: float, say: str | None = None, overlap: float = 0.0
) -> None:
    page.evaluate(CAPTION_JS, text)
    page.wait_for_timeout(int(NARRATOR.hold(say, hold, overlap) * 1000))


def scroll(page: Page, to: int, seconds: float) -> None:
    page.evaluate(
        "([to, ms]) => new Promise(done => { const from = scrollY, t0 = performance.now();"
        " const step = now => { const k = Math.min(1, (now - t0) / ms);"
        " scrollTo(0, from + (to - from) * (0.5 - Math.cos(Math.PI * k) / 2));"
        " k < 1 ? requestAnimationFrame(step) : done(); }; requestAnimationFrame(step); })",
        [to, int(seconds * 1000)],
    )


def record(held: HeldDemo, platform: Platform, root: str, video_dir: Path) -> Path:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport=SIZE, record_video_dir=str(video_dir), record_video_size=SIZE
        )
        page = context.new_page()
        NARRATOR.start()

        page.goto(BANNER.as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(int(NARRATOR.hold("intro", 3.5) * 1000))

        page.goto(f"{root}/ui/login")
        caption(
            page,
            "The offline demo: a generated repository, synthetic logs and metrics.",
            2.5,
            say="login",
        )
        page.click("input[name=api_key]")
        page.keyboard.type(held.keys["reviewer"], delay=8)
        page.click("button[type=submit]")
        page.wait_for_url(f"{root}/ui")
        caption(
            page,
            "Opsmith ingested logs and metrics, ran chained analysis and a code audit,"
            " and ranked what it found as tasks.",
            4.5,
            say="ranked",
        )

        page.goto(f"{root}/ui/review")
        caption(page, "Nothing changes code until a human approves.", 3.5, say="queue")

        page.goto(f"{root}/ui/tasks/{held.task_id}")
        caption(
            page,
            "Each task ties production evidence to the code: problem, approach, acceptance"
            " criteria.",
            4,
            say="task",
        )
        scroll(page, 1200, 3.5)
        caption(
            page,
            "Provenance: from raw log lines and metrics to the finding in the code.",
            3,
            say="provenance",
        )
        form = page.locator("form[action$='/review']")
        scroll(page, int(form.evaluate("el => el.getBoundingClientRect().top + scrollY - 140")), 2)
        caption(
            page,
            "A reviewer approves. The approval covers exactly what they saw.",
            1.5,
            say="approve",
            overlap=2.5,  # the note is typed while the line is spoken
        )
        form.locator("textarea[name=comment]").click()
        page.keyboard.type("Runtime timeouts match the audit finding. Ship it.", delay=30)
        page.wait_for_timeout(600)
        form.locator("button[type=submit]").click()
        page.wait_for_load_state("networkidle")
        held.approved.set()
        caption(
            page,
            "A sandboxed agent implements the fix, runs the tests and opens a pull request."
            " A human merges it: Opsmith cannot merge.",
            5,
            say="execute",
        )
        held.thread.join(timeout=300)
        if held.error is not None or held.result is None:
            raise RuntimeError(f"the demo failed: {held.error!r}")
        with platform.db.session() as s:
            _hide_host_paths(s)  # show "python", not this machine's interpreter path

        page.goto(f"{root}/ui/tasks/{held.task_id}")
        scroll(page, 0, 0.1)
        caption(
            page,
            "After the merge, fresh telemetry confirms the problem is gone: verified.",
            4,
            say="verified",
        )
        scroll(page, 900, 3)
        caption(
            page,
            "Every step is recorded: execution, review, findings, analysis runs.",
            3.5,
            say="record",
        )

        page.goto(f"{root}/ui/executions")
        caption(page, "Executions, checks and pull requests at a glance.", 3, say="executions")

        page.goto(BANNER.as_uri())
        page.wait_for_load_state("networkidle")
        caption(page, "Try it yourself:  uv run opsmith demo", 4, say="cta")

        video = page.video
        context.close()
        browser.close()
        assert video is not None
        return Path(video.path())


def narration_args() -> list[str]:
    """ffmpeg inputs and filter for the narration: each line delayed to the moment its beat
    started, all mixed, then normalized to the loudness usual for spoken video (-16 LUFS)."""
    inputs: list[str] = []
    filters: list[str] = []
    for index, (key, start) in enumerate(NARRATOR.cues, start=1):
        inputs += ["-i", str(NARRATION_DIR / f"{key}.mp3")]
        delay = max(int(start * 1000) + 150, 0)  # the caption is drawn just before the line
        filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
    mixed = "".join(f"[a{i}]" for i in range(1, len(NARRATOR.cues) + 1))
    filters.append(
        f"{mixed}amix=inputs={len(NARRATOR.cues)}:normalize=0,"
        "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[voice]"
    )
    return [*inputs, "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[voice]",
            "-c:a", "aac", "-b:a", "128k"]  # fmt: skip


def encode(webm: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mp4, gif = OUT / "demo.mp4", OUT / "demo.gif"
    audio = narration_args() if NARRATOR.enabled else []
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), *audio, "-c:v", "libx264",
         "-preset", "slow", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(mp4)],
        check=True,
    )  # fmt: skip
    # The README preview: without the title card, 1.5x faster, 720 px, 7 fps, 64 colours, and
    # only the changed part of each frame stored. About 3 MB instead of 7.5.
    palette = webm.with_suffix(".png")
    scale = "setpts=PTS/1.5,fps=7,scale=720:-1:flags=lanczos"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-ss", "3.5", "-i", str(webm),
         "-vf", f"{scale},palettegen=max_colors=64:stats_mode=diff", str(palette)],
        check=True,
    )  # fmt: skip
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-ss", "3.5", "-i", str(webm), "-i", str(palette),
         "-lavfi",
         f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
         str(gif)],
        check=True,
    )  # fmt: skip
    for path in (mp4, gif):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1e6:.1f} MB")


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="opsmith-video-"))
    held = HeldDemo(base)
    held.thread.start()
    if not held.at_review.wait(timeout=300) or held.error is not None:
        print(f"the demo did not reach its review step: {held.error!r}", file=sys.stderr)
        return 1

    platform = Platform(
        Settings(data_dir=base / "data", log_json=False, sandbox_backend="local", local_root=base)
    )
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(platform), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    video_dir = base / "video"
    if NARRATOR.enabled:
        print(f"narrated: {len(NARRATOR.lengths)} lines from {NARRATION_DIR.relative_to(ROOT)}")
    else:
        missing = ", ".join(sorted(set(NARRATION) - set(NARRATOR.lengths)))
        print(f"recording silent: no narration for {missing}")
    try:
        webm = record(held, platform, f"http://127.0.0.1:{port}", video_dir)
        if held.result is None or held.result.final_status != "verified":
            print("the demo did not end verified", file=sys.stderr)
            return 1
        encode(webm)
    finally:
        server.should_exit = True
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
