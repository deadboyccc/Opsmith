"""Render the vertical promo reel: 1080 x 1920, 30 fps, about 31 seconds, no sound.

Made for phones and for sharing (Instagram Reels, TikTok, YouTube Shorts, LinkedIn). It tells
the Opsmith loop in seven beats, with real screens from the offline demo (the screenshots in
``docs/images``, all synthetic data) and the mascot as it is in ``docs/images/brand``.

The storyboard is an HTML page with a timeline function, ``renderAt(seconds)``. Every frame
is rendered by setting the time and taking a screenshot, so the result is the same on every
run and on every machine, whatever its speed. The frames are piped straight into ffmpeg.

    uvx --from playwright playwright install chromium         # once
    uv run --no-project --with playwright --with imageio-ffmpeg python scripts/reel_video.py

Writes ``docs/images/reel.mp4`` (H.264, ``faststart`` for streaming) and
``docs/images/reel-poster.jpg`` (its first beat, shown before the video plays).
"""

# ruff: noqa: E501 - the storyboard below is an HTML template; its lines read best unwrapped.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"
OUT_VIDEO = IMAGES / "reel.mp4"
OUT_POSTER = IMAGES / "reel-poster.jpg"
WIDTH, HEIGHT, FPS = 1080, 1920, 30
DURATION = 31.0
POSTER_AT = 2.6  # seconds: the hook, fully on screen


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


