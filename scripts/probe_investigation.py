"""Drive the real investigation nodes against a live simulator.

This is not a unit test of the policy module in isolation — it builds a
:class:`NodeContext` with the real :class:`ToolExecutor` (which really calls
the simulator over HTTP), runs the real ``load_context`` /
``investigation_planner`` / ``parallel_investigation`` / ``evidence_aggregation``
/ ``hypothesis_generation`` / ``hypothesis_verification`` nodes, and reports
which fault domain the Agent converges on for each scenario.

    python scripts/probe_investigation.py [--url http://127.0.0.1:8090]

The simulator must be running. If a scenario's expected category does not come
out on top, that is a real defect in the reasoning, not a test to relax.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "backend", "src"))

from opspilot_backend.agent.budget import Budget  # noqa: E402
from opspilot_backend.agent.context import NodeContext  # noqa: E402
from opspilot_backend.agent.nodes import (  # noqa: E402
    evidence_aggregation,
    hypothesis_generation,
    hypothesis_verification,
    investigation_planner,
    load_context,
    parallel_investigation,
)
from opspilot_backend.agent.state import IncidentState, IncidentRef  # noqa: E402
from opspilot_backend.domain.enums import HypothesisStatus  # noqa: E402
from opspilot_backend.infrastructure.container import get_providers  # noqa: E402
from opspilot_backend.tools.executor import ToolExecutor  # noqa: E402
from opspilot_backend.tools.hooks import NullToolHooks  # noqa: E402

# scenario name → the category the diagnosis must land on
EXPECTED: dict[str, str] = {
    "checkout-db-pool-exhaustion": "database",
    "redis-failure": "redis",
    "checkout-memory-leak": "memory",
    "payment-bad-deployment": "deployment",
    "payment-third-party-timeout": "third_party",
    "postgres-slow-queries": "database",
    "inventory-cpu-saturation": "capacity",
    "gateway-dependency-cascade": "dependency",
    "payment-high-error-rate": "deployment",
    "checkout-deployment-cascade": "deployment",
    "payment-provider-outage": "third_party",
    "checkout-cpu-saturation": "capacity",
}


class HarnessPersistence:
    """In-memory stand-in for the repository layer.

    The nodes only ever talk to ``AgentPersistence``, so a dict is enough to
    run them — and it keeps this probe free of database setup.
    """

    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []
        self.hypotheses: list[dict[str, Any]] = []
        self.diagnosis: dict[str, Any] | None = None
        self.steps: list[dict[str, Any]] = []
        self.statuses: list[str] = []

    async def start_step(self, run_id, stage, sequence, attempt, input_payload,
                         trace_id="", span_id=""):
        self.steps.append({"stage": str(stage), "sequence": sequence})
        return f"step-{sequence}"

    async def finish_step(self, step_id, *, status, output, error=None, duration_ms=0):
        return None

    async def save_evidence(self, incident_id, run_id, items):
        self.evidence = items
        return {}

    async def save_hypotheses(self, incident_id, run_id, items):
        self.hypotheses = items
        return {}

    async def save_diagnosis(self, incident_id, run_id, diagnosis):
        self.diagnosis = diagnosis
        return None

    async def save_recovery_plan(self, incident_id, run_id, plan):
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
        self.statuses.append(f"{status}: {summary}")

    async def update_run(self, run_id, *, status=None, current_stage=None,
                         error=None, interrupt_payload=None):
        return None

    async def update_run_budget(self, run_id, budget):
        return None

    async def commit(self):
        return None


def merge(state: IncidentState, update: dict[str, Any]) -> IncidentState:
    """Apply a node's output the way LangGraph would.

    ``model_copy(update=...)`` deliberately skips validation, which leaves
    ``state.plan`` a bare dict and the next node blows up. Rebuilding through
    the constructor is what the graph actually does.
    """
    return IncidentState(**{**state.model_dump(), **update})


def make_context(scenario: str) -> NodeContext:
    hooks = NullToolHooks()
    return NodeContext(
        run_id=f"run-{scenario}",
        incident_id=f"inc-{scenario}",
        executor=ToolExecutor(hooks=hooks),
        hooks=hooks,
        persistence=HarnessPersistence(),
        budget=Budget.from_settings(),
    )


async def run_scenario(scenario: str, *, verbose: bool) -> dict[str, Any]:
    providers = get_providers()
    # reset() needs a real scenario name — the route rejects an empty one, and
    # a stale fault left over from the previous scenario would poison the run.
    await providers.services.reset(scenario)
    payload = await providers.services.inject(scenario)
    service = payload.get("alert_service") or payload.get("service") or ""
    if not service:
        listed = await providers.services.list_services()
        service = str(listed[0].get("name", ""))

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

    state = merge(state, await load_context(state, config))
    state = merge(state, await investigation_planner(state, config))

    rounds = 0
    while rounds < 4:
        rounds += 1
        if not state.plan.steps:
            break
        state = merge(state, await parallel_investigation(state, config))
        update = await evidence_aggregation(state, config)
        state = merge(state, update)
        if state.decision != "replan":
            break
        state = merge(state, await investigation_planner(state, config))

    state = merge(state, await hypothesis_generation(state, config))

    hypothesis_rounds = 0
    while hypothesis_rounds < 3:
        update = await hypothesis_verification(state, config)
        state = merge(state, update)
        if state.decision != "replan":
            break
        state = merge(state, await investigation_planner(state, config))
        if not state.plan.steps:
            break
        state = merge(state, await parallel_investigation(state, config))
        state = merge(state, await hypothesis_generation(state, config))
        hypothesis_rounds += 1

    if verbose:
        from opspilot_backend.agent.investigation import (
            extract_signals as _es, score_domains as _sd, dependency_targets as _dt,
        )
        sig = _es(state.evidence, dependency_targets=_dt(state))
        for k in sorted(sig):
            print(f"         signal {k:<22} {sig[k].weight:.2f}")
        print("         domains:", _sd(sig))

    best = None
    for hyp in state.hypotheses:
        if hyp.status == HypothesisStatus.REJECTED.value:
            continue
        if best is None or hyp.confidence > best.confidence:
            best = hyp

    ranked = [
        (h.domain, h.confidence, h.status, h.reasoning) for h in state.hypotheses
    ]
    return {
        "scenario": scenario,
        "service": service,
        "expected": EXPECTED.get(scenario, "?"),
        "top_domain": best.domain if best else None,
        "top_category": best.category if best else None,
        "confidence": best.confidence if best else 0.0,
        "status": best.status if best else None,
        "hypotheses": ranked,
        "rounds": rounds,
        "evidence": len(state.evidence),
        "tool_calls": ctx.budget.tool_calls,
        "budget": ctx.budget.as_dict(),
        "items": [
            {
                "ref": i.ref,
                "service": i.service,
                "source": i.source,
                "title": i.title,
                "relevance": i.relevance,
                "pool": f"{(i.value or {}).get('db_connections')}/{(i.value or {}).get('pool_max')}",
            }
            for i in state.evidence
        ],
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("OPSPILOT_SIM_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args()

    os.environ["OPSPILOT_SIMULATOR_URL"] = args.url
    os.environ.setdefault("OPSPILOT_DATABASE_URL", "sqlite+aiosqlite:///./probe.db")

    names = [n for n in EXPECTED if args.only in n] if args.only else list(EXPECTED)
    failures: list[str] = []
    for name in names:
        try:
            result = await run_scenario(name, verbose=args.verbose)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
            print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
            continue
        ok = result["top_category"] == result["expected"]
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures.append(name)
        print(
            f"[{mark}] {result['scenario']:<32} "
            f"got={result['top_category']}({result['top_domain']}) "
            f"want={result['expected']} conf={result['confidence']:.2f} "
            f"status={result['status']} "
            f"rounds={result['rounds']} evidence={result['evidence']} "
            f"tools={result['tool_calls']}"
        )
        if args.verbose or not ok:
            for domain, conf, status, why in result["hypotheses"]:
                print(f"         - {domain:<16} {conf:.2f} {status}")
                print(f"           {why[-160:]}")
        if args.dump:
            for item in result["items"]:
                print(
                    f"         E {item['ref']} {item['service']:<20} "
                    f"{item['source']:<24} {item['title'][:60]:<60} pool={item['pool']}"
                )

    print()
    print(f"{len(names) - len(failures)}/{len(names)} scenarios converged correctly")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
