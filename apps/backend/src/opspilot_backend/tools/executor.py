"""Tool executor — the six-step gate every invocation passes through.

    Policy Check → Permission Check → Risk Check → Human Approval
                 → Execution → Verification

Steps 1–4 can *block* a call without ever touching the outside world; step 6
forces every mutating tool to be followed by a real health probe.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import ValidationError

from opspilot_backend.core.config import get_settings
from opspilot_backend.core.logging import log_event
from opspilot_backend.domain.enums import EventType, PermissionLevel, ToolCallStatus
from opspilot_backend.infrastructure.http_client import HttpError
from opspilot_backend.tools.hooks import NullToolHooks, ToolHooks
from opspilot_backend.tools.registry import HANDLERS, TOOL_REGISTRY, idempotency_key
from opspilot_backend.tools.spec import ToolContext, ToolResult, ToolSpec


class ToolExecutor:
    def __init__(
        self,
        hooks: ToolHooks | None = None,
        *,
        transport: str | None = None,
        auto_verify: bool = True,
    ) -> None:
        self.hooks: ToolHooks = hooks or NullToolHooks()
        settings = get_settings()
        self.transport = transport or settings.mcp_transport
        self.auto_verify = auto_verify

    # ------------------------------------------------------------------
    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        approval_id: str | None = None,
        skip_verification: bool = False,
    ) -> ToolResult:
        started = time.perf_counter()
        spec = self._policy_check(tool_name, ctx)
        self._permission_check(spec, ctx)
        validated = self._validate(spec, arguments)
        args = validated.model_dump()

        idem = idempotency_key(
            tool_name, args, scope=ctx.run_id, occurrence=ctx.occurrence
        )
        tool_call_id = await self.hooks.on_tool_start(
            tool_name=tool_name,
            arguments=args,
            idempotency_key=idem,
            run_id=ctx.run_id,
            step_id=ctx.step_id,
            risk_level=spec.risk_level.value,
            permission_level=spec.permission_level.value,
            transport=self.transport,
        )
        await self.hooks.emit(
            EventType.TOOL_STARTED.value,
            {"tool_name": tool_name, "arguments": args, "risk_level": spec.risk_level.value},
            run_id=ctx.run_id,
            incident_id=ctx.incident_id,
            stage=ctx.stage.value if ctx.stage else None,
        )

        # --- Step 3 + 4: risk → approval gate -------------------------
        if spec.requires_approval:
            gate = await self._approval_gate(spec, ctx, args, approval_id, tool_call_id)
            if gate is not None:
                return gate

        # --- Step 5: execution ----------------------------------------
        result, error_code, error_message, attempts = await self._run_with_retry(
            spec, args, ctx
        )
        ok = error_code is None
        duration_ms = int((time.perf_counter() - started) * 1000)

        # --- Step 6: verification -------------------------------------
        verification: dict[str, Any] | None = None
        if ok and spec.side_effect and self.auto_verify and not skip_verification:
            verification = await self._verify(spec, args, ctx)
            if verification is not None and not verification.get("passed", True):
                # The action ran, but the environment did not recover. That is
                # a real outcome and must be reported as such.
                ok = False
                error_code = "verification_failed"
                error_message = "动作执行后的健康探测未通过"

        status = ToolCallStatus.SUCCEEDED if ok else ToolCallStatus.FAILED
        if error_code == "timeout":
            status = ToolCallStatus.TIMEOUT

        await self.hooks.on_tool_finish(
            tool_call_id,
            status=status.value,
            result=result,
            error_code=error_code,
            error_message=error_message,
            duration_ms=duration_ms,
            attempts=attempts,
        )
        await self.hooks.emit(
            EventType.TOOL_COMPLETED.value if ok else EventType.TOOL_FAILED.value,
            {
                "tool_name": tool_name,
                "ok": ok,
                "error_code": error_code,
                "error_message": error_message,
                "duration_ms": duration_ms,
                "attempts": attempts,
                "result_summary": _summarise(result),
            },
            run_id=ctx.run_id,
            incident_id=ctx.incident_id,
            stage=ctx.stage.value if ctx.stage else None,
        )
        await self.hooks.audit(
            action=f"tool.{tool_name}",
            actor=ctx.actor,
            actor_type=ctx.actor_type,
            resource_type="tool_call",
            resource_id=tool_call_id,
            incident_id=ctx.incident_id or None,
            run_id=ctx.run_id or None,
            tool_name=tool_name,
            risk_level=spec.risk_level.value,
            approval_id=approval_id,
            parameters=args,
            outcome="success" if ok else "failure",
            detail=error_message,
        )

        return ToolResult(
            tool_name=tool_name,
            ok=ok,
            arguments=args,
            result=result,
            error_type=error_code,
            error_message=error_message,
            duration_ms=duration_ms,
            attempts=attempts,
            risk_level=spec.risk_level.value,
            permission_level=spec.permission_level.value,
            approval_id=approval_id,
            tool_call_id=tool_call_id,
            transport=self.transport,
            verification=verification,
        )

    # ------------------------------------------------------------------
    # Step 1 — policy
    # ------------------------------------------------------------------
    def _policy_check(self, tool_name: str, ctx: ToolContext) -> ToolSpec:
        spec = TOOL_REGISTRY.get(tool_name)
        if spec is None:
            raise KeyError(f"未知工具：{tool_name!r}")
        if (
            spec.allowed_stages
            and ctx.stage is not None
            and ctx.stage not in spec.allowed_stages
        ):
            raise PermissionError(
                f"工具 {tool_name!r} 不允许在当前阶段 {ctx.stage.value!r} 调用"
            )
        return spec

    # ------------------------------------------------------------------
    # Step 2 — permission
    # ------------------------------------------------------------------
    def _permission_check(self, spec: ToolSpec, ctx: ToolContext) -> None:
        if spec.permission_level not in ctx.granted_permissions:
            raise PermissionError(
                f"执行者 {ctx.actor!r} 缺少 {spec.name!r} 所需的 "
                f"{spec.permission_level.value!r} 权限"
            )

    def _validate(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        try:
            return spec.validate_arguments(arguments)
        except ValidationError as exc:
            raise ValueError(
                f"工具 {spec.name} 的参数不合法：{exc.errors()}"
            ) from exc

    # ------------------------------------------------------------------
    # Steps 3 + 4 — risk assessment + approval gate
    # ------------------------------------------------------------------
    async def _approval_gate(
        self,
        spec: ToolSpec,
        ctx: ToolContext,
        args: dict[str, Any],
        approval_id: str | None,
        tool_call_id: str,
    ) -> ToolResult | None:
        """Return a ToolResult when the call must not proceed, else None."""
        if approval_id:
            approval = await self.hooks.get_approval(approval_id)
            if approval is None or approval.get("status") != "approved":
                return ToolResult(
                    tool_name=spec.name,
                    ok=False,
                    arguments=args,
                    blocked=True,
                    error_type="approval_required",
                    error_message=(
                        "高风险动作需要一个状态为 APPROVED 的审批记录 "
                        f"(approval_id={approval_id!r})"
                    ),
                    risk_level=spec.risk_level.value,
                    permission_level=spec.permission_level.value,
                    approval_id=approval_id,
                    tool_call_id=tool_call_id,
                )
            return None

        # No approval supplied → create the request and stop. There is
        # deliberately no timer that approves this on the caller's behalf.
        approval = await self.hooks.create_approval(
            incident_id=ctx.incident_id,
            run_id=ctx.run_id,
            action_id=None,
            action_type=spec.name,
            risk_level=spec.risk_level.value,
            reason=args.get("reason", f"Agent 申请执行 {spec.name}"),
            requested_by=ctx.actor,
        )
        await self.hooks.emit(
            EventType.APPROVAL_REQUIRED.value,
            {
                "approval_id": approval.get("id"),
                "tool_name": spec.name,
                "risk_level": spec.risk_level.value,
                "arguments": args,
            },
            run_id=ctx.run_id,
            incident_id=ctx.incident_id,
            stage=ctx.stage.value if ctx.stage else None,
        )
        await self.hooks.audit(
            action="approval.requested",
            actor=ctx.actor,
            actor_type=ctx.actor_type,
            resource_type="approval",
            resource_id=str(approval.get("id") or ""),
            incident_id=ctx.incident_id or None,
            run_id=ctx.run_id or None,
            tool_name=spec.name,
            risk_level=spec.risk_level.value,
            approval_id=str(approval.get("id") or ""),
            parameters=args,
            outcome="blocked",
            detail="等待人工决策",
        )
        return ToolResult(
            tool_name=spec.name,
            ok=False,
            arguments=args,
            blocked=True,
            error_type="approval_required",
            error_message=f"{spec.name} 需要人工审批后才能执行",
            risk_level=spec.risk_level.value,
            permission_level=spec.permission_level.value,
            approval_id=str(approval.get("id") or ""),
            tool_call_id=tool_call_id,
        )

    # ------------------------------------------------------------------
    # Step 5 — execution with timeout + retry
    # ------------------------------------------------------------------
    async def _run_with_retry(
        self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext
    ) -> tuple[Any, str | None, str | None, int]:
        last_error: str | None = None
        last_message: str | None = None
        attempts = 0
        max_attempts = max(1, spec.max_retries + 1)
        while attempts < max_attempts:
            attempts += 1
            try:
                payload = await asyncio.wait_for(
                    self._dispatch(spec, args, ctx), timeout=spec.timeout_s
                )
                return payload, None, None, attempts
            except asyncio.TimeoutError:
                last_error = "timeout"
                last_message = f"调用超过 {spec.timeout_s}s 未返回"
                log_event(
                    "tool.timeout", tool=spec.name, attempt=attempts, run_id=ctx.run_id
                )
            except HttpError as exc:
                last_error = "upstream_error"
                last_message = str(exc)
                if not exc.retryable:
                    break
            except KeyError as exc:
                last_error = "not_found"
                last_message = str(exc)
                break
            except PermissionError as exc:
                last_error = "permission_denied"
                last_message = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - normalised at the boundary
                last_error = "upstream_error"
                last_message = f"{type(exc).__name__}: {exc}"
                break
        return None, last_error, last_message, attempts

    async def _dispatch(
        self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext
    ) -> Any:
        if self.transport == "mcp":
            return await self._dispatch_mcp(spec, args)
        handler = HANDLERS[spec.name]
        return await handler(args, ctx)

    async def _dispatch_mcp(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        from opspilot_backend.mcp.client import McpError, get_mcp_client

        client = await get_mcp_client()
        if spec.name not in client.tool_names:
            # Not every tool is exposed by the MCP server yet — fall back to
            # the in-process adapter rather than failing the investigation.
            handler = HANDLERS[spec.name]
            return await handler(args, None)  # type: ignore[arg-type]
        try:
            return await client.call_tool(spec.name, args, timeout_s=spec.timeout_s + 5)
        except McpError:
            handler = HANDLERS[spec.name]
            return await handler(args, None)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Step 6 — verification
    # ------------------------------------------------------------------
    async def _verify(
        self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any] | None:
        service = args.get("service")
        if not service:
            return None
        verify_spec = TOOL_REGISTRY["verify_service_health"]
        try:
            outcome = await asyncio.wait_for(
                HANDLERS["verify_service_health"](
                    {"service": service}, ctx
                ),
                timeout=verify_spec.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        return dict(outcome)


def _summarise(result: Any) -> Any:
    """Keep SSE payloads small — never ship 200 metric points to a browser."""
    if isinstance(result, dict):
        summary: dict[str, Any] = {}
        for key, value in result.items():
            if isinstance(value, list):
                summary[key] = {"count": len(value)}
            else:
                summary[key] = value
        return summary
    if isinstance(result, list):
        return {"count": len(result)}
    return result


__all__ = ["ToolExecutor", "PermissionLevel"]
