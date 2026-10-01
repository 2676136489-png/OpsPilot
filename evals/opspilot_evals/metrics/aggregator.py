"""Aggregate scored runs into the report the evaluation is for.

Every metric here is computed from rows a run actually wrote, and each one is
paired with the question it answers. That pairing matters more than the number:
"root cause accuracy 0.92" is only meaningful next to "and 0 answered wrongly
with confidence, 1 abstained", which is why false-diagnosis rate and escalation
rate are reported alongside accuracy rather than buried.

The absent cases are handled explicitly. A run that escalated before recovery
has no verification to disagree with, so it is excluded from the verification
denominator instead of being counted as a failure — otherwise the safest
behaviour available to the Agent would be to quit early.
"""

from __future__ import annotations

import statistics
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from opspilot_evals.datasets.ground_truth import EVALUATION_NOTES
from opspilot_evals.runners.eval_runner import CaseResult


def _pad(text: str, width: int) -> str:
    """Left-align `text` inside `width` terminal columns.

    ``str.ljust`` counts code points, but a CJK glyph occupies two columns on
    every terminal that will print this. Padding a Chinese label with ASCII
    spaces therefore produces a visibly ragged table — the columns drift by one
    space per double-width character. Measure the display width instead of the
    length.
    """
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - used)


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (len(ordered) - 1) * pct
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 1)


