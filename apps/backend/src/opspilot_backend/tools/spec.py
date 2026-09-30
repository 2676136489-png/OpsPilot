"""Tool specification — the contract every tool must satisfy."""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field

from opspilot_backend.domain.enums import (
    AgentStage,
    PermissionLevel,
    RiskLevel,
)


class ToolResult(BaseModel):
    """Normalised outcome of a tool invocation."""

    tool_name: str
    ok: bool
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    attempts: int = 1
    risk_level: str = RiskLevel.LOW.value
    permission_level: str = PermissionLevel.READ_ONLY.value
    approval_id: str | None = None
    blocked: bool = False
    tool_call_id: str | None = None
    transport: str = "inprocess"
    verification: dict[str, Any] | None = None


class ToolSpec(BaseModel):
    """Static metadata + schema for one tool."""

    name: str
    description: str
    category: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    timeout_s: float = 5.0
    max_retries: int = 1
    permission_level: PermissionLevel = PermissionLevel.READ_ONLY
    risk_level: RiskLevel = RiskLevel.LOW
    idempotent: bool = True
    side_effect: bool = False
    error_types: list[str] = Field(default_factory=lambda: ["timeout", "upstream_error"])
    # Stages in which this tool may be invoked (empty = any stage).
    allowed_stages: list[AgentStage] = Field(default_factory=list)
    # MCP server this tool belongs to (§7 grouping).
    mcp_server: str = "ops"

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> dict[str, Any]:
        if self.output_model is None:
            return {"type": "object"}
        return self.output_model.model_json_schema()

    @property
    def requires_approval(self) -> bool:
        """Only ``LOW`` runs unattended.

        Previously HIGH/CRITICAL only, which let a MEDIUM action — scaling,
        raising a pool limit, opening a circuit breaker, filing a tracker
        ticket — change production with no human in the loop. That is not a
        risk model, it is a hole in one.
        """
        return self.risk_level != RiskLevel.LOW

    def validate_arguments(self, arguments: dict[str, Any]) -> BaseModel:
        """Raise ``pydantic.ValidationError`` when the arguments do not match."""
        return self.input_model(**arguments)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "permission_level": self.permission_level.value,
            "risk_level": self.risk_level.value,
            "idempotent": self.idempotent,
            "side_effect": self.side_effect,
            "error_types": list(self.error_types),
            "requires_approval": self.requires_approval,
            "mcp_server": self.mcp_server,
        }


Handler = Callable[[dict[str, Any], "ToolContext"], Coroutine[Any, Any, dict[str, Any]]]


class ToolContext(BaseModel):
    """Everything the executor needs from the calling layer.

    Deliberately Pydantic (not a repository) so the Tool Layer can be unit
    tested without a database or an agent.
    """

    model_config = {"arbitrary_types_allowed": True}

    run_id: str = ""
    incident_id: str = ""
    step_id: str | None = None
    stage: AgentStage | None = None
    actor: str = "agent"
    actor_type: str = "agent"
    #: Monotonic index of this invocation within the run. Part of the
    #: idempotency key: two runs asking the same question are two different
    #: calls, and so are two calls inside one run.
    occurrence: int = 0
    granted_permissions: list[PermissionLevel] = Field(
        default_factory=lambda: list(PermissionLevel)
    )
    hooks: Any = None  # ToolHooks implementation (persistence + events)
