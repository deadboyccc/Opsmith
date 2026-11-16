"""FastAPI dependencies: DB session, authenticated principal, role checks, tenant scoping."""

from collections.abc import Callable, Iterator
from typing import Annotated, Any, TypeVar

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from opsmith.platform import Platform
from opsmith.security import authenticate

COOKIE = "opsmith_key"
HUMAN_ROLES = ("viewer", "reviewer", "admin")
T = TypeVar("T")


def get_platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def get_session(request: Request) -> Iterator[Session]:
    with get_platform(request).db.session() as session:
        yield session


# scope="function": the transaction is committed when the handler returns, before the
# response is sent. With the default scope the commit runs after the client already has its
# 2xx, so a commit that fails is reported as success.
SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def api_key_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("x-opsmith-key")
    if header:
        return header
    # The UI's cookie is accepted for reading the API in a browser. A call that changes
    # something must carry the key in a header, which a cross-site form cannot set.
    if request.method in {"GET", "HEAD"}:
        return request.cookies.get(COOKIE)
    return None


def get_principal(request: Request, session: SessionDep) -> Any:
    user = authenticate(session, api_key_from(request))
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(*roles: str) -> Callable[..., Any]:
    def dep(user: Annotated[Any, Depends(get_principal)]) -> Any:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role: {', '.join(roles)}")
        return user

    return dep


def owned[T](session: Session, model: type[T], obj_id: str, tenant_id: str) -> T:
    """Fetch by id within the caller's tenant; other tenants' rows are indistinguishable
    from missing ones (404), never 403."""
    # An id never holds a NUL, and PostgreSQL cannot compare text that does.
    obj = None if "\x00" in obj_id else session.get(model, obj_id)
    if obj is None or getattr(obj, "tenant_id", None) != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__name__} not found")
    return obj


def to_dict(obj: Any, exclude: tuple[str, ...] = ("api_key_hash",)) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        if col.key in exclude:
            continue
        value = getattr(obj, col.key)
        out[col.key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out
