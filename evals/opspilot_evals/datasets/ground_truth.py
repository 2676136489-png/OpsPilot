"""The answer key, projected from the simulator's own scenario catalogue.

The previous version of this file was a hand-written list of twenty cases
across five scenario names that no longer exist — it answered questions the
simulator had stopped asking. This one contains **no scenario data at all**:
it projects :mod:`opspilot_simulator.scenarios` into eval cases, so a scenario
added or retuned in the simulator is scored correctly without touching the
evaluator. There is exactly one place where the answer lives.

That projection is also the reason the evaluation can be trusted: the Agent
has no path to this module (it is outside the backend package, and the
simulator only serves the answer key over ``/simulator/ground-truth/{name}``
in eval mode), so a passing score means the Agent derived the answer from tool
output rather than read it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opspilot_simulator.scenarios import SCENARIOS, scenario_names

# ---------------------------------------------------------------------------
# Tool-selection rubric
# ---------------------------------------------------------------------------

#: Which tool family actually holds the answer for each fault kind.
#:
#: Keyed by *fault*, not by scenario: a scenario is a story and stories get
#: rewritten, but a leaked connection pool is always visible in the pool
#: metrics and a bad release is always visible in the deployment history. A new
#: scenario assembled from existing faults is therefore scored correctly with
#: no change here.
_TOOLS_FOR_FAULT: dict[str, frozenset[str]] = {
    "bad_deployment": frozenset({"get_deployments"}),
    "high_error_rate": frozenset({"query_logs"}),
    "db_connection_exhaustion": frozenset({"query_metrics"}),
    "slow_database": frozenset({"query_metrics", "query_logs"}),
    "redis_failure": frozenset({"query_logs"}),
    "memory_leak": frozenset({"query_metrics"}),
    "cpu_spike": frozenset({"query_metrics"}),
    "dependency_failure": frozenset({"get_dependencies"}),
    "api_timeout": frozenset({"query_logs", "get_dependencies"}),
    "third_party_api_failure": frozenset({"query_logs"}),
}

#: Every investigation starts by asking what the alerted service looks like,
#: so this is required of every scenario regardless of the fault.
_BASELINE_TOOLS: frozenset[str] = frozenset({"get_service_status"})

#: Tools that can satisfy any requirement — resolution is by capability, not
#: by literal name, so an Agent that answers "which pool is exhausted?" with
#: ``query_logs`` because the logs carry the pool counter is not marked wrong
#: for skipping ``query_metrics``.
_ALTERNATIVES: dict[str, frozenset[str]] = {
    "query_metrics": frozenset({"query_metrics", "query_logs"}),
    "query_logs": frozenset({"query_logs", "query_metrics"}),
    "get_deployments": frozenset({"get_deployments", "get_recent_commits"}),
    "get_dependencies": frozenset({"get_dependencies", "get_service_status"}),
    "get_service_status": frozenset({"get_service_status"}),
}


def required_tools(fault_kinds: tuple[str, ...]) -> frozenset[str]:
    """Tool families a correct investigation of these faults has to consult."""
    needed: set[str] = set(_BASELINE_TOOLS)
    for kind in fault_kinds:
        needed |= _TOOLS_FOR_FAULT.get(kind, frozenset())
    return frozenset(needed)


def tool_requirement_satisfied(required: str, called: set[str]) -> bool:
    """True when ``called`` covers ``required``, directly or by a substitute."""
    return bool(called & _ALTERNATIVES.get(required, frozenset({required})))


# ---------------------------------------------------------------------------
# Taxonomy bridge
# ---------------------------------------------------------------------------

#: The Agent reports a fault *domain* and a fault *category*, and for one fault
#: it deliberately uses both words: ``cascading`` is the domain key, and
#: ``dependency`` is the category it maps to (see ``agent/analysis.py``). The
#: simulator's catalogue names the same fault ``cascading``. Recording the
#: equivalence here — once, in the scoring rubric — is the difference between a
#: translation and a hole: without it the scoreboard marks a correct diagnosis
#: wrong because two layers spelled one concept differently.
CATEGORY_ALIASES: dict[str, frozenset[str]] = {
    "cascading": frozenset({"cascading", "dependency"}),
}


def category_matches(expected: str, diagnosed: str | None) -> bool:
    """Does the Agent's category (or one of its accepted aliases) match?"""
    if not diagnosed:
        return False
    accepted = CATEGORY_ALIASES.get(expected, frozenset({expected}))
    return diagnosed in accepted


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    """One scenario and everything needed to score a run against it."""

    scenario: str
    title: str
    severity: str
    service: str
    expected_category: str
    expected_root_cause: str
    correct_recovery: tuple[str, ...]
    required_tools: frozenset[str]
    expected_evidence: tuple[str, ...]
    verification_criteria: tuple[dict[str, Any], ...]
    fault_kinds: tuple[str, ...] = ()
    #: The operator-visible half — this is what the incident record is built
    #: from. It deliberately contains no hint of the cause.
    description: str = ""
    symptoms: tuple[str, ...] = ()
    runbook_hint: str = ""
    tags: tuple[str, ...] = field(default=())

    @property
    def difficulty(self) -> str:
        """A coarse tier, derived from the story rather than asserted.

        A cascade is harder than a local fault because the alerted service is
        healthy and the damaged one is somewhere else in the graph; a third
        party outage is harder still because nothing the operator owns is
        broken.
        """
        if "cascading" in self.expected_category or "third_party" in self.expected_category:
            return "hard"
        if len(self.fault_kinds) > 1 or self.expected_category == "memory":
            return "medium"
        return "easy"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "title": self.title,
            "severity": self.severity,
            "service": self.service,
            "expected_category": self.expected_category,
            "expected_root_cause": self.expected_root_cause,
            "correct_recovery": list(self.correct_recovery),
            "required_tools": sorted(self.required_tools),
            "expected_evidence": list(self.expected_evidence),
            "verification_criteria": list(self.verification_criteria),
            "fault_kinds": list(self.fault_kinds),
            "difficulty": self.difficulty,
            "symptoms": list(self.symptoms),
            "tags": list(self.tags),
        }


