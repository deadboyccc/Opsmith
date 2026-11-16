"""Server-rendered UI: dashboard, review queue, task detail/provenance, leads, runs,
executions, configuration, jobs. Auth is the same API key, held in an HttpOnly,
SameSite=Strict cookie; every POST is a form handled by the same services as the API."""

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select

from opsmith import audit_log, queue
from opsmith.api.deps import COOKIE, HUMAN_ROLES, SessionDep, get_platform, owned
from opsmith.audit_log import Actor
from opsmith.dashboard import project_summary
from opsmith.models import (
    AuditRun,
    Connector,
    ExecutionRun,
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
from opsmith.review import REASON_CODES, ReviewError, ReviewRequest
from opsmith.scheduler import ensure_project_schedules
from opsmith.schemas import ProjectSettings
from opsmith.security import authenticate

TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "ui" / "templates")
)
router = APIRouter(prefix="/ui", include_in_schema=False)


class LoginRequired(Exception):
    pass


def ui_user(request: Request, session: SessionDep) -> User:
    user = authenticate(session, request.cookies.get(COOKIE))
    if user is None or user.role not in HUMAN_ROLES:  # machine `service` keys have no UI access
        raise LoginRequired
    return user


UIUser = Annotated[User, Depends(ui_user)]


def _check_origin(request: Request) -> None:
    """Defence in depth on top of SameSite=Strict: reject cross-origin form posts."""
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "cross-origin form post rejected")


def _render(request: Request, name: str, user: User | None, **ctx: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, {"user": user, **ctx})


def _projects(session: SessionDep, user: User) -> list[Project]:
    return list(
        session.scalars(
            select(Project).where(Project.tenant_id == user.tenant_id).order_by(Project.name)
        ).all()
    )


def _current_project(session: SessionDep, user: User, project_id: str | None) -> Project | None:
    projects = _projects(session, user)
    if project_id:
        return owned(session, Project, project_id, user.tenant_id)
    return projects[0] if projects else None


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    return _render(request, "login.html", None, error=error)


@router.post("/login")
def login(request: Request, session: SessionDep, api_key: Annotated[str, Form()]) -> Response:
    _check_origin(request)
    user = authenticate(session, api_key)
    if user is None or user.role not in HUMAN_ROLES:
        return RedirectResponse("/ui/login?error=Invalid+API+key", status_code=303)
    resp = RedirectResponse("/ui", status_code=303)
    resp.set_cookie(
        COOKIE,
        api_key,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=12 * 3600,
    )
    return resp


@router.get("/logout")
def logout() -> Response:
    resp = RedirectResponse("/ui/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request, session: SessionDep, user: UIUser, project_id: str | None = None
) -> HTMLResponse:
    project = _current_project(session, user, project_id)
    summary = project_summary(session, project) if project else None
    recent_inference = recent_audits = []
    if project:
        recent_inference = list(
            session.scalars(
                select(InferenceRun)
                .where(InferenceRun.project_id == project.id)
                .order_by(InferenceRun.started_at.desc())
                .limit(8)
            ).all()
        )
        recent_audits = list(
            session.scalars(
                select(AuditRun)
                .where(AuditRun.project_id == project.id)
                .order_by(AuditRun.started_at.desc())
                .limit(8)
            ).all()
        )
    return _render(
        request,
        "dashboard.html",
        user,
        project=project,
        projects=_projects(session, user),
        summary=summary,
        inference=recent_inference,
        audits=recent_audits,
    )


@router.get("/review", response_class=HTMLResponse)
def review_queue(
    request: Request, session: SessionDep, user: UIUser, project_id: str | None = None
) -> HTMLResponse:
    project = _current_project(session, user, project_id)
    tasks = []
    if project:
        tasks = list(
            session.scalars(
                select(Task)
                .where(Task.project_id == project.id, Task.status == "in_review")
                .order_by(Task.score.desc())
            ).all()
        )
    return _render(
        request,
        "review.html",
        user,
        project=project,
        projects=_projects(session, user),
        tasks=tasks,
    )


