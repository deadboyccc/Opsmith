"""Ingestion + ETL: fetch -> parse/normalize -> enrich -> redact -> store -> bundle.

Raw (normalized, redacted) records live in object storage; ``AnalysisBundle`` rows hold
the compact, LLM-sized summary and reference the raw objects by key (spec 5.1).

Signals are untrusted and arrive on a worker shared by every tenant, so this module is also
where their size and shape are bounded: over-long strings are truncated and non-finite
metric values dropped in ``store_signals`` (the one place every record passes through),
a connector reads at most ``MAX_WINDOW`` per run, and a service whose batches cannot be
bundled fails on its own without holding up the others.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from opsmith import audit_log, queue
from opsmith.config import CredentialRefError, resolve_tenant_credential
from opsmith.connectors import build_connector
from opsmith.connectors.base import BaseConnector, supported_keywords
from opsmith.db import new_id, utcnow
from opsmith.etl.features import compute_features, series_key
from opsmith.etl.templates import TemplateMiner, extract_locations, fingerprint_of
from opsmith.localfs import LocalAccessError
from opsmith.models import AnalysisBundle, Connector, Project, RawBatch, Service
from opsmith.observability import METRICS
from opsmith.redaction import Redactor
from opsmith.schemas import SEVERITY_RANK, LogEvent, MetricPoint, ProjectSettings
from opsmith.storage import ObjectStore

log = logging.getLogger(__name__)

SEVERITY_ALIASES = {
    "trace": "debug",
    "debug": "debug",
    "verbose": "debug",
    "fine": "debug",
    "finer": "debug",
    "finest": "debug",
    "info": "info",
    "information": "info",
    "informational": "info",
    "notice": "info",
    "warn": "warning",
    "warning": "warning",
    "error": "error",
    "err": "error",
    "severe": "error",
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
# Longest message / attribute / label string kept. Longer ones are truncated before any
# pattern (redaction, template masks, location extraction) runs over them.
MAX_FIELD_CHARS = 16 * 1024
# A pull connector reads up to ``now - lag``: sources keep receiving lines stamped slightly
# in the past, and a window that ended at "now" would never see them.
DEFAULT_LAG_SECONDS = 60.0
# Longest window one run reads. A connector that fell behind catches up one window per run.
MAX_WINDOW = timedelta(hours=24)
# How far back, and over how many stored batches, a baseline is looked up for metrics that
# arrive without one (pushed metrics carry no look-back).
BASELINE_LOOKBACK = timedelta(hours=6)
MAX_BASELINE_BATCHES = 200
# Label added when redaction changed a label value, so the series stay apart (see store_signals).
LABEL_DIGEST = "label_digest"
_UNCOMMITTED = "opsmith_uncommitted_objects"


class ConnectorDisabledError(RuntimeError):
    """Signals were pushed to a connector that is switched off."""


class BundleBuildError(RuntimeError):
    """One or more services could not be bundled. The other services' bundles are kept."""


class _Contended(Exception):
    """Another builder claimed some of the batches this one was about to bundle."""


def normalize_severity(value: str) -> str:
    text = value.strip().strip("[]:").lower()
    if text.isdigit() and int(text) >= 10:  # pino/bunyan: 10 trace, 20 debug ... 60 fatal
        level = int(text)
        if level >= 60:
            return "critical"
        return {5: "error", 4: "warning", 3: "info"}.get(level // 10, "debug")
    return SEVERITY_ALIASES.get(text, "info")


def _cap(value: Any) -> Any:
    """Truncate over-long strings (recursively) and say so in the text."""
    if isinstance(value, str):
        if len(value) <= MAX_FIELD_CHARS:
            return value
        return f"{value[:MAX_FIELD_CHARS]}...[truncated {len(value) - MAX_FIELD_CHARS} chars]"
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap(v) for v in value]
    return value


