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

The video is silent; ``scripts/soundtrack.py`` adds its music. Run it afterwards.
"""

from __future__ import annotations

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


def caption(page: Page, text: str, hold: float) -> None:
    page.evaluate(CAPTION_JS, text)
    page.wait_for_timeout(int(hold * 1000))


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

        page.goto(BANNER.as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3500)

        page.goto(f"{root}/ui/login")
        caption(page, "The offline demo: a generated repository, synthetic logs and metrics.", 2.5)
        page.click("input[name=api_key]")
        page.keyboard.type(held.keys["reviewer"], delay=8)
        page.click("button[type=submit]")
        page.wait_for_url(f"{root}/ui")
        caption(
            page,
            "Opsmith ingested logs and metrics, ran chained analysis and a code audit,"
            " and ranked what it found as tasks.",
            4.5,
        )

        page.goto(f"{root}/ui/review")
        caption(page, "Nothing changes code until a human approves.", 3.5)

        page.goto(f"{root}/ui/tasks/{held.task_id}")
        caption(
            page,
            "Each task ties production evidence to the code: problem, approach, acceptance"
            " criteria.",
            4,
        )
        scroll(page, 1200, 3.5)
        caption(page, "Provenance: from raw log lines and metrics to the finding in the code.", 3)
        form = page.locator("form[action$='/review']")
        scroll(page, int(form.evaluate("el => el.getBoundingClientRect().top + scrollY - 140")), 2)
        caption(page, "A reviewer approves. The approval covers exactly what they saw.", 1.5)
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
        )
        held.thread.join(timeout=300)
        if held.error is not None or held.result is None:
            raise RuntimeError(f"the demo failed: {held.error!r}")
        with platform.db.session() as s:
            _hide_host_paths(s)  # show "python", not this machine's interpreter path

        page.goto(f"{root}/ui/tasks/{held.task_id}")
        scroll(page, 0, 0.1)
        caption(page, "After the merge, fresh telemetry confirms the problem is gone: verified.", 4)
        scroll(page, 900, 3)
        caption(page, "Every step is recorded: execution, review, findings, analysis runs.", 3.5)

        page.goto(f"{root}/ui/executions")
        caption(page, "Executions, checks and pull requests at a glance.", 3)

        page.goto(BANNER.as_uri())
        page.wait_for_load_state("networkidle")
        caption(page, "Try it yourself:  uv run opsmith demo", 4)

        video = page.video
        context.close()
        browser.close()
        assert video is not None
        return Path(video.path())


def encode(webm: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mp4, gif = OUT / "demo.mp4", OUT / "demo.gif"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264",
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
