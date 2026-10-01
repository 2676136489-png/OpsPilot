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
            reason="回退那个引入回归的版本。",
            impact=(
                "流量回到上一个版本；被回退版本新增的功能在重新发布之前不可用。"
            ),
            verify="error_rate 与 latency_p95 回到发布前的基线",
            rollback_strategy=(
                "不可逆：被回退的版本只能重新发布一次才能恢复。需要发布负责人配合。"
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
            reason="重启该组件，清掉它进程内的状态。",
            impact="实例轮转期间短暂不可用；正在处理的请求会失败。",
            verify="health 恢复为 healthy，且错误率停止上升",
            rollback_strategy=(
                "重启本身就是自己的逆操作，不需要额外的补偿动作。"
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="scale_service",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="扩容，让负载分散到更多实例上。",
            impact="故障期间基础设施成本上升。",
            verify="cpu 与 latency_p95 回落到各自阈值以下",
            rollback_tool="scale_service",
            # The count is filled in from the measured fleet size, not fixed
            # here — see build_recovery_actions.
            rollback_strategy="缩容回原来的实例数。",
        ),
        ActionProfile(
            tool="increase_pool_size",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="调高连接池上限，让排队的请求先能被服务。",
            impact="数据库并发压力上升；只是争取时间，并不能止住泄漏。",
            verify="连接池利用率降到 80% 以下，连接超时不再出现",
            rollback_strategy=(
                "无法就地回退：底层的泄漏修好之后，抬高的上限自然失效，因此保留原样。"
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="restart_redis",
            risk=RiskLevel.HIGH.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="对缓存集群做故障转移，让客户端能重新连上。",
            impact="缓存冷启动，回暖之前读延迟偏高。",
            verify="缓存恢复健康，且依赖它的服务不再打连接失败日志",
            rollback_strategy="重启本身就是自己的逆操作。",
            targets_dependency=True,
        ),
        ActionProfile(
            tool="flush_cache",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="丢弃正在被返回给客户端的脏缓存条目。",
            impact="所有缓存值丢失；回源压力上升，直到缓存重新填满。",
            verify="缓存读取成功，且源站错误率没有上升",
            rollback_strategy="不可逆：被清空的缓存只能从源站重新填充。",
            targets_dependency=True,
        ),
        ActionProfile(
            tool="restart_postgres",
            risk=RiskLevel.CRITICAL.value,
            permission=PermissionLevel.DESTRUCTIVE.value,
            reason="重启数据库，清掉卡住的会话与锁。",
            impact="所有连接断开；期间依赖它的服务全部不可用。",
            verify="postgres 恢复健康，调用方不再超时",
            rollback_strategy=(
                "不可逆：会话与未提交事务都会丢失。需要数据库负责人配合。"
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="clear_deadlock",
            risk=RiskLevel.HIGH.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="杀掉那些一直持有行锁的阻塞事务。",
            impact="被阻塞行上正在执行的语句会被回滚。",
            verify="查询延迟回到基线，锁等待消失",
            rollback_strategy=(
                "不可逆：被强杀的事务需要由应用侧重试。"
            ),
            targets_dependency=True,
        ),
        ActionProfile(
            tool="enable_circuit_breaker",
            risk=RiskLevel.MEDIUM.value,
            permission=PermissionLevel.MUTATE_INFRA.value,
            reason="停止调用正在失败的上游，让调用方快速失败而不是挂起。",
            impact="发往该依赖的请求立刻失败；相关功能降级。",
            verify="超时不再累积，调用方延迟回落",
            rollback_tool=None,
            rollback_strategy=(
                "上游恢复健康后关闭熔断器即可回退；Agent 不会自动重新打开。"
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
            reason="把授权请求切到备用渠道。",
            impact="流量转移到备用通道；后续需要做对账。",
            verify="调用方的授权成功率恢复",
            rollback_strategy=(
                "不做对账就无法回退：两个通道上可能都存在扣款。需要财务确认。"
            ),
            targets_dependency=False,
        ),
        ActionProfile(
            tool="notify_oncall",
            risk=RiskLevel.LOW.value,
            permission=PermissionLevel.READ_ONLY.value,
            reason="把当前诊断结果呼叫给值班工程师。",
            impact="有人类加入这次故障处理。不改变任何基础设施。",
            verify="呼叫已被接收",
            rollback_strategy="无需回退。",
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
