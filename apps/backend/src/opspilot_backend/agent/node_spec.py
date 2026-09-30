"""Node metadata + the wrapper that enforces it.

Every node declares *reads / writes / retry / timeout*. The wrapper is what
actually applies them: it persists an AgentStep before and after, enforces the
timeout, runs the retry policy, and records the failure instead of letting it
escape and kill the whole run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig

from opspilot_backend.agent.context import NodeContext, get_context
from opspilot_backend.agent.state import ErrorRecord, IncidentState
from opspilot_backend.core.logging import log_event
from opspilot_backend.core.tracing import aspan
from opspilot_backend.domain.enums import AgentStage, StepStatus

try:
    from langgraph.errors import GraphBubbleUp, GraphInterrupt

    # Control-flow signals from LangGraph. They must never be swallowed by the
    # retry wrapper, or interrupt()/resume would silently stop working.
    _CONTROL_FLOW: tuple[type[BaseException], ...] = (GraphBubbleUp, GraphInterrupt)
except ImportError:  # pragma: no cover - very old langgraph
    _CONTROL_FLOW = ()


@dataclass
class NodeSpec:
    stage: AgentStage
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    description: str
    max_attempts: int = 2
    timeout_s: float = 60.0


NODE_SPECS: dict[str, NodeSpec] = {}


def workflow_node(spec: NodeSpec) -> Callable[..., Any]:
    """Register a node and wrap it with persistence / timeout / retry."""

    def decorator(
        fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]]
    ) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
        NODE_SPECS[fn.__name__] = spec

        # The second parameter MUST be annotated as RunnableConfig — LangGraph
        # inspects the signature and only injects a config it recognises.
        async def wrapper(
            state: IncidentState, config: RunnableConfig
        ) -> dict[str, Any]:
            ctx: NodeContext = get_context(config)
            stage = spec.stage
            started = time.perf_counter()

            async with aspan(
                f"node.{fn.__name__}", kind="node", stage=stage.value
            ) as sp:
                sp.attribute("reads", list(spec.reads))
                sp.attribute("writes", list(spec.writes))
                step_id = await ctx.start_step(
                    stage,
                    {"reads": list(spec.reads), "evidence": len(state.evidence)},
                    trace_id=sp.trace_id,
                    span_id=sp.span_id,
                )
                await ctx.emit(
                    "agent.step.started",
                    {"stage": stage.value, "attempt": 1},
                    stage=stage,
                )

                last_error: str | None = None
                for attempt in range(1, spec.max_attempts + 1):
                    sp.attribute("attempt", attempt)
                    try:
                        update = await asyncio.wait_for(
                            fn(state, config), timeout=spec.timeout_s
                        )
                    except _CONTROL_FLOW:  # type: ignore[misc]
                        # interrupt() / Command — let LangGraph handle it. The
                        # step bookkeeping must be committed first: LangGraph
                        # saves a checkpoint through a SECOND connection the
                        # moment this exception escapes, and SQLite allows
                        # only one writer — an uncommitted update here
                        # deadlocks it.
                        await ctx.finish_step(
                            step_id,
                            status=StepStatus.RUNNING,
                            output={"interrupted": True},
                        )
                        await ctx.persistence.commit()
                        raise
                    except asyncio.TimeoutError:
                        last_error = f"timeout after {spec.timeout_s}s"
                        sp.fail(last_error)
                        await ctx.finish_step(
                            step_id,
                            status=StepStatus.TIMEOUT,
                            output={},
                            error=last_error,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                        await ctx.persistence.commit()
                        return _failure_update(
                            state, stage, "timeout", last_error, step_id
                        )
                    except Exception as exc:  # noqa: BLE001 - bounded handling
                        last_error = f"{type(exc).__name__}: {exc}"
                        log_event(
                            "agent.node.error",
                            stage=stage.value,
                            attempt=attempt,
                            error=last_error,
                            run_id=ctx.run_id,
                        )
                        if attempt < spec.max_attempts:
                            await asyncio.sleep(0.25 * attempt)
                            continue
                        sp.fail(last_error)
                        await ctx.finish_step(
                            step_id,
                            status=StepStatus.FAILED,
                            output={},
                            error=last_error,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                        await ctx.persistence.commit()
                        return _failure_update(
                            state, stage, type(exc).__name__, last_error, step_id
                        )

                    duration_ms = int((time.perf_counter() - started) * 1000)
                    await ctx.finish_step(
                        step_id,
                        status=StepStatus.COMPLETED,
                        output=_safe_output(update),
                        duration_ms=duration_ms,
                    )
                    await ctx.persistence.commit()
                    payload = dict(update)
                    payload.setdefault("meta", state.meta.model_dump())
                    payload["meta"]["current_stage"] = stage.value
                    payload["execution"] = {
                        **state.execution.model_dump(),
                        "completed_stages": state.execution.completed_stages
                        + [stage.value],
                    }
                    await ctx.emit(
                        "agent.step.completed",
                        {"stage": stage.value, "duration_ms": duration_ms},
                        stage=stage,
                    )
                    return payload

                # Retry budget exhausted (defensive; the loop returns above).
                sp.fail(last_error or "retries exhausted")
                await ctx.finish_step(
                    step_id, status=StepStatus.FAILED, output={}, error=last_error
                )
                await ctx.persistence.commit()
                return _failure_update(
                    state, stage, "retries_exhausted", last_error or "", step_id
                )

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


def _failure_update(
    state: IncidentState,
    stage: AgentStage,
    error_type: str,
    message: str,
    step_id: str,
) -> dict[str, Any]:
    record = ErrorRecord(stage=stage.value, error_type=error_type, message=message)
    meta = state.meta.model_dump()
    meta["current_stage"] = stage.value
    meta["terminal_reason"] = f"{stage.value}:{error_type}"
    return {
        "meta": meta,
        "execution": {
            **state.execution.model_dump(),
            "errors": state.execution.errors + [record],
        },
        "decision": "stop",
    }


def _safe_output(update: dict[str, Any]) -> dict[str, Any]:
    """Keep step outputs small — evidence payloads can be megabytes."""
    out: dict[str, Any] = {}
    for key, value in update.items():
        if key == "evidence" and isinstance(value, list):
            out[key] = {
                "count": len(value),
                "refs": [getattr(v, "ref", None) for v in value][-20:],
            }
        elif key == "hypotheses" and isinstance(value, list):
            out[key] = [
                {"ref": v.ref, "confidence": v.confidence, "status": v.status}
                for v in value
            ]
        else:
            out[key] = value
    return out


def node_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "node": name,
            "stage": spec.stage.value,
            "reads": list(spec.reads),
            "writes": list(spec.writes),
            "max_attempts": spec.max_attempts,
            "timeout_s": spec.timeout_s,
            "description": spec.description,
        }
        for name, spec in NODE_SPECS.items()
    ]


@dataclass
class _Unused:
    x: int = field(default=0)
