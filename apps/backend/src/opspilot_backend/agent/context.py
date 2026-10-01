"""Runtime context handed to every node through LangGraph's ``config``.

Nodes never import repositories, SQLAlchemy or FastAPI — they only see this
facade. That is what keeps the Agent layer swappable and unit-testable.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from opspilot_backend.agent.budget import Budget
from opspilot_backend.core.logging import log_event
from opspilot_backend.core.tracing import aspan
from opspilot_backend.domain.enums import AgentStage, EventType, StepStatus
from opspilot_backend.tools.executor import ToolExecutor
from opspilot_backend.tools.hooks import ToolHooks
from opspilot_backend.tools.spec import ToolContext, ToolResult


class AgentPersistence(Protocol):
    """Everything the agent needs to leave behind in the database."""

    async def start_step(
        self, run_id: str, stage: AgentStage, sequence: int, attempt: int,
        input_payload: dict[str, Any], trace_id: str = "", span_id: str = "",
    ) -> str: ...

    async def finish_step(
        self, step_id: str, *, status: StepStatus, output: dict[str, Any],
        error: str | None = None, duration_ms: int = 0,
    ) -> None: ...

    async def save_evidence(
        self, incident_id: str, run_id: str, items: list[dict[str, Any]]
    ) -> dict[str, str]: ...

    async def save_hypotheses(
        self, incident_id: str, run_id: str, items: list[dict[str, Any]]
    ) -> dict[str, str]: ...

    async def save_diagnosis(
        self, incident_id: str, run_id: str, diagnosis: dict[str, Any]
    ) -> None: ...

    async def save_recovery_plan(
        self, incident_id: str, run_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def save_recovery_action_result(
        self, action_id: str, *, status: str, result: dict[str, Any] | None,
        error: str | None, tool_call_id: str | None, executed_by: str,
    ) -> None: ...

    async def create_approval(
        self, *, incident_id: str, run_id: str, action_id: str | None,
        action_type: str, risk_level: str, reason: str, requested_by: str,
    ) -> dict[str, Any]: ...

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None: ...

    async def find_pending_approval(self, run_id: str) -> dict[str, Any] | None: ...

    async def find_latest_approval(self, run_id: str) -> dict[str, Any] | None: ...

    async def save_verification(
        self, incident_id: str, run_id: str, plan_id: str | None,
        payload: dict[str, Any],
    ) -> str: ...

    async def save_postmortem(
        self, incident_id: str, payload: dict[str, Any]
    ) -> str: ...

    async def set_incident_status(
        self, incident_id: str, status: str, *, actor: str = "agent",
        summary: str = "", stage: AgentStage | None = None,
    ) -> None: ...

    async def update_run(
        self, run_id: str, *, status: str | None = None,
        current_stage: AgentStage | None = None, error: str | None = None,
        interrupt_payload: dict[str, Any] | None = None,
    ) -> None: ...

    async def update_run_budget(self, run_id: str, budget: "Budget") -> None: ...

    async def commit(self) -> None:
        """Make everything written so far durable.

        Mandatory before the graph saves a checkpoint: the checkpointer writes
        through a *different* connection, and an uncommitted write transaction
        on the run session would deadlock it (SQLite: one writer).
        """


class NodeContext:
    """Per-run bundle: identifiers, tool executor, event sink, persistence, budget."""

    def __init__(
        self,
        *,
        run_id: str,
        incident_id: str,
        executor: ToolExecutor,
        hooks: ToolHooks,
        persistence: AgentPersistence,
        actor: str = "agent",
        actor_type: str = "agent",
        sequence_start: int = 0,
        budget: Budget | None = None,
        db_lock: "asyncio.Lock | None" = None,
    ) -> None:
        self.run_id = run_id
        self.incident_id = incident_id
        self.executor = executor
        self.hooks = hooks
        self.persistence = persistence
        self.actor = actor
        self.actor_type = actor_type
        # Resumed runs re-enter mid-workflow; sequence numbers must continue
        # after the steps already stored, not restart at 1.
        self._sequence = sequence_start
        # Tool invocations get their own counter, seeded the same way so a
        # post-approval leg cannot re-use the first leg's indices.
        self._call_index = sequence_start
        # The step tool calls are currently being made on behalf of.
        self._current_step_id: str | None = None
        # The run's writes that happen *between* tool calls (the budget
        # update after each one) share the session with the hooks' writes.
        # Without the same lock they race — see RepositoryToolHooks.
        self._db_lock = db_lock or asyncio.Lock()
        # A resumed run inherits the budget it already spent — otherwise
        # "wait for approval" would silently refill the allowance.
        self.budget = budget or Budget.from_settings()

    # -- events --------------------------------------------------------
    async def emit(
        self,
        event_type: EventType | str,
        data: dict[str, Any],
        *,
        stage: AgentStage | None = None,
    ) -> None:
        await self.hooks.emit(
            event_type.value if isinstance(event_type, EventType) else event_type,
            data,
            run_id=self.run_id,
            incident_id=self.incident_id,
            stage=stage.value if stage else None,
        )

    async def audit(self, **kwargs: Any) -> None:
        await self.hooks.audit(**kwargs)

    # -- steps ---------------------------------------------------------
    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def next_call_index(self) -> int:
        """Position of the next tool invocation in this run.

        Fed into the idempotency key so that "the same tool with the same
        arguments, twice" is recorded as two audited calls, while a genuine
        replay of one call still resolves to the row it already wrote.
        """
        self._call_index += 1
        return self._call_index

    async def start_step(
        self,
        stage: AgentStage,
        input_payload: dict[str, Any],
        attempt: int = 1,
        *,
        trace_id: str = "",
        span_id: str = "",
    ) -> str:
        step_id = await self.persistence.start_step(
            self.run_id,
            stage,
            self.next_sequence(),
            attempt,
            input_payload,
            trace_id,
            span_id,
        )
        # Remember it so tool calls can be attributed to the node that made
        # them. Without this the column stayed NULL for every call, and the
        # only way to answer "which node asked for this metric" was to compare
        # timestamps — which is not an answer, it is a guess.
        self._current_step_id = step_id
        return step_id

    @property
    def current_step_id(self) -> str | None:
        """The step tool calls are currently being made on behalf of.

        Deliberately not cleared by :meth:`finish_step`: a call that lands
        between a step finishing and the next one starting still happened
        inside that node, and losing the attribution is worse than attributing
        it to the node that was still in scope.
        """
        return self._current_step_id

    async def finish_step(
        self,
        step_id: str,
        *,
        status: StepStatus,
        output: dict[str, Any],
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        await self.persistence.finish_step(
            step_id, status=status, output=output, error=error, duration_ms=duration_ms
        )

    # -- tools ---------------------------------------------------------
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        stage: AgentStage,
        approval_id: str | None = None,
        skip_verification: bool = False,
    ) -> ToolResult:
        """Invoke a tool, charging it against the investigation budget.

        A call that would breach the budget does not happen. Returning a
        ``blocked`` result rather than raising keeps the node's control flow
        intact: the Agent sees "no data, budget spent" and escalates.
        """
        if not self.budget.can_call_tool():
            await self.emit(
                "agent.budget.exhausted",
                {
                    "tool": tool_name,
                    "reason": self.budget.exhausted_reason.value
                    if self.budget.exhausted_reason
                    else "BUDGET_EXHAUSTED",
                    "detail": self.budget.exhaustion_detail,
                    "spent": self.budget.as_dict()["spent"],
                },
                stage=stage,
            )
            log_event(
                "agent.budget.blocked",
                run_id=self.run_id,
                tool=tool_name,
                detail=self.budget.exhaustion_detail,
            )
            return ToolResult(
                tool_name=tool_name,
                ok=False,
                arguments=arguments,
                blocked=True,
                error_type="budget_exhausted",
                error_message=(
                    "调查预算已耗尽："
                    f"{self.budget.exhaustion_detail or '已达上限'}"
                ),
            )

        ctx = ToolContext(
            run_id=self.run_id,
            incident_id=self.incident_id,
            stage=stage,
            actor=self.actor,
            actor_type=self.actor_type,
            occurrence=self.next_call_index(),
            step_id=self.current_step_id,
        )
        async with aspan(
            f"tool.{tool_name}", kind="tool", tool=tool_name, stage=stage.value
        ) as sp:
            result = await self.executor.execute(
                tool_name,
                arguments,
                ctx,
                approval_id=approval_id,
                skip_verification=skip_verification,
            )
            sp.attribute("ok", result.ok)
            sp.attribute("attempts", result.attempts)
            if not result.ok:
                sp.attribute("error_type", result.error_type or "")

        self.budget.record_tool_call(
            ok=result.ok,
            retries=max(0, result.attempts),
        )
        await self.persist_budget()
        return result

    async def persist_budget(self) -> None:
        """Keep the run row in step with what has been spent.

        Cheap enough to do per call and it means an operator watching the run
        sees the allowance draining in real time instead of only at the end.
        Takes the run's shared session lock: this fires right after a tool
        finishes, i.e. exactly while its sibling probes are writing their own
        bookkeeping, and an unlocked write here is what starved concurrent
        rounds of their evidence.
        """
        try:
            async with self._db_lock:
                await self.persistence.update_run_budget(self.run_id, self.budget)
        except Exception as exc:  # noqa: BLE001 - telemetry must not kill a run
            log_event(
                "agent.budget.persist_failed", run_id=self.run_id, error=str(exc)
            )


def get_context(config: dict[str, Any] | None) -> NodeContext:
    """Pull the context out of LangGraph's RunnableConfig."""
    if not config:
        raise RuntimeError("节点在没有运行时上下文的情况下被调用")
    ctx = config.get("configurable", {}).get("ctx")
    if ctx is None:
        raise RuntimeError("RunnableConfig 缺少 configurable.ctx")
    return ctx
