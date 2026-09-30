"""Drive the real recovery nodes against a live simulator, end to end.

This is the recovery half of ``probe_investigation.py``: it runs the actual
``recovery_planner`` / ``risk_assessment`` / ``recovery_executor`` /
``verification`` / ``rollback`` nodes through the real :class:`ToolExecutor`,
which really calls the simulator over HTTP and really mutates the environment.

    python scripts/probe_recovery.py [--url http://127.0.0.1:8090]

The approval node is skipped on purpose — ``interrupt()`` needs a LangGraph
checkpointer, and the gate itself is covered by the backend lifecycle test.
Everything either side of it is the production code path.

Scoring is deliberately two-sided. The Agent's own verdict is one column; the
scenario's ground-truth criteria, evaluated against the live environment after
the run, is the other. A recovery that reports success while the environment
disagrees is the failure mode this script exists to catch.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "backend", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from opspilot_backend.agent.budget import Budget  # noqa: E402
from opspilot_backend.agent.context import NodeContext  # noqa: E402
from opspilot_backend.agent.nodes import (  # noqa: E402
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
from opspilot_backend.agent.state import IncidentRef, IncidentState  # noqa: E402
from opspilot_backend.domain.enums import (  # noqa: E402
    DiagnosisOutcome,
    HypothesisStatus,
)
from opspilot_backend.infrastructure.container import get_providers  # noqa: E402
from opspilot_backend.tools.executor import ToolExecutor  # noqa: E402
from opspilot_backend.tools.hooks import NullToolHooks  # noqa: E402
from probe_investigation import EXPECTED, HarnessPersistence, merge  # noqa: E402

#: The approval id the probe pretends a reviewer issued.
PROBE_APPROVAL = "probe-approval"


class ApprovingHooks(NullToolHooks):
    """Stands in for the human at the approval gate.

    The gate itself is not bypassed — ``_approval_gate`` still asks for an
    APPROVED approval and still refuses anything else. Only the answer is
    supplied, which is exactly what the real resume path does after a reviewer
    clicks approve.
    """

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        if approval_id == PROBE_APPROVAL:
            return {"id": approval_id, "status": "approved"}
        return None


def make_context(scenario: str) -> NodeContext:  # type: ignore[no-redef]
    hooks = ApprovingHooks()
    return NodeContext(
        run_id=f"run-{scenario}",
        incident_id=f"inc-{scenario}",
        executor=ToolExecutor(hooks=hooks),
        hooks=hooks,
        persistence=HarnessPersistence(),
        budget=Budget.from_settings(),
    )

#: Categories whose correct fix the simulator will not accept as "done" from
#: the Agent's side alone — kept only for the summary line.
_RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


async def investigate(state: IncidentState, config: dict[str, Any]) -> IncidentState:
    """The investigation half — identical to the investigation probe."""
    state = merge(state, await load_context(state, config))
    state = merge(state, await investigation_planner(state, config))

    rounds = 0
    while rounds < 4:
        rounds += 1
        if not state.plan.steps:
            break
        state = merge(state, await parallel_investigation(state, config))
        state = merge(state, await evidence_aggregation(state, config))
        if state.decision != "replan":
            break
        state = merge(state, await investigation_planner(state, config))

    state = merge(state, await hypothesis_generation(state, config))

    hyp_rounds = 0
    while hyp_rounds < 3:
        state = merge(state, await hypothesis_verification(state, config))
        if state.decision != "replan":
            break
        state = merge(state, await investigation_planner(state, config))
        if not state.plan.steps:
            break
        state = merge(state, await parallel_investigation(state, config))
        state = merge(state, await hypothesis_generation(state, config))
        hyp_rounds += 1

    state = merge(state, await root_cause_diagnosis(state, config))
    return state


async def recover(state: IncidentState, config: dict[str, Any]) -> IncidentState:
    """Plan → assess → execute → verify, following the compensation path."""
    state = merge(state, await recovery_planner(state, config))
    if state.decision == "stop":
        return state
    state = merge(state, await risk_assessment(state, config))

    # The probe plays the approving human. The real gate is the interrupt.
    recovery = state.recovery.model_dump()
    recovery["approval_status"] = "approved"
    recovery["approval_id"] = "probe-approval"
    state = merge(state, {"recovery": recovery})

    state = merge(state, await recovery_executor(state, config))

    guard = 0
    while guard < 4:
        guard += 1
        if state.decision == "rollback":
            state = merge(state, await rollback(state, config))
            continue
        if state.decision in ("verify", "reverify"):
            before = state.recovery.rollback_outcome
            state = merge(state, await verification(state, config))
            # A verification that fails after a rollback escalates; the node
            # sets the reason, so stop the loop on anything but another
            # rollback request.
            if state.decision == "rollback" and before:
                break
            if state.decision != "rollback":
                break
            continue
        break
    return state


async def score_environment(scenario: str) -> dict[str, Any]:
    """Ask the simulator whether the scenario's own criteria now hold."""
    providers = get_providers()
    truth = await providers.services.ground_truth(scenario)
    criteria = truth.get("verification_criteria") or []
    if not criteria:
        return {"evaluated": False, "passed": None, "criteria": []}
    result = await providers.services.evaluate_criteria(criteria)
    return {
        "evaluated": True,
        "passed": bool(result.get("passed")),
        "checks": result.get("checks", []),
        "criteria": criteria,
        "correct_recovery": truth.get("correct_recovery"),
        "hidden_root_cause": truth.get("hidden_root_cause"),
    }


