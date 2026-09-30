"""Recovery policy, approval tiers, and the compensation path.

Two levels of proof, because they answer different questions:

* ``test_approval_tiers_*`` — pure policy. Every risk level maps to exactly one
  gate, and the gate for a plan is the worst tier in it. No simulator needed.
* ``test_*rollback*`` — the real nodes against the in-process simulator. The
  fault is re-injected behind the Agent's back after its recovery lands, which
  is the only honest way to produce "the fix ran, and then it stopped holding".
  What must happen next is fixed: compensate, re-measure, and only then hand
  over — never silently retry.
"""

from __future__ import annotations

from typing import Any

import pytest

from opspilot_backend.agent.budget import Budget
from opspilot_backend.agent.context import NodeContext
from opspilot_backend.agent.nodes import (
    evidence_aggregation,
    hypothesis_generation,
    hypothesis_verification,
    investigation_planner,
    load_context,
    parallel_investigation,
    recovery_executor,
    recovery_planner,
    risk_assessment,
    rollback,
    root_cause_diagnosis,
    verification,
)
from opspilot_backend.agent.recovery import (
    TIER_APPROVAL,
    TIER_APPROVAL_REVERIFY,
    TIER_AUTO,
    TIER_MANUAL_ONLY,
    approval_tier,
    auto_executable,
    build_recovery_actions,
    may_attempt_rollback,
    plan_summary,
)
from opspilot_backend.agent.state import IncidentRef, IncidentState
from opspilot_backend.domain.enums import EscalationReason, RecoveryActionStatus
from opspilot_backend.tools.executor import ToolExecutor
from opspilot_backend.tools.hooks import NullToolHooks

SCENARIO = "inventory-cpu-saturation"
SERVICE = "inventory-service"


# ---------------------------------------------------------------------------
# Policy — no simulator
# ---------------------------------------------------------------------------


def test_approval_tiers_are_total_and_ordered() -> None:
    assert approval_tier("LOW") == TIER_AUTO
    assert approval_tier("MEDIUM") == TIER_APPROVAL
    assert approval_tier("HIGH") == TIER_APPROVAL_REVERIFY
    assert approval_tier("CRITICAL") == TIER_MANUAL_ONLY
    # An unknown level is treated as the most dangerous one, not the least.
    assert approval_tier("SOMETHING_NEW") == TIER_MANUAL_ONLY

    assert auto_executable("LOW") is True
    assert auto_executable("MEDIUM") is False
    # The Agent never compensates a change that was itself too dangerous to
    # make without a human.
    assert may_attempt_rollback("CRITICAL") is False
    assert may_attempt_rollback("HIGH") is True


def test_plan_tier_is_the_worst_action_in_it() -> None:
    actions = build_recovery_actions("database", SERVICE)
    summary = plan_summary(actions)
    # database → rollback_deployment (CRITICAL) first.
    assert summary["tier"] == TIER_MANUAL_ONLY
    assert summary["requires_approval"] is True
    assert summary["manual_only"] is True

    actions = build_recovery_actions("capacity", SERVICE)
    summary = plan_summary(actions)
    assert summary["tier"] == TIER_APPROVAL
    assert summary["reverify"] is False

    assert plan_summary([])["tier"] == TIER_AUTO, "an empty plan cannot need approval"


def test_actions_carry_the_full_contract() -> None:
    """Every action states why, what it risks, and how it would be undone."""
    required = {
        "ref",
        "tool",
        "target_service",
        "reason",
        "risk_level",
        "expected_impact",
        "verification_strategy",
        "required_permission",
        "rollback_strategy",
        "approval_tier",
        "approval_status",
    }
    for category in ("database", "redis", "third_party", "capacity", "memory", "slow_database"):
        for action in build_recovery_actions(category, SERVICE, dependency="postgres"):
            assert required <= set(action), (category, action["tool"], required - set(action))
            assert action["reason"] and action["expected_impact"]
            assert action["verification_strategy"]
            assert action["rollback_strategy"], "an action must declare how it is undone"
            assert action["approval_tier"] == approval_tier(action["risk_level"])


def test_dependency_actions_target_the_dependency_not_the_caller() -> None:
    """The fix goes where the fault is, with the caller-side exceptions named."""
    redis = build_recovery_actions("redis", "checkout-service", dependency="redis")
    assert {a["target_service"] for a in redis} == {"redis"}

    # Circuit breaking is installed on the caller: a client-side decision that
    # cannot be pushed onto a third party.
    third_party = build_recovery_actions(
        "third_party", "payment-service", dependency="external-payment-api"
    )
    assert {a["target_service"] for a in third_party} == {"payment-service"}

    # A pool that belongs to the caller stays the caller's problem. The
    # diagnosis resolves no remote target here, so every action lands locally —
    # rolling back the datastore for a leak in the service would be nonsense.
    database = build_recovery_actions("database", "checkout-service")
    assert {a["target_service"] for a in database} == {"checkout-service"}

    # When the diagnosis *does* resolve a remote target, the release that broke
    # is reverted there rather than on the component that noticed.
    deployment = build_recovery_actions(
        "deployment", "gateway", dependency="checkout-service"
    )
    assert {a["target_service"] for a in deployment} == {"checkout-service"}


