"""Process-level settings, read from ``OPSMITH_*`` environment variables.

Per-project behaviour (budgets, protected paths, models per stage, ...) lives in
:class:`opsmith.schemas.ProjectSettings`, stored on the ``Project`` row, so it can be
changed per tenant without restarting processes.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"OPSMITH_{name}", default)


def _env_list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseModel):
    data_dir: Path = Field(default=Path("./data"))
    database_url: str = ""
    # LLM providers that may receive data at all (data-residency / provider allowlist).
    llm_provider_allowlist: list[str] = Field(default_factory=lambda: ["heuristic", "anthropic"])
    default_llm_provider: str = "heuristic"
    anthropic_api_key_ref: str = "env:ANTHROPIC_API_KEY"
    # Executor sandbox backend: "local" (subprocess, env-scrubbed) or "docker" (network=none).
    sandbox_backend: str = "local"
    sandbox_image: str = "python:3.12-slim"
    worker_lease_seconds: int = 900
    log_level: str = "INFO"
    log_json: bool = True
    bot_name: str = "Opsmith Bot"
    bot_email: str = "opsmith-bot@users.noreply.github.com"

    @property
    def object_store_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'opsmith.db').resolve()}"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(_env("DATA_DIR", "./data")),
            database_url=_env("DATABASE_URL", ""),
            llm_provider_allowlist=_env_list("LLM_PROVIDER_ALLOWLIST", "heuristic,anthropic"),
            default_llm_provider=_env("LLM_PROVIDER", "heuristic"),
            anthropic_api_key_ref=_env("ANTHROPIC_API_KEY_REF", "env:ANTHROPIC_API_KEY"),
            sandbox_backend=_env("SANDBOX_BACKEND", "local"),
            sandbox_image=_env("SANDBOX_IMAGE", "python:3.12-slim"),
            worker_lease_seconds=int(_env("WORKER_LEASE_SECONDS", "900")),
            log_level=_env("LOG_LEVEL", "INFO"),
            log_json=_env("LOG_JSON", "true").lower() in {"1", "true", "yes"},
            bot_name=_env("BOT_NAME", "Opsmith Bot"),
            bot_email=_env("BOT_EMAIL", "opsmith-bot@users.noreply.github.com"),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.object_store_dir.mkdir(parents=True, exist_ok=True)


def resolve_credential(ref: str | None) -> str | None:
    """Resolve a credentials *reference*; secrets are never stored in the database.

    Supported forms: ``env:NAME`` and ``file:/path/to/secret``. ``None``/empty -> ``None``.
    """
    if not ref:
        return None
    scheme, _, value = ref.partition(":")
    if scheme == "env":
        return os.environ.get(value) or None
    if scheme == "file":
        path = Path(value)
        return path.read_text(encoding="utf-8").strip() if path.exists() else None
    raise ValueError(f"unsupported credentials reference scheme: {scheme!r}")
