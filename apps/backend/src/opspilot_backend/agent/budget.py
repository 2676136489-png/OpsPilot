"""Investigation budget — the Agent is not allowed to investigate forever.

An unbounded agent is not an agent, it is a denial-of-service on your own
tooling: it will keep querying until something answers, and then report
whatever the last answer was as the root cause. A budget turns "keep going"
into a decision — when the allowance is spent the run **escalates** to a human
with ``INSUFFICIENT_EVIDENCE`` rather than inventing a cause.

Five independent limits, because they fail differently:

``tool_calls``
    the obvious one — how much the environment may be interrogated.
``seconds``
    wall clock. Fifteen cheap calls still burn an incident's entire MTTR.
``tokens``
    spend control for the LLM-backed reasoning paths.
``retries``
    a tool that keeps failing is a signal, not a reason to loop.
``hypothesis_rounds``
    how many times the Agent may throw away its best explanation and start
    again. Without this, "hypothesis rejected → new hypothesis" is an infinite
    loop with better branding.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from opspilot_backend.core.config import get_settings
from opspilot_backend.domain.enums import EscalationReason


@dataclass
class Budget:
    """Allowance for one agent run and what has been spent of it."""

    max_tool_calls: int = 25
    max_seconds: float = 300.0
    max_tokens: int = 50_000
    max_retries: int = 6
    max_hypothesis_rounds: int = 3
    max_parallel_tools: int = 4

    tool_calls: int = 0
    tokens: int = 0
    retries: int = 0
    hypothesis_rounds: int = 0
    failed_tool_calls: int = 0

    started_at: float = field(default_factory=time.monotonic)
    exhausted_reason: EscalationReason | None = None
    exhaustion_detail: str = ""

    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls) -> "Budget":
        settings = get_settings()
        return cls(
            max_tool_calls=settings.agent_budget_tool_calls,
            max_seconds=settings.agent_budget_seconds,
            max_tokens=settings.agent_budget_tokens,
            max_retries=settings.agent_budget_retries,
            max_hypothesis_rounds=settings.agent_budget_hypothesis_rounds,
            max_parallel_tools=settings.agent_max_parallel_tools,
        )

    @classmethod
    def restore(
        cls,
        *,
        spent_tool_calls: int = 0,
        spent_tokens: int = 0,
        spent_retries: int = 0,
        spent_seconds: float = 0.0,
    ) -> "Budget":
        """Rebuild the budget of a run that already did work.

        A resumed leg (post-approval) used to start from a fresh allowance and
        then persist it — wiping the first leg's spend off the run row, so the
        operator saw ``tool_calls: 2`` on a run that had made a dozen. The
        clock is seeded backwards by what the earlier leg spent so the wall
        clock limit keeps counting from the run's real start, not the resume.
        """
        budget = cls.from_settings()
        budget.tool_calls = int(spent_tool_calls or 0)
        budget.tokens = int(spent_tokens or 0)
        budget.retries = int(spent_retries or 0)
        budget.started_at = time.monotonic() - max(0.0, float(spent_seconds or 0.0))
        return budget

    # ------------------------------------------------------------------
    # Spending
    # ------------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def can_call_tool(self, count: int = 1) -> bool:
        if self.exhausted_reason is not None:
            return False
        return (
            self.tool_calls + count <= self.max_tool_calls
            and self.elapsed_seconds <= self.max_seconds
        )

    def record_tool_call(
        self, *, ok: bool = True, retries: int = 0, tokens: int = 0
    ) -> None:
        self.tool_calls += 1
        self.retries += max(0, retries - 1) if retries else 0
        self.tokens += tokens
        if not ok:
            self.failed_tool_calls += 1
        self._check()

    def record_tokens(self, tokens: int) -> None:
        self.tokens += max(0, tokens)
        self._check()

    def record_hypothesis_round(self) -> None:
        self.hypothesis_rounds += 1
        self._check()

    def _check(self) -> None:
        """Latch the first limit that is breached.

        Latched, not recomputed: once a run has escalated it must not
        silently become "healthy" again on the next node because the clock
        happens to read differently.
        """
        if self.exhausted_reason is not None:
            return
        if self.tool_calls >= self.max_tool_calls:
            self._exhaust(
                EscalationReason.BUDGET_EXHAUSTED,
                f"工具调用次数已达上限（{self.tool_calls}/{self.max_tool_calls}）",
            )
        elif self.elapsed_seconds >= self.max_seconds:
            self._exhaust(
                EscalationReason.BUDGET_EXHAUSTED,
                f"耗时已达上限（{self.elapsed_seconds:.1f}s/{self.max_seconds:.0f}s）",
            )
        elif self.tokens >= self.max_tokens:
            self._exhaust(
                EscalationReason.BUDGET_EXHAUSTED,
                f"Token 用量已达上限（{self.tokens}/{self.max_tokens}）",
            )
        elif self.retries >= self.max_retries:
            self._exhaust(
                EscalationReason.TOOL_FAILURES,
                f"重试次数已达上限（{self.retries}/{self.max_retries}）",
            )

    def _exhaust(self, reason: EscalationReason, detail: str) -> None:
        self.exhausted_reason = reason
        self.exhaustion_detail = detail

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    def can_start_hypothesis_round(self) -> bool:
        return (
            not self.exhausted
            and self.hypothesis_rounds < self.max_hypothesis_rounds
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "limits": {
                "max_tool_calls": self.max_tool_calls,
                "max_seconds": self.max_seconds,
                "max_tokens": self.max_tokens,
                "max_retries": self.max_retries,
                "max_hypothesis_rounds": self.max_hypothesis_rounds,
                "max_parallel_tools": self.max_parallel_tools,
            },
            "spent": {
                "tool_calls": self.tool_calls,
                "tokens": self.tokens,
                "retries": self.retries,
                "hypothesis_rounds": self.hypothesis_rounds,
                "failed_tool_calls": self.failed_tool_calls,
                "seconds": round(self.elapsed_seconds, 2),
            },
            "remaining_tool_calls": self.remaining_tool_calls,
            "remaining_seconds": round(self.remaining_seconds, 2),
            "exhausted": self.exhausted,
            "exhausted_reason": self.exhausted_reason.value
            if self.exhausted_reason
            else None,
            "exhaustion_detail": self.exhaustion_detail,
        }


__all__ = ["Budget"]
