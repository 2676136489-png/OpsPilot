"""One real scenario, driven end to end by the evaluator.

This is the test that keeps the evaluation engine honest: it asserts that the
harness actually reaches the interrupt, clears it, executes the recovery the
simulator expects, and can read every dimension it reports back out of the
database. A scoring layer that silently returns zeroes would pass every unit
test in ``test_eval_scoring`` and fail here.

Run with::

    python -m pytest evals/opspilot_evals/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for _relative in ("evals", "apps/backend/src", "simulator/src"):
    _path = str(_ROOT / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from opspilot_evals.metrics.aggregator import build_report  # noqa: E402
from opspilot_evals.runners.eval_runner import run_suite  # noqa: E402

SCENARIO = "payment-bad-deployment"


@pytest.mark.asyncio
async def test_suite_scores_one_scenario_from_real_rows(tmp_path) -> None:
    suite = await run_suite(
        scenarios=[SCENARIO],
        db_path=tmp_path / "eval.db",
        keep_db=True,
    )
    assert len(suite.cases) == 1
    case = suite.cases[0]
    assert case.error is None, case.error

    # --- the workflow really ran --------------------------------------
    assert case.run_id and case.incident_id and case.trace_id
    assert case.run_status == "completed", case.run_status
    assert case.incident_status == "RESOLVED", case.incident_status
    assert case.node_count >= 12
    # Not a fixed pipeline: at least one node re-ran.
    assert case.replan_rounds >= 1, case.stage_sequence
    assert case.hypotheses, "no hypothesis was recorded"
    assert case.hypotheses[0].status == "confirmed", case.hypotheses[0]

    # --- the human gate was real --------------------------------------
    assert case.approval_required is True
    assert case.approval_risk == "CRITICAL", case.approval_risk

    # --- the numbers came from rows -----------------------------------
    assert case.tool_call_count >= 5, case.tools_called
    assert case.tool_selection_ok, case.missing_tools
    assert case.evidence, "no evidence rows were collected"
    assert all(e.has_tool_call for e in case.evidence), "evidence not traceable to a tool call"
    assert case.evidence_recall > 0.0
    assert case.root_cause_correct, case.diagnosed_category

    # --- recovery actually fixed the environment -----------------------
    assert case.recovery_attempted
    assert case.recovery_effective
    assert case.env_evaluated and case.env_passed is True, case.env_checks
    assert case.recovery_success
    assert case.agent_verdict == "passed"
    assert case.verification_agrees is True
    assert case.reported_success_env_broken is False

    # --- the trace is reconstructable ---------------------------------
    assert case.span_count >= 30, case.span_kinds
    assert case.trace_single_root is True
    assert case.trace_dangling_parents == 0
    assert case.trace_max_depth >= 4, case.trace_max_depth
    assert {"run", "node", "tool"} <= set(case.span_kinds)

    report = build_report(suite)
    assert report.total == 1
    assert report.root_cause_accuracy == 1.0
    assert report.failures() == 0
    assert report.recovery_success_rate == 1.0
    assert report.traces_single_root == 1
