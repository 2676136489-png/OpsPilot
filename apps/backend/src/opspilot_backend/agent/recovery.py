"""Recovery actions: what may be done, at what risk, and how to undo it.

Every action the Agent can propose is declared here once — its risk tier, the
permission it needs, why it is being proposed, what it should change, how the
Agent will check that it worked, and what it does if it does not.

Two rules are enforced by construction rather than by comment:

1. **An action is not a command.** A profile describes a proposal. Whether it
   runs is decided by :func:`approval_tier` and, above ``LOW``, by a human.
2. **Every action declares its own undo.** ``rollback_tool`` is the
   compensating action, and it is ``None`` when the action genuinely cannot be
   reversed — in which case the note says so out loud instead of leaving the
   Agent to improvise a fix for a fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opspilot_backend.domain.enums import PermissionLevel, RiskLevel

# ---------------------------------------------------------------------------
# Approval tiers
# ---------------------------------------------------------------------------

#: LOW         → the Agent may execute it unattended (it is a read, or inert).
#: MEDIUM      → a human approves before it runs.
#: HIGH        → a human approves, and the result is verified twice.
#: CRITICAL    → a human approves, and the Agent will not attempt any
#:               autonomous compensation if it fails. It escalates instead.
TIER_AUTO = "auto"
TIER_APPROVAL = "approval"
TIER_APPROVAL_REVERIFY = "approval_and_reverify"
TIER_MANUAL_ONLY = "manual_only"

_APPROVAL_TIERS: dict[str, str] = {
    RiskLevel.LOW.value: TIER_AUTO,
    RiskLevel.MEDIUM.value: TIER_APPROVAL,
    RiskLevel.HIGH.value: TIER_APPROVAL_REVERIFY,
    RiskLevel.CRITICAL.value: TIER_MANUAL_ONLY,
}


def approval_tier(risk_level: str) -> str:
    return _APPROVAL_TIERS.get(risk_level, TIER_MANUAL_ONLY)


def auto_executable(risk_level: str) -> bool:
    """Only ``LOW`` runs without a human. There is no middle way."""
    return approval_tier(risk_level) == TIER_AUTO


def may_attempt_rollback(risk_level: str) -> bool:
    """A failed CRITICAL action is never compensated by the Agent.

    Undoing a change that was dangerous enough to need a human is, by
    definition, at least as dangerous — and the Agent has just demonstrated
    that its model of the system was wrong.
    """
    return approval_tier(risk_level) != TIER_MANUAL_ONLY


# ---------------------------------------------------------------------------
# Action profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionProfile:
    """Everything the Agent must state before proposing an action."""

    tool: str
    risk: str
    permission: str
    #: Filled in with the diagnosed cause so the reason is specific.
    reason: str
    impact: str
    #: How the Agent will know it worked.
    verify: str
    #: Compensating tool, or None when the action cannot be reversed.
    rollback_tool: str | None = None
    rollback_parameters: dict[str, Any] = field(default_factory=dict)
    rollback_strategy: str = ""
    #: Which component the action targets: the alerting service, or the
    #: dependency that is actually broken.
    targets_dependency: bool = False


PROFILES: dict[str, ActionProfile] = {
    p.tool: p
    for p in (
        ActionProfile(
            tool="rollback_deployment",
            risk=RiskLevel.CRITICAL.value,
            permission=PermissionLevel.DESTRUCTIVE.value,
            reason="Revert the release that introduced the regression.",
            impact=(
                "Traffic returns to the previous version; features added in the "
                "reverted release are unavailable until it is re-landed."
            ),
            verify="error_rate and latency_p95 return to their pre-release baseline",
            rollback_strategy=(
                "Not reversible: a rolled-back release can only be re-landed by "
                "shipping it again. Requires the release owner."
            ),
            # The service that alerted is not always the service that shipped.
            # When the bad release is behind an edge component, rolling back the
            # edge is a no-op: it has no deployment in the window to revert.
            targets_dependency=True,
        ),
        ActionProfile(
            tool="restart_service",
            risk=RiskLevel.HIGH.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Restart the component to clear its in-process state.",
            impact="Brief unavailability while replicas cycle; in-flight requests fail.",
            verify="health returns to healthy and the error rate stops climbing",
            rollback_strategy=(
                "Restart is its own inverse — no compensating action is needed."
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="scale_service",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Add capacity so the work can be spread across more replicas.",
            impact="Higher infrastructure cost for the duration of the incident.",
            verify="cpu and latency_p95 fall back under their thresholds",
            rollback_tool="scale_service",
            # The count is filled in from the measured fleet size, not fixed
            # here — see build_recovery_actions.
            rollback_strategy="Scale back down to the original replica count.",
        ),
        ActionProfile(
            tool="increase_pool_size",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Raise the connection pool ceiling so queued requests can be served.",
            impact="Higher concurrent load on the database; buys time, does not stop a leak.",
            verify="pool utilisation drops below 80% and connection timeouts stop",
            rollback_strategy=(
                "Not reversible in place: the raised ceiling is inert once the "
                "underlying leak is fixed, so it is left as-is."
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="restart_redis",
            risk=RiskLevel.HIGH.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Fail over the cache cluster so clients can reconnect.",
            impact="Cache is cold until it warms; read latency is elevated meanwhile.",
            verify="the cache reports healthy and dependent services stop logging "
                   "connection failures",
            rollback_strategy="Restart is its own inverse.",
            targets_dependency=True,
        ),
        ActionProfile(
            tool="flush_cache",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Discard poisoned cache entries that are being served to clients.",
            impact="All cached values are lost; origin load spikes until it refills.",
            verify="cache reads succeed and the origin error rate does not rise",
            rollback_strategy="Not reversible: a flushed cache refills from origin.",
            targets_dependency=True,
        ),
        ActionProfile(
            tool="restart_postgres",
            risk=RiskLevel.CRITICAL.value,
            permission=PermissionLevel.DESTRUCTIVE.value,
            reason="Recycle the datastore to clear stuck sessions and locks.",
            impact="Every connection drops; all dependent services fail for the duration.",
            verify="postgres reports healthy and callers stop timing out",
            rollback_strategy=(
                "Not reversible: sessions and in-flight transactions are lost. "
                "Requires the database owner."
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="clear_deadlock",
            risk=RiskLevel.HIGH.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Kill the blocking transactions that are holding row locks.",
            impact="In-flight statements on the blocked rows are rolled back.",
            verify="query latency returns to baseline and lock waits stop",
            rollback_strategy=(
                "Not reversible: the killed transactions must be retried by the "
                "application."
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="enable_circuit_breaker",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="Stop calling a failing upstream so callers fail fast instead of hanging.",
            impact="Requests to that dependency fail immediately; dependent features are degraded.",
            verify="timeouts stop accumulating and the caller's latency falls",
            rollback_tool=None,
            rollback_strategy=(
                "Reversible by closing the breaker once the upstream is healthy; "
                "the Agent does not reopen it automatically."
            ),
            # Applied to the *caller*, not the provider. Circuit breaking is a
            # decision made by the client — it cannot be installed on a third
            # party, and the simulator only honours it against the caller's
            # dependency edges.
            targets_dependency=False,
        ),
        ActionProfile(
            tool="switch_payment_provider",
            risk=RiskLevel.CRITICAL.value,
            permission=PermissionLevel.WRITE_EXTERNAL.value,
            reason="Route authorisations to the standby provider.",
            impact="Traffic moves to the backup processor; reconciliations are needed.",
            verify="authorisation success rate recovers at the caller",
            rollback_strategy=(
                "Not reversible without reconciliation: charges may exist on both "
                "processors. Requires finance sign-off."
            ),
            targets_dependency=False,
        ),
        ActionProfile(
            tool="notify_oncall",
            risk=RiskLevel.LOW.value,
            permission=PermissionLevel.READ_ONLY.value,
            reason="Page the on-call engineer with the current diagnosis.",
            impact="A human joins the incident. No infrastructure change.",
            verify="the page was accepted",
            rollback_strategy="Nothing to undo.",
        ),
    )
}


# ---------------------------------------------------------------------------
# Plans by diagnosed category
# ---------------------------------------------------------------------------

#: Ordered most-curative first. The executor tries them in order and stops at
#: the first one that actually changes the observed state, so a plan is a
#: ranked set of hypotheses about the fix, not a script.
CATEGORY_PLANS: dict[str, tuple[str, ...]] = {
    "database": ("rollback_deployment", "increase_pool_size", "restart_service"),
    "memory": ("rollback_deployment", "restart_service"),
    "deployment": ("rollback_deployment",),
    "redis": ("restart_redis", "flush_cache"),
    "third_party": ("enable_circuit_breaker", "switch_payment_provider"),
    "capacity": ("scale_service",),
    # ``cascading`` is the domain key; ``dependency`` is the category value the
    # domain carries. Both resolve to the same plan.
    "cascading": ("restart_service", "enable_circuit_breaker"),
    "dependency": ("restart_service", "enable_circuit_breaker"),
    "slow_database": ("clear_deadlock", "restart_postgres"),
    "unknown": ("restart_service", "notify_oncall"),
}


def build_recovery_actions(
    category: str,
    service: str,
    *,
    dependency: str | None = None,
    current_replicas: int | None = None,
) -> list[dict[str, Any]]:
    """Propose ordered actions for a diagnosed category.

    ``dependency`` is the component the diagnosis blamed, and it is ``None``
    when that turned out to be the alerting service itself. That single fact
    decides targeting, and it is deliberately not a per-category lookup table:
    the same category can be local or remote — a bad release on the service, or
    on something it calls — so only the diagnosis knows which. A table of
    "always local" categories got the checkout-behind-the-gateway case wrong and
    rolled back the edge component, which had nothing to revert.

    ``targets_dependency`` on the profile is the other half of the rule, and it
    is only about the action's nature: a circuit breaker is a decision made by
    the caller, so it lands on the caller no matter where the fault is.

    ``current_replicas`` is the count the Agent actually *measured*, and it is
    not optional detail: "scale to 3" is capacity for a two-replica fleet and a
    no-op for a three-replica one. Proposing a fixed number without reading the
    current one produced a plan that did nothing, a probe that correctly said
    so, and an incident escalated for the wrong reason.

    Returns plain dicts because this is serialised into the graph state and
    the database; the shape matches ``RecoveryActionItem``.
    """
    tools = CATEGORY_PLANS.get(category, CATEGORY_PLANS["unknown"])

    actions: list[dict[str, Any]] = []
    for index, tool in enumerate(tools, start=1):
        profile = PROFILES.get(tool)
        if profile is None:
            continue
        target = dependency if (profile.targets_dependency and dependency) else service
        parameters: dict[str, Any] = {"service": target, "reason": profile.reason}
        rollback_parameters = (
            {**profile.rollback_parameters, "service": target}
            if profile.rollback_tool
            else {}
        )
        if tool == "scale_service":
            # Buy capacity relative to what is already there, and undo back to
            # exactly that. A hardcoded pair of numbers is only ever correct for
            # the one fleet size it was written against.
            previous = current_replicas if current_replicas and current_replicas > 0 else 2
            parameters["replicas"] = previous + 2
            rollback_parameters["replicas"] = previous
        actions.append(
            {
                "ref": f"A{index:02d}",
                "order": index,
                "tool": profile.tool,
                "target_service": target,
                "parameters": parameters,
                "reason": profile.reason,
                "risk_level": profile.risk,
                "expected_impact": profile.impact,
                "verification_strategy": profile.verify,
                "required_permission": profile.permission,
                "rollback_tool": profile.rollback_tool,
                "rollback_parameters": rollback_parameters,
                "rollback_strategy": profile.rollback_strategy,
                "approval_tier": approval_tier(profile.risk),
                "approval_status": "not_required",
                "status": "pending",
            }
        )
    return actions


def plan_summary(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """The worst tier in the plan decides how the whole plan is gated."""
    tiers = [str(a.get("approval_tier") or TIER_MANUAL_ONLY) for a in actions]
    order = {
        TIER_AUTO: 0,
        TIER_APPROVAL: 1,
        TIER_APPROVAL_REVERIFY: 2,
        TIER_MANUAL_ONLY: 3,
    }
    highest = max(tiers, key=lambda t: order.get(t, 3), default=TIER_AUTO)
    risks = [str(a.get("risk_level") or RiskLevel.MEDIUM.value) for a in actions]
    risk_order = {r.value: i for i, r in enumerate(RiskLevel)}
    top_risk = max(risks, key=lambda r: risk_order.get(r, 0), default=RiskLevel.LOW.value)
    return {
        "tier": highest,
        "risk_level": top_risk,
        "requires_approval": highest != TIER_AUTO,
        "reverify": highest == TIER_APPROVAL_REVERIFY,
        "manual_only": highest == TIER_MANUAL_ONLY,
    }


__all__ = [
    "CATEGORY_PLANS",
    "PROFILES",
    "TIER_APPROVAL",
    "TIER_APPROVAL_REVERIFY",
    "TIER_AUTO",
    "TIER_MANUAL_ONLY",
    "ActionProfile",
    "approval_tier",
    "auto_executable",
    "build_recovery_actions",
    "may_attempt_rollback",
    "plan_summary",
]