def storyboard() -> str:
    mascot = _uri(IMAGES / "brand" / "mascot.svg")
    queue = _uri(IMAGES / "ui-review-queue.png")
    review = _uri(IMAGES / "ui-task-in-review.png")
    verified = _uri(IMAGES / "ui-task-verified.png")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@500;600;700&display=block" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; width: {WIDTH}px; height: {HEIGHT}px; overflow: hidden; }}
  body {{
    background:
      radial-gradient(700px 600px at 20% 85%, rgba(249,115,22,.20), transparent 70%),
      radial-gradient(800px 700px at 90% 10%, rgba(94,234,212,.10), transparent 70%),
      linear-gradient(160deg, #0B1120 0%, #111A33 55%, #161B3A 100%);
    font-family: Inter, system-ui, sans-serif; color: #E2E8F0; position: relative;
  }}
  .dots {{ position: absolute; inset: 0;
    background-image: radial-gradient(rgba(148,163,184,.12) 1.6px, transparent 1.7px);
    background-size: 36px 36px; }}
  .el {{ position: absolute; opacity: 0; will-change: transform, opacity; }}
  .center {{ left: 80px; right: 80px; text-align: center; }}
  h1, h2 {{ font-family: 'Space Grotesk', sans-serif; margin: 0; letter-spacing: -1.5px; }}
  h1 {{ font-size: 104px; line-height: 1.04; color: #F8FAFC; font-weight: 700; }}
  h2 {{ font-size: 76px; line-height: 1.08; color: #F8FAFC; font-weight: 700; }}
  .hot {{ background: linear-gradient(90deg,#FDBA74,#F97316); -webkit-background-clip: text;
          color: transparent; }}
  .teal {{ color: #5EEAD4; }}
  .sub {{ font-size: 40px; line-height: 1.35; color: #CBD5E1; font-weight: 500; }}
  .chip {{ display: inline-block; font-size: 38px; font-weight: 600; padding: 18px 30px;
          margin: 10px; border-radius: 999px; background: rgba(148,163,184,.12);
          border: 2px solid rgba(148,163,184,.30); color: #E2E8F0; }}
  .chip b {{ color: #5EEAD4; margin-right: 12px; }}
  .chip.no b {{ color: #FB923C; }}
  .card {{ border-radius: 28px; overflow: hidden; background: #F6F6F4;
           box-shadow: 0 40px 90px rgba(0,0,0,.55), 0 0 0 2px rgba(148,163,184,.25); }}
  .crop {{ position: relative; overflow: hidden; }}
  .crop img {{ position: absolute; left: 0; top: 0; transform-origin: 0 0; }}
  .ring {{ position: absolute; border: 6px solid #F97316; border-radius: 16px;
           box-shadow: 0 0 0 0 rgba(249,115,22,.6); }}
  .stamp {{ font-family: 'Space Grotesk', sans-serif; font-size: 64px; font-weight: 700;
            color: #052e16; background: #4ADE80; padding: 14px 40px; border-radius: 22px;
            box-shadow: 0 20px 50px rgba(74,222,128,.35); }}
  .pr {{ width: 920px; left: 80px; padding: 40px 44px; border-radius: 28px; background: #0F172A;
         border: 2px solid rgba(148,163,184,.30); box-shadow: 0 40px 90px rgba(0,0,0,.5); }}
  .pr .t {{ font-size: 42px; font-weight: 700; color: #F8FAFC; line-height: 1.25; }}
  .pr .b {{ margin-top: 18px; font: 600 30px/1.4 ui-monospace, 'JetBrains Mono', monospace;
            color: #93C5FD; }}
  .pr .row {{ margin-top: 26px; font-size: 32px; color: #CBD5E1; }}
  .pr .ok {{ color: #4ADE80; font-weight: 700; }}
  .pr .merge {{ margin-top: 30px; display: inline-block; font-size: 32px; font-weight: 700;
                padding: 14px 26px; border-radius: 14px; color: #FDBA74;
                border: 2px dashed rgba(253,186,116,.7); }}
  .url {{ font: 600 40px/1.3 ui-monospace, 'JetBrains Mono', monospace; color: #FDBA74; }}
  .bar {{ position: absolute; left: 0; bottom: 0; height: 10px;
          background: linear-gradient(90deg,#FDBA74,#F97316); }}
</style></head>
<body>
<div class="dots"></div>

<!-- 1 · hook, 0 to 3.4 s -->
<img class="el" data-t="0.15 3.4 pop" src="{mascot}" style="left:390px;top:300px;width:300px">
<h1 class="el center" data-t="0.35 3.4 up" style="top:700px">Your logs already know</h1>
<h1 class="el center" data-t="0.9 3.4 up" style="top:930px"><span class="hot">what's broken.</span></h1>
<div class="el center sub" data-t="1.7 3.4 fade" style="top:1220px">Opsmith turns them into reviewed pull requests.</div>

<!-- 2 · reads everything, ranks it, 3.4 to 8 s -->
<h2 class="el center" data-t="3.5 8 up" style="top:260px">It reads your <span class="hot">logs, metrics</span> and <span class="hot">code</span></h2>
<div class="el center" data-t="4.0 8 fade" style="top:520px">
  <span class="chip"><b>▸</b>Loki</span><span class="chip"><b>▸</b>Prometheus</span><span class="chip"><b>▸</b>Code audits</span>
</div>
<div class="el center sub" data-t="4.6 8 fade" style="top:700px">and links them into <span class="teal">ranked tasks</span></div>
<div class="el card" data-t="5.0 8 up" style="left:70px;top:860px;width:940px">
  <div class="crop" style="height:356px"><img src="{queue}" data-crop="40 58 660 250 940"></div>
</div>

<!-- 3 · a human approves, 8 to 13 s -->
<h2 class="el center" data-t="8.1 13 up" style="top:200px">A <span class="hot">human</span> approves<br>every change</h2>
<div class="el card" data-t="8.6 13 up" style="left:110px;top:520px;width:860px">
  <div class="crop" style="height:1134px"><img src="{review}" data-crop="645 150 580 765 860"></div>
  <div class="ring" data-pulse="10.6 13" style="left:12px;top:1053px;width:190px;height:68px"></div>
</div>

<!-- 4 · the sandboxed agent, 13 to 17.4 s -->
<h2 class="el center" data-t="13.1 17.4 up" style="top:330px">Then a <span class="hot">sandboxed agent</span><br>writes the fix</h2>
<div class="el center" data-t="13.8 17.4 fade" style="top:720px"><span class="chip no"><b>✕</b>No network</span><span class="chip no"><b>✕</b>No shell</span></div>
<div class="el center" data-t="14.2 17.4 fade" style="top:860px"><span class="chip no"><b>✕</b>No credentials</span><span class="chip"><b>✓</b>Your tests must pass</span></div>
<div class="el center sub" data-t="14.9 17.4 fade" style="top:1060px">Every diff is checked for protected paths<br>and secrets before anything is pushed.</div>

<!-- 5 · pull request, never merges, 17.4 to 21.4 s -->
<h2 class="el center" data-t="17.5 21.4 up" style="top:300px">It opens the <span class="hot">pull request</span></h2>
<div class="el pr" data-t="17.9 21.4 up" style="top:560px">
  <div class="t">Add a timeout to the payment gateway call</div>
  <div class="b">opsmith/performance/errors-in-production…</div>
  <div class="row"><span class="ok">✓</span> Checks passed in the sandbox</div>
  <div class="row"><span class="ok">✓</span> Evidence, review and run linked</div>
  <div class="merge">Merge: a human's job</div>
</div>
<h2 class="el center" data-t="19.0 21.4 pop" style="top:1250px">It <span class="hot">never</span> merges.</h2>

<!-- 6 · verified after the merge, 21.4 to 26 s -->
<h2 class="el center" data-t="21.5 26 up" style="top:140px">After you merge,<br>it checks the bug<br>is <span class="hot">really gone</span></h2>
<div class="el card" data-t="22.0 26 up" style="left:80px;top:470px;width:920px">
  <div class="crop" style="height:103px"><img src="{verified}" data-crop="48 52 790 88 920"></div>
</div>
<div class="el card" data-t="22.4 26 up" style="left:80px;top:610px;width:920px">
  <div class="crop" style="height:754px"><img src="{verified}" data-crop="300 1688 500 410 920"></div>
</div>
<div class="el" data-t="24.6 26 pop" style="left:0;right:0;top:1420px;text-align:center"><span class="stamp">✓ verified</span></div>

<!-- 7 · call to action, 26 to 31 s -->
<img class="el" data-t="26.1 31.5 pop" src="{mascot}" style="left:340px;top:240px;width:400px">
<h1 class="el center" data-t="26.4 31.5 up" style="top:690px">Op<span class="hot">smith</span></h1>
<div class="el center sub" data-t="26.8 31.5 fade" style="top:830px">Production signals in.<br>Reviewed pull requests out.</div>
<div class="el center" data-t="27.4 31.5 fade" style="top:1020px">
  <span class="chip"><b>✓</b>Free &amp; open source</span><span class="chip"><b>✓</b>Self-hosted</span>
  <span class="chip"><b>✓</b>Claude, OpenAI or local models</span>
</div>
<div class="el center url" data-t="28.2 31.5 fade" style="top:1330px">github.com/deadboyccc/Opsmith</div>

<div class="bar" id="bar"></div>

<script>
const ease = p => 1 - Math.pow(1 - Math.min(Math.max(p, 0), 1), 3);
const DURATION = {DURATION};
function layoutCrops() {{
  for (const img of document.querySelectorAll('img[data-crop]')) {{
    const [x, y, w, h, shown] = img.dataset.crop.split(' ').map(Number);
    const s = shown / w;
    img.dataset.base = JSON.stringify({{x, y, s}});
  }}
}}
function renderAt(t) {{
  for (const el of document.querySelectorAll('[data-t]')) {{
    const [start, end, kind] = el.dataset.t.split(' ');
    const a = ease((t - start) / 0.55), out = ease((t - (end - 0.35)) / 0.35);
    const visible = t >= start && t < end;
    let o = visible ? a * (1 - out) : 0, tr = '';
    if (kind === 'up') tr = `translateY(${{(1 - a) * 60 - out * 30}}px)`;
    if (kind === 'pop') tr = `scale(${{0.82 + 0.18 * a}})`;
    el.style.opacity = o;
    el.style.transform = tr;
  }}
  // A slow push-in on every screenshot while it is on screen.
  for (const img of document.querySelectorAll('img[data-crop]')) {{
    const b = JSON.parse(img.dataset.base);
    const [start, end] = img.closest('[data-t]').dataset.t.split(' ').map(Number);
    const z = 1 + 0.035 * Math.min(Math.max((t - start) / (end - start), 0), 1);
    img.style.transform = `scale(${{b.s * z}}) translate(${{-b.x}}px, ${{-b.y}}px)`;
  }}
  for (const ring of document.querySelectorAll('[data-pulse]')) {{
    const [start, end] = ring.dataset.pulse.split(' ').map(Number);
    const p = t < start || t > end ? 0 : (t - start) % 1.0;
    ring.style.opacity = t < start || t > end ? 0 : 1;
    ring.style.boxShadow = `0 0 0 ${{p * 26}}px rgba(249,115,22,${{0.55 * (1 - p)}})`;
  }}
  document.getElementById('bar').style.width = `${{100 * t / DURATION}}%`;
}}
window.ready = document.fonts.ready.then(() => Promise.all(
  [...document.images].map(i => i.complete ? 0 : new Promise(r => i.onload = i.onerror = r))
)).then(() => {{ layoutCrops(); renderAt(0); }});
</script>
</body></html>
"""


def main() -> int:
    page_file = ROOT / "build" / "reel" / "storyboard.html"
    page_file.parent.mkdir(parents=True, exist_ok=True)
    page_file.write_text(storyboard(), encoding="utf-8")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    frames = round(DURATION * FPS)
    encoder = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS),
         "-c:v", "png", "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "24",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(OUT_VIDEO)],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    assert encoder.stdin is not None
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.goto(page_file.as_uri(), wait_until="networkidle")
        page.evaluate("window.ready")
        for frame in range(frames):
            page.evaluate(f"renderAt({frame / FPS})")
            encoder.stdin.write(page.screenshot(type="png"))
            if frame % FPS == 0:
                print(f"\rrendering {frame // FPS:>2}/{int(DURATION)} s", end="", flush=True)
        page.evaluate(f"renderAt({POSTER_AT})")
        poster = page.screenshot(type="jpeg", quality=82)
        browser.close()
    encoder.stdin.close()
    if encoder.wait() != 0:
        print("\nffmpeg failed", file=sys.stderr)
        return 1
    OUT_POSTER.write_bytes(poster)
    print(
        f"\nwrote {OUT_VIDEO.relative_to(ROOT)} ({OUT_VIDEO.stat().st_size // 1024} KiB)"
        f" and {OUT_POSTER.relative_to(ROOT)} ({OUT_POSTER.stat().st_size // 1024} KiB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
