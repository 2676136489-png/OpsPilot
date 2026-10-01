"""Evidence → signals → hypotheses → diagnosis.

Everything here is a **pure function of the evidence**. There is no path
where the model is asked "what do you think broke?" and its answer is
believed: a hypothesis only exists if a fault domain is supported by named
signals, and a root cause may only cite evidence refs that actually produced
those signals.

The vocabulary of signals and the catalogue of fault domains live in
:mod:`opspilot_backend.agent.investigation`. This module owns what happens
*after* a domain has been ranked: turning it into a testable hypothesis,
probing it, and deciding what the run can honestly claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from opspilot_backend.agent.investigation import (
    DOMAIN_BY_KEY,
    dependency_targets,
    evidence_refs_for,
    extract_signals,
    score_domains,
)
from opspilot_backend.agent.state import (
    EvidenceItem,
    HypothesisItem,
    IncidentState,
)
from opspilot_backend.domain.enums import (
    DiagnosisOutcome,
    EvidenceType,
    HypothesisStatus,
)

#: Confidence at and above which a tested hypothesis is called confirmed.
_CONFIRM_AT = 0.75
#: Confidence at and above which an untested but well-supported hypothesis is
#: still worth telling a human about, labelled as probable.
_PROBABLE_AT = 0.55
#: Below this a hypothesis is discarded rather than carried forward. Set below
#: the confidence an untested-but-plausible alternative starts at, so that a
#: failed probe downgrades without erasing: "could not reproduce" is weaker
#: evidence than "was contradicted".
_REJECT_BELOW = 0.35


def _signals(names) -> str:
    """Rendered list of signal names for a sentence an operator will read.

    The names themselves stay in their wire spelling — they are the vocabulary
    :data:`~opspilot_backend.agent.investigation._DOMAINS` matches on, and a
    signal that is not spelled exactly right stops matching. What must not
    survive is the Python container: ``f"{sorted(x)}"`` used to put
    ``['deployment_recent']`` — brackets, quotes and all — in the middle of a
    Chinese sentence, which reads as a bug even when the diagnosis is right.
    """
    return "、".join(sorted(str(name) for name in names))


# ---------------------------------------------------------------------------
# Hypothesis generation — one hypothesis per ranked fault domain
# ---------------------------------------------------------------------------


def generate_hypotheses(
    state: IncidentState,
    *,
    dependency_targets: tuple[str, ...] = (),
    max_hypotheses: int = 3,
) -> list[HypothesisItem]:
    """Propose the leading explanations, each citing only its own evidence.

    Hypotheses come from the ranked fault domains, not from a keyword list
    scanned in isolation — which is why a service whose logs mention the
    database does not automatically get "database problem" as a hypothesis.
    """
    signals = extract_signals(state.evidence, dependency_targets=dependency_targets)
    if not signals:
        return []

    rejected = set(state.plan.rejected_domains or ())
    domains = score_domains(signals, exclude=tuple(rejected))
    if not domains:
        return []

    hypotheses: list[HypothesisItem] = []
    for key, score in domains[:max_hypotheses]:
        domain = DOMAIN_BY_KEY.get(key)
        if domain is None:
            continue
        refs = evidence_refs_for(signals, key)
        if not refs:
            continue
        support = [s for s in domain.required if s in signals]
        corroboration = [s for s in domain.supporting if s in signals]
        hypotheses.append(
            HypothesisItem(
                # Local counter: state.hypotheses does not grow inside this
                # loop, so state.next_hypothesis_ref() would mint H001 for
                # every domain and the DB upsert would collapse them into one.
                ref=f"H{len(state.hypotheses) + len(hypotheses) + 1:03d}",
                statement=domain.label,
                category=domain.category,
                domain=domain.key,
                confidence=round(score, 2),
                status=HypothesisStatus.PROPOSED.value,
                reasoning=(
                    f"所需信号 {_signals(support)} 已经出现"
                    + (
                        f"；并有 {_signals(corroboration)} 佐证"
                        if corroboration
                        else ""
                    )
                    + f"。引用证据：{'、'.join(refs[:6])}。"
                ),
                evidence_refs=refs,
            )
        )
    return hypotheses


# ---------------------------------------------------------------------------
# Verification — a probe that can falsify, not a probe that can only confirm
# ---------------------------------------------------------------------------

#: One probe per domain, chosen so that it reproduces that domain's
#: *required* signal — otherwise the probe can only ever fail to confirm, and
#: every hypothesis decays to "testing" no matter how strong the evidence is.
#: (Querying ``db_connections`` for the database domain looked reasonable and
#: was useless: it produces ``db_pool_pressure``, which is only a supporting
#: signal, so the required signal was never reproduced.)
_VERIFICATION_QUERIES: dict[str, dict[str, Any]] = {
    # Pool saturation is a ratio, and the status snapshot is the only thing
    # that carries pool_max — and the only thing that drops back under the
    # threshold once the pool is drained, which is what makes this falsifiable.
    "database": {"tool": "get_service_status"},
    # Log-signature domains re-read the logs, but over a *short* window.
    # Reading the whole incident window made verification unfalsifiable: the
    # errors that happened ten minutes ago are still in the log after the fix,
    # so every hypothesis was "confirmed" no matter what had changed since.
    # Current state, not history: a cache that has recovered stops being
    # reported as a cache failure, so the same probe that confirms an active
    # outage falsifies a fixed one.
    "slow_database": {"tool": "query_logs", "level": "ERROR", "minutes": 3},
    "deployment": {"tool": "get_deployments", "limit": 5},
    "memory": {"tool": "get_service_status"},
    "redis": {"tool": "get_service_status"},
    "third_party": {"tool": "get_service_status"},
    "capacity": {"tool": "query_metrics", "metric_names": ["cpu", "latency_p95"]},
    # Run against the dependency, whose name the node reads from the evidence.
    "cascading": {"tool": "get_service_status"},
}


def verification_query(hypothesis: HypothesisItem) -> dict[str, Any] | None:
    """The probe whose answer would most change this hypothesis's standing."""
    query = _VERIFICATION_QUERIES.get(hypothesis.domain)
    if query is None:
        query = _VERIFICATION_QUERIES.get(hypothesis.category)
    return dict(query) if query else None


