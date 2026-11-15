"""JSON REST API (``/api/v1``). Every handler is tenant-scoped through the principal."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from opsmith import audit_log, queue
from opsmith.api.deps import SessionDep, get_platform, get_principal, owned, require_role, to_dict
from opsmith.audit_log import Actor
from opsmith.audits import AUDIT_TYPES, registered
from opsmith.connectors import available_types
from opsmith.dashboard import project_summary
from opsmith.models import (
    AuditLog,
    AuditRun,
    Connector,
    ExecutionRun,
    Finding,
    InferenceRun,
    Job,
    Lead,
    Project,
    Repository,
    Schedule,
    Service,
    Task,
    User,
)
from opsmith.platform import Platform
from opsmith.provenance import task_provenance
from opsmith.review import ReviewError, ReviewRequest
from opsmith.scheduler import CronExpr, ensure_project_schedules
from opsmith.schemas import ProjectSettings

router = APIRouter(prefix="/api/v1")
Principal = Annotated[User, Depends(get_principal)]
Admin = Annotated[User, Depends(require_role("admin"))]
Reviewer = Annotated[User, Depends(require_role("reviewer", "admin"))]
PlatformDep = Annotated[Platform, Depends(get_platform)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectIn(_Strict):
    name: str = Field(min_length=1, max_length=200)
    settings: dict[str, Any] = Field(default_factory=dict)


class SettingsPatch(_Strict):
    settings: dict[str, Any]


class RepositoryIn(_Strict):
    name: str
    scm_provider: Literal["local", "github"] = "github"
    url: str
    full_name: str | None = None
    default_branch: str = "main"
    credentials_ref: str | None = Field(None, description="env:NAME or file:/path, never a secret")
    test_command: str | None = None
    lint_command: str | None = None
    build_command: str | None = None
    audit_types: list[str] = Field(default_factory=lambda: list(AUDIT_TYPES[:5]))


class ServiceIn(_Strict):
    name: str
    environment: str = "production"
    owner: str | None = None
    repository_id: str | None = None
    path_prefixes: list[str] = Field(default_factory=list)
    inference_interval_hours: float | None = Field(None, gt=0)


class ConnectorIn(_Strict):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    credentials_ref: str | None = None
    service_id: str | None = None
    interval_seconds: int = Field(900, ge=60)
    enabled: bool = True


class ScheduleUpdate(_Strict):
    enabled: bool | None = None
    interval_seconds: int | None = Field(None, ge=60)
    cron: str | None = None


class TriggerIn(_Strict):
    job: Literal["ingest", "inference", "audit", "aggregate", "track_prs"]
    target_id: str | None = None
    mode: Literal["auto", "full", "incremental"] = "auto"


def _project(session: SessionDep, project_id: str, user: User) -> Project:
    return owned(session, Project, project_id, user.tenant_id)


def _refresh_schedules(session: SessionDep, project: Project) -> None:
    ensure_project_schedules(session, project)


# --------------------------------------------------------------------------- meta


@router.get("/me")
def me(user: Principal) -> dict[str, Any]:
    return {"id": user.id, "name": user.name, "role": user.role, "tenant_id": user.tenant_id}


@router.get("/catalog")
def catalog(_: Principal) -> dict[str, Any]:
    return {"connectors": available_types(), "analyzers": registered(), "audit_types": AUDIT_TYPES}


# --------------------------------------------------------------------------- configuration


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectIn, session: SessionDep, user: Admin) -> dict[str, Any]:
    try:
        settings = ProjectSettings.model_validate(body.settings)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False)) from exc
    exists = session.scalar(
        select(Project).where(Project.tenant_id == user.tenant_id, Project.name == body.name)
    )
    if exists:
        raise HTTPException(409, "project already exists")
    project = Project(
        tenant_id=user.tenant_id, name=body.name, settings=settings.model_dump(mode="json")
    )
    session.add(project)
    session.flush()
    audit_log.record(
        session,
        tenant_id=user.tenant_id,
        entity_type="project",
        entity_id=project.id,
        action="project.created",
        actor=Actor("user", user.id),
    )
    _refresh_schedules(session, project)
    return to_dict(project)


@router.get("/projects")
def list_projects(session: SessionDep, user: Principal) -> list[dict[str, Any]]:
    rows = session.scalars(select(Project).where(Project.tenant_id == user.tenant_id)).all()
    return [to_dict(p) for p in rows]


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: SessionDep, user: Principal) -> dict[str, Any]:
    return to_dict(_project(session, project_id, user))


@router.patch("/projects/{project_id}/settings")
def patch_settings(
    project_id: str, body: SettingsPatch, session: SessionDep, user: Admin
) -> dict[str, Any]:
    project = _project(session, project_id, user)
    merged = {**project.settings, **body.settings}
    try:
        settings = ProjectSettings.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False)) from exc
    project.settings = settings.model_dump(mode="json")
    audit_log.record(
        session,
        tenant_id=user.tenant_id,
        entity_type="project",
        entity_id=project.id,
        action="project.settings_updated",
        actor=Actor("user", user.id),
        details={"keys": sorted(body.settings)},
    )
    _refresh_schedules(session, project)
    return to_dict(project)


@router.post("/projects/{project_id}/repositories", status_code=201)
def create_repository(
    project_id: str, body: RepositoryIn, session: SessionDep, user: Admin
) -> dict[str, Any]:
    project = _project(session, project_id, user)
    bad = set(body.audit_types) - set(AUDIT_TYPES)
    if bad:
        raise HTTPException(422, f"unknown audit types: {sorted(bad)}")
    repo = Repository(tenant_id=user.tenant_id, project_id=project.id, **body.model_dump())
    session.add(repo)
    session.flush()
    _refresh_schedules(session, project)
    return to_dict(repo)


@router.get("/projects/{project_id}/repositories")
def list_repositories(
    project_id: str, session: SessionDep, user: Principal
) -> list[dict[str, Any]]:
    project = _project(session, project_id, user)
    return [
        to_dict(r)
        for r in session.scalars(
            select(Repository).where(Repository.project_id == project.id)
        ).all()
    ]


@router.post("/projects/{project_id}/services", status_code=201)
def create_service(
    project_id: str, body: ServiceIn, session: SessionDep, user: Admin
) -> dict[str, Any]:
    project = _project(session, project_id, user)
    if body.repository_id:
        owned(session, Repository, body.repository_id, user.tenant_id)
    svc = Service(tenant_id=user.tenant_id, project_id=project.id, **body.model_dump())
    session.add(svc)
    session.flush()
    _refresh_schedules(session, project)
    return to_dict(svc)


@router.get("/projects/{project_id}/services")
def list_services(project_id: str, session: SessionDep, user: Principal) -> list[dict[str, Any]]:
    project = _project(session, project_id, user)
    return [
        to_dict(s)
        for s in session.scalars(select(Service).where(Service.project_id == project.id)).all()
    ]


@router.post("/projects/{project_id}/connectors", status_code=201)
def create_connector(
    project_id: str, body: ConnectorIn, session: SessionDep, user: Admin
) -> dict[str, Any]:
    project = _project(session, project_id, user)
    types = available_types()
    if body.type not in types:
        raise HTTPException(422, f"unknown connector type {body.type!r}; known: {sorted(types)}")
    if body.service_id:
        owned(session, Service, body.service_id, user.tenant_id)
    conn = Connector(
        tenant_id=user.tenant_id, project_id=project.id, kind=types[body.type], **body.model_dump()
    )
    session.add(conn)
    session.flush()
    _refresh_schedules(session, project)
    return to_dict(conn)


@router.get("/projects/{project_id}/connectors")
def list_connectors(project_id: str, session: SessionDep, user: Principal) -> list[dict[str, Any]]:
    project = _project(session, project_id, user)
    return [
        to_dict(c)
        for c in session.scalars(select(Connector).where(Connector.project_id == project.id)).all()
    ]


@router.post("/connectors/{connector_id}/push", status_code=202)
def push_signals(
    connector_id: str,
    records: list[dict[str, Any]],
    session: SessionDep,
    user: Annotated[User, Depends(require_role("service", "admin"))],
    platform: PlatformDep,
) -> dict[str, Any]:
    """Push-mode ingestion for collectors (log shippers, OTel exporters)."""
    conn = owned(session, Connector, connector_id, user.tenant_id)
    if len(records) > 10_000:
        raise HTTPException(413, "max 10000 records per push")
    try:
        batches = platform.ingestion.ingest_push(session, conn, records)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False)) from exc
    return {"batches": [b.id for b in batches], "records": sum(b.record_count for b in batches)}


@router.get("/projects/{project_id}/schedules")
def list_schedules(project_id: str, session: SessionDep, user: Principal) -> list[dict[str, Any]]:
    project = _project(session, project_id, user)
    return [
        to_dict(s)
        for s in session.scalars(select(Schedule).where(Schedule.project_id == project.id)).all()
    ]


@router.patch("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: str, body: ScheduleUpdate, session: SessionDep, user: Admin
) -> dict[str, Any]:
    sch = owned(session, Schedule, schedule_id, user.tenant_id)
    if body.cron is not None:
        try:
            CronExpr.parse(body.cron)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        sch.cron, sch.interval_seconds = body.cron, None
    if body.interval_seconds is not None:
        sch.interval_seconds, sch.cron = body.interval_seconds, None
    if body.enabled is not None:
        sch.enabled = body.enabled
    return to_dict(sch)


@router.post("/projects/{project_id}/trigger", status_code=202)
def trigger(project_id: str, body: TriggerIn, session: SessionDep, user: Admin) -> dict[str, Any]:
    """Run a pipeline stage now (still queued, idempotent, retried like scheduled runs)."""
    project = _project(session, project_id, user)
    payload: dict[str, Any] = {"project_id": project.id}
    if body.job == "ingest":
        conn = owned(session, Connector, body.target_id or "", user.tenant_id)
        payload["connector_id"] = conn.id
    elif body.job == "inference":
        svc = owned(session, Service, body.target_id or "", user.tenant_id)
        payload.update(service_id=svc.id, slot=f"manual-{uuid.uuid4().hex[:8]}")
    elif body.job == "audit":
        repo = owned(session, Repository, body.target_id or "", user.tenant_id)
        payload.update(repository_id=repo.id, mode=body.mode)
    job = queue.enqueue(
        session,
        tenant_id=user.tenant_id,
        kind=body.job,
        payload=payload,
        idempotency_key=f"manual:{body.job}:{uuid.uuid4().hex}",
    )
    audit_log.record(
        session,
        tenant_id=user.tenant_id,
        entity_type="job",
        entity_id=job.id,
        action="job.triggered",
        actor=Actor("user", user.id),
        details={"kind": body.job},
    )
    return {"job_id": job.id}


# --------------------------------------------------------------------------- pipeline state


def _list(
    session: SessionDep,
    model: Any,
    project_id: str,
    user: User,
    *,
    order: Any,
    limit: int,
    **filters: Any,
) -> list[dict[str, Any]]:
    project = _project(session, project_id, user)
    query = select(model).where(model.project_id == project.id)
    for key, value in filters.items():
        if value is not None:
            query = query.where(getattr(model, key) == value)
    return [to_dict(r) for r in session.scalars(query.order_by(order).limit(limit)).all()]


@router.get("/projects/{project_id}/inference-runs")
def inference_runs(
    project_id: str, session: SessionDep, user: Principal, limit: int = Query(50, le=500)
) -> list[dict[str, Any]]:
    return _list(
        session, InferenceRun, project_id, user, order=InferenceRun.started_at.desc(), limit=limit
    )


@router.get("/projects/{project_id}/audit-runs")
def audit_runs(
    project_id: str, session: SessionDep, user: Principal, limit: int = Query(50, le=500)
) -> list[dict[str, Any]]:
    return _list(session, AuditRun, project_id, user, order=AuditRun.started_at.desc(), limit=limit)


@router.get("/projects/{project_id}/leads")
def leads(
    project_id: str,
    session: SessionDep,
    user: Principal,
    state: str | None = None,
    limit: int = Query(100, le=1000),
) -> list[dict[str, Any]]:
    return _list(
        session, Lead, project_id, user, order=Lead.updated_at.desc(), limit=limit, state=state
    )


@router.get("/projects/{project_id}/findings")
def findings(
    project_id: str,
    session: SessionDep,
    user: Principal,
    status: str | None = None,
    limit: int = Query(100, le=1000),
) -> list[dict[str, Any]]:
    return _list(
        session,
        Finding,
        project_id,
        user,
        order=Finding.created_at.desc(),
        limit=limit,
        status=status,
    )


@router.get("/projects/{project_id}/tasks")
def tasks(
    project_id: str,
    session: SessionDep,
    user: Principal,
    status: str | None = None,
    limit: int = Query(100, le=1000),
) -> list[dict[str, Any]]:
    return _list(
        session, Task, project_id, user, order=Task.score.desc(), limit=limit, status=status
    )


@router.get("/projects/{project_id}/executions")
def executions(
    project_id: str, session: SessionDep, user: Principal, limit: int = Query(50, le=500)
) -> list[dict[str, Any]]:
    return _list(
        session, ExecutionRun, project_id, user, order=ExecutionRun.started_at.desc(), limit=limit
    )


@router.get("/tasks/{task_id}")
def get_task(task_id: str, session: SessionDep, user: Principal) -> dict[str, Any]:
    return to_dict(owned(session, Task, task_id, user.tenant_id))


@router.get("/tasks/{task_id}/provenance")
def get_provenance(task_id: str, session: SessionDep, user: Principal) -> dict[str, Any]:
    return task_provenance(session, owned(session, Task, task_id, user.tenant_id))


@router.post("/tasks/{task_id}/review")
def review_task(
    task_id: str, body: ReviewRequest, session: SessionDep, user: Reviewer, platform: PlatformDep
) -> dict[str, Any]:
    task = owned(session, Task, task_id, user.tenant_id)
    try:
        review = platform.reviews.review(session, task, user, body)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"review": to_dict(review), "task": to_dict(task)}


@router.post("/tasks/{task_id}/resubmit")
def resubmit(
    task_id: str, session: SessionDep, user: Reviewer, platform: PlatformDep
) -> dict[str, Any]:
    task = owned(session, Task, task_id, user.tenant_id)
    try:
        platform.reviews.resubmit(session, task, user)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return to_dict(task)


@router.get("/jobs")
def jobs(
    session: SessionDep,
    user: Principal,
    status: str | None = None,
    limit: int = Query(100, le=1000),
) -> list[dict[str, Any]]:
    query = select(Job).where(Job.tenant_id == user.tenant_id)
    if status:
        query = query.where(Job.status == status)
    return [
        to_dict(j)
        for j in session.scalars(query.order_by(Job.created_at.desc()).limit(limit)).all()
    ]


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, session: SessionDep, user: Admin) -> dict[str, Any]:
    job = owned(session, Job, job_id, user.tenant_id)
    try:
        queue.retry_dead_job(session, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit_log.record(
        session,
        tenant_id=user.tenant_id,
        entity_type="job",
        entity_id=job.id,
        action="job.retried",
        actor=Actor("user", user.id),
    )
    return to_dict(job)


@router.get("/audit-log")
def get_audit_log(
    session: SessionDep,
    user: Principal,
    entity_id: str | None = None,
    limit: int = Query(200, le=2000),
) -> list[dict[str, Any]]:
    query = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    return [
        to_dict(a) for a in session.scalars(query.order_by(AuditLog.id.desc()).limit(limit)).all()
    ]


@router.get("/projects/{project_id}/summary")
def summary(project_id: str, session: SessionDep, user: Principal) -> dict[str, Any]:
    return project_summary(session, _project(session, project_id, user))
