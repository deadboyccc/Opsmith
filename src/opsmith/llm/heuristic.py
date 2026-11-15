"""Deterministic, offline "model" implementing every stage contract.

It makes Opsmith fully functional without an API key (local dev, CI, air-gapped installs)
and gives tests reproducible behaviour. It reads the structured ``variables`` the gateway
passes alongside the rendered prompt; its output is validated exactly like a remote
model's.
"""

from __future__ import annotations

import math
import re
from typing import Any

from opsmith.llm.base import ProviderRequest, ProviderResponse

_PLACEHOLDER = re.compile(r"(?i)example|dummy|placeholder|changeme|xxx+|test|fake|sample")
_VENDORED = re.compile(r"(^|/)(node_modules|vendor|third_party|\.venv|venv|dist|build)/")
_TEST_PATH = re.compile(r"(^|/)(tests?|fixtures?|examples?|testdata|__tests__)/|_test\.|test_")
_PERF_WORDS = re.compile(r"(?i)timeout|timed out|slow|latency|deadline|too long|pool exhausted")
_PLACEHOLDER_TOKEN = re.compile(r"<(?:num|hex|uuid|ts|str|redacted|\*)>")


def _readable(template: str) -> str:
    """Turn a mined log template into a human-friendly title fragment."""
    return _PLACEHOLDER_TOKEN.sub("N", template)


_SEC_WORDS = re.compile(r"(?i)unauthori[sz]ed|forbidden|injection|csrf|xss|permission denied")


