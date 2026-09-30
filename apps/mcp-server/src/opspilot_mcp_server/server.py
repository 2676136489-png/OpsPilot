"""OpsPilot MCP Server — the observability tool surface over stdio.

Exposes the simulator as MCP tools for the Investigation Agent. All tools call
the in-process :mod:`opspilot_simulator` directly — no HTTP round-trip — so a
simulated incident stays fully deterministic.

Run with:
    uv run python -m opspilot_mcp_server.server

Two things are deliberately *not* exposed:

* ``active_faults`` — it names the injected fault, which is the scenario's
  hidden root cause. A tool that hands the Agent the answer is not a tool.
* the ground-truth endpoints — those exist only in the simulator's eval mode.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Make the simulator importable when running from the mcp-server directory.
# Both packages live in the same monorepo, so we simply ensure the sibling
# src/ is on sys.path (idempotent, safe for installed environments).
# ---------------------------------------------------------------------------

_MONOREPO_ROOT = Path(__file__).resolve().parents[4]
_SIM_SRC = _MONOREPO_ROOT / "simulator" / "src"
if _SIM_SRC.exists() and str(_SIM_SRC) not in sys.path:
    sys.path.insert(0, str(_SIM_SRC))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from opspilot_simulator.engine import METRIC_ALIASES, get_simulator  # noqa: E402
from opspilot_simulator.faults import ACTION_MODELS  # noqa: E402
from opspilot_simulator.scenarios import scenario_names  # noqa: E402


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "OpsPilot",
    description=(
        "OpsPilot Tool Server — provides production observability data "
        "(service health, logs, metrics, deployments, commits) to the "
        "Incident Investigation Agent. Data comes from the in-process "
        "deterministic incident simulator."
    ),
    version="0.2.0",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Field names in a simulator snapshot. Everything except ``active_faults`` is
#: safe to hand over — see ``_public_status``.
#:
#: Renames on the way out are deliberate: the Agent-facing contract uses
#: ``latency_p95``, and both transports (in-process and MCP) must produce the
#: same keys or a tool's meaning would depend on how it was routed.
_STATUS_FIELDS = (
    ("service", "service"),
    ("kind", "kind"),
    ("health", "health"),
    ("version", "version"),
    ("replicas", "replicas"),
    ("error_rate", "error_rate"),
    ("latency_p50_ms", "latency_p50"),
    ("latency_p95_ms", "latency_p95"),
    ("request_rate", "request_rate"),
    ("cpu_percent", "cpu_percent"),
    ("memory_mb", "memory_mb"),
    ("memory_limit_mb", "memory_limit_mb"),
    ("db_connections", "db_connections"),
    ("pool_max", "pool_max"),
    ("pool_utilisation", "pool_utilisation"),
    ("circuit_breaker_open", "circuit_breaker_open"),
)


def _known_services() -> list[str]:
    """Component names, read from the simulator — never a hardcoded list.

    A stale constant here is worse than no validation: it rejects the services
    that exist while accepting ones that do not.
    """
    return sorted(sim["service"] for sim in get_simulator().list_services())


def _known_metrics() -> list[str]:
    return sorted(METRIC_ALIASES)


def _public_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project a simulator snapshot down to what the Agent may see.

    ``active_faults`` is dropped on purpose: it names the injected fault, and
    that name *is* the answer. Everything else stays, because a connection
    count is meaningless without ``pool_max`` and a heap size is meaningless
    without ``memory_limit_mb``.
    """
    return {
        out: snapshot[src] for src, out in _STATUS_FIELDS if src in snapshot
    }


def _resolve_service(service_name: str) -> str | None:
    """Service name, or ``None`` when it is not a real component."""
    return service_name if service_name in _known_services() else None


def _clamp(value: int, lo: int, hi: int, default: int) -> int:
    if value < lo or value > hi:
        return default
    return value


