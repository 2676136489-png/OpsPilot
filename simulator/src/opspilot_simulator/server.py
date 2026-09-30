"""OpsPilot Simulator — HTTP surface over the simulated environment.

Everything here is a projection of :class:`opspilot_simulator.engine.Simulator`.
Two endpoints deserve special mention:

``GET /simulator/scenarios``
    returns only operator-visible fields. The hidden root cause is not in it.

``GET /simulator/ground-truth/{name}``
    returns the answer key — and answers only while the process has
    ``OPSPILOT_SIM_EVAL_MODE=1``. Otherwise it is a 404, so an Agent cannot
    read the answer even if it guesses the URL. The flag is re-read per
    request, so a caller that exports it after this module is imported still
    gets the answer key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from opspilot_simulator.engine import METRIC_ALIASES, get_simulator
from opspilot_simulator.faults import ACTION_MODELS, FAULT_MODELS
from opspilot_simulator.scenarios import (
    Criterion,
    get_scenario,
    list_scenarios,
    scenario_names,
)

EVAL_MODE = os.environ.get("OPSPILOT_SIM_EVAL_MODE", "").lower() in {"1", "true", "yes"}


def eval_mode_enabled() -> bool:
    """Whether the harness-only routes may be served, read per request.

    The flag describes the *process*, not whichever module imported this one
    first. Reading it once at import time makes the answer key's availability
    depend on import order — a combined test run that imports the app before
    exporting the variable would silently lose the route and score zeroes.
    """
    return os.environ.get("OPSPILOT_SIM_EVAL_MODE", "").strip().lower() in {"1", "true", "yes"}


def _require_eval_mode() -> None:
    """Refuse harness-only routes with a plain 404 when eval mode is off.

    The status code deliberately matches an unmounted route so a probing Agent
    cannot infer that the answer key exists at all.
    """
    if not eval_mode_enabled():
        raise HTTPException(status_code=404, detail="Not Found")

logger = logging.getLogger("opspilot.simulator")

app = FastAPI(
    title="OpsPilot Incident Simulator",
    version="1.0.0",
    description=(
        "A stateful model of a small production environment. Injecting a fault "
        "changes metrics, logs and deployment history together; a recovery "
        "action only works if it addresses the fault that is actually present."
    ),
)


def _parse_traceparent(value: str) -> str:
    """Trace id from a W3C ``traceparent``, or ``""`` when malformed."""
    parts = (value or "").split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        try:
            int(parts[1], 16)
        except ValueError:
            return ""
        return parts[1]
    return ""


@app.middleware("http")
async def chaos_middleware(request: Request, call_next):
    """Fault injection for the transport itself, plus trace propagation.

    The system has to survive a simulator that is slow or gone, so the
    failure modes are reachable on demand instead of only in production.

    The simulated environment takes part in the caller's trace: it adopts the
    incoming ``traceparent`` and echoes the ids back, so a line in the
    simulator's log and the tool call that caused it share one trace id. That
    is the difference between "the tool returned an error" and "the tool
    returned an error because *this* request timed out at *this* step".
    """
    trace_id = _parse_traceparent(request.headers.get("traceparent", "")) or uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    request_id = request.headers.get("x-request-id", "") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    status_code = 500

    sim = get_simulator()
    if sim.chaos == "unavailable":
        response: Any = JSONResponse(
            status_code=503,
            content={"detail": "simulator unavailable (chaos=unavailable)"},
        )
    else:
        if sim.chaos == "slow":
            await asyncio.sleep(float(os.environ.get("OPSPILOT_SIM_CHAOS_SLEEP", "12")))
        response = await call_next(request)
    status_code = response.status_code

    response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
    response.headers["X-Request-Id"] = request_id
    # Ids in the message, not in a `extra_fields` payload: this process has no
    # JSON formatter configured, and a trace field that only materialises under
    # someone else's log config is a trace field that is missing when read.
    logger.info(
        "simulator.request trace_id=%s span_id=%s request_id=%s %s %s -> %s (%.1fms, scenario=%s)",
        trace_id,
        span_id,
        request_id,
        request.method,
        request.url.path,
        status_code,
        (time.perf_counter() - started) * 1000,
        sim.active_scenario,
    )
    return response


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, Any]:
    return {"status": "ok", "eval_mode": eval_mode_enabled()}


@app.get("/simulator/status")
def status() -> dict[str, Any]:
    return get_simulator().status()


# ---------------------------------------------------------------------------
# Services / topology
# ---------------------------------------------------------------------------


@app.get("/simulator/services")
def list_services() -> dict[str, Any]:
    sim = get_simulator()
    return {"scenario": sim.active_scenario, "services": sim.list_services()}


@app.get("/simulator/services/{name}")
def get_service(name: str) -> dict[str, Any]:
    try:
        return get_simulator().get_service(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/simulator/dependencies")
def dependencies() -> dict[str, Any]:
    return get_simulator().dependencies()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@app.get("/simulator/metrics")
def metrics(
    service: str = Query(...),
    metric: str = Query(...),
    minutes_back: int = Query(default=60, ge=1, le=1440),
) -> dict[str, Any]:
    try:
        points = get_simulator().get_metrics(service, metric, minutes_back)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"service": service, "metric": metric, "points": points}


@app.get("/simulator/logs")
def logs(
    service: str = Query(...),
    minutes_back: int = Query(default=30, ge=1, le=1440),
    level: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    try:
        entries = get_simulator().get_logs(service, minutes_back, level, limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"service": service, "count": len(entries), "entries": entries}


@app.get("/simulator/deployments")
def deployments(
    service: str = Query(...), limit: int = Query(default=10, ge=1, le=100)
) -> dict[str, Any]:
    try:
        rows = get_simulator().get_deployments(service, limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"service": service, "deployments": rows}


@app.get("/simulator/commits")
def commits(
    repository: str = Query(default="opspilot"),
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    return {
        "repository": repository,
        "commits": get_simulator().get_commits(repository, limit),
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@app.get("/simulator/scenarios")
def scenarios() -> dict[str, Any]:
    return {"scenarios": list_scenarios()}


@app.post("/simulator/incidents/{name}/inject")
def inject(name: str) -> dict[str, Any]:
    try:
        return get_simulator().inject(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/simulator/incidents/{name}/reset")
def reset(name: str) -> dict[str, Any]:
    if get_scenario(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown scenario: {name}")
    return get_simulator().reset()


@app.post("/simulator/advance")
def advance(minutes: float = Query(default=1.0, ge=0.1, le=1440)) -> dict[str, Any]:
    return get_simulator().advance(minutes)


# ---------------------------------------------------------------------------
# Recovery actions
# ---------------------------------------------------------------------------


@app.get("/simulator/actions")
def list_actions() -> dict[str, Any]:
    return {
        "actions": [
            {
                "name": m.name,
                "label": m.label,
                "risk": m.risk,
                "removes": sorted(m.removes),
                "mitigates": m.mitigates,
                "resets": list(m.resets),
            }
            for m in ACTION_MODELS.values()
        ],
        "faults": [
            {
                "kind": m.kind,
                "label": m.label,
                "resistant_to": sorted(m.resistant_to),
            }
            for m in FAULT_MODELS.values()
        ],
    }


@app.post("/simulator/services/{name}/restart")
def restart(name: str) -> dict[str, Any]:
    try:
        return get_simulator().act("restart_service", name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/simulator/services/{name}/scale")
def scale(name: str, replicas: int = Query(default=4, ge=1, le=50)) -> dict[str, Any]:
    try:
        return get_simulator().act("scale_service", name, replicas=replicas)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/simulator/deployments/rollback")
def rollback(service: str = Query(...)) -> dict[str, Any]:
    try:
        return get_simulator().act("rollback_deployment", service)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/simulator/actions/{action}")
def perform_action(
    action: str,
    service: str = Query(...),
    replicas: int | None = Query(default=None),
) -> dict[str, Any]:
    """Generic action endpoint — lets new actions ship without new routes."""
    params: dict[str, Any] = {}
    if replicas is not None:
        params["replicas"] = replicas
    try:
        return get_simulator().act(action, service, **params)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/simulator/verify")
def verify(criteria: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate verification criteria against the live environment."""
    parsed = [
        Criterion(
            service=str(c.get("service", "")),
            metric=str(c.get("metric", "")),
            op=str(c.get("op", "<=")),
            threshold=c.get("threshold"),
        )
        for c in criteria
    ]
    if not parsed:
        raise HTTPException(status_code=400, detail="no criteria supplied")
    return get_simulator().evaluate(parsed)


# ---------------------------------------------------------------------------
# Chaos controls (testing only)
# ---------------------------------------------------------------------------


@app.post("/simulator/chaos/{mode}")
def set_chaos(mode: str) -> dict[str, Any]:
    if mode not in {"off", "slow", "unavailable"}:
        raise HTTPException(
            status_code=400, detail="mode must be one of off | slow | unavailable"
        )
    sim = get_simulator()
    sim.chaos = mode
    return {"chaos": mode}


# ---------------------------------------------------------------------------
# Ground truth — evaluation harness only
# ---------------------------------------------------------------------------

@app.get("/simulator/ground-truth")
def all_ground_truth() -> dict[str, Any]:
    _require_eval_mode()
    return {
        "scenarios": [
            get_scenario(name).ground_truth()  # type: ignore[union-attr]
            for name in scenario_names()
            if get_scenario(name) is not None
        ]
    }


@app.get("/simulator/ground-truth/{name}")
def ground_truth(name: str) -> dict[str, Any]:
    _require_eval_mode()
    scenario = get_scenario(name)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"Unknown scenario: {name}")
    return scenario.ground_truth()


__all__ = ["app", "EVAL_MODE", "METRIC_ALIASES", "eval_mode_enabled"]