def verification_target(
    hypothesis: HypothesisItem, state: IncidentState
) -> str | None:
    """Which component the verification probe should run against.

    The claim is tested where the evidence came from, not where the alert
    fired — a "cache unavailable" hypothesis is tested against the cache,
    because probing the service that *called* it only re-observes the symptom.

    Concretely: the component that produced one of this domain's *required*
    signals. Not just any component in the citation list — a pool-exhaustion
    hypothesis also cites the dependency sweep, and probing a healthy postgres
    for it "disproved" a fault that was in checkout all along.
    """
    domain = DOMAIN_BY_KEY.get(hypothesis.domain)
    if domain is not None and domain.key == "third_party":
        # A claim about a third party is tested against the third party, even
        # though external components are otherwise excluded from the dependency
        # sweep (they cannot be restarted, so "dependency is down" would send
        # the Agent looking for a fix that does not exist).
        for item in state.evidence:
            if item.type != EvidenceType.DEPENDENCY.value:
                continue
            for edge in ((item.value or {}).get("edges") or []):
                if not isinstance(edge, dict):
                    continue
                target = str(edge.get("depends_on") or "")
                if target.startswith("external-"):
                    return target
    if domain is not None:
        # Extracted *with* the dependency topology, not without it. Signals like
        # ``dependency_unhealthy`` only exist once the extractor knows which
        # components are dependencies, so omitting them made the cascading
        # domain unresolvable here and sent its recovery at the alerting
        # service instead of the dependency that was actually down.
        signals = extract_signals(
            state.evidence, dependency_targets=dependency_targets(state)
        )
        candidates: list[str] = []
        for name in domain.required:
            signal = signals.get(name)
            if signal is None:
                continue
            for ref in signal.evidence_refs:
                item = state.evidence_by_ref(ref)
                if item is None:
                    continue
                service = str(item.service or "")
                if not service:
                    continue
                # Prefer a component other than the alerting service that is
                # actually measured faulty: that is the thing the hypothesis is
                # about. A healthy dependency merely mentioned in the citation
                # list is not — probing it "disproved" faults that were local.
                if service != state.incident.service and str(
                    (item.value or {}).get("health") or ""
                ).lower() not in {"healthy", ""}:
                    return service
                candidates.append(service)
        if candidates:
            return candidates[0]
    return state.incident.service


def recovery_target(state: IncidentState) -> str | None:
    """The component a recovery action should be applied to, or None.

    Deliberately the same answer as :func:`verification_target`: the fix belongs
    where the claim was tested. Returns ``None`` when that resolves to the
    alerting service itself, which lets the caller fall back to it without the
    recovery layer needing to know the difference.
    """
    diagnosis = state.diagnosis
    if diagnosis is None:
        return None

    domain = diagnosis.domain or ""
    hypothesis: HypothesisItem | None = None
    for item in state.hypotheses:
        if domain and item.domain == domain:
            hypothesis = item
            if item.status == HypothesisStatus.CONFIRMED.value:
                break
        elif hypothesis is None and item.category == diagnosis.category:
            hypothesis = item
    if hypothesis is None:
        return None

    target = verification_target(hypothesis, state)
    if target is None or target == state.incident.service:
        return None
    return target