def _timeout_protected(fn_name: str, deadline: float = 2.0):
    """Naive timeout wrapper via daemon thread.

    Runs the callable in a background thread; if it doesn't finish within
    ``deadline`` seconds we return an error dict instead of raising.
    """
    import threading

    def decorator(func):
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            container: dict[str, Any] = {"result": None, "error": None}

            def _runner() -> None:
                try:
                    container["result"] = func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    container["error"] = exc

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(timeout=deadline)

            if t.is_alive():
                return {
                    "error": f"timeout exceeded for {fn_name} ({deadline}s)",
                    "service": fn_name,
                    "timestamp": time.time(),
                }
            if container["error"] is not None:
                err = container["error"]
                return {
                    "error": str(err),
                    "error_type": type(err).__name__,
                    "service": fn_name,
                    "timestamp": time.time(),
                }
            return container["result"]

        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = func.__qualname__
        wrapper.__doc__ = func.__doc__
        wrapper.__annotations__ = func.__annotations__
        wrapper.__wrapped__ = func
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_service_status(service_name: str) -> dict[str, Any]:
    """Return real-time health and key metrics for a single service.

    Args:
        service_name: A component name, e.g. ``payment-service``,
            ``checkout-service``, ``gateway-service``, ``postgres``,
            ``redis``.

    Returns:
        Structured dict with keys: service, kind, health, version, replicas,
        error_rate, latency_p50_ms, latency_p95_ms, request_rate, cpu_percent,
        memory_mb, memory_limit_mb, db_connections, pool_max,
        pool_utilisation, circuit_breaker_open.
    """
    @_timeout_protected("get_service_status")
    def _inner() -> dict[str, Any]:
        if _resolve_service(service_name) is None:
            return {
                "error": f"Unknown service '{service_name}'. Valid: {_known_services()}",
                "known_services": _known_services(),
            }
        try:
            return _public_status(get_simulator().get_service(service_name))
        except KeyError as exc:
            return {"error": str(exc), "known_services": _known_services()}

    return _inner()


@mcp.tool()
def query_logs(
    service_name: str,
    start_time: str = "",
    end_time: str = "",
    level: str | None = None,
    minutes_back: int = 30,
) -> dict[str, Any]:
    """Query structured logs for a service.

    Args:
        service_name: Service to query (payment-service, user-service,
            gateway-service).
        start_time: ISO-8601 start time string (UTC). Optional — when
            provided, ``minutes_back`` is ignored.
        end_time: ISO-8601 end time string (UTC). Optional.
        level: Filter to DEBUG | INFO | WARN | ERROR. Optional.
        minutes_back: Look back this many minutes when start_time is not
            provided (default 30, max 1440).

    Returns:
        List of log-entry dicts (timestamp, level, service, message).
        Empty list when no logs match; error dict otherwise — but this
        tool always returns a list, any error is surfaced as a single
        error-wrapped dict at the head of the list for MCP compatibility.
    """
    clamped_minutes = _clamp(minutes_back, 1, 1440, 30)

    @_timeout_protected("query_logs")
    def _inner() -> Any:
        if _resolve_service(service_name) is None:
            return {"error": f"Unknown service '{service_name}'. Valid: {_known_services()}"}
        try:
            entries = get_simulator().get_logs(
                service_name, minutes_back=clamped_minutes, level=level
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "error_type": type(exc).__name__}
        # Wrapped, never returned as a bare list: MCP turns a list return into
        # one content block per element, and the client must be able to tell a
        # one-element list from a scalar.
        return {"service": service_name, "total": len(entries), "entries": entries}

    return _inner()


@mcp.tool()
def query_metrics(
    service_name: str,
    metric: str,
    start_time: str = "",
    end_time: str = "",
    minutes_back: int = 60,
) -> dict[str, Any]:
    """Query a metric time-series for a service.

    Args:
        service_name: Service name.
        metric: One of the supported metric names (see
            ``get_service_status`` for the values each one maps to).
        start_time: ISO-8601 start time (UTC). Optional.
        end_time: ISO-8601 end time (UTC). Optional.
        minutes_back: Window size in minutes (default 60, max 1440).

    Returns:
        Dict with ``service`` and ``series``; each series entry carries
        ``metric``, ``points`` (timestamp, value), ``latest`` and ``previous``.
    """
    clamped_minutes = _clamp(minutes_back, 1, 1440, 60)

    @_timeout_protected("query_metrics")
    def _inner() -> Any:
        if _resolve_service(service_name) is None:
            return {"error": f"Unknown service '{service_name}'. Valid: {_known_services()}"}
        if metric not in METRIC_ALIASES:
            return {"error": f"Unknown metric '{metric}'. Valid: {_known_metrics()}"}
        try:
            points = get_simulator().get_metrics(
                service_name, metric, minutes_back=clamped_minutes
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "error_type": type(exc).__name__}
        return {
            "service": service_name,
            "series": [
                {
                    "metric": metric,
                    "service": service_name,
                    "points": points,
                    "latest": points[-1]["value"] if points else None,
                    "previous": points[-6]["value"] if len(points) >= 6 else None,
                }
            ],
        }

    return _inner()


