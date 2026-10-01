"""Resilient HTTP client — the only place raw network I/O is allowed.

Every outbound request gets: a timeout, bounded retries with exponential
backoff + jitter, a request id header, and a structured log line. Business
code never constructs an httpx client of its own.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Any

import httpx

from opspilot_backend.core.logging import get_request_id, log_event
from opspilot_backend.core.tracing import aspan, current_context, trace_headers

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class HttpError(Exception):
    """Normalised transport error so callers never see httpx exceptions."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable


class ResilientHttpClient:
    """Thin async wrapper enforcing timeout / retry / backoff / request-id."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        max_retries: int = 2,
        backoff_base_s: float = 0.25,
        backoff_max_s: float = 2.0,
        service_name: str = "http",
        client: httpx.AsyncClient | None = None,
        trust_env: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Tests inject an ASGI-transport client so the simulator is exercised
        # over a real HTTP round-trip without needing a live subprocess.
        self._injected_client = client
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.service_name = service_name
        # httpx resolves proxy settings from the environment by default. That is
        # right for traffic leaving the host and wrong for a client whose only
        # destination is another process on this one — see
        # :func:`SimulatorInfraProvider.__init__` for who turns it off and why.
        self.trust_env = trust_env
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_s),
                headers={"X-OpsPilot-Service": self.service_name},
                trust_env=self.trust_env,
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        #: Any JSON-serialisable body. Not narrowed to a dict: the simulator's
        #: criteria-evaluation endpoint takes a top-level *array*.
        json_body: Any | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        # Reuse the caller's request id when there is one, so a tool call and
        # the simulator log line it produced are trivially pairable. Only a
        # call with no ambient context invents an id of its own.
        request_id = current_context().request_id or uuid.uuid4().hex[:16]
        headers = {
            "X-Request-Id": request_id,
            "X-Correlation-Id": get_request_id(),
            # traceparent — the simulator echoes it back, so the *simulated*
            # environment's own log lines join the same trace.
            **trace_headers(),
        }
        # An injected (test/ASGI) client carries its own base_url, so the
        # relative path must reach it untouched — otherwise tests silently
        # escape the in-process transport and hit a real network endpoint.
        if path.startswith("http") or self._injected_client is not None:
            url = path
        else:
            url = f"{self.base_url}{path}"
        attempt = 0
        started = time.perf_counter()

        async with aspan(
            f"http.{self.service_name}",
            kind="http.client",
            method=method.upper(),
            path=path,
        ) as sp:
            while True:
                attempt += 1
                try:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                        timeout=timeout_s or self.timeout_s,
                    )
                    if response.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                        await self._sleep_backoff(attempt)
                        continue
                    sp.attribute("status", response.status_code)
                    sp.attribute("attempt", attempt)
                    if response.status_code >= 400:
                        log_event(
                            "http.request.failed",
                            service=self.service_name,
                            method=method,
                            path=path,
                            status=response.status_code,
                            request_id=request_id,
                            attempt=attempt,
                        )
                        sp.fail(f"HTTP {response.status_code}")
                        raise HttpError(
                            f"{self.service_name} returned {response.status_code}",
                            status_code=response.status_code,
                            request_id=request_id,
                            retryable=response.status_code in RETRYABLE_STATUS,
                        )
                    log_event(
                        "http.request.completed",
                        service=self.service_name,
                        method=method,
                        path=path,
                        status=response.status_code,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        request_id=request_id,
                    )
                    payload: dict[str, Any] = response.json()
                    return payload
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt > self.max_retries:
                        log_event(
                            "http.request.error",
                            service=self.service_name,
                            method=method,
                            path=path,
                            error=type(exc).__name__,
                            request_id=request_id,
                            attempt=attempt,
                        )
                        sp.fail(f"重试 {attempt} 次后仍然失败：{type(exc).__name__}")
                        raise HttpError(
                            f"{self.service_name} 不可达：{type(exc).__name__}",
                            request_id=request_id,
                            retryable=True,
                        ) from exc
                    await self._sleep_backoff(attempt)

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self.backoff_max_s, self.backoff_base_s * (2 ** (attempt - 1)))
        await asyncio.sleep(delay * (0.5 + random.random() * 0.5))  # noqa: S311

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
