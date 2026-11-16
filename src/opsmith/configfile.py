"""Declarative configuration (``opsmith apply opsmith.toml``): idempotent upserts by name."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log
from opsmith.audits import AUDIT_TYPES
from opsmith.config import CredentialRefError, check_credentials_ref
from opsmith.connectors import available_types
from opsmith.models import Connector, Project, Repository, Service, Tenant
from opsmith.scheduler import ensure_project_schedules
from opsmith.schemas import YEAR_HOURS, YEAR_SECONDS, ProjectSettings

_REPO_FIELDS = {
    "scm_provider",
    "url",
    "full_name",
    "default_branch",
    "credentials_ref",
    "test_command",
    "lint_command",
    "build_command",
    "audit_types",
}
_SERVICE_FIELDS = {"environment", "owner", "path_prefixes", "inference_interval_hours"}
_CONNECTOR_FIELDS = {"type", "config", "credentials_ref", "interval_seconds", "enabled"}


class ConfigError(ValueError):
    pass


def load(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _pick(data: dict[str, Any], allowed: set[str], what: str) -> dict[str, Any]:
    extra = set(data) - allowed - {"name", "repository", "service"}
    if extra:
        raise ConfigError(f"{what}: unknown keys {sorted(extra)}")
    return {k: v for k, v in data.items() if k in allowed}


def _name(data: Any, what: str) -> str:
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ConfigError(f"every {what} needs a name (a string of at most 200 characters)")
    return name


def _check_ref(ref: Any, what: str) -> None:
    if ref is not None and not isinstance(ref, str):
        raise ConfigError(f"{what}: credentials_ref must be a string")
    try:
        check_credentials_ref(ref)
    except CredentialRefError as exc:
        raise ConfigError(f"{what}: {exc}") from exc


def _validate_repository(data: dict[str, Any], what: str) -> None:
    if data.get("scm_provider", "local") not in {"local", "github"}:
        raise ConfigError(f"{what}: scm_provider must be 'local' or 'github'")
    bad = set(data.get("audit_types", [])) - set(AUDIT_TYPES)
    if bad:
        raise ConfigError(f"{what}: unknown audit types {sorted(bad)}")
    _check_ref(data.get("credentials_ref"), what)


def _validate_connector(data: dict[str, Any], what: str) -> None:
    interval = data.get("interval_seconds", 900)
    if (
        not isinstance(interval, int)
        or isinstance(interval, bool)
        or not 60 <= interval <= YEAR_SECONDS
    ):
        raise ConfigError(
            f"{what}: interval_seconds must be an integer from 60 to {YEAR_SECONDS} (a year)"
        )
    if not isinstance(data.get("config", {}), dict):
        raise ConfigError(f"{what}: config must be a table")
    _check_ref(data.get("credentials_ref"), what)


def _validate_service(data: dict[str, Any], what: str) -> None:
    hours = data.get("inference_interval_hours")
    if hours is not None and not (
        isinstance(hours, (int, float)) and not isinstance(hours, bool) and 0 < hours <= YEAR_HOURS
    ):
        raise ConfigError(
            f"{what}: inference_interval_hours must be a positive number of at most "
            f"{YEAR_HOURS} (a year)"
        )


def apply_config(session: Session, tenant: Tenant, data: dict[str, Any]) -> list[Project]:
    """Upsert every project in ``data``. Raises ``ConfigError`` (or pydantic's
    ``ValidationError`` for ``settings``) before anything invalid is written."""
    projects: list[Project] = []
    for pdata in data.get("projects", []):
        name = _name(pdata, "project")
        settings = ProjectSettings.model_validate(pdata.get("settings", {}))
        project = session.scalar(
            select(Project).where(Project.tenant_id == tenant.id, Project.name == name)
        )
        if project is None:
            project = Project(tenant_id=tenant.id, name=name)
            session.add(project)
        project.settings = settings.model_dump(mode="json")
        session.flush()

        repos: dict[str, Repository] = {}
        for rdata in pdata.get("repositories", []):
            what = f"repository {_name(rdata, 'repository')}"
            _validate_repository(rdata, what)
            repo = session.scalar(
                select(Repository).where(
                    Repository.project_id == project.id, Repository.name == rdata["name"]
                )
            )
            if repo is None:
                if not rdata.get("url"):
                    raise ConfigError(f"{what}: url is required")
                repo = Repository(
                    tenant_id=tenant.id, project_id=project.id, name=rdata["name"], url=rdata["url"]
                )
                session.add(repo)
            for key, value in _pick(rdata, _REPO_FIELDS, what).items():
                setattr(repo, key, value)
            session.flush()
            repos[repo.name] = repo

        services: dict[str, Service] = {}
        for sdata in pdata.get("services", []):
            what = f"service {_name(sdata, 'service')}"
            _validate_service(sdata, what)
            env = sdata.get("environment", "production")
            svc = session.scalar(
                select(Service).where(
                    Service.project_id == project.id,
                    Service.name == sdata["name"],
                    Service.environment == env,
                )
            )
            if svc is None:
                svc = Service(tenant_id=tenant.id, project_id=project.id, name=sdata["name"])
                session.add(svc)
            for key, value in _pick(sdata, _SERVICE_FIELDS, what).items():
                setattr(svc, key, value)
            if "repository" in sdata:
                if sdata["repository"] not in repos:
                    raise ConfigError(f"{what}: unknown repository {sdata['repository']!r}")
                svc.repository_id = repos[sdata["repository"]].id
            session.flush()
            services[svc.name] = svc

        types = available_types()
        for cdata in pdata.get("connectors", []):
            what = f"connector {_name(cdata, 'connector')}"
            if cdata.get("type") not in types:
                raise ConfigError(
                    f"{what}: unknown type {cdata.get('type')!r}; known: {sorted(types)}"
                )
            _validate_connector(cdata, what)
            conn = session.scalar(
                select(Connector).where(
                    Connector.project_id == project.id, Connector.name == cdata["name"]
                )
            )
            if conn is None:
                conn = Connector(
                    tenant_id=tenant.id,
                    project_id=project.id,
                    name=cdata["name"],
                    type=cdata["type"],
                    kind=types[cdata["type"]],
                )
                session.add(conn)
            for key, value in _pick(cdata, _CONNECTOR_FIELDS, what).items():
                setattr(conn, key, value)
            conn.kind = types[conn.type]
            if "service" in cdata:
                if cdata["service"] not in services:
                    raise ConfigError(f"{what}: unknown service {cdata['service']!r}")
                conn.service_id = services[cdata["service"]].id
            session.flush()
        ensure_project_schedules(session, project)
        audit_log.record(
            session,
            tenant_id=tenant.id,
            entity_type="project",
            entity_id=project.id,
            action="project.applied",
        )
        projects.append(project)
    return projects