def load_eval_cases(names: list[str] | None = None) -> list[EvalCase]:
    """Every scenario in the simulator catalogue, as a scoreable case."""
    selected = names or scenario_names()
    cases: list[EvalCase] = []
    for name in selected:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise KeyError(f"unknown scenario: {name}")
        faults = tuple(f.kind for f in scenario.faults)
        cases.append(
            EvalCase(
                scenario=scenario.name,
                title=scenario.title,
                severity=scenario.severity,
                service=scenario.alert_service,
                expected_category=scenario.root_cause_category,
                expected_root_cause=scenario.hidden_root_cause,
                correct_recovery=tuple(scenario.correct_recovery),
                required_tools=required_tools(faults),
                expected_evidence=tuple(scenario.expected_evidence),
                verification_criteria=tuple(c.as_dict() for c in scenario.verification_criteria),
                fault_kinds=faults,
                description=scenario.description,
                symptoms=tuple(scenario.symptoms),
                runbook_hint=scenario.runbook_hint,
                tags=tuple(sorted({f for f in faults})),
            )
        )
    return cases


EVALUATION_NOTES = {
    "suite": "opspilot-agent-eval",
    "version": "2.0",
    "scoring": {
        "root_cause_accuracy": "diagnosed category == scenario root_cause_category (domain aliases accepted, see CATEGORY_ALIASES)",
        "evidence_recall": "share of the scenario's expected_evidence keywords present in collected evidence",
        "evidence_utilisation": "share of collected evidence the final diagnosis actually cites",
        "tool_selection_accuracy": "every tool family the fault requires was actually called",
        "recovery_success_rate": "a recovery action succeeded AND the live environment passes the scenario's own criteria",
        "verification_accuracy": "the Agent's own verdict agrees with the environment's ground-truth evaluation",
        "false_diagnosis_rate": "a CONFIRMED/PROBABLE diagnosis that was wrong (abstentions are not counted)",
        "escalation_rate": "run handed over to a human instead of resolving",
    },
}

__all__ = [
    "CATEGORY_ALIASES",
    "EVALUATION_NOTES",
    "EvalCase",
    "category_matches",
    "load_eval_cases",
    "required_tools",
    "tool_requirement_satisfied",
]
