"""Process-level settings, read from ``OPSMITH_*`` environment variables.

Per-project behaviour (budgets, protected paths, models per stage, ...) lives in
:class:`opsmith.schemas.ProjectSettings`, stored on the ``Project`` row, so it can be
changed per tenant without restarting processes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _env(name: str, default: str) -> str:
    return os.environ.get(f"OPSMITH_{name}", default)


def _env_list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseModel):
    data_dir: Path = Field(default=Path("./data"))
    database_url: str = ""
    # LLM providers that may receive data at all (data-residency / provider allowlist).
    llm_provider_allowlist: list[str] = Field(
        default_factory=lambda: ["heuristic", "anthropic", "openai"]
    )
    anthropic_api_key_ref: str = "env:ANTHROPIC_API_KEY"
    # The "openai" provider: any OpenAI-compatible Chat Completions endpoint (OpenAI, Azure
    # OpenAI, Ollama, vLLM, LM Studio, LiteLLM, OpenRouter, ...). A local server needs no key.
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key_ref: str = "env:OPENAI_API_KEY"
    # How structured output is requested: "json_schema" (falls back automatically when the
    # server rejects it) or "json_object" for servers that only support plain JSON mode.
    openai_json_mode: Literal["json_schema", "json_object"] = "json_schema"
    # Executor sandbox backend. "docker" isolates (no network, read-only root, dropped
    # capabilities). "local" is an unconfined subprocess for development and the demo only;
    # the executor refuses to use it for repositories outside the local SCM provider.
    sandbox_backend: Literal["docker", "local"] = "docker"
    sandbox_image: str = "python:3.12-slim"
    # Where execution checkouts are created (default: the system temp dir). When the worker
    # itself runs in a container and talks to the host's Docker daemon, this must be a path
    # that is mounted at the same location on the host.
    work_dir: Path | None = None
    # The only directory file connectors and local-SCM repositories may read. Unset disables
    # both, so project configuration can never point the worker at arbitrary host files.
    local_root: Path | None = None
    worker_lease_seconds: int = Field(default=900, ge=30)
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    log_json: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _level_name(cls, value: object) -> object:
        """``debug`` and ``DEBUG`` are the same level; anything else is refused when the
        process starts instead of raising from ``logging`` later."""
        return value.upper() if isinstance(value, str) else value

    bot_name: str = "Opsmith Bot"
    bot_email: str = "opsmith-bot@users.noreply.github.com"
    # Public URL of the UI (e.g. https://opsmith.example.com). When set, pull requests link
    # back to their task instead of only naming its id.
    base_url: str = ""
    # The path the API and UI are served under, behind a reverse proxy that routes one path of
    # a shared domain to Opsmith (``/opsmith``). Empty: served at the root.
    base_path: str = ""

    @field_validator("base_path")
    @classmethod
    def _plain_path(cls, value: str) -> str:
        """``/name`` or ``/a/b``: no trailing slash, no ``..``, nothing a URL treats specially."""
        if value and (
            not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", value) or ".." in value.split("/")
        ):
            raise ValueError("base_path must look like /opsmith (letters, digits, . _ ~ -)")
        return value

    @property
    def object_store_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'opsmith.db').resolve()}"

    @property
    def database_display(self) -> str:
        """The database URL with any password hidden; safe to print or log."""
        from sqlalchemy.engine import make_url  # noqa: PLC0415 - keep this module import-light

        return make_url(self.resolved_database_url).render_as_string(hide_password=True)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(_env("DATA_DIR", "./data")),
            database_url=_env("DATABASE_URL", ""),
            llm_provider_allowlist=_env_list(
                "LLM_PROVIDER_ALLOWLIST", "heuristic,anthropic,openai"
            ),
            anthropic_api_key_ref=_env("ANTHROPIC_API_KEY_REF", "env:ANTHROPIC_API_KEY"),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_api_key_ref=_env("OPENAI_API_KEY_REF", "env:OPENAI_API_KEY"),
            openai_json_mode=_env("OPENAI_JSON_MODE", "json_schema"),
            sandbox_backend=_env("SANDBOX_BACKEND", "docker"),
            sandbox_image=_env("SANDBOX_IMAGE", "python:3.12-slim"),
            work_dir=Path(_env("WORK_DIR", "")) if _env("WORK_DIR", "") else None,
            local_root=Path(_env("LOCAL_ROOT", "")) if _env("LOCAL_ROOT", "") else None,
            worker_lease_seconds=int(_env("WORKER_LEASE_SECONDS", "900")),
            log_level=_env("LOG_LEVEL", "INFO"),
            log_json=_env("LOG_JSON", "true").lower() in {"1", "true", "yes"},
            bot_name=_env("BOT_NAME", "Opsmith Bot"),
            bot_email=_env("BOT_EMAIL", "opsmith-bot@users.noreply.github.com"),
            base_url=_env("BASE_URL", ""),
            base_path=_env("BASE_PATH", ""),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.object_store_dir.mkdir(parents=True, exist_ok=True)


def resolve_credential(ref: str | None) -> str | None:
    """Resolve an operator-level credentials *reference* (process configuration).

    Supported forms: ``env:NAME`` and ``file:/path/to/secret``. ``None``/empty -> ``None``.
    Secrets are never stored in the database; only references are.
    """
    if not ref:
        return None
    scheme, _, value = ref.partition(":")
    if scheme == "env":
        return os.environ.get(value) or None
    if scheme == "file":
        path = Path(value)
        return path.read_text(encoding="utf-8").strip() if path.is_file() else None
    raise ValueError(f"unsupported credentials reference scheme: {scheme!r}")


class CredentialRefError(ValueError):
    """A repository or connector references a credential it is not allowed to use."""


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The platform's own secrets. A repository or connector must never be able to name them,
# because its credential is sent to a URL its (tenant) admin chose.
_RESERVED_ENV = frozenset({"POSTGRES_PASSWORD", "PGPASSWORD", "DATABASE_URL"})


_PROVIDER_KEY_REFS = (
    ("ANTHROPIC_API_KEY_REF", "env:ANTHROPIC_API_KEY"),
    ("OPENAI_API_KEY_REF", "env:OPENAI_API_KEY"),
)


def _reserved_env_names() -> frozenset[str]:
    """The model providers' API keys, wherever the operator has pointed them."""
    names = set(_RESERVED_ENV)
    for setting, default in _PROVIDER_KEY_REFS:
        scheme, _, name = _env(setting, default).partition(":")
        if scheme == "env":
            names.add(name)
    return frozenset(names)


