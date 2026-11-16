"""Composition root: wires settings, storage, LLM gateway, SCM and every pipeline stage, and
exposes the queue handlers. API, worker, scheduler, CLI and tests all build a ``Platform``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from opsmith import audit_log, notifications
from opsmith.aggregation.aggregator import Aggregator
from opsmith.audits.runner import AuditRunner
from opsmith.config import Settings
from opsmith.connectors import build_connector
from opsmith.db import Database
from opsmith.etl.pipeline import BundleBuilder, ConnectorFactory, IngestionService
from opsmith.executor.agents import ClaudeCodingAgent, CodingAgent, HeuristicAgent
from opsmith.executor.service import AgentFactory, ExecutionService
from opsmith.inference.orchestrator import InferenceOrchestrator
from opsmith.llm import LLMGateway, ProviderNotAllowed, default_gateway
from opsmith.models import Job
from opsmith.queue import Handler, Worker
from opsmith.review import ReviewService
from opsmith.scheduler import Scheduler
from opsmith.schemas import ProjectSettings
from opsmith.scm import GitHubSCM, SCMRegistry
from opsmith.scm.local import LocalSCM
from opsmith.storage import LocalObjectStore
from opsmith.tracking import PRTracker, Verifier

log = logging.getLogger(__name__)


def default_agent_factories(settings: Settings, gateway: LLMGateway) -> dict[str, AgentFactory]:
    def claude(ps: ProjectSettings) -> CodingAgent:
        if "anthropic" not in gateway.allowed(ps):
            raise ProviderNotAllowed("the claude executor requires 'anthropic' in the allowlist")
        cfg = ps.llm.for_stage("executor")
        model = cfg.model if cfg.provider == "anthropic" else "claude-opus-5-5"
        return ClaudeCodingAgent(
            model=model,
            effort=cfg.effort if cfg.provider == "anthropic" else "high",
            api_key_ref=settings.anthropic_api_key_ref,
        )

    return {"heuristic": lambda _ps: HeuristicAgent(), "claude": claude}


class Platform:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        gateway: LLMGateway | None = None,
        scm: SCMRegistry | None = None,
        agent_factories: dict[str, AgentFactory] | None = None,
        connector_factory: ConnectorFactory = build_connector,
        base_url: str = "",
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.resolved_database_url)
        self.db.create_all()
        self.store = LocalObjectStore(self.settings.object_store_dir)
        self.gateway = gateway or default_gateway(self.settings)
        self.scm = scm or SCMRegistry(
            {
                "local": LocalSCM(
                    self.settings.data_dir / "local_prs",
                    root=self.settings.local_root,
                    deny=(self.settings.data_dir,),
                ),
                "github": GitHubSCM(),
            }
        )
        self.ingestion = IngestionService(
            self.store,
            connector_factory,
            local_root=self.settings.local_root,
            local_deny=(self.settings.data_dir,),
        )
        self.bundles = BundleBuilder(self.store)
        self.inference = InferenceOrchestrator(self.gateway)
        self.audits = AuditRunner(self.gateway, self.scm, self.settings.data_dir / "checkouts")
        self.aggregator = Aggregator(self.gateway)
        self.reviews = ReviewService()
        self.executor = ExecutionService(
            self.db,
            self.settings,
            self.scm,
            agent_factories or default_agent_factories(self.settings, self.gateway),
            base_url=base_url or self.settings.base_url,
        )
        self.tracker = PRTracker(self.scm)
        self.verifier = Verifier()
        self.scheduler = Scheduler(self.db)

    # ------------------------------------------------------------------ queue wiring
    def handlers(self) -> dict[str, Handler]:
        def ingest(s: Session, job: Job) -> dict[str, Any]:
            return self.ingestion.run_connector(s, job.payload["connector_id"])

        def build_bundles(s: Session, job: Job) -> dict[str, Any]:
            bundles = self.bundles.build_for_project(s, job.payload["project_id"])
            return {"bundles": [b.id for b in bundles]}

        def inference(s: Session, job: Job) -> dict[str, Any]:
            slot = job.payload.get("slot") or job.created_at.strftime("%Y%m%dT%H%M%S")
            run = self.inference.run(s, job.payload["service_id"], slot)
            return {"run_id": run.id if run else None, "status": run.status if run else "no_input"}

        def audit(s: Session, job: Job) -> dict[str, Any]:
            run = self.audits.run(
                s,
                job.payload["repository_id"],
                mode=job.payload.get("mode", "auto"),
                audit_types=job.payload.get("audit_types"),
            )
            return {"run_id": run.id, "findings": run.findings_count}

        def aggregate(s: Session, job: Job) -> dict[str, Any]:
            return dict(self.aggregator.run(s, job.payload["project_id"]))

        def execute(_s: Session, job: Job) -> dict[str, Any]:
            return self.executor.execute(job.payload["task_id"], job.payload["review_id"])

        def track_prs(s: Session, job: Job) -> dict[str, Any]:
            return self.tracker.run(s, job.payload["project_id"])

        def verify(s: Session, job: Job) -> dict[str, Any]:
            return self.verifier.run(s, job.payload["task_id"], attempt=job.deferrals)

        return {
            "ingest": ingest,
            "build_bundles": build_bundles,
            "inference": inference,
            "audit": audit,
            "aggregate": aggregate,
            "execute": execute,
            "track_prs": track_prs,
            "verify": verify,
            "notify": notifications.handle_notify_job,
        }

    def worker(self, **kwargs: Any) -> Worker:
        return Worker(
            self.db,
            self.handlers(),
            lease_seconds=self.settings.worker_lease_seconds,
            on_dead=self._on_dead,
            **kwargs,
        )

    @staticmethod
    def _on_dead(session: Session, job: Job) -> None:
        if job.kind == "notify":  # don't notify about failed notifications (loop guard)
            return
        project_id = job.payload.get("project_id")
        notifications.notify(
            session,
            tenant_id=job.tenant_id,
            project_id=project_id,
            event="job.dead",
            subject=f"Pipeline job failed: {job.kind}",
            body=(job.last_error or "")[:1000],
        )
        audit_log.record(
            session,
            tenant_id=job.tenant_id,
            entity_type="job",
            entity_id=job.id,
            action="job.dead_notified",
        )
