"""Declarative configuration (``opsmith apply opsmith.toml``): idempotent upserts by name."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log
from opsmith.connectors import available_types
from opsmith.models import Connector, Project, Repository, Service, Tenant
from opsmith.scheduler import ensure_project_schedules
from opsmith.schemas import ProjectSettings

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


def apply_config(session: Session, tenant: Tenant, data: dict[str, Any]) -> list[Project]:
    projects: list[Project] = []
    for pdata in data.get("projects", []):
        name = pdata["name"]
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
            repo = session.scalar(
                select(Repository).where(
                    Repository.project_id == project.id, Repository.name == rdata["name"]
                )
            )
            if repo is None:
                repo = Repository(
                    tenant_id=tenant.id, project_id=project.id, name=rdata["name"], url=rdata["url"]
                )
                session.add(repo)
            for key, value in _pick(rdata, _REPO_FIELDS, f"repository {rdata['name']}").items():
                setattr(repo, key, value)
            session.flush()
            repos[repo.name] = repo

        services: dict[str, Service] = {}
        for sdata in pdata.get("services", []):
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
            for key, value in _pick(sdata, _SERVICE_FIELDS, f"service {sdata['name']}").items():
                setattr(svc, key, value)
            if "repository" in sdata:
                if sdata["repository"] not in repos:
                    raise ConfigError(f"service {sdata['name']}: unknown repository")
                svc.repository_id = repos[sdata["repository"]].id
            session.flush()
            services[svc.name] = svc

        types = available_types()
        for cdata in pdata.get("connectors", []):
            if cdata.get("type") not in types:
                raise ConfigError(f"connector {cdata['name']}: unknown type {cdata.get('type')!r}")
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
            for key, value in _pick(cdata, _CONNECTOR_FIELDS, f"connector {cdata['name']}").items():
                setattr(conn, key, value)
            conn.kind = types[conn.type]
            if "service" in cdata:
                if cdata["service"] not in services:
                    raise ConfigError(f"connector {cdata['name']}: unknown service")
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
