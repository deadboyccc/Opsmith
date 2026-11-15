"""Pull request description rendering - every PR links back to its task and evidence."""

from __future__ import annotations

from typing import Any

from opsmith.models import ExecutionRun, Task

MARKER = "<!-- opsmith:task={task_id} -->"


def render_pr_body(
    task: Task,
    run: ExecutionRun,
    checks: list[dict[str, Any]],
    *,
    base_url: str = "",
    reviewer: str = "",
) -> str:
    def bullet(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    evidence = []
    for ev in task.evidence[:12]:
        label = ev.get("title") or ev.get("type", "evidence")
        refs = ", ".join(f"`{r}`" for r in (ev.get("refs") or [])[:3])
        where = f" ({ev['path']}:{ev.get('line') or ''})" if ev.get("path") else ""
        evidence.append(f"[{ev.get('source', ev.get('type', ''))}] {label}{where} {refs}".strip())
    check_rows = (
        "\n".join(
            f"| {c['name']} | `{c['command']}` | {'pass' if c['exit_code'] == 0 else 'FAIL'} |"
            for c in checks
        )
        or "| (none configured) | | |"
    )
    prov = task.provenance
    link = f"{base_url.rstrip('/')}/ui/tasks/{task.id}" if base_url else f"task `{task.id}`"
    return f"""## Problem
{task.problem_statement}

## Evidence
{bullet(evidence)}

## Approach
{task.proposed_approach}

## Acceptance criteria
{bullet([str(c) for c in task.acceptance_criteria])}

## Verification
Checks run in the Opsmith sandbox before opening this PR:

| Check | Command | Result |
|---|---|---|
{check_rows}

Tests to add: {", ".join(task.verification.get("tests_to_add", [])) or "n/a"}
Metrics to watch after deploy: {", ".join(task.verification.get("metrics_to_watch", [])) or "n/a"}

## Risk
{task.risk_assessment} (effort {task.effort_estimate}, priority {task.priority}, \
confidence {task.confidence:.2f})

## Traceability
- Opsmith task: {link} (approved by {reviewer or "a human reviewer"})
- Execution run: `{run.id}` (attempt {run.attempt}, agent `{run.agent}`)
- Findings: {", ".join(f"`{f}`" for f in prov.get("finding_ids", [])[:10]) or "n/a"}
- Inference runs: {", ".join(f"`{r}`" for r in prov.get("inference_run_ids", [])) or "n/a"}
- Audit runs: {", ".join(f"`{r}`" for r in prov.get("audit_run_ids", [])) or "n/a"}
- Leads: {", ".join(f"`{r}`" for r in prov.get("lead_ids", [])) or "n/a"}

> Opened by Opsmith. Opsmith never merges: a human must review and merge this PR.

{MARKER.format(task_id=task.id)}
"""