def apply_verification(
    hypothesis: HypothesisItem,
    new_evidence: list[EvidenceItem],
    *,
    dependency_targets: tuple[str, ...] = (),
) -> HypothesisItem:
    """Update a hypothesis after its targeted probe came back.

    A probe that returns nothing is a falsification, not a neutral event: the
    Agent asked "is the pool still saturated?" and the data says it cannot
    show that it is. Treating silence as "no news" is how a hypothesis survives
    to the end of a run without ever having been supported.
    """
    domain = DOMAIN_BY_KEY.get(hypothesis.domain)

    if not new_evidence:
        hypothesis.confidence = round(max(0.0, hypothesis.confidence - 0.20), 2)
        hypothesis.status = HypothesisStatus.REJECTED.value
        hypothesis.reasoning += (
            " | 验证探针没有返回任何数据 —— 该假设不成立"
        )
        return hypothesis

    fresh = extract_signals(new_evidence, dependency_targets=dependency_targets)
    hypothesis.evidence_refs = sorted(
        set(hypothesis.evidence_refs) | {e.ref for e in new_evidence}
    )

    # A component that measures healthy right now falsifies any hypothesis
    # claiming it is broken. Without this the Agent could keep asserting a
    # cause it had already fixed, because the historical evidence it collected
    # on the way in still described the broken state.
    recovered = any(
        str((e.value or {}).get("health") or "").lower() == "healthy"
        for e in new_evidence
    )
    if recovered and domain is not None and domain.key != "deployment":
        hypothesis.confidence = round(max(0.0, hypothesis.confidence - 0.30), 2)
        hypothesis.status = HypothesisStatus.REJECTED.value
        hypothesis.reasoning += (
            " | 被探测的组件当前测量结果为健康 —— 所声称的故障已不复存在"
        )
        return hypothesis

    if domain is None:
        # No domain means no declared test — nothing was actually verified.
        hypothesis.status = HypothesisStatus.TESTING.value
        hypothesis.reasoning += (
            f" | 新增了 {len(new_evidence)} 条证据，但这条假设没有定义可执行的检验"
        )
        return hypothesis

    required_hits = [s for s in domain.required if s in fresh]
    contradicting = [s for s in domain.contradicts if s in fresh]

    if contradicting and not required_hits:
        hypothesis.confidence = round(max(0.0, hypothesis.confidence - 0.30), 2)
        hypothesis.status = HypothesisStatus.REJECTED.value
        hypothesis.reasoning += (
            f" | 被 {_signals(contradicting)} 反驳；而所需的 "
            f"{_signals(domain.required)} 并未出现"
        )
        return hypothesis

    if required_hits:
        hypothesis.confidence = round(
            min(0.97, hypothesis.confidence + 0.12 * len(required_hits)), 2
        )
        hypothesis.status = (
            HypothesisStatus.CONFIRMED.value
            if hypothesis.confidence >= _CONFIRM_AT
            else HypothesisStatus.TESTING.value
        )
        hypothesis.reasoning += f" | 被 {_signals(required_hits)} 证实"
        return hypothesis

    # The probe ran but could not reproduce this domain's signals. That
    # downgrades the hypothesis but does not necessarily kill it: a capacity
    # claim probed *after* the autoscaler caught up measures a healthy service
    # and still deserves to reach a human as "probable". Only a hypothesis that
    # has run out of support is discarded — which is what ``_REJECT_BELOW``
    # draws the line at.
    hypothesis.confidence = round(max(0.0, hypothesis.confidence - 0.15), 2)
    hypothesis.status = (
        HypothesisStatus.REJECTED.value
        if hypothesis.confidence < _REJECT_BELOW
        else HypothesisStatus.TESTING.value
    )
    hypothesis.reasoning += (
        f" | 探针未能复现所需的 {_signals(domain.required)}"
    )
    return hypothesis


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootCause:
    """The run's conclusion, with everything the layers after it need.

    A tuple was enough while the diagnosis was only read by a log line. It is
    not enough now: the recovery layer has to know the *domain* the winning
    hypothesis came from, because ``slow_database`` and ``database`` share a
    category and need opposite remediations — clear the datastore, or roll the
    service back. Dropping the domain here silently sent every blocked-query
    incident down the connection-pool path.
    """

    statement: str
    category: str
    domain: str
    confidence: float
    evidence_refs: list[str]
    summary: str
    outcome: str