def _rate(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


@dataclass
class EvaluationReport:
    """The scorecard: eleven measured dimensions plus the breakdowns."""

    total: int = 0
    errors: int = 0

    # 1. Root cause accuracy
    root_cause_accuracy: float = 0.0
    # 2. Evidence accuracy (recall is the headline; utilisation is reported
    #    beside it because a low one means "reasoned from a subset", which is
    #    normal, not "got it wrong")
    evidence_recall: float = 0.0
    evidence_accuracy: float = 0.0
    evidence_utilisation: float = 0.0
    evidence_traceability: float = 0.0
    # 3. Tool selection accuracy
    tool_selection_accuracy: float = 0.0
    # 4. Investigation steps
    avg_investigation_steps: float = 0.0
    avg_distinct_stages: float = 0.0
    avg_replan_rounds: float = 0.0
    runs_that_replanned: int = 0
    avg_rejected_hypotheses: float = 0.0
    # 5. Recovery success rate
    recovery_success_rate: float = 0.0
    environment_fixed_rate: float = 0.0
    recovery_effective_rate: float = 0.0
    rollback_rate: float = 0.0
    # 6. Verification accuracy
    verification_accuracy: float = 0.0
    verification_scored: int = 0
    reported_success_env_broken: int = 0
    # 7. False diagnosis rate
    false_diagnosis_rate: float = 0.0
    answered_runs: int = 0
    abstained_runs: int = 0
    # 8. Escalation rate
    escalation_rate: float = 0.0
    escalation_reasons: dict[str, int] = field(default_factory=dict)
    # 9. Tool calls
    total_tool_calls: int = 0
    avg_tool_calls: float = 0.0
    failed_tool_calls: int = 0
    failed_tool_call_rate: float = 0.0
    # 10. Token usage
    total_tokens: int = 0
    avg_tokens: float = 0.0
    reasoning_modes: dict[str, int] = field(default_factory=dict)
    # 11. Latency
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    avg_workflow_ms: float = 0.0

    # Trace integrity — how reconstructable the above is
    traces_single_root: int = 0
    traces_with_dangling_parents: int = 0
    avg_spans_per_run: float = 0.0
    avg_trace_depth: float = 0.0

    budget_exhausted: int = 0
    category_breakdown: dict[str, float] = field(default_factory=dict)
    scenario_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    difficulty_breakdown: dict[str, float] = field(default_factory=dict)
    severity_breakdown: dict[str, float] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=lambda: dict(EVALUATION_NOTES))
    #: Kept off the serialised report (see ``as_dict``) — it is the same rows
    #: the other fields were computed from, and copying them would triple the
    #: size of the artefact to say nothing new.
    _cases: list[CaseResult] = field(default_factory=list, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "errors": self.errors,
            "root_cause_accuracy": self.root_cause_accuracy,
            "evidence_accuracy": {
                "recall": self.evidence_recall,
                "utilisation": self.evidence_utilisation,
                "traceability": self.evidence_traceability,
            },
            "tool_selection_accuracy": self.tool_selection_accuracy,
            "investigation": {
                "avg_steps": self.avg_investigation_steps,
                "avg_distinct_stages": self.avg_distinct_stages,
                "avg_replan_rounds": self.avg_replan_rounds,
                "runs_that_replanned": self.runs_that_replanned,
                "avg_rejected_hypotheses": self.avg_rejected_hypotheses,
            },
            "recovery": {
                "success_rate": self.recovery_success_rate,
                "environment_fixed_rate": self.environment_fixed_rate,
                "effective_action_rate": self.recovery_effective_rate,
                "rollback_rate": self.rollback_rate,
            },
            "verification": {
                "accuracy": self.verification_accuracy,
                "scored_runs": self.verification_scored,
                "reported_success_env_broken": self.reported_success_env_broken,
            },
            "diagnosis_honesty": {
                "false_diagnosis_rate": self.false_diagnosis_rate,
                "answered": self.answered_runs,
                "abstained": self.abstained_runs,
            },
            "escalation": {
                "rate": self.escalation_rate,
                "reasons": self.escalation_reasons,
            },
            "cost": {
                "total_tool_calls": self.total_tool_calls,
                "avg_tool_calls": self.avg_tool_calls,
                "failed_tool_calls": self.failed_tool_calls,
                "failed_tool_call_rate": self.failed_tool_call_rate,
                "total_tokens": self.total_tokens,
                "avg_tokens": self.avg_tokens,
                "reasoning_modes": self.reasoning_modes,
                "budget_exhausted_runs": self.budget_exhausted,
            },
            "latency_ms": {
                "avg": self.avg_latency_ms,
                "p50": self.p50_latency_ms,
                "p95": self.p95_latency_ms,
                "max": self.max_latency_ms,
                "avg_workflow": self.avg_workflow_ms,
            },
            "trace_integrity": {
                "single_root": self.traces_single_root,
                "with_dangling_parents": self.traces_with_dangling_parents,
                "avg_spans": self.avg_spans_per_run,
                "avg_depth": self.avg_trace_depth,
            },
            "breakdowns": {
                "category": self.category_breakdown,
                "scenario": self.scenario_breakdown,
                "difficulty": self.difficulty_breakdown,
                "severity": self.severity_breakdown,
            },
            "notes": self.notes,
        }

    # ------------------------------------------------------------------
    def headline(self) -> list[str]:
        rows: list[tuple[str, str]] = [
            ("已评分场景", f"{self.total}   (出错 {self.errors})"),
            ("根因判定准确率", f"{self.root_cause_accuracy:.1%}"),
            (
                "证据召回率",
                f"{self.evidence_recall:.1%}"
                f"   (被诊断引用 {self.evidence_utilisation:.1%},"
                f" 可追溯到假设 {self.evidence_traceability:.1%})",
            ),
            ("工具选择准确率", f"{self.tool_selection_accuracy:.1%}"),
            (
                "调查步数",
                f"平均 {self.avg_investigation_steps:.1f}"
                f"   (涉及 {self.avg_distinct_stages:.1f} 个阶段,"
                f" {self.runs_that_replanned}/{self.total} 次运行发生了重新规划,"
                f" 平均额外跑 {self.avg_replan_rounds:.1f} 个节点,"
                f" 被否假设 {self.avg_rejected_hypotheses:.1f})",
            ),
            (
                "恢复成功率",
                f"{self.recovery_success_rate:.1%}"
                f"   (环境确实被修复 {self.environment_fixed_rate:.1%})",
            ),
            (
                "验证准确率",
                f"{self.verification_accuracy:.1%}"
                f"   覆盖 {self.verification_scored} 次运行"
                f"; 有 {self.reported_success_env_broken} 次声称已修复但环境仍是坏的",
            ),
            (
                "误诊率",
                f"{self.false_diagnosis_rate:.1%}"
                f"   ({self.answered_runs} 次给出结论, {self.abstained_runs} 次弃权)",
            ),
            (
                "升级率",
                f"{self.escalation_rate:.1%}"
                + (f"   {self.escalation_reasons}" if self.escalation_reasons else ""),
            ),
            (
                "工具调用",
                f"共 {self.total_tool_calls} 次,"
                f" 平均 {self.avg_tool_calls:.1f}/次运行,"
                f" 失败 {self.failed_tool_call_rate:.1%}",
            ),
            (
                "Token 用量",
                f"共 {self.total_tokens},"
                f" 平均 {self.avg_tokens:.1f}/次运行   {self.reasoning_modes}",
            ),
            (
                "延迟",
                f"平均 {self.avg_latency_ms:.0f}ms,"
                f" p50 {self.p50_latency_ms:.0f}ms, p95 {self.p95_latency_ms:.0f}ms",
            ),
            (
                "追踪完整性",
                f"{self.traces_single_root}/{self.total} 单一根节点,"
                f" {self.traces_with_dangling_parents} 个悬空父节点,"
                f" 平均 {self.avg_spans_per_run:.0f} 个 span, 深度 {self.avg_trace_depth:.1f}",
            ),
        ]
        return [f"{_pad(name, 20)}{value}" for name, value in rows]

    def failures(self) -> int:
        """Runs that neither diagnosed correctly nor honestly abstained.

        Derived from the rows rather than from the accuracy figure, so it stays
        exact when ``total`` is small enough for rounding to bite.
        """
        return sum(
            1
            for c in self._cases
            if not c.root_cause_correct
            and c.diagnosis_outcome not in {"INSUFFICIENT_EVIDENCE", "INVESTIGATION_FAILED"}
        )


