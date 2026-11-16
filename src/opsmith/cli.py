"""``opsmith`` command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import __version__
from opsmith.config import Settings
from opsmith.models import Tenant
from opsmith.observability import configure_logging

log = logging.getLogger("opsmith.cli")


def _platform(settings: Settings):  # type: ignore[no-untyped-def]
    from opsmith.platform import Platform  # noqa: PLC0415 - keep `--help` fast

    return Platform(settings)


def _tenant(session: Session, name: str) -> Tenant:
    tenant = session.scalar(select(Tenant).where(Tenant.name == name))
    if tenant is None:
        raise SystemExit(f"tenant {name!r} not found (run `opsmith init --tenant {name}`)")
    return tenant


def cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    from opsmith.security import create_tenant, create_user  # noqa: PLC0415

    platform = _platform(settings)
    with platform.db.session() as s:
        if s.scalar(select(Tenant).where(Tenant.name == args.tenant)):
            print(f"tenant {args.tenant!r} already exists; use `opsmith create-user`.")
            return 1
        tenant = create_tenant(s, args.tenant)
        user, key = create_user(s, tenant, args.admin, "admin", args.email)
    print(f"database: {settings.database_display}")
    print(f"tenant:   {tenant.name} ({tenant.id})")
    print(f"admin:    {user.name}")
    print(f"API key:  {key}\n(shown once - store it in your secret manager)")
    return 0


def cmd_create_user(args: argparse.Namespace, settings: Settings) -> int:
    from opsmith.security import create_user  # noqa: PLC0415

    platform = _platform(settings)
    with platform.db.session() as s:
        user, key = create_user(s, _tenant(s, args.tenant), args.name, args.role, args.email)
    print(f"{user.name} ({user.role}) API key: {key}")
    return 0


def cmd_apply(args: argparse.Namespace, settings: Settings) -> int:
    import tomllib  # noqa: PLC0415

    from pydantic import ValidationError  # noqa: PLC0415

    from opsmith.configfile import ConfigError, apply_config, load  # noqa: PLC0415

    try:
        data = load(Path(args.file))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    platform = _platform(settings)
    try:
        with platform.db.session() as s:
            tenant = _tenant(s, args.tenant or data.get("tenant", "default"))
            projects = apply_config(s, tenant, data)
    except (ConfigError, ValidationError) as exc:  # nothing was written: the session rolled back
        print(f"invalid configuration in {args.file}:\n{exc}", file=sys.stderr)
        return 1
    for p in projects:
        print(f"applied project {p.name} ({p.id})")
    return 0


def _serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn  # noqa: PLC0415

    from opsmith.api.app import create_app  # noqa: PLC0415

    uvicorn.run(create_app(_platform(settings)), host=host, port=port, log_config=None)


def cmd_api(args: argparse.Namespace, settings: Settings) -> int:
    _serve(settings, args.host, args.port)
    return 0


_STOP = threading.Event()


def _install_signal_handlers() -> None:
    def stop(_signum: int, _frame: object) -> None:
        _STOP.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


# After an unexpected error (typically the database being briefly unreachable) the loops log
# and keep going instead of exiting, so one outage does not silently stop the pipeline.
_ERROR_BACKOFF_SECONDS = 5.0


def _worker_loop(settings: Settings, poll: float, once: bool) -> None:
    platform = _platform(settings)
    worker = platform.worker()
    log.info("worker started", extra={"worker_id": worker.worker_id})
    while not _STOP.is_set():
        try:
            worker.recover_expired_leases()
            # One job at a time, so a stop signal takes effect after the current job
            # instead of after a whole batch.
            results: list[object] = []
            while not _STOP.is_set() and len(results) < 50:
                result = worker.run_once()
                if result is None:
                    break
                results.append(result)
        except Exception:
            if once:
                raise
            log.exception("worker loop error; retrying")
            _STOP.wait(_ERROR_BACKOFF_SECONDS)
            continue
        if once:
            return
        if not results:
            _STOP.wait(poll)


def _scheduler_loop(settings: Settings, interval: float, once: bool) -> None:
    platform = _platform(settings)
    while not _STOP.is_set():
        try:
            enqueued = platform.scheduler.tick()
        except Exception:
            if once:
                raise
            log.exception("scheduler tick failed; retrying")
            _STOP.wait(_ERROR_BACKOFF_SECONDS)
            continue
        if enqueued:
            log.info("scheduled jobs", extra={"count": len(enqueued)})
        if once:
            return
        _STOP.wait(interval)


def cmd_worker(args: argparse.Namespace, settings: Settings) -> int:
    _install_signal_handlers()
    _worker_loop(settings, args.poll, args.once)
    return 0


def cmd_scheduler(args: argparse.Namespace, settings: Settings) -> int:
    _install_signal_handlers()
    _scheduler_loop(settings, args.interval, args.once)
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """All-in-one for development: API + scheduler + worker in one process."""
    threading.Thread(target=_scheduler_loop, args=(settings, 30.0, False), daemon=True).start()
    threading.Thread(target=_worker_loop, args=(settings, 2.0, False), daemon=True).start()
    _serve(settings, args.host, args.port)
    _STOP.set()
    return 0


def cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    from opsmith.demo import run_demo, summarize  # noqa: PLC0415

    base = Path(args.dir).resolve()
    started = time.monotonic()
    result = run_demo(base)
    print(json.dumps(summarize(result), indent=2))
    print(
        f"\nDone in {time.monotonic() - started:.1f}s. Browse it with:\n"
        f"  OPSMITH_DATA_DIR={result.data_dir} opsmith api\n"
        f"  then sign in at http://127.0.0.1:8000/ui with the admin key above."
    )
    return 0 if result.final_status in {"verified", "merged"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsmith", description=__doc__)
    parser.add_argument("--version", action="version", version=f"opsmith {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the database, a tenant and its first admin")
    p.add_argument("--tenant", default="default")
    p.add_argument("--admin", default="admin")
    p.add_argument("--email")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("create-user", help="create a user/service principal and print its key")
    p.add_argument("--tenant", default="default")
    p.add_argument("--name", required=True)
    p.add_argument("--role", choices=["viewer", "reviewer", "admin", "service"], default="reviewer")
    p.add_argument("--email")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("apply", help="apply a declarative opsmith.toml")
    p.add_argument("file")
    p.add_argument("--tenant")
    p.set_defaults(func=cmd_apply)

    for name, func, help_text in (
        ("api", cmd_api, "serve the API and UI"),
        ("run", cmd_run, "API + scheduler + worker (development)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)
        p.set_defaults(func=func)

    p = sub.add_parser("worker", help="process queued jobs")
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("scheduler", help="enqueue due scheduled jobs")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_scheduler)

    p = sub.add_parser("demo", help="run the offline end-to-end demo")
    p.add_argument("--dir", default="./opsmith-demo")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:  # includes pydantic's ValidationError
        raise SystemExit(f"invalid OPSMITH_* configuration:\n{exc}") from None
    configure_logging(settings.log_level, settings.log_json)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