@router.get("/tasks", response_class=HTMLResponse)
def task_list(
    request: Request,
    session: SessionDep,
    user: UIUser,
    project_id: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    project = _current_project(session, user, project_id)
    tasks = []
    if project:
        q = select(Task).where(Task.project_id == project.id)
        if status:
            q = q.where(Task.status == status)
        tasks = list(session.scalars(q.order_by(Task.updated_at.desc()).limit(200)).all())
    return _render(
        request,
        "tasks.html",
        user,
        project=project,
        projects=_projects(session, user),
        tasks=tasks,
        status=status,
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(
    request: Request, task_id: str, session: SessionDep, user: UIUser, error: str | None = None
) -> HTMLResponse:
    task = owned(session, Task, task_id, user.tenant_id)
    prov = task_provenance(session, task)
    return _render(
        request,
        "task.html",
        user,
        task=task,
        prov=prov,
        reasons=REASON_CODES,
        error=error,
        can_review=user.role in {"reviewer", "admin"},
        json=lambda v: json.dumps(v, indent=2, default=str),
    )


@router.post("/tasks/{task_id}/review")
def task_review(
    request: Request,
    task_id: str,
    session: SessionDep,
    user: UIUser,
    platform: Annotated[Platform, Depends(get_platform)],
    decision: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
    reason_code: Annotated[str, Form()] = "",
    investigation: Annotated[str, Form()] = "",
    elevated_ack: Annotated[str, Form()] = "",
    edit_title: Annotated[str, Form()] = "",
    edit_approach: Annotated[str, Form()] = "",
    edit_priority: Annotated[str, Form()] = "",
    expected_version: Annotated[int | None, Form()] = None,
) -> Response:
    _check_origin(request)
    task = owned(session, Task, task_id, user.tenant_id)
    edits: dict[str, Any] = {}
    if edit_title.strip() and edit_title.strip() != task.title:
        edits["title"] = edit_title.strip()
    if edit_approach.strip() and edit_approach.strip() != task.proposed_approach:
        edits["proposed_approach"] = edit_approach.strip()
    if edit_priority and edit_priority != task.priority:
        edits["priority"] = edit_priority
    if decision == "approve" and edits:
        decision = "approve_with_edits"
    try:
        req = ReviewRequest(
            decision=decision,
            comment=comment,
            reason_code=reason_code or None,
            investigation=investigation or None,
            elevated_ack=elevated_ack == "on",
            edits=edits,
            expected_version=expected_version,
        )
        platform.reviews.review(session, task, user, req)
    except (ReviewError, ValidationError) as exc:
        session.rollback()
        msg = str(exc).splitlines()[0][:200]
        return RedirectResponse(f"/ui/tasks/{task_id}?error={msg}", status_code=303)
    return RedirectResponse(f"/ui/tasks/{task_id}", status_code=303)


@router.get("/leads", response_class=HTMLResponse)
def leads_page(
    request: Request, session: SessionDep, user: UIUser, project_id: str | None = None
) -> HTMLResponse:
    project = _current_project(session, user, project_id)
    leads = []
    if project:
        leads = list(
            session.scalars(
                select(Lead)
                .where(Lead.project_id == project.id)
                .order_by(Lead.updated_at.desc())
                .limit(200)
            ).all()
        )
    return _render(
        request, "leads.html", user, project=project, projects=_projects(session, user), leads=leads
    )


@router.get("/executions", response_class=HTMLResponse)
def executions_page(
    request: Request, session: SessionDep, user: UIUser, project_id: str | None = None
) -> HTMLResponse:
    project = _current_project(session, user, project_id)
    runs: list[ExecutionRun] = []
    tasks: dict[str, Task] = {}
    if project:
        runs = list(
            session.scalars(
                select(ExecutionRun)
                .where(ExecutionRun.project_id == project.id)
                .order_by(ExecutionRun.started_at.desc())
                .limit(100)
            ).all()
        )
        ids = {r.task_id for r in runs}
        if ids:
            tasks = {t.id: t for t in session.scalars(select(Task).where(Task.id.in_(ids)))}
    live = any(r.status == "running" for r in runs)
    return _render(
        request,
        "executions.html",
        user,
        project=project,
        projects=_projects(session, user),
        runs=runs,
        tasks=tasks,
        live=live,
    )


@router.get("/config", response_class=HTMLResponse)
def config_page(
    request: Request,
    session: SessionDep,
    user: UIUser,
    project_id: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    project = _current_project(session, user, project_id)
    ctx: dict[str, Any] = {"connectors": [], "services": [], "repos": [], "schedules": []}
    if project:
        for key, model in (
            ("connectors", Connector),
            ("services", Service),
            ("repos", Repository),
            ("schedules", Schedule),
        ):
            ctx[key] = list(
                session.scalars(select(model).where(model.project_id == project.id)).all()
            )
    settings_json = json.dumps(project.settings, indent=2) if project else "{}"
    return _render(
        request,
        "config.html",
        user,
        project=project,
        projects=_projects(session, user),
        settings_json=settings_json,
        error=error,
        is_admin=user.role == "admin",
        **ctx,
    )


@router.post("/projects/{project_id}/settings")
def save_settings(
    request: Request,
    project_id: str,
    session: SessionDep,
    user: UIUser,
    settings_json: Annotated[str, Form()],
) -> Response:
    _check_origin(request)
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    project = owned(session, Project, project_id, user.tenant_id)
    try:
        settings = ProjectSettings.model_validate(json.loads(settings_json))
    except (ValueError, ValidationError) as exc:
        msg = str(exc).splitlines()[0][:200]
        return RedirectResponse(f"/ui/config?project_id={project_id}&error={msg}", status_code=303)
    project.settings = settings.model_dump(mode="json")
    audit_log.record(
        session,
        tenant_id=user.tenant_id,
        entity_type="project",
        entity_id=project.id,
        action="project.settings_updated",
        actor=Actor("user", user.id),
    )
    ensure_project_schedules(session, project)
    return RedirectResponse(f"/ui/config?project_id={project_id}", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request, session: SessionDep, user: UIUser, status: str | None = None
) -> HTMLResponse:
    q = select(Job).where(Job.tenant_id == user.tenant_id)
    if status:
        q = q.where(Job.status == status)
    jobs = list(session.scalars(q.order_by(Job.created_at.desc()).limit(200)).all())
    return _render(
        request, "jobs.html", user, jobs=jobs, status=status, is_admin=user.role == "admin"
    )


@router.post("/jobs/{job_id}/retry")
def jobs_retry(request: Request, job_id: str, session: SessionDep, user: UIUser) -> Response:
    _check_origin(request)
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    job = owned(session, Job, job_id, user.tenant_id)
    if job.status == "dead":
        queue.retry_dead_job(session, job)
        audit_log.record(
            session,
            tenant_id=user.tenant_id,
            entity_type="job",
            entity_id=job.id,
            action="job.retried",
            actor=Actor("user", user.id),
        )
    return RedirectResponse("/ui/jobs?status=dead", status_code=303)