def _label_digest(scope: str, labels: dict[str, str]) -> str:
    raw = json.dumps([scope, sorted(labels.items())], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def _lag_seconds(config: dict[str, Any]) -> float:
    try:
        lag = float(config.get("lag_seconds", DEFAULT_LAG_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_LAG_SECONDS
    return lag if math.isfinite(lag) and lag >= 0 else DEFAULT_LAG_SECONDS


def _reached(cursor: dict[str, Any], start: datetime, end: datetime) -> datetime:
    """Where the connector stopped reading: ``end``, unless it reports an earlier point."""
    raw = cursor.get("last_end")
    try:
        stopped = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        stopped = None
    if stopped is not None and stopped.tzinfo is not None and start < stopped < end:
        return stopped
    return end


def _instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


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
    def __init__(
        self,
        store: ObjectStore,
        connector_factory: ConnectorFactory = build_connector,
        *,
        local_root: Path | None = None,
        local_deny: tuple[Path, ...] = (),
    ) -> None:
        """``local_root`` is the only directory file connectors may read (``None`` disables
        them) and ``local_deny`` the directories inside it that stay off limits."""
        self.store = store
        self.connector_factory = connector_factory
        self.local_root = local_root
        self.local_deny = tuple(local_deny)

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
        conn_id, conn_name, conn_kind = conn.id, conn.name, conn.kind
        seen_run_at = conn.last_run_at  # this run's view of the connector, checked on write
        if end is None:
            end = utcnow() - timedelta(seconds=_lag_seconds(conn.config))
        last_end = conn.cursor.get("last_end")
        start = (
            datetime.fromisoformat(last_end)
            if last_end
            else end - timedelta(seconds=conn.interval_seconds or 900)
        )
        if start >= end:
            return {"skipped": "empty window"}
        end = min(end, start + MAX_WINDOW)
        default_service = "unknown"
        if conn.service_id:
            svc = session.get(Service, conn.service_id)
            default_service = svc.name if svc else default_service
        try:
            credential = resolve_tenant_credential(conn.credentials_ref)
        except CredentialRefError as exc:
            raise queue.PermanentFailure(f"connector {conn_name}: {exc}") from exc
        confinement = supported_keywords(
            self.connector_factory,
            {"local_root": self.local_root, "local_deny": self.local_deny},
        )
        try:
            impl = self.connector_factory(
                conn.type,
                conn.config,
                default_service=default_service,
                credential=credential,
                **confinement,
            )
            with METRICS.timer("opsmith_stage_seconds", stage="ingest"):
                result = impl.fetch(start, end, dict(conn.cursor))
        except LocalAccessError as exc:  # a path outside the local root: retrying cannot help
            raise queue.PermanentFailure(f"connector {conn_name}: {exc}") from exc
        end = _reached(result.cursor, start, end)
        dropped: Counter[str] = Counter()
        batches = self.store_signals(
            session, conn, project, result.logs, result.metrics, start, end, dropped=dropped
        )
        # Compare-and-set: a second run of this connector that read the same cursor must not
        # also store the window. Whichever commits second finds the connector changed.
        unchanged = (
            Connector.last_run_at.is_(None)
            if seen_run_at is None
            else Connector.last_run_at == seen_run_at
        )
        advanced = session.execute(
            update(Connector)
            .where(Connector.id == conn_id, unchanged)
            .values(cursor={**result.cursor, "last_end": end.isoformat()}, last_run_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if getattr(advanced, "rowcount", 0) != 1:
            raise queue.RetryLater(30, f"connector {conn_name} was advanced by a concurrent run")
        session.expire(conn)
        METRICS.inc(
            "opsmith_ingested_records_total", len(result.logs) + len(result.metrics), kind=conn_kind
        )
        skipped = int(getattr(result, "skipped", 0) or 0) + sum(dropped.values())
        if skipped:
            METRICS.inc("opsmith_skipped_records_total", skipped, kind=conn_kind)
            log.warning(
                "connector skipped unusable records",
                extra={"connector_id": conn_id, "skipped": skipped},
            )
        if batches:
            queue.enqueue(
                session,
                tenant_id=project.tenant_id,
                kind="build_bundles",
                payload={"project_id": project.id},
                idempotency_key=f"bundles:{conn_id}:{end.isoformat()}",
            )
        return {
            "batches": [b.id for b in batches],
            "records": sum(b.record_count for b in batches),
            "skipped_records": skipped,
            "window": [start.isoformat(), end.isoformat()],
        }

    # ------------------------------------------------------------------ push mode
    def ingest_push(
        self, session: Session, conn: Connector, records: list[dict[str, Any]]
    ) -> list[RawBatch]:
        if not conn.enabled:
            raise ConnectorDisabledError(f"connector {conn.name!r} is disabled")
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
        *,
        dropped: Counter[str] | None = None,
    ) -> list[RawBatch]:
        """Normalize, bound, redact and store the signals; one ``RawBatch`` per service/kind.

        Records that cannot be used (non-finite metric values) are left out and counted in
        ``dropped`` by reason.
        """
        settings = ProjectSettings.model_validate(project.settings)
        redactor = Redactor(settings.redaction_extra_patterns)
        dropped = dropped if dropped is not None else Counter()
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        counts: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
        for ev in logs:
            key = (ev.service, ev.environment, "logs")
            c = counts[key]
            record = ev.model_dump(mode="json")
            record["severity"] = normalize_severity(ev.severity)
            record["message"] = redactor.redact(_cap(record["message"]), c)
            record["attributes"] = redactor.redact(_cap(record["attributes"]), c)
            for name in ("trace_id", "correlation_id", "version"):
                if record[name] is not None:
                    record[name] = redactor.redact(_cap(record[name]), c)
            grouped[key].append(record)
        for mp in metrics:
            if not math.isfinite(mp.value):
                dropped["non_finite_value"] += 1
                continue
            key = (mp.service, mp.environment, "metrics")
            record = mp.model_dump(mode="json")
            labels = _cap(record["labels"])
            # Values only: a label *named* like a credential (kube `secret="db-creds"`) names
            # an object, it does not hold one, so labels get no key-based masking.
            redacted = {k: redactor.redact(v, counts[key]) for k, v in labels.items()}
            if redacted != labels:
                # Redaction can make two series look alike (instance=10.0.0.1 and 10.0.0.2
                # both become [REDACTED:ipv4]) and their points would then be averaged as
                # one. A digest of the original labels keeps them apart without storing them.
                redacted = {**redacted, LABEL_DIGEST: _label_digest(project.id, labels)}
            record["labels"] = redacted
            grouped[key].append(record)
        if dropped["non_finite_value"]:
            METRICS.inc(
                "opsmith_dropped_records_total",
                dropped["non_finite_value"],
                reason="non_finite_value",
            )

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
            self._discard_on_rollback(session, object_key)
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

    def _discard_on_rollback(self, session: Session, object_key: str) -> None:
        """Best effort: remove an object whose transaction is rolled back instead of committed.

        Object storage is not transactional, so a failure after the write would otherwise
        leave a stored object no ``RawBatch`` refers to. Needs a store with ``delete``.
        """
        delete = getattr(self.store, "delete", None)
        if delete is None:
            return
        if _UNCOMMITTED not in session.info:

            def _forget(sess: Session) -> None:
                sess.info[_UNCOMMITTED] = []

            def _discard(sess: Session) -> None:
                pending, sess.info[_UNCOMMITTED] = sess.info.get(_UNCOMMITTED, []), []
                for remove, key in pending:
                    try:
                        remove(key)
                    except Exception:  # cleanup must never mask the original failure
                        log.warning("could not remove orphaned object", extra={"key": key})

            event.listen(session, "after_commit", _forget)
            event.listen(session, "after_rollback", _discard)
            session.info[_UNCOMMITTED] = []
        session.info[_UNCOMMITTED].append((delete, object_key))


class BundleBuilder:
    def __init__(self, store: ObjectStore, max_tokens: int = 12_000) -> None:
        self.store = store
        self.max_tokens = max_tokens

    def build_for_project(self, session: Session, project_id: str) -> list[AnalysisBundle]:
        """Bundle every service's pending batches.

        Each service is built on its own: when one cannot be bundled, the others' bundles
        are committed and ``BundleBuildError`` is raised, so the job fails (and retries) for
        the services that are still pending instead of holding everything back.
        """
        service_ids = session.scalars(
            select(RawBatch.service_id)
            .where(RawBatch.project_id == project_id, RawBatch.bundle_id.is_(None))
            .distinct()
        ).all()
        bundles: list[AnalysisBundle] = []
        failures: list[str] = []
        for service_id in sorted(service_ids):
            try:
                bundle = self._build_service(session, project_id, service_id)
            except Exception as exc:
                log.exception("bundle build failed", extra={"service_id": service_id})
                failures.append(f"service {service_id}: {type(exc).__name__}: {exc}")
                continue
            if bundle is not None:
                bundles.append(bundle)
        if failures:
            session.commit()
            raise BundleBuildError("; ".join(failures)[:2000])
        return bundles

    def _build_service(
        self, session: Session, project_id: str, service_id: str
    ) -> AnalysisBundle | None:
        for _ in range(3):
            batches = session.scalars(
                select(RawBatch)
                .where(
                    RawBatch.project_id == project_id,
                    RawBatch.service_id == service_id,
                    RawBatch.bundle_id.is_(None),
                )
                .order_by(RawBatch.window_start)
                .execution_options(populate_existing=True)
            ).all()
            if not batches:
                return None
            bundle = self._build(session, service_id, list(batches))  # reads objects, no writes
            batch_ids = [b.id for b in batches]
            try:
                with session.begin_nested():
                    session.add(bundle)
                    # Compare-and-set: only batches nobody bundled meanwhile are claimed. If
                    # another builder got there first, this bundle is discarded and the
                    # service is looked at again.
                    claimed = session.execute(
                        update(RawBatch)
                        .where(RawBatch.id.in_(batch_ids), RawBatch.bundle_id.is_(None))
                        .values(bundle_id=bundle.id)
                    )
                    if getattr(claimed, "rowcount", 0) != len(batch_ids):
                        raise _Contended
            except _Contended:
                continue
            return bundle
        raise BundleBuildError("its batches kept being claimed by another builder")

    def _build(self, session: Session, service_id: str, batches: list[RawBatch]) -> AnalysisBundle:
        first = batches[0]
        bundle_id = new_id("bnd")
        window_start = min(b.window_start for b in batches)
        window_end = max(b.window_end for b in batches)
        service = session.get(Service, service_id)
        service_name = service.name if service else service_id
        metric_start = min(
            (b.window_start for b in batches if b.kind == "metrics"), default=window_start
        )

        events: list[tuple[dict[str, Any], str]] = []
        points: list[MetricPoint] = []
        # Consecutive metric batches each carry a baseline look-back that overlaps the
        # previous window; a point is counted once however many batches hold it.
        seen_points: set[tuple[str, datetime]] = set()

        def add_points(object_key: str, before: datetime | None = None) -> None:
            for rec in self.store.iter_jsonl(object_key):
                point = MetricPoint.model_validate(rec)
                identity = (series_key(point), point.timestamp)
                if identity in seen_points or (before is not None and point.timestamp >= before):
                    continue
                seen_points.add(identity)
                points.append(point)

        redactions: Counter[str] = Counter()
        for batch in batches:
            redactions.update(batch.redactions)
            if batch.kind == "metrics":
                add_points(batch.object_key)
                continue
            for line_no, rec in enumerate(self.store.iter_jsonl(batch.object_key), start=1):
                events.append((rec, f"obj:{batch.object_key}#L{line_no}"))
        if points and not any(p.timestamp < metric_start for p in points):
            # Nothing predates the window (pushed metrics have no look-back), so the
            # baseline comes from what this service stored before.
            earlier = session.scalars(
                select(RawBatch)
                .where(
                    RawBatch.tenant_id == first.tenant_id,
                    RawBatch.service_id == service_id,
                    RawBatch.kind == "metrics",
                    RawBatch.bundle_id.is_not(None),
                    RawBatch.window_start < metric_start,
                    RawBatch.window_end > metric_start - BASELINE_LOOKBACK,
                )
                .order_by(RawBatch.window_start.desc())
                .limit(MAX_BASELINE_BATCHES)
            ).all()
            for batch in earlier:
                add_points(batch.object_key, before=metric_start)

        templates = self._templates(bundle_id, service_name, events)
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
        return AnalysisBundle(
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
            stamps = sorted((rec["timestamp"] for rec, _ in members), key=_instant)
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
        """Trim the bundle to the token budget.

        Samples go first, then metric rows that show nothing unusual, then the lowest-weight
        log templates, and only last the anomalous metric rows. A service with many quiet
        series therefore never loses its log templates to them.
        """
        if _tokens({"t": templates, "m": features}) <= self.max_tokens:
            return templates, features
        for t in templates:
            t["samples"] = t["samples"][:1]
            t["event_refs"] = t["event_refs"][:2]
        features = sorted(features, key=lambda f: (not f["anomalous"], f["series"]))
        budget = self.max_tokens * 4  # characters of JSON

        def trim(
            rows: list[dict[str, Any]], others: list[dict[str, Any]], quiet_only: bool = False
        ) -> list[dict[str, Any]]:
            sizes = [len(json.dumps(row, default=str)) + 2 for row in rows]
            total = len(json.dumps(others, default=str)) + 16 + sum(sizes)
            keep = len(rows)
            while keep and total > budget and not (quiet_only and rows[keep - 1]["anomalous"]):
                keep -= 1
                total -= sizes[keep]
            return rows[:keep]

        features = trim(features, templates, quiet_only=True)
        templates = trim(templates, features)
        features = trim(features, templates)
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