class HeuristicProvider:
    name = "heuristic"

    def __init__(
        self,
        *,
        lead_min_count: int = 3,
        finding_min_count: int = 50,
        confirm_min_count: int = 10,
    ) -> None:
        self.lead_min_count = lead_min_count
        self.finding_min_count = finding_min_count
        self.confirm_min_count = confirm_min_count

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        handler = {
            "inference.analyze": self._inference,
            "audit.triage": self._triage,
            "task.draft": self._task_draft,
        }.get(request.prompt_id)
        if handler is None:
            raise ValueError(f"heuristic provider has no implementation for {request.prompt_id}")
        data = handler(request.variables)
        approx_in = (len(request.system) + len(request.user)) // 4
        return ProviderResponse(
            data=data,
            input_tokens=approx_in,
            output_tokens=len(str(data)) // 4,
            model="heuristic-v1",
        )

    # ------------------------------------------------------------------ inference
    def _inference(self, v: dict[str, Any]) -> dict[str, Any]:
        logs: dict[str, dict[str, Any]] = {}
        metrics: dict[str, dict[str, Any]] = {}
        for bundle in v.get("bundles", []):
            for t in bundle.get("log_templates", []):
                agg = logs.setdefault(
                    t["signal"],
                    {
                        "count": 0,
                        "errors": 0,
                        "refs": [],
                        "locations": [],
                        "template": t["template"],
                        "max_severity": "info",
                        "samples": t.get("samples", []),
                    },
                )
                sev = t.get("severities", {})
                agg["count"] += t["count"]
                agg["errors"] += sev.get("error", 0) + sev.get("critical", 0)
                agg["refs"].extend([t["ref"], *t.get("event_refs", [])[:2]])
                agg["locations"].extend(t.get("locations", []))
                if sev.get("critical"):
                    agg["max_severity"] = "critical"
                elif agg["errors"] and agg["max_severity"] != "critical":
                    agg["max_severity"] = "error"
            for f in bundle.get("metric_features", []):
                prev = metrics.get(f["signal"])
                if prev is None or abs(f.get("z_score") or 0) > abs(prev.get("z_score") or 0):
                    metrics[f["signal"]] = {**f, "refs": [f["ref"]]}
        rejected = {r.get("signal") for r in v.get("rejections", []) if r.get("signal")}

        findings: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        new_leads: list[dict[str, Any]] = []
        handled: set[str] = set()

        for lead in v.get("leads", []):
            sig = lead.get("signal")
            if (not sig or (sig not in logs and sig not in metrics)) and lead.get("suspected_path"):
                sig = self._signal_for_path(logs, lead["suspected_path"]) or sig
            if sig and sig in logs and logs[sig]["errors"]:
                agg = logs[sig]
                handled.add(sig)
                human_asked = lead.get("source") == "review_request"
                if human_asked or agg["errors"] >= self.confirm_min_count or lead.get("age", 0):
                    findings.append(self._log_finding(sig, agg, lead_id=lead["id"], bonus=0.1))
                    updates.append(
                        {
                            "lead_id": lead["id"],
                            "action": "confirm",
                            "finding_index": len(findings) - 1,
                            "evidence_refs": agg["refs"][:3],
                            "context_update": f"Recurred: {agg['errors']} errors.",
                        }
                    )
                else:
                    updates.append(
                        {
                            "lead_id": lead["id"],
                            "action": "carry",
                            "evidence_refs": agg["refs"][:2],
                            "context_update": f"Seen again ({agg['errors']} errors).",
                        }
                    )
            elif sig and sig in metrics:
                m = metrics[sig]
                handled.add(sig)
                if m.get("anomalous"):
                    if lead.get("age", 0) >= 1 or abs(m.get("z_score") or 0) >= 6:
                        findings.append(self._metric_finding(sig, m, lead_id=lead["id"]))
                        updates.append(
                            {
                                "lead_id": lead["id"],
                                "action": "confirm",
                                "finding_index": len(findings) - 1,
                                "evidence_refs": m["refs"],
                                "context_update": "Anomaly persisted across windows.",
                            }
                        )
                    else:
                        updates.append(
                            {
                                "lead_id": lead["id"],
                                "action": "carry",
                                "evidence_refs": m["refs"],
                                "context_update": f"Still anomalous (z={m.get('z_score')}).",
                            }
                        )
                else:
                    updates.append(
                        {
                            "lead_id": lead["id"],
                            "action": "refute",
                            "evidence_refs": m["refs"],
                            "context_update": "Metric returned to baseline.",
                        }
                    )
            else:
                updates.append(
                    {
                        "lead_id": lead["id"],
                        "action": "carry",
                        "context_update": "Signal not observed in this window.",
                    }
                )

        for sig, agg in sorted(logs.items(), key=lambda kv: -kv[1]["errors"]):
            if sig in handled or not agg["errors"]:
                continue
            if agg["errors"] >= self.finding_min_count or agg["max_severity"] == "critical":
                finding = self._log_finding(sig, agg)
                if sig in rejected:
                    finding["confidence"] = round(finding["confidence"] * 0.6, 3)
                findings.append(finding)
            elif agg["errors"] >= self.lead_min_count:
                new_leads.append(
                    {
                        "title": f"Recurring error: {_readable(agg['template'])[:120]}",
                        "hypothesis": (
                            "Error template recurs; confirm whether it persists and grows."
                        ),
                        "evidence_refs": agg["refs"][:3],
                        "signal": sig,
                        "suspected_path": agg["locations"][0]["path"] if agg["locations"] else None,
                    }
                )
        for sig, m in sorted(metrics.items()):
            if sig in handled or not m.get("anomalous"):
                continue
            if abs(m.get("z_score") or 0) >= 6 and abs(m.get("delta_pct") or 0) >= 100:
                findings.append(self._metric_finding(sig, m))
            else:
                new_leads.append(
                    {
                        "title": f"Metric anomaly: {m['series']}",
                        "hypothesis": (
                            f"{m['name']} deviates from baseline (delta {m.get('delta_pct')}%, "
                            f"z={m.get('z_score')}); check whether it persists."
                        ),
                        "evidence_refs": m["refs"],
                        "signal": sig,
                    }
                )
        summary = (
            f"{len(logs)} log signals, {len(metrics)} metric series; {len(findings)} findings, "
            f"{len(new_leads)} new leads, {len(updates)} lead updates."
        )
        return {
            "summary": summary,
            "findings": findings,
            "lead_updates": updates,
            "new_leads": new_leads,
        }

    @staticmethod
    def _signal_for_path(logs: dict[str, dict[str, Any]], path: str) -> str | None:
        for sig, agg in logs.items():
            for loc in agg["locations"]:
                if loc["path"].endswith(path) or path.endswith(loc["path"]):
                    return sig
        return None

    @staticmethod
    def _log_finding(
        sig: str, agg: dict[str, Any], lead_id: str | None = None, bonus: float = 0.0
    ) -> dict[str, Any]:
        errors = agg["errors"]
        text = agg["template"] + " " + " ".join(agg.get("samples", []))
        category = "fix"
        if _PERF_WORDS.search(text):
            category = "performance"
        elif _SEC_WORDS.search(text):
            category = "security"
        severity = (
            "critical"
            if agg["max_severity"] == "critical"
            else ("high" if errors >= 100 else "medium")
        )
        confidence = min(0.95, 0.5 + math.log10(errors + 1) / 4 + bonus)
        loc = agg["locations"][0] if agg["locations"] else None
        return {
            "title": f"Errors in production: {_readable(agg['template'])[:140]}",
            "description": (
                f"Log template `{agg['template'][:300]}` produced {errors} error events "
                f"in the analyzed window(s)."
                + (f" Stack traces point at {loc['path']}:{loc['line']}." if loc else "")
            ),
            "category": category,
            "severity": severity,
            "confidence": round(confidence, 3),
            "evidence_refs": agg["refs"][:5],
            "signal": sig,
            "suspected_path": loc["path"] if loc else None,
            "suspected_line": loc["line"] if loc else None,
            "suggested_next_investigation": "Inspect the failing code path and recent deploys.",
            "from_lead_id": lead_id,
        }

    @staticmethod
    def _metric_finding(sig: str, m: dict[str, Any], lead_id: str | None = None) -> dict[str, Any]:
        z = abs(m.get("z_score") or 0)
        name = m["name"].lower()
        category = "performance" if re.search(r"latency|duration|cpu|memory|p9", name) else "fix"
        return {
            "title": f"Regression in {m['series']}",
            "description": (
                f"{m['name']} mean {m.get('mean')} vs baseline {m.get('baseline_mean')} "
                f"(delta {m.get('delta_pct')}%, p95 shift {m.get('p95_shift_pct')}%, z={z})."
            ),
            "category": category,
            "severity": "high" if z >= 6 else "medium",
            "confidence": round(min(0.9, 0.4 + min(z, 10) / 20), 3),
            "evidence_refs": m["refs"],
            "signal": sig,
            "suspected_path": None,
            "suggested_next_investigation": "Correlate with deploys and dependency latency.",
            "from_lead_id": lead_id,
        }

    # ------------------------------------------------------------------ audit triage
    def _triage(self, v: dict[str, Any]) -> dict[str, Any]:
        decisions = []
        for item in v.get("findings", []):
            path = item.get("path") or ""
            rule = item.get("rule_id") or ""
            snippet = item.get("snippet") or ""
            keep, confidence, why = True, float(item.get("confidence", 0.6)), "Deterministic hit."
            if _VENDORED.search(path):
                keep, why = False, "Vendored or generated code."
            elif (
                rule.startswith("secret")
                and _TEST_PATH.search(path)
                and (item.get("placeholder_value") or _PLACEHOLDER.search(snippet))
            ):
                keep, why = False, "Placeholder credential in test fixture."
            elif _TEST_PATH.search(path):
                confidence, why = max(0.2, confidence - 0.25), "Only affects test code."
            else:
                confidence = min(0.95, confidence + 0.05)
            decisions.append(
                {
                    "index": item["index"],
                    "keep": keep,
                    "confidence": round(confidence, 3),
                    "rationale": why,
                }
            )
        return {"decisions": decisions}

    # ------------------------------------------------------------------ task drafting
    def _task_draft(self, v: dict[str, Any]) -> dict[str, Any]:
        findings = v.get("findings", [])
        primary = findings[0] if findings else {"title": "Investigate issue", "description": ""}
        paths = v.get("paths", [])
        category = v.get("category", "fix")
        evidence_lines = [f"- [{f.get('source')}] {f.get('title')}" for f in findings[:6]]
        approach = {
            "security": "Remove the vulnerable pattern, prefer safe APIs, move secrets to the "
            "environment/secret manager and rotate any exposed credential.",
            "performance": "Remove the hot-path inefficiency (batch queries, add limits/"
            "timeouts, avoid repeated work) and measure before/after.",
            "dependency": "Upgrade or pin the affected dependency to a fixed version and "
            "run the full test suite.",
            "compliance": "Bring the flagged files in line with the policy rule.",
            "observability": "Add structured context to the failing log path.",
            "refactor": "Refactor the flagged code without behaviour change.",
        }.get(category, "Fix the root cause on the failing code path and add a regression test.")
        effort = "S" if len(paths) <= 1 else ("M" if len(paths) <= 3 else "L")
        runtime = any(str(f.get("source", "")).startswith("inference") for f in findings)
        return {
            "title": primary["title"][:300],
            "problem_statement": (
                f"{primary.get('description', '')}\n\nEvidence:\n" + "\n".join(evidence_lines)
            ).strip(),
            "proposed_approach": approach + (f" Scope: {', '.join(paths[:5])}." if paths else ""),
            "acceptance_criteria": [
                "The flagged pattern no longer appears in the affected paths.",
                "A regression test covers the fixed behaviour and passes in CI.",
                "Existing tests, lint and build pass.",
            ]
            + (
                ["The correlated error/metric signal stays at baseline after deploy."]
                if runtime
                else []
            ),
            "risk_assessment": (
                "Low: change is localized."
                if effort == "S"
                else "Medium: touches several files; review carefully."
            ),
            "effort_estimate": effort,
            "tests_to_add": [f"Regression test for {p}" for p in paths[:3]],
            "metrics_to_watch": [s for s in v.get("signals", []) if s][:5],
        }
