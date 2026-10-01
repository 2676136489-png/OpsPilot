"""Minimal MCP client (stdio transport, JSON-RPC 2.0).

Implemented against the wire protocol rather than a vendored SDK so the
backend has no hard dependency on the ``mcp`` package, while still being a
genuine MCP client: handshake → ``notifications/initialized`` → ``tools/list``
→ ``tools/call``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from opspilot_backend.core.logging import log_event
from opspilot_backend.core.tracing import aspan, current_context

PROTOCOL_VERSION = "2024-11-05"


class McpError(Exception):
    pass


class McpStdioClient:
    """One long-lived MCP server process, multiplexed by request id."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        startup_timeout_s: float = 20.0,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.startup_timeout_s = startup_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._tools: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env={**os.environ, **(self.env or {})},
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            await self._handshake()

    async def _handshake(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "opspilot-backend", "version": "1.0.0"},
            },
        )
        log_event(
            "mcp.initialized",
            server=result.get("serverInfo", {}).get("name", "unknown"),
            protocol=result.get("protocolVersion"),
        )
        await self._notify("notifications/initialized", {})
        listing = await self._request("tools/list", {})
        self._tools = list(listing.get("tools", []))

    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                self._proc.kill()
        self._proc = None

    # ------------------------------------------------------------------
    # RPC plumbing
    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            msg_id = message.get("id")
            if msg_id is not None and msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if not future.done():
                    future.set_result(message)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _request(
        self, method: str, params: dict[str, Any], timeout_s: float = 20.0
    ) -> dict[str, Any]:
        if self._proc is None:
            raise McpError("MCP 客户端尚未启动")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        envelope_params = self._with_trace(method, params)
        async with aspan(f"mcp.{method}", kind="mcp", rpc_id=msg_id) as sp:
            await self._send(
                {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": envelope_params}
            )
            try:
                response = await asyncio.wait_for(future, timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                self._pending.pop(msg_id, None)
                sp.fail(f"超过 {timeout_s}s 未返回")
                raise McpError(f"MCP {method} 超过 {timeout_s}s 未返回") from exc
            if "error" in response:
                sp.fail(str(response["error"]))
                raise McpError(f"MCP {method} 返回错误：{response['error']}")
            result: dict[str, Any] = response.get("result", {})
            sp.attribute("ok", True)
            return result

    def _with_trace(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Attach the current trace to the JSON-RPC envelope.

        ``_meta`` is the spec-defined place for out-of-band request metadata,
        so the server can (and a real OTel-instrumented server will) continue
        the trace. Sent only when there is a trace in scope: an empty ``_meta``
        on every request would be noise on the wire.
        """
        envelope = dict(params)
        ctx = current_context()
        if ctx.trace_id:
            envelope["_meta"] = {
                "traceparent": f"00-{ctx.trace_id}-{ctx.span_id}-01",
                "requestId": ctx.request_id,
                "client": "opspilot-backend",
                "rpcMethod": method,
            }
        return envelope

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def tool_names(self) -> set[str]:
        return {t.get("name", "") for t in self._tools}

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_s: float = 20.0
    ) -> Any:
        """Invoke a tool on the MCP server and unwrap the content payload."""
        if self._proc is None:
            await self.start()
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s
        )
        content = result.get("content", [])
        if result.get("isError"):
            raise McpError(f"工具 {name} 返回了一个错误：{content}")
        if content and isinstance(content, list):
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "text":
                try:
                    return json.loads(first["text"])
                except (json.JSONDecodeError, TypeError):
                    return first.get("text")
        return result


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def _monorepo_root() -> Path:
    """Locate the monorepo root by looking for `apps/mcp-server`, not by counting.

    Counting parents encodes the source layout into the code: five levels up
    works only from `apps/backend/src/opspilot_backend/mcp/`. The deploy unit
    flattens that to `deploy/src/...`, and a hosting sandbox may mount the app a
    couple of levels below `/`, where the walk runs out of parents and raises
    IndexError. Anchoring on a directory that only the monorepo root contains
    survives all three.
    """
    override = os.environ.get("OPSPILOT_MONOREPO_ROOT", "").strip()
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "apps" / "mcp-server").is_dir():
            return parent
    return here.parents[min(2, len(here.parents) - 1)]


_MONOREPO_ROOT = _monorepo_root()


def default_mcp_command() -> tuple[list[str], str]:
    """Locate the MCP server interpreter + working directory.

    Prefers the mcp-server's own virtualenv, where ``opspilot_mcp_server`` is
    installed as a package, and falls back to the interpreter running this
    process — which only works if that one has it installed too. ``cwd`` is
    returned as part of the contract because the server resolves its own
    relative paths from there.
    """
    server_dir = _MONOREPO_ROOT / "apps" / "mcp-server"
    venv_python = server_dir / ".venv" / "Scripts" / "python.exe"
    python_exe = venv_python if venv_python.exists() else Path(sys.executable)
    command = [
        str(python_exe),
        "-m",
        "opspilot_mcp_server.server",
    ]
    return command, str(server_dir)


_client: McpStdioClient | None = None


async def get_mcp_client() -> McpStdioClient:
    global _client
    if _client is None:
        command, cwd = default_mcp_command()
        _client = McpStdioClient(command, cwd=cwd)
        await _client.start()
    return _client


async def close_mcp_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
