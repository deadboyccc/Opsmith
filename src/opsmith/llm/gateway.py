"""The single choke point between Opsmith and any LLM.

Responsibilities (spec 7, 8): provider allowlist, per-stage model selection, defensive
redaction, budget enforcement, structured output validated against a pydantic schema
(with one corrective retry), and usage/cost recording for every call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsmith.db import utcnow
from opsmith.llm.base import (
    BudgetExceeded,
    LLMError,
    LLMOutputError,
    LLMProvider,
    ProviderNotAllowed,
    ProviderRequest,
    ProviderResponse,
)
from opsmith.llm.pricing import cost_usd
from opsmith.llm.prompts import corrective_retry, get_prompt
from opsmith.models import Project, UsageRecord
from opsmith.observability import METRICS
from opsmith.redaction import Redactor
from opsmith.schemas import MAX_MODEL_CHARS, ProjectSettings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult[T: BaseModel]:
    output: T
    provider: str
    model: str
    prompt_id: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class BudgetGuard:
    """Token/cost caps per run, per project per day."""

    def __init__(self, session: Session, project: Project) -> None:
        self.session = session
        self.project = project
        self.budgets = ProjectSettings.model_validate(project.settings).budgets

    def spent_today(self) -> float:
        since = utcnow() - timedelta(days=1)
        total = self.session.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(
                UsageRecord.project_id == self.project.id, UsageRecord.created_at >= since
            )
        )
        return float(total or 0.0)

    def run_usage(self, run_id: str | None) -> tuple[int, float]:
        if not run_id:
            return 0, 0.0
        row = self.session.execute(
            select(
                func.coalesce(func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0),
                func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
            ).where(UsageRecord.run_id == run_id)
        ).one()
        return int(row[0]), float(row[1])

    def check(
        self,
        run_id: str | None,
        estimated_tokens: int,
        *,
        call_tokens: int = 0,
        call_cost: float = 0.0,
    ) -> None:
        """``call_tokens``/``call_cost``: what the current gateway call has already spent. They
        stand in for the run's recorded usage when there is no ``run_id`` to look it up by, so
        the per-run caps also bound a corrective retry of a run-less stage."""
        if self.spent_today() >= self.budgets.max_cost_per_day_usd:
            raise BudgetExceeded(
                f"project {self.project.name}: daily LLM budget "
                f"${self.budgets.max_cost_per_day_usd:.2f} exhausted"
            )
        tokens, cost = self.run_usage(run_id) if run_id else (call_tokens, call_cost)
        if tokens + estimated_tokens > self.budgets.max_tokens_per_run:
            raise BudgetExceeded(
                f"run {run_id}: token cap {self.budgets.max_tokens_per_run} would be exceeded"
            )
        if cost >= self.budgets.max_cost_per_run_usd:
            raise BudgetExceeded(f"run {run_id}: cost cap ${self.budgets.max_cost_per_run_usd}")


class LLMGateway:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        *,
        process_allowlist: list[str],
    ) -> None:
        self.providers = providers
        self.process_allowlist = set(process_allowlist)

    def allowed(self, settings: ProjectSettings) -> set[str]:
        allowed = set(self.process_allowlist)
        if settings.llm.provider_allowlist is not None:
            allowed &= set(settings.llm.provider_allowlist)
        return allowed

    @staticmethod
    def redactor(settings: ProjectSettings) -> Redactor:
        """The redactor applied to everything sent to a provider. A caller that matches model
        output against identifiers it sent (evidence refs, signals) must compare against their
        redacted form, because that is the only form the model ever saw."""
        return Redactor(settings.redaction_extra_patterns)

    def structured(
        self,
        session: Session,
        *,
        project: Project,
        stage: str,
        prompt_id: str,
        variables: dict[str, Any],
        schema: type[T],
        run_id: str | None = None,
    ) -> LLMResult[T]:
        settings = ProjectSettings.model_validate(project.settings)
        cfg = settings.llm.for_stage(stage)
        if cfg.provider not in self.allowed(settings) or cfg.provider not in self.providers:
            raise ProviderNotAllowed(
                f"provider {cfg.provider!r} is not allowed/configured for stage {stage!r}"
            )
        provider = self.providers[cfg.provider]
        # Defence in depth: ETL already redacted, but nothing leaves unredacted.
        safe_vars = self.redactor(settings).redact(variables)
        prompt = get_prompt(prompt_id)
        system, user = prompt.render(safe_vars)
        guard = BudgetGuard(session, project)

        def worst_case_tokens(user_text: str) -> int:
            # ~4 characters per input token, plus the whole output allowance (already tokens).
            return (len(system) + len(user_text)) // 4 + cfg.max_tokens

        guard.check(run_id, worst_case_tokens(user))

        request = ProviderRequest(
            stage=stage,
            prompt_id=prompt_id,
            model=cfg.model,
            system=system,
            user=user,
            schema=schema,
            variables=safe_vars,
            max_tokens=cfg.max_tokens,
            effort=cfg.effort,
        )
        total_in = total_out = 0
        total_cost = 0.0
        last_error: str | None = None
        model_used = cfg.model

        def record(response: ProviderResponse, latency_ms: int) -> tuple[str, float]:
            # The id a provider reports is its own text; clipped to the columns it is stored in.
            model = (response.model or cfg.model)[:MAX_MODEL_CHARS]
            call_cost = cost_usd(
                model,
                response.input_tokens,
                response.output_tokens,
                response.cache_read_tokens,
                fallback_model=cfg.model,
                prices=cfg.prices,
            )
            session.add(
                UsageRecord(
                    tenant_id=project.tenant_id,
                    project_id=project.id,
                    stage=stage,
                    run_id=run_id,
                    provider=provider.name,
                    model=model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=call_cost,
                    latency_ms=latency_ms,
                )
            )
            session.flush()
            METRICS.inc("opsmith_llm_calls_total", stage=stage, provider=provider.name)
            METRICS.inc("opsmith_llm_cost_usd_total", call_cost, stage=stage)
            METRICS.observe("opsmith_llm_latency_seconds", latency_ms / 1000, stage=stage)
            return model, call_cost

        for attempt in range(2):
            if attempt == 1:
                guard.check(
                    run_id,
                    worst_case_tokens(request.user),
                    call_tokens=total_in + total_out,
                    call_cost=total_cost,
                )
            started = time.perf_counter()
            try:
                response = provider.complete(request)
            except LLMError as exc:
                # A refusal or truncation was still billed: record it, then let it surface.
                if exc.usage is not None:
                    record(exc.usage, int((time.perf_counter() - started) * 1000))
                raise
            except Exception as exc:
                # A provider's own exception type (an SDK's rate-limit or connection error)
                # means nothing to the stages, which handle ``LLMError``: it would pass them
                # by and roll back the job, together with the usage of calls already billed.
                raise LLMError(
                    f"{provider.name} call failed: {type(exc).__name__}: {str(exc)[:300]}"
                ) from exc
            model_used, call_cost = record(response, int((time.perf_counter() - started) * 1000))
            total_in += response.input_tokens
            total_out += response.output_tokens
            total_cost += call_cost
            try:
                data = response.data
                parsed = (
                    schema.model_validate_json(data)
                    if isinstance(data, str)
                    else schema.model_validate(data)
                )
            except ValidationError as exc:
                last_error = str(exc)[:2000]
                log.warning(
                    "LLM output failed validation", extra={"stage": stage, "attempt": attempt}
                )
                request.user = corrective_retry(user, last_error)
                continue
            return LLMResult(
                output=parsed,
                provider=provider.name,
                model=model_used,
                prompt_id=prompt.id,
                prompt_version=prompt.version,
                input_tokens=total_in,
                output_tokens=total_out,
                cost_usd=round(total_cost, 6),
            )
        raise LLMOutputError(f"stage {stage}: output failed validation twice: {last_error}")