def test_tools_that_require_approval_are_exactly_the_non_low_ones() -> None:
    from opspilot_backend.tools.registry import TOOL_REGISTRY

    for name, spec in TOOL_REGISTRY.items():
        assert spec.requires_approval == (spec.risk_level != spec.risk_level.LOW), name
    # The recovery tools exist and are gated by their own risk, not by a rule
    # that only looked at HIGH and above.
    assert TOOL_REGISTRY["restart_redis"].requires_approval is True
    assert TOOL_REGISTRY["flush_cache"].requires_approval is True
    assert TOOL_REGISTRY["notify_oncall"].requires_approval is False


# ---------------------------------------------------------------------------
# The compensation path — real nodes, in-process simulator
# ---------------------------------------------------------------------------


class _Persistence:
    """In-memory stand-in, mirroring what the nodes ask of the repository."""

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.plans: dict[str, Any] = {}
        self.events: list[str] = []

    async def start_step(self, run_id, stage, sequence, attempt, input_payload,
                         trace_id="", span_id=""):
        return f"step-{sequence}"

    async def finish_step(self, step_id, *, status, output, error=None, duration_ms=0):
        return None

    async def save_evidence(self, incident_id, run_id, items):
        return {}

    async def save_hypotheses(self, incident_id, run_id, items):
        return {}

    async def save_diagnosis(self, incident_id, run_id, diagnosis):
        return None

    async def save_recovery_plan(self, incident_id, run_id, plan):
        self.plans[incident_id] = plan
        return plan

    async def save_recovery_action_result(self, action_id, *, status, result, error,
                                          tool_call_id, executed_by):
        return None

    async def create_approval(self, **kwargs):
        return {"id": "approval-1", "status": "pending"}

    async def get_approval(self, approval_id):
        return None

    async def find_pending_approval(self, run_id):
        return None

    async def find_latest_approval(self, run_id):
        return None

    async def save_verification(self, incident_id, run_id, plan_id, payload):
        return "ver-1"

    async def save_postmortem(self, incident_id, payload):
        return "pm-1"

    async def set_incident_status(self, incident_id, status, *, actor="agent",
                                  summary="", stage=None):
        self.statuses.append(status)

    async def update_run(self, run_id, *, status=None, current_stage=None,
                         error=None, interrupt_payload=None):
        return None

    async def update_run_budget(self, run_id, budget):
        return None

    async def commit(self):
        return None


class _ApprovingHooks(NullToolHooks):
    """Supplies the reviewer's answer. The gate itself still runs."""

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return {"id": approval_id, "status": "approved"}


def _merge(state: IncidentState, update: dict[str, Any]) -> IncidentState:
    return IncidentState(**{**state.model_dump(), **update})