def decide_root_cause(state: IncidentState) -> RootCause:
    """Pick the winning hypothesis and say how sure the run is.

    The outcome is one of the four values in :class:`DiagnosisOutcome` — the
    run never reports a bare "unknown", because "I tested and could not
    confirm" and "my tools broke" demand completely different responses.
    """
    cited = [
        h
        for h in state.hypotheses
        if h.status != HypothesisStatus.REJECTED.value
        and [r for r in h.evidence_refs if state.evidence_by_ref(r) is not None]
    ]
    if not cited:
        if state.evidence:
            return RootCause(
                "", "unknown", "", 0.0, [],
                "所有假设都被否决，或者没有任何假设引用了已存证据。",
                DiagnosisOutcome.INSUFFICIENT_EVIDENCE.value,
            )
        return RootCause(
            "", "unknown", "", 0.0, [],
            "没有收集到任何证据，因此也无法检验任何解释。",
            DiagnosisOutcome.INVESTIGATION_FAILED.value,
        )

    confirmed = [h for h in cited if h.status == HypothesisStatus.CONFIRMED.value]
    ranked = sorted(
        confirmed or cited,
        key=lambda h: (h.confidence, len(h.evidence_refs)),
        reverse=True,
    )
    top = ranked[0]
    refs = [r for r in top.evidence_refs if state.evidence_by_ref(r) is not None]

    if confirmed:
        outcome = DiagnosisOutcome.ROOT_CAUSE_CONFIRMED.value
    elif top.confidence >= _PROBABLE_AT:
        outcome = DiagnosisOutcome.ROOT_CAUSE_PROBABLE.value
    else:
        outcome = DiagnosisOutcome.INSUFFICIENT_EVIDENCE.value

    summary = (
        f"{top.statement}。判定为 {outcome}，置信度 {top.confidence:.2f}，"
        f"依据 {len(refs)} 条证据：" + "、".join(refs[:8]) + f"。{top.reasoning}"
    )
    return RootCause(
        statement=top.statement,
        category=top.category,
        domain=top.domain,
        confidence=top.confidence,
        evidence_refs=refs,
        summary=summary,
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Recovery planning — driven by the diagnosed category
# ---------------------------------------------------------------------------

RECOVERY_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "database": [
        {
            "tool": "rollback_deployment",
            "risk_level": "CRITICAL",
            "reason": "如果故障窗口内确实有过一次发布，回退那个泄漏连接的变更。",
            "expected_impact": "回退版本会暂时失去它带来的功能。",
        },
        {
            "tool": "increase_pool_size",
            "risk_level": "MEDIUM",
            "reason": "调高连接池上限，让排队的请求先被服务，同时再修泄漏。",
            "expected_impact": "数据库负载升高；这只是争取时间，治不了泄漏。",
        },
        {
            "tool": "restart_service",
            "risk_level": "HIGH",
            "reason": "重启可以把被打满的连接池清空。",
            "expected_impact": "短暂停机；如果泄漏还在，连接池会重新填满。",
        },
    ],
    "slow_database": [
        {
            "tool": "clear_deadlock",
            "risk_level": "HIGH",
            "reason": "杀掉一直持有行锁的阻塞事务。",
            "expected_impact": "被阻塞行上正在执行的语句会被回滚。",
        },
        {
            "tool": "restart_postgres",
            "risk_level": "CRITICAL",
            "reason": "如果锁清不掉，就重启数据库。",
            "expected_impact": "所有连接断开；依赖它的服务会短暂全面不可用。",
        },
    ],
    "deployment": [
        {
            "tool": "rollback_deployment",
            "risk_level": "CRITICAL",
            "reason": "回退到错误率飙升之前最后一个已知正常的版本。",
            "expected_impact": "回退版本会暂时失去它带来的功能。",
        }
    ],
    "memory": [
        {
            "tool": "rollback_deployment",
            "risk_level": "CRITICAL",
            "reason": "回退那个引入无界持有的版本。",
            "expected_impact": "暂时失去该功能；堆内存会永久回到基线。",
        },
        {
            "tool": "restart_service",
            "risk_level": "HIGH",
            "reason": "在容器被 OOM 杀掉之前，先重启把泄漏的堆内存收回。",
            "expected_impact": "短暂停机；泄漏会从零重新开始累积。",
        },
    ],
    "redis": [
        {
            "tool": "restart_redis",
            "risk_level": "HIGH",
            "reason": "对缓存集群做故障转移或重启。",
            "expected_impact": "缓存冷启动，几分钟内命中率偏低。",
        }
    ],
    "third_party": [
        {
            "tool": "enable_circuit_breaker",
            "risk_level": "MEDIUM",
            "reason": "停止调用一个正在失败的渠道，改为快速失败。",
            "expected_impact": "请求立刻失败而不是挂起；部分功能降级。",
        },
        {
            "tool": "switch_payment_provider",
            "risk_level": "CRITICAL",
            "reason": "把授权请求切到备用渠道。",
            "expected_impact": "流量转移到备用通道；后续需要做对账。",
        },
    ],
    "capacity": [
        {
            "tool": "scale_service",
            "risk_level": "MEDIUM",
            "reason": "扩容，把 CPU 和延迟拉回 SLO 以内。",
            "expected_impact": "资源成本上升。",
        }
    ],
    "dependency": [
        {
            "tool": "restart_service",
            "risk_level": "HIGH",
            "reason": "重启告警服务所调用的那个故障依赖。",
            "expected_impact": "该依赖短暂停机。",
        },
        {
            "tool": "enable_circuit_breaker",
            "risk_level": "MEDIUM",
            "reason": "把流量从故障依赖上卸掉，让调用方优雅降级。",
            "expected_impact": "依赖恢复之前功能受限。",
        },
    ],
    "unknown": [
        {
            "tool": "restart_service",
            "risk_level": "HIGH",
            "reason": "没有足够可信的结论，重启是风险最低的缓解手段。",
            "expected_impact": "短暂停机。",
        }
    ],
}


