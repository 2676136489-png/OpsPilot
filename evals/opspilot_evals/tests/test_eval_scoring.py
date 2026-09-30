"""Tests for the evaluation engine's scoring and reporting.

The suite is the thing that produces the numbers people quote, so the numbers
have to be computed by code that is itself checked. These cover the pure
scoring functions and the report roll-up; the end-to-end run is covered by
:mod:`test_eval_suite`, which drives one real scenario.

Run with::

    python -m pytest evals/opspilot_evals/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _relative in ("evals", "apps/backend/src", "simulator/src"):
    _path = str(_ROOT / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from opspilot_evals.datasets.ground_truth import (  # noqa: E402
    load_eval_cases,
    required_tools,
    tool_requirement_satisfied,
)
from opspilot_evals.metrics.aggregator import build_report  # noqa: E402
from opspilot_evals.runners.eval_runner import (  # noqa: E402
    CaseResult,
    score_evidence,
    score_tool_selection,
    trace_integrity,
)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def test_cases_are_projected_from_the_simulator_catalogue() -> None:
    """The dataset must not be a second, drifting copy of the scenarios."""
    from opspilot_simulator.scenarios import SCENARIOS

    cases = load_eval_cases()
    assert {c.scenario for c in cases} == set(SCENARIOS)
    assert len(cases) == len(SCENARIOS) >= 12

    for case in cases:
        scenario = SCENARIOS[case.scenario]
        assert case.expected_root_cause == scenario.hidden_root_cause
        assert case.expected_category == scenario.root_cause_category
        assert list(case.correct_recovery) == list(scenario.correct_recovery)
        assert list(case.symptoms) == list(scenario.symptoms)
        # The operator-visible half must not carry the answer.
        assert scenario.hidden_root_cause not in case.description


def test_required_tools_follow_the_fault_not_the_scenario_name() -> None:
    assert "get_deployments" in required_tools(("bad_deployment",))
    assert "get_dependencies" in required_tools(("dependency_failure",))
    # Every scenario needs the baseline health check, whatever the fault.
    assert "get_service_status" in required_tools(("cpu_spike",))
    # An unknown fault must not silently drop the baseline requirement.
    assert "get_service_status" in required_tools(("something_new",))


def test_tool_requirement_accepts_a_capable_substitute() -> None:
    # Logs carry the pool counter too, so answering with query_logs is not a miss.
    assert tool_requirement_satisfied("query_metrics", {"query_logs"})
    assert tool_requirement_satisfied("get_deployments", {"get_recent_commits"})
    assert not tool_requirement_satisfied("get_dependencies", {"query_logs"})


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_root_cause_scoring_accepts_the_declared_domain_alias() -> None:
    from opspilot_evals.datasets.ground_truth import category_matches

    # The Agent says "dependency" for the fault the simulator catalogues as
    # "cascading"; both name the same thing and must not read as a failure.
    assert category_matches("cascading", "dependency")
    assert category_matches("cascading", "cascading")
    assert not category_matches("cascading", "redis")
    assert not category_matches("redis", None)
    # The alias is one-way and does not leak into unrelated categories.
    assert not category_matches("database", "dependency")


def test_evidence_recall_counts_missing_keywords() -> None:
    case = load_eval_cases(["redis-failure"])[0]
    recall, _, missing = score_evidence(
        case, "redis connection refused behind the cache proxy", cited=0, total=0
    )
    assert recall == 1.0
    assert missing == []

    recall, _, missing = score_evidence(case, "nothing relevant here", cited=0, total=0)
    assert recall == 0.0
    assert len(missing) == len(case.expected_evidence)


def test_evidence_utilisation_is_a_share_of_what_was_collected() -> None:
    case = load_eval_cases(["redis-failure"])[0]
    _, utilisation, _ = score_evidence(case, "redis", cited=3, total=12)
    assert utilisation == 0.25
    _, none, _ = score_evidence(case, "redis", cited=0, total=0)
    assert none == 0.0


def test_tool_selection_reports_exactly_what_was_missed() -> None:
    case = load_eval_cases(["payment-bad-deployment"])[0]
    ok, missing = score_tool_selection(case, {"get_service_status", "get_deployments"})
    assert ok and missing == []
    ok, missing = score_tool_selection(case, {"query_logs"})
    assert not ok
    assert "get_deployments" in missing and "get_service_status" in missing


def test_trace_integrity_detects_a_dangling_parent() -> None:
    spans = [
        {"span_id": "a", "parent_span_id": "", "kind": "run"},
        {"span_id": "b", "parent_span_id": "a", "kind": "node"},
        {"span_id": "c", "parent_span_id": "b", "kind": "tool"},
    ]
    assert trace_integrity(spans) == (True, 0, 3)

    broken = [*spans, {"span_id": "d", "parent_span_id": "ghost", "kind": "db"}]
    single_root, dangling, _ = trace_integrity(broken)
    assert single_root is True
    assert dangling == 1


def test_trace_integrity_detects_two_roots() -> None:
    spans = [
        {"span_id": "a", "parent_span_id": "", "kind": "run"},
        {"span_id": "b", "parent_span_id": "", "kind": "run"},
    ]
    single_root, dangling, depth = trace_integrity(spans)
    assert single_root is False
    assert dangling == 0 and depth == 1


# ---------------------------------------------------------------------------
# Report roll-up
# ---------------------------------------------------------------------------


def _case(**overrides) -> CaseResult:
    base = dict(
        scenario="s",
        title="t",
        service="svc",
        severity="SEV2",
        difficulty="easy",
        expected_category="database",
        expected_root_cause="boom",
        expected_recovery=["restart_postgres"],
        required_tools=["get_service_status"],
    )
    base.update(overrides)
    return CaseResult(**base)


def test_abstaining_is_not_a_false_diagnosis() -> None:
    """The whole point of four outcomes: "I don't know" is not "I was wrong"."""
    report = build_report(
        [
            _case(
                diagnosis_outcome="ROOT_CAUSE_CONFIRMED",
                diagnosed_category="database",
            ),
            _case(
                diagnosis_outcome="ROOT_CAUSE_PROBABLE",
                diagnosed_category="redis",
            ),
            _case(diagnosis_outcome="INSUFFICIENT_EVIDENCE"),
        ]
    )
    assert report.total == 3
    assert report.answered_runs == 2
    assert report.abstained_runs == 1
    # One confident answer was wrong out of two attempts.
    assert report.false_diagnosis_rate == 0.5
    assert report.root_cause_accuracy == round(1 / 3, 4)
    # The abstention is not counted as a failure of nerve *or* of accuracy.
    assert report.failures() == 1


def test_a_confident_abstention_is_still_counted_as_an_abstention() -> None:
    """``INSUFFICIENT_EVIDENCE`` with a category attached is not a wrong answer."""
    report = build_report(
        [_case(diagnosis_outcome="INSUFFICIENT_EVIDENCE", diagnosed_category="redis")]
    )
    assert report.false_diagnosis_rate == 0.0
    assert report.abstained_runs == 1
    assert report.failures() == 0


def test_verification_accuracy_excludes_runs_with_nothing_to_verify() -> None:
    """A run that escalated before recovery must not count against accuracy."""
    report = build_report(
        [
            _case(agent_verdict="passed", env_passed=True),
            _case(agent_verdict="failed", env_passed=True),
            # No verification and no environment verdict — nothing to score.
            _case(),
        ]
    )
    assert report.verification_scored == 2
    assert report.verification_accuracy == 0.5


def test_recovery_success_needs_both_a_real_action_and_a_healthy_environment() -> None:
    report = build_report(
        [
            _case(recovery_effective=True, env_passed=True),
            # The Agent's action "succeeded" but the environment disagrees.
            _case(recovery_effective=False, env_passed=False),
            # The environment recovered with no effective action — not a
            # recovery by the Agent, so it does not count as one.
            _case(recovery_effective=False, env_passed=True),
        ]
    )
    assert report.recovery_success_rate == round(1 / 3, 4)
    assert report.environment_fixed_rate == round(2 / 3, 4)


def test_reported_success_while_broken_is_flagged() -> None:
    report = build_report(
        [
            _case(agent_verdict="passed", env_passed=False),
            _case(agent_verdict="passed", env_passed=True),
        ]
    )
    assert report.reported_success_env_broken == 1
    assert report.verification_accuracy == 0.5


def test_replan_rounds_and_escalation_are_aggregated() -> None:
    report = build_report(
        [
            _case(replan_rounds=4, escalation_reason="BUDGET_EXHAUSTED"),
            _case(replan_rounds=0),
        ]
    )
    assert report.avg_replan_rounds == 2.0
    assert report.runs_that_replanned == 1
    assert report.escalation_rate == 0.5
    assert report.escalation_reasons == {"BUDGET_EXHAUSTED": 1}


def test_empty_suite_does_not_divide_by_zero() -> None:
    report = build_report([])
    assert report.total == 0
    assert report.root_cause_accuracy == 0.0
    assert report.failures() == 0
    # The definitions travel with the numbers.
    assert report.notes["scoring"]


def test_report_is_json_serialisable() -> None:
    import json

    payload = build_report([_case(recovery_effective=True, env_passed=True)]).as_dict()
    assert json.loads(json.dumps(payload, default=str))["root_cause_accuracy"] == 0.0
