"""Domain error hierarchy + HTTP mapping.

Application and Agent layers raise :class:`AppError` subclasses only. The API
layer is the single place that turns them into HTTP responses, so no business
module ever imports FastAPI.
"""

from __future__ import annotations

from typing import Any

from opspilot_backend.domain.enums import IncidentStatus


class ErrorCode:
    """Stable machine-readable error codes (Appendix A of the API contract)."""

    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"
    AGENT_RUN_NOT_FOUND = "AGENT_RUN_NOT_FOUND"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    SERVICE_NOT_FOUND = "SERVICE_NOT_FOUND"
    RECOVERY_PLAN_NOT_FOUND = "RECOVERY_PLAN_NOT_FOUND"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    CONFLICT = "CONFLICT"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    APPROVAL_ALREADY_DECIDED = "APPROVAL_ALREADY_DECIDED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    RUN_NOT_RUNNING = "RUN_NOT_RUNNING"
    RATE_LIMITED = "RATE_LIMITED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """Base class for every error the API layer knows how to render."""

    code: str = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class BadRequestError(AppError):
    code = ErrorCode.BAD_REQUEST
    http_status = 400


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class UnauthorizedError(AppError):
    code = ErrorCode.UNAUTHORIZED
    http_status = 401


class ForbiddenError(AppError):
    code = ErrorCode.FORBIDDEN
    http_status = 403


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404

    def __init__(self, resource: str, resource_id: Any = None, **kwargs: Any) -> None:
        details = {"resource": resource}
        if resource_id is not None:
            details["id"] = str(resource_id)
        super().__init__(f"{resource} not found", details=details, **kwargs)


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    http_status = 409


class InvalidStateTransitionError(ConflictError):
    code = ErrorCode.INVALID_STATE_TRANSITION

    def __init__(self, current: IncidentStatus, target: IncidentStatus) -> None:
        allowed = sorted(
            s.value
            for s in _allowed_for(current)
        )
        super().__init__(
            f"故障状态不能从 {current.value} 直接变为 {target.value}",
            details={
                "current_status": current.value,
                "target_status": target.value,
                "allowed_targets": allowed,
            },
        )


class ApprovalRequiredError(ConflictError):
    code = ErrorCode.APPROVAL_REQUIRED

    def __init__(self, approval_id: str, risk: str) -> None:
        super().__init__(
            "该恢复动作必须先经过人工审批才能执行",
            details={"approval_id": approval_id, "risk_level": risk},
        )


class ToolExecutionError(AppError):
    code = ErrorCode.TOOL_EXECUTION_FAILED
    http_status = 502

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(
            f"工具 {tool_name!r} 执行失败：{reason}",
            details={"tool_name": tool_name, "reason": reason},
        )


class ToolTimeoutError(ToolExecutionError):
    code = ErrorCode.TOOL_TIMEOUT

    def __init__(self, tool_name: str, timeout_s: float) -> None:
        super().__init__(tool_name, f"超过 {timeout_s}s 未返回")


class DependencyUnavailableError(AppError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    http_status = 503


def _allowed_for(current: IncidentStatus) -> frozenset[IncidentStatus]:
    from opspilot_backend.domain.enums import ALLOWED_TRANSITIONS

    return ALLOWED_TRANSITIONS.get(current, frozenset())
