"""Persistence hooks the Tool Layer needs from the calling layer.

The Tool Layer must not import SQLAlchemy, so it talks to this interface and
the Application layer supplies a repository-backed implementation.
"""

from __future__ import annotations

from typing import Any, Protocol


class ToolHooks(Protocol):
    async def on_tool_start(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        run_id: str,
        step_id: str | None,
        risk_level: str,
        permission_level: str,
        transport: str,
    ) -> str:
        """Persist a pending tool call and return its id."""
        ...

    async def on_tool_finish(
        self,
        tool_call_id: str,
        *,
        status: str,
        result: Any,
        error_code: str | None,
        error_message: str | None,
        duration_ms: int,
        attempts: int,
    ) -> None: ...

    async def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        run_id: str,
        incident_id: str,
        stage: str | None = None,
    ) -> None: ...

    async def audit(
        self,
        *,
        action: str,
        actor: str,
        actor_type: str,
        resource_type: str,
        resource_id: str | None,
        incident_id: str | None,
        run_id: str | None,
        tool_name: str | None,
        risk_level: str | None,
        approval_id: str | None,
        parameters: dict[str, Any] | None,
        outcome: str,
        detail: str | None = None,
    ) -> None: ...

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None: ...

    async def create_approval(
        self,
        *,
        incident_id: str,
        run_id: str,
        action_id: str | None,
        action_type: str,
        risk_level: str,
        reason: str,
        requested_by: str,
    ) -> dict[str, Any]: ...


class NullToolHooks:
    """Default implementation — records nothing, used in pure unit tests."""

    async def on_tool_start(self, **kwargs: Any) -> str:
        return ""

    async def on_tool_finish(self, tool_call_id: str, **kwargs: Any) -> None:
        return None

    async def emit(self, event_type: str, data: dict[str, Any], **kwargs: Any) -> None:
        return None

    async def audit(self, **kwargs: Any) -> None:
        return None

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return None

    async def create_approval(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": "", "status": "pending"}


class RecordingToolHooks(NullToolHooks):
    """In-memory hooks that remember what happened — used by the test-suite."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self._counter = 0

    async def on_tool_start(self, **kwargs: Any) -> str:
        self._counter += 1
        call_id = f"tc-{self._counter}"
        self.calls.append({"id": call_id, **kwargs})
        return call_id

    async def on_tool_finish(self, tool_call_id: str, **kwargs: Any) -> None:
        for call in self.calls:
            if call["id"] == tool_call_id:
                call.update(kwargs)

    async def emit(self, event_type: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.events.append((event_type, data))

    async def audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return self.approvals.get(approval_id)

    async def create_approval(self, **kwargs: Any) -> dict[str, Any]:
        self._counter += 1
        approval = {"id": f"appr-{self._counter}", "status": "pending", **kwargs}
        self.approvals[approval["id"]] = approval
        return approval
