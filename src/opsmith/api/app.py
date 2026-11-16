"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from opsmith import __version__
from opsmith.api import routes, ui
from opsmith.observability import METRICS
from opsmith.platform import Platform

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"


def create_app(platform: Platform | None = None) -> FastAPI:
    app = FastAPI(
        title="Opsmith",
        version=__version__,
        description="Autonomous engineering ops: observe, analyze, propose, human approve, "
        "execute, PR.",
    )
    app.state.platform = platform or Platform()
    app.include_router(routes.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.exception_handler(ui.LoginRequired)
    def _login_required(_request: Request, _exc: ui.LoginRequired) -> Response:
        return RedirectResponse("/ui/login", status_code=303)

    @app.exception_handler(IntegrityError)
    def _conflict(_request: Request, _exc: IntegrityError) -> Response:
        # A uniqueness race (two identical creates at once): report it as a conflict, not a 500.
        return JSONResponse({"detail": "conflict with an existing record"}, status_code=409)

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/ui", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        with app.state.platform.db.session() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        with METRICS.timer("opsmith_http_seconds", method=request.method):
            response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        return response

    return app