def build_recovery_actions(category: str, service: str) -> list[dict[str, Any]]:
    """Ordered candidate actions for a diagnosed category.

    Ordered most-curative first. The risk assessment node decides which of
    them may actually run.
    """
    templates = RECOVERY_TEMPLATES.get(category, RECOVERY_TEMPLATES["unknown"])
    actions: list[dict[str, Any]] = []
    for index, template in enumerate(templates, start=1):
        parameters: dict[str, Any] = {
            "service": service,
            "reason": template["reason"],
        }
        if template["tool"] == "scale_service":
            parameters["replicas"] = 3
        actions.append(
            {
                "ref": f"A{index:02d}",
                "tool": template["tool"],
                "target_service": service,
                "parameters": parameters,
                "reason": template["reason"],
                "risk_level": template["risk_level"],
                "expected_impact": template["expected_impact"],
                "status": "pending",
            }
        )
    return actions


def verification_criteria(category: str) -> list[str]:
    """What "fixed" means for this kind of cause, in checkable terms.

    Memory is expressed as a *fraction of the component's limit*, never as an
    absolute megabyte figure: the datastores idle at 4 GB, so "memory < 1024MB"
    is a criterion that can never pass for postgres and would have made every
    database-category recovery look like a failure.
    """
    base = ["error_rate <= 1%", "latency_p95 <= 500ms", "health_status == healthy"]
    if category in ("database", "slow_database"):
        return ["db_connections < 80", "query_latency_p95 <= 200ms"] + base
    if category == "memory":
        return ["memory_ratio < 0.85"] + base
    if category == "capacity":
        return ["cpu < 80%"] + base
    if category == "redis":
        return ["cache_health == healthy", "cache_error_rate <= 1%"] + base
    if category in ("third_party", "cascading", "dependency"):
        return ["dependency_health == healthy"] + base
    return base


# ---------------------------------------------------------------------------
# Deployment correlation — "did a deploy land right before the incident?"
# ---------------------------------------------------------------------------


def correlate_deployment(
    evidence: list[EvidenceItem], *, window_minutes: int = 60
) -> dict[str, Any] | None:
    """Find a deployment that landed within the incident window."""
    for item in evidence:
        if item.type != EvidenceType.DEPLOYMENT.value:
            continue
        rows = (item.value or {}).get("deployments") or []
        for row in rows:
            when = _parse_ts(row.get("deployed_at"))
            if when is None:
                continue
            if datetime.now(timezone.utc) - when <= timedelta(minutes=window_minutes):
                return {
                    "evidence_ref": item.ref,
                    "service": item.service,
                    "version": row.get("version"),
                    "status": row.get("status"),
                    "deployed_at": row.get("deployed_at"),
                    "minutes_before": int(
                        (datetime.now(timezone.utc) - when).total_seconds() // 60
                    ),
                }
    return None


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.replace("Z", "")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        import re

        match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
        if not match:
            return None
        return datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