@mcp.tool()
def get_deployments(service_name: str, limit: int = 10) -> dict[str, Any]:
    """Return recent deployment history for a service.

    Args:
        service_name: Service to query.
        limit: Max number of records (default 10, max 100).

    Returns:
        Dict with ``service`` and ``deployments`` (version, deployed_at,
        status, author), latest first.
    """
    clamped_limit = _clamp(limit, 1, 100, 10)

    @_timeout_protected("get_deployments")
    def _inner() -> Any:
        if _resolve_service(service_name) is None:
            return {"error": f"Unknown service '{service_name}'. Valid: {_known_services()}"}
        try:
            rows = get_simulator().get_deployments(service_name, limit=clamped_limit)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "error_type": type(exc).__name__}
        return {"service": service_name, "deployments": rows}

    return _inner()


@mcp.tool()
def get_recent_commits(repository: str = "opspilot", limit: int = 10) -> dict[str, Any]:
    """Return recent commits for a repository.

    Deterministic, scenario-aware stand-in for GitHub's API so the Agent has
    something to diff a suspicious deployment against. Different incidents
    surface different commits.

    Args:
        repository: Repository name (currently only ``opspilot``).
        limit: Max number of commits to return (default 10, max 50).

    Returns:
        Dict with ``commits``: sha, message, author, timestamp, repository.
        Oldest → newest preserved.
    """
    clamped_limit = _clamp(limit, 1, 50, 10)

    @_timeout_protected("get_recent_commits")
    def _inner() -> Any:
        return {"commits": get_simulator().get_commits(repository, clamped_limit)}

    return _inner()


# ---------------------------------------------------------------------------
# Phase 6 placeholder
# ---------------------------------------------------------------------------


