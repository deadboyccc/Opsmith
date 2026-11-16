"""Capture screenshots of the review UI, filled with the offline demo's data.

Everything shown is synthetic: the demo generates its own repository, logs and metrics
(a made-up ``payments-api`` service, made-up people) and never touches real systems.

    uvx --from playwright playwright install chromium         # once
    uv run --with playwright python scripts/ui_screenshots.py

Writes PNG files to ``docs/images/``.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith.api.app import create_app
from opsmith.config import Settings
from opsmith.demo import run_demo
from opsmith.models import ExecutionRun, Repository, Task
from opsmith.platform import Platform

OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
VIEWPORT = {"width": 1280, "height": 800}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hide_host_paths(session: Session) -> None:
    """The demo runs its checks with this machine's interpreter (``sys.executable``). Show the
    portable command instead, so the pictures say nothing about the machine that made them."""

    def clean(value: Any) -> Any:
        return json.loads(json.dumps(value).replace(json.dumps(sys.executable)[1:-1], "python"))

    for repo in session.scalars(select(Repository)):
        repo.test_command = clean(repo.test_command)
    for task in session.scalars(select(Task)):
        task.execution_report = clean(task.execution_report)
    for run in session.scalars(select(ExecutionRun)):
        run.checks = clean(run.checks)


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="opsmith-screens-"))
    result = run_demo(base, verbose=False)
    if result.final_status != "verified":
        print(f"demo ended {result.final_status!r}, expected 'verified'", file=sys.stderr)
        return 1
    platform = Platform(
        Settings(data_dir=result.data_dir, log_json=False, sandbox_backend="local", local_root=base)
    )
    with platform.db.session() as s:
        _hide_host_paths(s)
        in_review = s.scalars(
            select(Task).where(Task.status == "in_review").order_by(Task.score.desc())
        ).first()
        review_task_id = in_review.id if in_review else result.approved_task_id

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(platform), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    root = f"http://127.0.0.1:{port}"

    pages = {
        "ui-dashboard": "/ui",
        "ui-review-queue": "/ui/review",
        "ui-task-verified": f"/ui/tasks/{result.approved_task_id}",
        "ui-task-in-review": f"/ui/tasks/{review_task_id}",
        "ui-tasks": "/ui/tasks",
        "ui-leads": "/ui/leads",
        "ui-executions": "/ui/executions",
        "ui-config": "/ui/config",
        "ui-jobs": "/ui/jobs",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport=VIEWPORT).new_page()
        page.goto(f"{root}/ui/login")
        page.screenshot(path=OUT / "ui-login.png")
        page.fill("input[name=api_key]", result.api_keys["admin"])
        page.click("button[type=submit]")
        page.wait_for_url(f"{root}/ui")
        for name, path in pages.items():
            page.goto(root + path)
            page.screenshot(path=OUT / f"{name}.png", full_page=True)
            print(f"{name}.png  <-  {path}")
        browser.close()
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