def build_report(suite: Any) -> EvaluationReport:
    """Score a :class:`SuiteResult` (or a plain list of case results)."""
    cases: list[CaseResult] = list(getattr(suite, "cases", suite))
    report = EvaluationReport(total=len(cases), _cases=cases)
    if not cases:
        return report

    report.errors = sum(1 for c in cases if c.error)

    # -- 1. root cause ---------------------------------------------------
    report.root_cause_accuracy = _rate(sum(c.root_cause_correct for c in cases), len(cases))

    # -- 2. evidence -----------------------------------------------------
    report.evidence_recall = _mean([c.evidence_recall for c in cases])
    # The headline "evidence accuracy" is recall: the question that matters is
    # whether the run gathered the facts that identify the fault. Averaging in
    # how much of the gathered pile was cited would punish thorough
    # investigations for being thorough.
    report.evidence_accuracy = report.evidence_recall
    report.evidence_utilisation = _mean([c.evidence_utilisation for c in cases])
    report.evidence_traceability = _mean([c.evidence_linked_share for c in cases])

    # -- 3. tool selection ----------------------------------------------
    report.tool_selection_accuracy = _rate(
        sum(c.tool_selection_ok for c in cases), len(cases)
    )

    # -- 4. investigation shape ------------------------------------------
    report.avg_investigation_steps = _mean([float(len(c.steps)) for c in cases])
    report.avg_distinct_stages = _mean([float(c.node_count) for c in cases])
    report.avg_replan_rounds = _mean([float(c.replan_rounds) for c in cases])
    report.runs_that_replanned = sum(1 for c in cases if c.replan_rounds)
    report.avg_rejected_hypotheses = _mean([float(c.rejected_hypotheses) for c in cases])

    # -- 5. recovery -----------------------------------------------------
    report.recovery_success_rate = _rate(sum(c.recovery_success for c in cases), len(cases))
    report.environment_fixed_rate = _rate(
        sum(1 for c in cases if c.env_passed), len(cases)
    )
    report.recovery_effective_rate = _rate(
        sum(c.recovery_effective for c in cases), len(cases)
    )
    report.rollback_rate = _rate(sum(c.rollback_used for c in cases), len(cases))

    # -- 6. verification (two-sided) -------------------------------------
    scored = [c for c in cases if c.verification_scored]
    report.verification_scored = len(scored)
    report.verification_accuracy = _rate(
        sum(1 for c in scored if c.verification_agrees), len(scored)
    )
    report.reported_success_env_broken = sum(
        1 for c in cases if c.reported_success_env_broken
    )

    # -- 7/8. honesty ----------------------------------------------------
    answered = [
        c
        for c in cases
        if c.diagnosis_outcome in {"ROOT_CAUSE_CONFIRMED", "ROOT_CAUSE_PROBABLE"}
    ]
    report.answered_runs = len(answered)
    report.abstained_runs = sum(
        1
        for c in cases
        if c.diagnosis_outcome in {"INSUFFICIENT_EVIDENCE", "INVESTIGATION_FAILED"}
    )
    report.false_diagnosis_rate = _rate(
        sum(c.false_diagnosis for c in answered), len(answered)
    )
    report.escalation_rate = _rate(sum(c.escalated for c in cases), len(cases))
    reasons: dict[str, int] = defaultdict(int)
    for c in cases:
        if c.escalation_reason:
            reasons[c.escalation_reason] += 1
    report.escalation_reasons = dict(sorted(reasons.items()))

    # -- 9/10. cost ------------------------------------------------------
    report.total_tool_calls = sum(c.tool_call_count for c in cases)
    report.avg_tool_calls = _mean([float(c.tool_call_count) for c in cases])
    report.failed_tool_calls = sum(c.failed_tool_calls for c in cases)
    if report.total_tool_calls:
        report.failed_tool_call_rate = _rate(
            report.failed_tool_calls, report.total_tool_calls
        )
    report.total_tokens = sum(c.tokens for c in cases)
    report.avg_tokens = _mean([float(c.tokens) for c in cases])
    modes: dict[str, int] = defaultdict(int)
    for c in cases:
        modes[c.reasoning_mode or "unknown"] += 1
    report.reasoning_modes = dict(sorted(modes.items()))
    report.budget_exhausted = sum(1 for c in cases if c.budget.get("exhausted"))

    # -- 11. latency -----------------------------------------------------
    latencies = [c.latency_ms for c in cases]
    report.avg_latency_ms = round(statistics.fmean(latencies), 1) if latencies else 0.0
    report.p50_latency_ms = _percentile(latencies, 0.50)
    report.p95_latency_ms = _percentile(latencies, 0.95)
    report.max_latency_ms = round(max(latencies), 1) if latencies else 0.0
    report.avg_workflow_ms = _mean([c.workflow_ms for c in cases])

    # -- trace integrity -------------------------------------------------
    report.traces_single_root = sum(c.trace_single_root for c in cases)
    report.traces_with_dangling_parents = sum(
        1 for c in cases if c.trace_dangling_parents
    )
    report.avg_spans_per_run = _mean([float(c.span_count) for c in cases])
    report.avg_trace_depth = _mean([float(c.trace_max_depth) for c in cases])

    # -- breakdowns ------------------------------------------------------
    by_category: dict[str, list[bool]] = defaultdict(list)
    by_difficulty: dict[str, list[bool]] = defaultdict(list)
    by_severity: dict[str, list[bool]] = defaultdict(list)
    for c in cases:
        by_category[c.expected_category].append(c.root_cause_correct)
        by_difficulty[c.difficulty].append(c.root_cause_correct)
        by_severity[c.severity].append(c.root_cause_correct)
    report.category_breakdown = {
        k: _rate(sum(v), len(v)) for k, v in sorted(by_category.items())
    }
    report.difficulty_breakdown = {
        k: _rate(sum(v), len(v)) for k, v in sorted(by_difficulty.items())
    }
    report.severity_breakdown = {
        k: _rate(sum(v), len(v)) for k, v in sorted(by_severity.items())
    }
    report.scenario_breakdown = {
        c.scenario: {
            "correct": c.root_cause_correct,
            "diagnosed": c.diagnosed_category,
            "expected": c.expected_category,
            "outcome": c.diagnosis_outcome,
            "tools": c.tool_call_count,
            "recall": c.evidence_recall,
            "env_fixed": c.env_passed,
            "escalated": c.escalated,
            "latency_ms": c.latency_ms,
        }
        for c in cases
    }
    return report


__all__ = ["EvaluationReport", "build_report"]
