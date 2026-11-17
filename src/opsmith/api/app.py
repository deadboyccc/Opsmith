"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opsmith import __version__
from opsmith.api import routes, ui
from opsmith.observability import METRICS
from opsmith.platform import Platform

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"
STRICT_CSP = "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
# The generated API reference (Swagger UI, ReDoc) loads its script and style sheet from a CDN
# and starts from an inline script, all of which the strict policy blocks: the pages were
# blank. They show the OpenAPI schema and nothing a user or a model wrote, so these two paths
# alone get a policy that lets them load.
API_REFERENCE_PATHS = frozenset({"/docs", "/redoc"})
_CDN = "https://cdn.jsdelivr.net"
API_REFERENCE_CSP = (
    f"default-src 'self'; script-src 'self' 'unsafe-inline' {_CDN}; "
    f"style-src 'self' 'unsafe-inline' {_CDN} https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; worker-src blob:; "
    f"img-src 'self' data: https://fastapi.tiangolo.com {_CDN}; frame-ancestors 'none'"
)


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# Bodies are read and parsed before the key is checked, so their size is bounded here, for
# every caller. Pushed signals (up to 10,000 records) get their own, larger limit.
MAX_BODY_BYTES = 1024 * 1024
MAX_PUSH_BODY_BYTES = 32 * 1024 * 1024


class BodyLimit:
    """Refuse a request whose body is over the limit: by its ``Content-Length`` when it has
    one, and by counting the bytes as they arrive when it has not."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = MAX_PUSH_BODY_BYTES if scope["path"].endswith("/push") else MAX_BODY_BYTES
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        if not declared.isdigit():
            await JSONResponse({"detail": "invalid Content-Length"}, 400)(scope, receive, send)
            return
        too_large = JSONResponse({"detail": f"request body is over {limit} bytes"}, 413)
        if int(declared) > limit:
            await too_large(scope, receive, send)
            return
        seen = 0
        refused = False

        async def counted() -> Message:
            # Raising from here would not do: the framework turns an error while it reads the
            # body into its own 400. So the refusal is sent from here, the application is
            # told the client has gone, and whatever it then tries to send is dropped.
            nonlocal seen, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    refused = True
                    await too_large(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def unless_refused(message: Message) -> None:
            if not refused:
                await send(message)

        await self.app(scope, counted, unless_refused)


def create_app(platform: Platform | None = None) -> FastAPI:
    """The API and UI. With ``base_path`` set they are mounted under it, so a reverse proxy can
    route one path of a shared domain (``/opsmith/...``) here without rewriting anything."""
    platform = platform or Platform()
    app = _application(platform)
    base = platform.settings.base_path
    if not base:
        return app
    outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    outer.state.platform = platform

    @outer.get(base, include_in_schema=False)
    def at_base() -> Response:
        return RedirectResponse(f"{base}/ui", status_code=303)

    # Health checks and Prometheus ask the root, and a proxy that routes only the base path
    # never exposes these copies. They are also served under the path, as since 1.4.0.
    outer.add_api_route("/healthz", lambda: _health(platform), include_in_schema=False)
    outer.add_api_route("/metrics", _metrics, include_in_schema=False)
    outer.mount(base, app)
    return outer


def _health(platform: Platform) -> dict[str, str]:
    with platform.db.session() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}


def _metrics() -> PlainTextResponse:
    return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4")


def _application(platform: Platform) -> FastAPI:
    app = FastAPI(
        title="Opsmith",
        version=__version__,
        description="Autonomous engineering ops: observe, analyze, propose, human approve, "
        "execute, PR.",
    )
    app.state.platform = platform
    app.include_router(routes.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.exception_handler(ui.LoginRequired)
    def _login_required(request: Request, _exc: ui.LoginRequired) -> Response:
        return RedirectResponse(ui.base_of(request) + "/ui/login", status_code=303)

    @app.exception_handler(IntegrityError)
    def _conflict(_request: Request, _exc: IntegrityError) -> Response:
        # A uniqueness race (two identical creates at once): report it as a conflict, not a 500.
        return JSONResponse({"detail": "conflict with an existing record"}, status_code=409)

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> Response:
        return RedirectResponse(ui.base_of(request) + "/ui", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return _health(app.state.platform)

    app.add_api_route("/metrics", _metrics, include_in_schema=False)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        # A fixed set of labels: the server accepts any token as a method, and each new label
        # value is a set of series kept for the life of the process.
        method = request.method if request.method in _METHODS else "OTHER"
        with METRICS.timer("opsmith_http_seconds", method=method):
            response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        reference = request.url.path.removeprefix(ui.base_of(request)) in API_REFERENCE_PATHS
        response.headers.setdefault(
            "Content-Security-Policy", API_REFERENCE_CSP if reference else STRICT_CSP
        )
        return response

    app.add_middleware(BodyLimit)  # outermost: runs before a body is read or parsed
    return app