async def run_scenario(scenario: str, *, verbose: bool) -> dict[str, Any]:
    providers = get_providers()
    await providers.services.reset(scenario)
    payload = await providers.services.inject(scenario)
    service = payload.get("alert_service") or payload.get("service") or ""

    state = IncidentState(
        incident=IncidentRef(
            incident_id=f"inc-{scenario}",
            service=service,
            severity="SEV2",
            title=scenario,
            scenario=scenario,
        )
    )
    ctx = make_context(scenario)
    config = {"configurable": {"ctx": ctx}}

    state = await investigate(state, config)

    best = None
    for hyp in state.hypotheses:
        if hyp.status == HypothesisStatus.REJECTED.value:
            continue
        if best is None or hyp.confidence > best.confidence:
            best = hyp

    state = await recover(state, config)
    env = await score_environment(scenario)

    actions = [
        {
            "ref": a.ref,
            "tool": a.tool,
            "target": a.target_service,
            "risk": a.risk_level,
            "tier": a.approval_tier,
            "status": a.status,
            "effective": a.effective,
            "rollback_tool": a.rollback_tool,
            "error": a.error,
        }
        for a in state.recovery.actions
    ]

    diagnosis_ok = bool(
        best
        and state.diagnosis
        and state.diagnosis.outcome
        in (
            DiagnosisOutcome.ROOT_CAUSE_CONFIRMED.value,
            DiagnosisOutcome.ROOT_CAUSE_PROBABLE.value,
        )
    )

    return {
        "scenario": scenario,
        "service": service,
        "expected": EXPECTED.get(scenario, "?"),
        "domain": best.domain if best else None,
        "category": best.category if best else None,
        "confidence": best.confidence if best else 0.0,
        "diagnosis_outcome": state.diagnosis.outcome if state.diagnosis else None,
        "diagnosis_ok": diagnosis_ok,
        "tier": state.recovery.approval_tier,
        "risk": state.recovery.risk_level,
        "plan_status": state.recovery.status,
        "effective_ref": state.recovery.effective_ref,
        "executed": state.recovery.executed_refs,
        "rollback_refs": state.recovery.rollback_refs,
        "rollback_outcome": state.recovery.rollback_outcome,
        "escalation_reason": state.recovery.escalation_reason,
        "actions": actions,
        "verification": state.verification.status,
        "checks": state.verification.checks,
        "passed_checks": state.verification.passed_checks,
        "total_checks": state.verification.total_checks,
        "decision": state.decision,
        "env": env,
        "tool_calls": ctx.budget.tool_calls,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", default=os.environ.get("OPSPILOT_SIM_URL", "http://127.0.0.1:8090")
    )
    parser.add_argument("--only", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    os.environ["OPSPILOT_SIMULATOR_URL"] = args.url
    os.environ.setdefault("OPSPILOT_DATABASE_URL", "sqlite+aiosqlite:///./probe.db")

    names = [n for n in EXPECTED if args.only in n] if args.only else list(EXPECTED)
    failures: list[str] = []
    mismatches: list[str] = []

    for name in names:
        try:
            r = await run_scenario(name, verbose=args.verbose)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
            print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
            continue

        env = r["env"]
        agent_ok = r["verification"] == "passed"
        env_ok = env.get("passed")
        recovered = r["effective_ref"] is not None

        mark = "PASS" if (agent_ok and env_ok is not False) else "FAIL"
        if mark == "FAIL":
            failures.append(name)
        if agent_ok and env_ok is False:
            mismatches.append(name)

        print(
            f"[{mark}] {r['scenario']:<32} "
            f"diag={r['category']}({r['domain']}) {r['confidence']:.2f} "
            f"tier={r['tier']:<21} "
            f"effective={r['effective_ref'] or '-':<4} "
            f"verify={r['verification']:<7} "
            f"env={'n/a' if env_ok is None else ('fixed' if env_ok else 'still-broken')} "
            f"tools={r['tool_calls']}"
        )
        if mark == "FAIL" or args.verbose:
            for a in r["actions"]:
                print(
                    f"         {a['ref']} {a['tool']:<24} -> {a['target']:<20} "
                    f"{a['risk']:<8} {a['tier']:<21} "
                    f"{a['status']:<12} effective={a['effective']} "
                    f"rollback={a['rollback_tool'] or '-'}"
                    + (f" err={a['error']}" if a["error"] else "")
                )
            for c in r["checks"]:
                if not c.get("passed"):
                    print(
                        f"         ! {c['name']:<28} actual={c['actual']} "
                        f"threshold={c['threshold']}"
                    )
            if r["rollback_refs"] or r["rollback_outcome"]:
                print(
                    f"         rollback refs={r['rollback_refs']} "
                    f"outcome={r['rollback_outcome']}"
                )
            if r["escalation_reason"]:
                print(f"         escalated: {r['escalation_reason']}")
            if env.get("correct_recovery"):
                print(f"         ground truth says: {env['correct_recovery']}")
            if not recovered and env_ok:
                print("         NOTE: environment fixed without the Agent doing anything")

    print()
    print(f"{len(names) - len(failures)}/{len(names)} scenarios recovered correctly")
    if mismatches:
        print(
            "reported success but the environment disagrees: " + ", ".join(mismatches)
        )
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
