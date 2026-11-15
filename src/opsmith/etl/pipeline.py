"""Ingestion + ETL: fetch -> parse/normalize -> enrich -> redact -> store -> bundle.

Raw (normalized, redacted) records live in object storage; ``AnalysisBundle`` rows hold
the compact, LLM-sized summary and reference the raw objects by key (spec 5.1).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, queue
from opsmith.config import resolve_credential
from opsmith.connectors import build_connector
from opsmith.connectors.base import BaseConnector
from opsmith.db import new_id, utcnow
from opsmith.etl.features import compute_features
from opsmith.etl.templates import TemplateMiner, extract_locations, fingerprint_of
from opsmith.models import AnalysisBundle, Connector, Project, RawBatch, Service
from opsmith.observability import METRICS
from opsmith.redaction import Redactor
from opsmith.schemas import SEVERITY_RANK, LogEvent, MetricPoint, ProjectSettings
from opsmith.storage import ObjectStore

log = logging.getLogger(__name__)

SEVERITY_ALIASES = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "information": "info",
    "notice": "info",
    "warn": "warning",
    "warning": "warning",
    "error": "error",
    "err": "error",
    "fatal": "critical",
    "critical": "critical",
    "crit": "critical",
    "panic": "critical",
    "alert": "critical",
    "emerg": "critical",
    "emergency": "critical",
}
_SEV_WEIGHT = {"debug": 0.2, "info": 0.5, "warning": 2.0, "error": 4.0, "critical": 6.0}
_LOC_ATTRS = ("stack", "stacktrace", "stack_trace", "exception", "traceback", "error.stack")
MAX_TEMPLATES = 40
MAX_SAMPLES = 3
SAMPLE_CHARS = 400


def normalize_severity(value: str) -> str:
    return SEVERITY_ALIASES.get(value.strip().lower(), "info")


def resolve_service(
    session: Session, project: Project, name: str, environment: str = "production"
) -> Service:
    svc = session.scalar(
        select(Service).where(
            Service.project_id == project.id,
            Service.name == name,
            Service.environment == environment,
        )
    )
    if svc is None:
        svc = Service(
            tenant_id=project.tenant_id, project_id=project.id, name=name, environment=environment
        )
        session.add(svc)
        session.flush()
        audit_log.record(
            session,
            tenant_id=project.tenant_id,
            entity_type="service",
            entity_id=svc.id,
            action="service.discovered",
            details={"name": name, "environment": environment},
        )
        from opsmith.scheduler import ensure_inference_schedule  # noqa: PLC0415 - cycle

        ensure_inference_schedule(session, project, svc)
    return svc


ConnectorFactory = Callable[..., BaseConnector]


class IngestionService:
    def __init__(self, store: ObjectStore, connector_factory: ConnectorFactory = build_connector):
        self.store = store
        self.connector_factory = connector_factory

    # ------------------------------------------------------------------ pull mode
    def run_connector(
        self, session: Session, connector_id: str, *, end: datetime | None = None
    ) -> dict[str, Any]:
        conn = session.get(Connector, connector_id)
        if conn is None:
            raise queue.PermanentFailure(f"connector {connector_id} not found")
        if not conn.enabled:
            return {"skipped": "disabled"}
        project = session.get(Project, conn.project_id)
        assert project is not None
        end = end or utcnow()
        last_end = conn.cursor.get("last_end")
        start = (
            datetime.fromisoformat(last_end)
            if last_end
            else end - timedelta(seconds=conn.interval_seconds or 900)
        )
        if start >= end:
            return {"skipped": "empty window"}
        default_service = "unknown"
        if conn.service_id:
            svc = session.get(Service, conn.service_id)
            default_service = svc.name if svc else default_service
        impl = self.connector_factory(
            conn.type,
            conn.config,
            default_service=default_service,
            credential=resolve_credential(conn.credentials_ref),
        )
        with METRICS.timer("opsmith_stage_seconds", stage="ingest"):
            result = impl.fetch(start, end, dict(conn.cursor))
        batches = self.store_signals(
            session, conn, project, result.logs, result.metrics, start, end
        )
        conn.cursor = {**result.cursor, "last_end": end.isoformat()}
        conn.last_run_at = utcnow()
        METRICS.inc(
            "opsmith_ingested_records_total", len(result.logs) + len(result.metrics), kind=conn.kind
        )
        if batches:
            queue.enqueue(
                session,
                tenant_id=project.tenant_id,
                kind="build_bundles",
                payload={"project_id": project.id},
                idempotency_key=f"bundles:{conn.id}:{end.isoformat()}",
            )
        return {"batches": [b.id for b in batches], "records": sum(b.record_count for b in batches)}

    # ------------------------------------------------------------------ push mode
    def ingest_push(
        self, session: Session, conn: Connector, records: list[dict[str, Any]]
    ) -> list[RawBatch]:
        project = session.get(Project, conn.project_id)
        assert project is not None
        if conn.kind == "logs":
            logs = [LogEvent.model_validate(r) for r in records]
            metrics: list[MetricPoint] = []
            stamps = [e.timestamp for e in logs]
        else:
            logs = []
            metrics = [MetricPoint.model_validate(r) for r in records]
            stamps = [m.timestamp for m in metrics]
        if not stamps:
            return []
        start, end = min(stamps), max(stamps) + timedelta(seconds=1)
        batches = self.store_signals(session, conn, project, logs, metrics, start, end)
        queue.enqueue(
            session,
            tenant_id=project.tenant_id,
            kind="build_bundles",
            payload={"project_id": project.id},
            idempotency_key=f"bundles:push:{uuid.uuid4().hex}",
        )
        return batches

    # ------------------------------------------------------------------ normalize/store
    def store_signals(
        self,
        session: Session,
        conn: Connector,
        project: Project,
        logs: list[LogEvent],
        metrics: list[MetricPoint],
        start: datetime,
        end: datetime,
    ) -> list[RawBatch]:
        settings = ProjectSettings.model_validate(project.settings)
        redactor = Redactor(settings.redaction_extra_patterns)
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        counts: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
        for ev in logs:
            key = (ev.service, ev.environment, "logs")
            c = counts[key]
            record = ev.model_dump(mode="json")
            record["severity"] = normalize_severity(ev.severity)
            record["message"] = redactor.redact(record["message"], c)
            record["attributes"] = redactor.redact(record["attributes"], c)
            grouped[key].append(record)
        for mp in metrics:
            key = (mp.service, mp.environment, "metrics")
            record = mp.model_dump(mode="json")
            record["labels"] = redactor.redact(record["labels"], counts[key])
            grouped[key].append(record)

        batches: list[RawBatch] = []
        for (service_name, env, kind), records in grouped.items():
            svc = resolve_service(session, project, service_name, env)
            for rec in records:  # enrichment
                if kind == "logs":
                    rec["attributes"] = {
                        **rec["attributes"],
                        "service.owner": svc.owner,
                        "service.repository_id": svc.repository_id,
                    }
            object_key = (
                f"{project.tenant_id}/{project.id}/{svc.id}/{kind}/"
                f"{start:%Y%m%dT%H%M%S}-{conn.id}-{uuid.uuid4().hex[:8]}.jsonl"
            )
            n = self.store.put_jsonl(object_key, records)
            batch = RawBatch(
                tenant_id=project.tenant_id,
                project_id=project.id,
                connector_id=conn.id,
                service_id=svc.id,
                kind=kind,
                window_start=start,
                window_end=end,
                object_key=object_key,
                record_count=n,
                redactions=dict(counts[(service_name, env, kind)]),
            )
            session.add(batch)
            batches.append(batch)
        session.flush()
        return batches


class BundleBuilder:
    def __init__(self, store: ObjectStore, max_tokens: int = 12_000) -> None:
        self.store = store
        self.max_tokens = max_tokens

    def build_for_project(self, session: Session, project_id: str) -> list[AnalysisBundle]:
        pending = session.scalars(
            select(RawBatch)
            .where(RawBatch.project_id == project_id, RawBatch.bundle_id.is_(None))
            .order_by(RawBatch.window_start)
        ).all()
        by_service: dict[str, list[RawBatch]] = defaultdict(list)
        for batch in pending:
            by_service[batch.service_id].append(batch)
        bundles = [self._build(session, sid, batches) for sid, batches in by_service.items()]
        return bundles

    def _build(self, session: Session, service_id: str, batches: list[RawBatch]) -> AnalysisBundle:
        first = batches[0]
        bundle_id = new_id("bnd")
        window_start = min(b.window_start for b in batches)
        window_end = max(b.window_end for b in batches)
        service = session.get(Service, service_id)
        service_name = service.name if service else service_id

        events: list[tuple[dict[str, Any], str]] = []
        points: list[MetricPoint] = []
        redactions: Counter[str] = Counter()
        for batch in batches:
            redactions.update(batch.redactions)
            for line_no, rec in enumerate(self.store.iter_jsonl(batch.object_key), start=1):
                if batch.kind == "logs":
                    events.append((rec, f"obj:{batch.object_key}#L{line_no}"))
                else:
                    points.append(MetricPoint.model_validate(rec))

        templates = self._templates(bundle_id, service_name, events)
        metric_start = min(
            (b.window_start for b in batches if b.kind == "metrics"), default=window_start
        )
        features = compute_features(points, metric_start)
        for feat in features:
            feat["ref"] = f"bundle:{bundle_id}#metric:{feat['series']}"

        sev_counts = Counter(e["severity"] for e, _ in events)
        errors = sev_counts.get("error", 0) + sev_counts.get("critical", 0)
        stats = {
            "events": len(events),
            "by_severity": dict(sev_counts),
            "error_rate": round(errors / len(events), 4) if events else 0.0,
            "templates": len(templates),
            "metric_series": len(features),
            "anomalies": sum(1 for f in features if f["anomalous"]),
            "redactions": dict(redactions),
        }
        templates, features = self._fit(templates, features)
        bundle = AnalysisBundle(
            id=bundle_id,
            tenant_id=first.tenant_id,
            project_id=first.project_id,
            service_id=service_id,
            window_start=window_start,
            window_end=window_end,
            log_templates=templates,
            metric_features=features,
            raw_refs=[b.object_key for b in batches],
            stats=stats,
            token_estimate=_tokens({"t": templates, "m": features, "s": stats}),
        )
        session.add(bundle)
        for batch in batches:
            batch.bundle_id = bundle_id
        session.flush()
        return bundle

    def _templates(
        self, bundle_id: str, service: str, events: list[tuple[dict[str, Any], str]]
    ) -> list[dict[str, Any]]:
        miner = TemplateMiner()
        for i, (rec, _) in enumerate(events):
            miner.add(rec["message"], i)
        out: list[dict[str, Any]] = []
        for cluster in miner.clusters():
            members = [events[i] for i in cluster.member_indices]
            severities = Counter(rec["severity"] for rec, _ in members)
            max_sev = max(severities, key=lambda s: SEVERITY_RANK.get(_sev_alias(s), 0))
            locations: list[dict[str, Any]] = []
            for rec, _ in members[:50]:
                texts = [rec["message"]] + [
                    str(rec["attributes"].get(k)) for k in _LOC_ATTRS if rec["attributes"].get(k)
                ]
                for path, line in extract_locations("\n".join(texts)):
                    loc = {"path": path, "line": line}
                    if loc not in locations:
                        locations.append(loc)
            fp = fingerprint_of(service, cluster.template)
            stamps = sorted(rec["timestamp"] for rec, _ in members)
            versions = sorted({str(rec["version"]) for rec, _ in members if rec.get("version")})
            out.append(
                {
                    "ref": f"bundle:{bundle_id}#tpl:{fp}",
                    "signal": f"log:{fp}",
                    "fingerprint": fp,
                    "template": cluster.template[:500],
                    "count": cluster.count,
                    "severities": dict(severities),
                    "max_severity": max_sev,
                    "first_seen": stamps[0],
                    "last_seen": stamps[-1],
                    "samples": [rec["message"][:SAMPLE_CHARS] for rec, _ in members[:MAX_SAMPLES]],
                    "locations": locations[:10],
                    "event_refs": [ref for _, ref in members[:5]],
                    "versions": versions[:5],
                    "_weight": cluster.count * _SEV_WEIGHT.get(max_sev, 0.5),
                }
            )
        out.sort(key=lambda t: t["_weight"], reverse=True)
        for t in out:
            t.pop("_weight")
        return out[:MAX_TEMPLATES]

    def _fit(
        self, templates: list[dict[str, Any]], features: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Trim the bundle to the token budget: fewer samples first, then low-weight rows."""
        if _tokens({"t": templates, "m": features}) <= self.max_tokens:
            return templates, features
        for t in templates:
            t["samples"] = t["samples"][:1]
            t["event_refs"] = t["event_refs"][:2]
        features = sorted(features, key=lambda f: (not f["anomalous"], f["series"]))
        while templates and _tokens({"t": templates, "m": features}) > self.max_tokens:
            templates = templates[:-1]
        while features and _tokens({"t": templates, "m": features}) > self.max_tokens:
            features = features[:-1]
        return templates, features


def _sev_alias(sev: str) -> str:
    return {
        "warning": "medium",
        "error": "high",
        "critical": "critical",
        "info": "info",
        "debug": "info",
    }.get(sev, "info")


def _tokens(obj: Any) -> int:
    return len(json.dumps(obj, default=str)) // 4
