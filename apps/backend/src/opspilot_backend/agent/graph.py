"""The LangGraph workflow.

    Incident → Load Context → Triage → Investigation Planner
      → Parallel Investigation → Evidence Aggregation ┐
      └────────────── replan ←────────────────────────┘
      → Hypothesis Generator → Hypothesis Verification ┐
      └────────────── replan ←────────────────────────┘
      → Root Cause Diagnosis → Recovery Planner → Risk Assessment
      → Human Approval (interrupt) → Recovery Executor → Verification
      → Postmortem → END

Compensating path — taken when a recovery does not hold::

    Recovery Executor ─┐
    Verification ──────┴→ Rollback → Verification → Escalate → END

Three loops, all bounded:

* **Evidence loop** — the planner re-reads all evidence each pass and only
  probes dimensions that could still change the ranking.
* **Hypothesis loop** — a falsified hypothesis is discarded with its domain
  excluded, and a new one is generated. Without the exclusions this would be
  an infinite loop with better branding.
* **Compensation loop** — verification may fail once before the rollback and
  once after it. The second failure ends the run, because a system that keeps
  improvising after two negative results is guessing.

Compiled with a database checkpointer, so a run parked at HUMAN_APPROVAL
survives a restart and resumes from the checkpoint rather than replaying the
whole investigation.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
from opspilot_backend.agent.node_spec import NODE_SPECS, node_catalogue
from opspilot_backend.agent.nodes import (  # noqa: F401 - registers NODE_SPECS
    evidence_aggregation,
    human_approval,
    hypothesis_generation,
    hypothesis_verification,
    investigation_planner,
    load_context,
    parallel_investigation,
    postmortem,
    recovery_executor,
    recovery_planner,
    risk_assessment,
    rollback,
    root_cause_diagnosis,
    triage,
    verification,
)
from opspilot_backend.agent.state import IncidentState


def _route_after_planner(state: IncidentState) -> str:
    """Honor a planner failure (decision="stop") instead of blindly continuing.

    A saturated investigation produces no probes at all; sending that into the
    parallel executor would run zero tools and then bounce back here forever.
    """
    if state.decision == "stop":
        return END
    if not state.plan.steps:
        return "hypothesis_generation"
    return "parallel_investigation"


def _route_after_aggregation(state: IncidentState) -> str:
    if state.decision == "replan":
        return "investigation_planner"
    if state.decision == "escalate":
        return "root_cause_diagnosis"
    return "hypothesis_generation"


def _route_after_verification_of_hypotheses(state: IncidentState) -> str:
    """The rejection loop.

    ``replan`` sends the run back to the planner with the failed domains
    excluded, which is the whole point of having a budget: the Agent may try
    again, a bounded number of times, and only then give up out loud.
    """
    if state.decision == "replan":
        return "investigation_planner"
    if state.decision == "stop":
        return END
    return "root_cause_diagnosis"


def _route_after_diagnosis(state: IncidentState) -> str:
    if state.decision == "escalate" or state.decision == "stop":
        return END
    if state.diagnosis is None or not state.diagnosis.root_cause:
        return END
    return "recovery_planner"


def _route_after_risk(state: IncidentState) -> str:
    if state.decision == "stop":
        return END
    return "human_approval" if state.recovery.requires_approval else "recovery_executor"


def _route_after_approval(state: IncidentState) -> str:
    if state.decision == "stop":
        return END
    return "recovery_executor"


def _route_after_execution(state: IncidentState) -> str:
    if state.decision == "verify":
        return "verification"
    if state.decision == "rollback":
        return "rollback"
    return END


def _route_after_rollback(state: IncidentState) -> str:
    """Always re-probe. A rollback that is not verified is an assumption."""
    if state.decision == "reverify":
        return "verification"
    return END


def _route_after_verification(state: IncidentState) -> str:
    if state.decision == "close":
        return "postmortem"
    if state.decision == "rollback":
        return "rollback"
    return END


def build_graph(*, checkpointer: Any | None = None):
    """Compile the workflow. ``checkpointer=None`` only for unit tests."""
    sg = StateGraph(IncidentState)

    for name, spec in NODE_SPECS.items():
        sg.add_node(name, globals()[name])

    sg.add_edge(START, "load_context")
    sg.add_edge("load_context", "triage")
    sg.add_edge("triage", "investigation_planner")
    sg.add_conditional_edges(
        "investigation_planner",
        _route_after_planner,
        {
            "parallel_investigation": "parallel_investigation",
            "hypothesis_generation": "hypothesis_generation",
            END: END,
        },
    )
    sg.add_edge("parallel_investigation", "evidence_aggregation")

    sg.add_conditional_edges(
        "evidence_aggregation",
        _route_after_aggregation,
        {
            "investigation_planner": "investigation_planner",
            "hypothesis_generation": "hypothesis_generation",
            "root_cause_diagnosis": "root_cause_diagnosis",
        },
    )
    sg.add_edge("hypothesis_generation", "hypothesis_verification")

    sg.add_conditional_edges(
        "hypothesis_verification",
        _route_after_verification_of_hypotheses,
        {
            "investigation_planner": "investigation_planner",
            "root_cause_diagnosis": "root_cause_diagnosis",
            END: END,
        },
    )

    sg.add_conditional_edges(
        "root_cause_diagnosis",
        _route_after_diagnosis,
        {"recovery_planner": "recovery_planner", END: END},
    )
    sg.add_edge("recovery_planner", "risk_assessment")

    sg.add_conditional_edges(
        "risk_assessment",
        _route_after_risk,
        {"human_approval": "human_approval", "recovery_executor": "recovery_executor", END: END},
    )
    sg.add_conditional_edges(
        "human_approval",
        _route_after_approval,
        {"recovery_executor": "recovery_executor", END: END},
    )
    sg.add_conditional_edges(
        "recovery_executor",
        _route_after_execution,
        {"verification": "verification", "rollback": "rollback", END: END},
    )
    sg.add_conditional_edges(
        "rollback",
        _route_after_rollback,
        {"verification": "verification", END: END},
    )
    sg.add_conditional_edges(
        "verification",
        _route_after_verification,
        {"postmortem": "postmortem", "rollback": "rollback", END: END},
    )
    sg.add_edge("postmortem", END)

    return sg.compile(checkpointer=checkpointer or DatabaseCheckpointer())


def workflow_catalogue() -> list[dict[str, Any]]:
    """Metadata exposed by ``GET /api/v1/agent-runs/workflow``."""
    return node_catalogue()


__all__ = ["build_graph", "workflow_catalogue", "IncidentState"]