@mcp.tool()
def search_runbooks(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the runbook knowledge base for an incident.

    Scores the real markdown runbooks in ``runbooks/`` against the query terms,
    so the Agent reads the same document an on-call engineer would.

    Args:
        query: Free-text terms to search (e.g. "db pool", "redis down").
        limit: Max runbooks to return (default 5, max 20).

    Returns:
        Dict with ``hits``: id, title, category, score, excerpt.
        An empty list when nothing matches.
    """
    clamped_limit = _clamp(limit, 1, 20, 5)
    return {"hits": _search_runbook_files(query, clamped_limit)}


_RUNBOOK_ROOT = Path(__file__).resolve().parents[4] / "runbooks"


def _runbook_id(path: Path, root: Path) -> str:
    """Stable, platform-independent id for a runbook.

    ``as_posix()`` is not cosmetic: on Windows ``relative_to`` yields
    ``database\\connection-pool``, which no caller can ever look up with the
    ``database/connection-pool`` it was shown.
    """
    return path.relative_to(root).with_suffix("").as_posix()


def _search_runbook_files(query: str, limit: int) -> list[dict[str, Any]]:
    """Score the real runbook markdown files in the repo against the query."""
    import re

    if not _RUNBOOK_ROOT.exists():
        return []
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    scored: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(_RUNBOOK_ROOT.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        score = sum(lowered.count(t) for t in terms)
        if score:
            scored.append(
                (
                    score,
                    {
                        "id": _runbook_id(path, _RUNBOOK_ROOT),
                        "title": path.stem,
                        "category": path.parent.name,
                        "score": score,
                        "excerpt": text[:400],
                    },
                )
            )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


# ---------------------------------------------------------------------------
# Incident control — very useful for demos
# ---------------------------------------------------------------------------


@mcp.tool()
def inject_scenario(scenario_name: str) -> dict[str, Any]:
    """Inject a predefined incident scenario into the simulator.

    The counterpart of ``reset_scenario`` — use it to make a service exhibit
    realistic fault metrics so downstream tools (``get_service_status``,
    ``query_logs``, ...) return incident data.

    Args:
        scenario_name: A scenario from ``list_scenarios``.

    Returns:
        Structured dict describing the injected scenario (affected service,
        symptom summary, expected recovery shape). The hidden root cause is
        not included.
    """
    @_timeout_protected("inject_scenario")
    def _inner() -> dict[str, Any]:
        try:
            return get_simulator().inject(scenario_name)
        except KeyError as exc:
            return {"error": str(exc), "valid_scenarios": scenario_names()}

    return _inner()


@mcp.tool()
def reset_scenario(scenario_name: str = "") -> dict[str, Any]:
    """Reset all services to their normal baseline.

    Args:
        scenario_name: Ignored — reset always clears whichever scenario is
            currently active. Kept for API symmetry with the REST endpoint.

    Returns:
        Dict with keys reset_from (previously active scenario name or None)
        and status ("normal").
    """
    _ = scenario_name
    return get_simulator().reset()


# ---------------------------------------------------------------------------
# Topology + recovery — the mutating half of the tool surface
#
# Earlier revisions only exposed read-only tools, which meant the agent could
# observe a broken environment but never actually fix it. These tools mutate
# simulator state, so "Recovery → Verification" is a real closed loop.
# ---------------------------------------------------------------------------


@mcp.tool()
def get_dependencies() -> dict[str, Any]:
    """Return the service dependency graph (blast-radius analysis).

    Returns:
        Dict with ``nodes`` and ``edges``; each edge carries ``service``,
        ``depends_on``, ``type`` and ``criticality``.
    """
    return get_simulator().dependencies()


@mcp.tool()
def list_recovery_actions() -> dict[str, Any]:
    """List the recovery actions the simulated environment supports.

    Returns:
        Dict with ``actions``; each entry has ``name``, ``label``, ``risk``,
        ``applies_to`` and ``removes`` — what the action can actually cure.
        Read this before prescribing a fix: the environment only accepts
        these, and the ``removes`` set is what tells you whether a candidate
        remediation matches the fault you think you have.
    """
    return {
        "actions": [
            {
                "name": model.name,
                "label": model.label,
                "risk": model.risk,
                "applies_to": list(model.applies_to),
                "removes": sorted(model.removes),
            }
            for model in ACTION_MODELS.values()
        ]
    }


def _apply_action(action: str, service_name: str, **params: Any) -> dict[str, Any]:
    """Shared recovery path: validate, act, report honestly.

    ``effective`` in the result is not decoration — an action that the fault is
    resistant to reports ``effective: False``, which is how the Agent learns
    that a restart did not fix a code-level leak.
    """
    if _resolve_service(service_name) is None:
        return {
            "error": f"Unknown service '{service_name}'. Valid: {_known_services()}",
            "known_services": _known_services(),
        }
    if action not in ACTION_MODELS:
        return {"error": f"Unknown action '{action}'.", "known_actions": sorted(ACTION_MODELS)}
    try:
        return get_simulator().act(action, service_name, **params)
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}


@mcp.tool()
def restart_service(service_name: str) -> dict[str, Any]:
    """Restart a service, clearing in-process state (pools, caches).

    This mutates the simulated environment. Note that a restart cannot un-ship
    code: a fault living in the running process resets its clock, so an
    ``effective: False`` result here is the environment telling you the restart
    did not fix anything. Follow it with ``verify_service_health``.

    Args:
        service_name: Component to restart.

    Returns:
        Dict with ``ok``, ``effective``, ``before`` / ``after`` metric
        snapshots and, when nothing changed, a ``reason``.
    """
    return _apply_action("restart_service", service_name)


@mcp.tool()
def rollback_deployment(service_name: str) -> dict[str, Any]:
    """Roll a service back to its previous deployment version.

    Args:
        service_name: Service whose latest deployment should be undone.

    Returns:
        Dict with ``ok``, ``effective``, the version change and before/after
        metrics. ``effective: False`` means there was no history to undo.
    """
    return _apply_action("rollback_deployment", service_name)


@mcp.tool()
def scale_service(service_name: str, replicas: int = 4) -> dict[str, Any]:
    """Scale a service horizontally to shed load.

    Direction matters: capacity is bought by scaling *out*. Scaling back down
    buys nothing and will not clear a saturation fault.

    Args:
        service_name: Service to scale.
        replicas: Target replica count (>= 1).

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("scale_service", service_name, replicas=max(1, replicas))


@mcp.tool()
def increase_pool_size(service_name: str) -> dict[str, Any]:
    """Raise the database connection pool limit for a service.

    The correct fix for pool exhaustion — unlike a restart, which drains the
    pool for a few seconds and then lets it fill again.

    Args:
        service_name: Service whose pool should be raised.

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("increase_pool_size", service_name)


@mcp.tool()
def clear_deadlock(service_name: str) -> dict[str, Any]:
    """Kill the blocking database transactions behind a slow database.

    Args:
        service_name: Service whose datastore should be unblocked.

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("clear_deadlock", service_name)


@mcp.tool()
def restart_postgres(service_name: str) -> dict[str, Any]:
    """Restart the database and clear stuck sessions.

    Args:
        service_name: The datastore component (``postgres``).

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("restart_postgres", service_name)


@mcp.tool()
def restart_redis(service_name: str) -> dict[str, Any]:
    """Fail over / restart the Redis cluster.

    The fix for a Redis outage. ``flush_cache`` is the milder alternative when
    the cache is serving stale data rather than being down.

    Args:
        service_name: The cache component (``redis``).

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("restart_redis", service_name)


@mcp.tool()
def flush_cache(service_name: str) -> dict[str, Any]:
    """Flush the cache without restarting it.

    Args:
        service_name: The cache component (``redis``).

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("flush_cache", service_name)


@mcp.tool()
def enable_circuit_breaker(service_name: str) -> dict[str, Any]:
    """Open the circuit breaker for a service's failing upstream.

    Sheds the failing dependency so the caller stops piling up timeouts. This
    treats the caller, not the dependency — the dependency stays broken.

    Args:
        service_name: The caller whose breaker should open.

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("enable_circuit_breaker", service_name)


@mcp.tool()
def switch_payment_provider(service_name: str) -> dict[str, Any]:
    """Fail over to the backup payment provider.

    Args:
        service_name: The component that owns the external integration.

    Returns:
        Dict with ``ok``, ``effective`` and before/after metrics.
    """
    return _apply_action("switch_payment_provider", service_name)


@mcp.tool()
def notify_oncall(service_name: str, summary: str = "") -> dict[str, Any]:
    """Page the on-call engineer.

    The only action that changes nothing about the environment — it is how the
    Agent escalates when it has run out of things it can safely do itself.

    Args:
        service_name: Component the page is about.
        summary: One-line context for whoever picks it up.

    Returns:
        Dict with ``ok`` and ``effective: False`` — paging is not a fix.
    """
    return _apply_action("notify_oncall", service_name, summary=summary)


@mcp.tool()
def get_runbook(runbook_id: str) -> dict[str, Any]:
    """Fetch a full runbook by id.

    Use ``search_runbooks`` first: its hits carry the ids this expects.

    Args:
        runbook_id: Relative runbook id (e.g. ``database/connection-pool``).

    Returns:
        Dict with ``id``, ``title``, ``category`` and the full markdown
        ``content``; an ``error`` key when the id does not exist.
    """
    if not _RUNBOOK_ROOT.exists():
        return {"error": "runbook directory is not available"}
    # Accept either separator: the id travels over the wire, where a caller is
    # as likely to type a backslash as the forward slash it was shown.
    wanted = runbook_id.strip().replace("\\", "/").removesuffix(".md")
    for path in sorted(_RUNBOOK_ROOT.rglob("*.md")):
        if _runbook_id(path, _RUNBOOK_ROOT) == wanted or path.stem == wanted:
            return {
                "id": _runbook_id(path, _RUNBOOK_ROOT),
                "title": path.stem,
                "category": path.parent.name,
                "content": path.read_text(encoding="utf-8"),
            }
    return {"error": f"runbook not found: {runbook_id}"}


@mcp.tool()
def verify_service_health(
    service_name: str, max_error_rate: float = 0.01, max_latency_p95: float = 500.0
) -> dict[str, Any]:
    """Probe a service against explicit health thresholds.

    A real measurement, not a restatement of the recovery request: it re-reads
    the component's current metrics and reports each threshold separately.

    Args:
        service_name: Service to verify.
        max_error_rate: Maximum acceptable error ratio.
        max_latency_p95: Maximum acceptable p95 latency in ms.

    Returns:
        Dict with ``passed`` and a list of individual ``checks``.
    """
    if _resolve_service(service_name) is None:
        return {"error": f"Unknown service '{service_name}'. Valid: {_known_services()}"}
    status = get_simulator().get_service(service_name)
    error_rate = float(status.get("error_rate") or 0.0)
    latency = float(status.get("latency_p95_ms") or 0.0)
    health = str(status.get("health", "unknown"))
    checks = [
        {"name": "error_rate", "passed": error_rate <= max_error_rate,
         "actual": error_rate, "threshold": max_error_rate},
        {"name": "latency_p95", "passed": latency <= max_latency_p95,
         "actual": latency, "threshold": max_latency_p95},
        {"name": "health_status", "passed": health == "healthy",
         "actual": health, "threshold": "healthy"},
    ]
    return {
        "service": service_name,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    mcp.run()