async def _investigate(state: IncidentState, config: dict[str, Any]) -> IncidentState:
    """Drive the investigation the way the graph does, loops included.

    Running the nodes once each is not the same workflow: the planner is
    *supposed* to come back with a second round of probes once the first round
    has narrowed the field, and a one-pass shortcut produces materially
    different evidence. The redis domain in particular only resolves its target
    on the round that reads the cache's own status.
    """
    state = _merge(state, await load_context(state, config))
    state = _merge(state, await investigation_planner(state, config))

    rounds = 0
    while rounds < 4:
        rounds += 1
        if not state.plan.steps:
            break
        state = _merge(state, await parallel_investigation(state, config))
        state = _merge(state, await evidence_aggregation(state, config))
        if state.decision != "replan":
            break
        state = _merge(state, await investigation_planner(state, config))

    state = _merge(state, await hypothesis_generation(state, config))

    hyp_rounds = 0
    while hyp_rounds < 3:
        state = _merge(state, await hypothesis_verification(state, config))
        if state.decision != "replan":
            break
        state = _merge(state, await investigation_planner(state, config))
        if not state.plan.steps:
            break
        state = _merge(state, await parallel_investigation(state, config))
        state = _merge(state, await hypothesis_generation(state, config))
        hyp_rounds += 1

    return _merge(state, await root_cause_diagnosis(state, config))


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_verification_failure_rolls_back_then_escalates(simulator) -> None:
    hooks = _ApprovingHooks()
    persistence = _Persistence()
    ctx = NodeContext(
        run_id="run-rollback",
        incident_id="inc-rollback",
        executor=ToolExecutor(hooks=hooks),
        hooks=hooks,
        persistence=persistence,
        budget=Budget.from_settings(),
    )
    config = {"configurable": {"ctx": ctx}}

    state = IncidentState(
        incident=IncidentRef(
            incident_id="inc-rollback",
            service=SERVICE,
            severity="SEV2",
            title=SCENARIO,
            scenario=SCENARIO,
        )
    )

    state = await _investigate(state, config)

    assert state.diagnosis is not None
    assert state.diagnosis.category == "capacity", state.diagnosis.category
    assert state.diagnosis.domain == "capacity", state.diagnosis.domain

    state = _merge(state, await recovery_planner(state, config))
    state = _merge(state, await risk_assessment(state, config))
    assert state.recovery.approval_tier == TIER_APPROVAL

    recovery = state.recovery.model_dump()
    recovery["approval_status"] = "approved"
    recovery["approval_id"] = "approval-1"
    state = _merge(state, {"recovery": recovery})

    # --- the recovery lands ------------------------------------------------
    state = _merge(state, await recovery_executor(state, config))
    assert state.decision == "verify"
    assert state.recovery.effective_ref == "A01"
    assert state.recovery.status == "executed"

    scope = [a for a in state.recovery.actions if a.ref == "A01"][0]
    assert scope.tool == "scale_service"
    assert scope.status == RecoveryActionStatus.SUCCEEDED.value
    assert scope.effective is True
    # The remaining candidate was not applied: an action's rationale dies with
    # the action that already worked.
    assert [a.status for a in state.recovery.actions[1:]] == ["pending"] * (
        len(state.recovery.actions) - 1
    )

    # --- the fix stops holding --------------------------------------------
    # Re-injecting the fault is the honest way to produce "the environment is
    # broken again": nothing about the Agent's own state is doctored.
    simulator.inject(SCENARIO)

    state = _merge(state, await verification(state, config))
    assert state.verification.status == "failed"
    assert state.decision == "rollback", state.verification.checks

    # --- compensate, then measure again -----------------------------------
    state = _merge(state, await rollback(state, config))
    assert state.decision == "reverify"
    assert state.recovery.rollback_refs == ["A01"]
    assert state.recovery.rollback_outcome == "completed"
    assert state.recovery.status == "rolled_back"
    assert "ROLLING_BACK" in persistence.statuses

    state = _merge(state, await verification(state, config))
    # Failing a second time ends the Agent's authority instead of looping.
    assert state.decision == "escalate"
    assert state.recovery.escalation_reason == EscalationReason.VERIFICATION_FAILED.value
    assert persistence.statuses[-1] == "ESCALATED"


@pytest.mark.parametrize("simulator", ["redis-failure"], indirect=True)
async def test_irreversible_action_escalates_without_improvising(simulator) -> None:
    """A restart has no compensating action, so there is nothing to roll back.

    The Agent must escalate rather than invent one — and it must not report the
    uncompensated recovery as successful.
    """
    hooks = _ApprovingHooks()
    persistence = _Persistence()
    ctx = NodeContext(
        run_id="run-irreversible",
        incident_id="inc-irreversible",
        executor=ToolExecutor(hooks=hooks),
        hooks=hooks,
        persistence=persistence,
        budget=Budget.from_settings(),
    )
    config = {"configurable": {"ctx": ctx}}
    state = IncidentState(
        incident=IncidentRef(
            incident_id="inc-irreversible",
            service="checkout-service",
            severity="SEV2",
            title="redis-failure",
            scenario="redis-failure",
        )
    )

    state = await _investigate(state, config)

    assert state.diagnosis is not None
    assert state.diagnosis.domain == "redis"

    state = _merge(state, await recovery_planner(state, config))
    state = _merge(state, await risk_assessment(state, config))
    assert state.recovery.approval_tier == TIER_APPROVAL_REVERIFY, state.recovery.approval_tier

    # The plan acts on the cache, not on the caller that noticed.
    assert {a.target_service for a in state.recovery.actions} == {"redis"}
    assert state.recovery.requires_reverification is True

    recovery = state.recovery.model_dump()
    recovery["approval_status"] = "approved"
    recovery["approval_id"] = "approval-1"
    state = _merge(state, {"recovery": recovery})

    state = _merge(state, await recovery_executor(state, config))
    assert state.decision == "verify"
    assert state.recovery.effective_ref == "A01"

    simulator.inject("redis-failure")

    state = _merge(state, await verification(state, config))
    assert state.decision == "escalate", state.verification.checks
    assert state.recovery.escalation_reason == EscalationReason.VERIFICATION_FAILED.value
    # Nothing was compensated, because nothing could be.
    assert state.recovery.rollback_refs == []
    assert persistence.statuses[-1] == "ESCALATED"


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_status_tool_never_leaks_the_injected_fault(simulator) -> None:
    """The Agent must not be able to read the answer key through a tool."""
    from opspilot_backend.infrastructure.container import get_providers

    raw = await get_providers().services.get_status(SERVICE)
    assert raw.get("active_faults"), "the simulator did inject a fault"

    from opspilot_backend.tools.registry import HANDLERS

    hooks = NullToolHooks()
    projected = await HANDLERS["get_service_status"]({"service": SERVICE}, hooks)  # type: ignore[arg-type]
    assert "active_faults" not in projected
    # …while the fields that make the numbers interpretable are kept.
    assert "memory_limit_mb" in projected
    assert "pool_max" in projected
    assert "kind" in projected