def _is_reserved_file(path: Path) -> bool:
    """Whether ``path`` is a file that holds one of the model providers' API keys.

    Docker secrets put those keys in the same directory as tenants' tokens, so a reference
    inside the secrets directory is not enough to tell them apart."""
    for setting, default in _PROVIDER_KEY_REFS:
        scheme, _, value = _env(setting, default).partition(":")
        if scheme != "file":
            continue
        key_file = Path(value).resolve()
        if path == key_file:
            return True
        try:
            if path.exists() and key_file.exists() and path.samefile(key_file):
                return True
        except OSError:
            return True  # cannot tell them apart: refuse
    return False


def secrets_dir() -> Path:
    return Path(_env("SECRETS_DIR", "/run/secrets"))


def check_credentials_ref(ref: str | None) -> None:
    """Validate a reference configured on a repository or connector (tenant-supplied).

    * ``env:NAME`` may not name the platform's own secrets (``OPSMITH_*``, the database
      password, the LLM API key).
    * ``file:/path`` must live inside ``OPSMITH_SECRETS_DIR`` (default ``/run/secrets``), so a
      reference cannot be used to read arbitrary files on the worker, and may not be the file
      that holds an LLM API key.
    """
    if not ref:
        return
    scheme, _, value = ref.partition(":")
    if scheme == "env":
        if not _ENV_NAME.fullmatch(value):
            raise CredentialRefError(f"invalid environment variable name in {ref!r}")
        if value.upper().startswith("OPSMITH_") or value in _reserved_env_names():
            raise CredentialRefError(f"{ref!r} names a variable reserved for the platform")
    elif scheme == "file":
        root = secrets_dir().resolve()
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            raise CredentialRefError(f"{ref!r} is outside the secrets directory {str(root)!r}")
        if _is_reserved_file(path):
            raise CredentialRefError(f"{ref!r} names a file reserved for the platform")
    else:
        raise CredentialRefError(
            f"unsupported credentials reference {ref!r}; use env:NAME or file:/path"
        )


def resolve_tenant_credential(ref: str | None) -> str | None:
    """Resolve a repository/connector reference, enforcing :func:`check_credentials_ref`."""
    check_credentials_ref(ref)
    return resolve_credential(ref)
