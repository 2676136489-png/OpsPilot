"""Provider interfaces + the simulator-backed implementations.

Nothing here returns invented numbers. If the simulated environment is
unreachable the call raises :class:`HttpError` and the Tool Layer records a
failed tool call — the agent sees "no data", never "fake data".
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from opspilot_backend.core.config import get_settings
from opspilot_backend.infrastructure.http_client import HttpError, ResilientHttpClient

# ---------------------------------------------------------------------------
# Protocols — the seams that let real Grafana / GitHub / K8s drop in later
# ---------------------------------------------------------------------------


class ServiceProvider(Protocol):
    """What the tool layer is allowed to ask of the environment.

    Note what is missing: ``act`` is here because a recovery is a legitimate
    thing to request, but the evaluator's ``ground_truth`` / ``evaluate_criteria``
    are deliberately absent. They exist on the simulator-backed implementation
    only, so no code path that the Agent can reach is typed against them.
    """

    async def get_status(self, service: str) -> dict[str, Any]: ...
    async def list_services(self) -> list[dict[str, Any]]: ...
    async def get_dependencies(self) -> dict[str, Any]: ...
    async def restart(self, service: str) -> dict[str, Any]: ...
    async def scale(self, service: str, replicas: int) -> dict[str, Any]: ...
    async def act(
        self, action: str, service: str, **params: Any
    ) -> dict[str, Any]: ...


class MetricsProvider(Protocol):
    async def query(
        self, service: str, metric_names: list[str], minutes: int
    ) -> dict[str, Any]: ...


class LogsProvider(Protocol):
    async def query(
        self, service: str, level: str | None, minutes: int, limit: int
    ) -> dict[str, Any]: ...


class DeploymentProvider(Protocol):
    async def list_deployments(self, service: str, limit: int) -> list[dict[str, Any]]: ...
    async def recent_commits(self, repository: str, limit: int) -> list[dict[str, Any]]: ...
    async def rollback(self, service: str) -> dict[str, Any]: ...


class RunbookProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[dict[str, Any]]: ...
    async def get(self, runbook_id: str) -> dict[str, Any] | None: ...


class GitHubProvider(Protocol):
    async def create_issue(
        self, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Simulator-backed implementation
# ---------------------------------------------------------------------------

DEFAULT_METRICS = [
    "error_rate",
    "latency_p95",
    "latency_p50",
    "request_rate",
    "cpu",
    "memory",
    "db_connections",
]


def _targets_this_host(url: str) -> bool:
    """True when ``url`` points back at the machine we are running on.

    This matters because of ``trust_env``. httpx reads ``http_proxy`` /
    ``https_proxy`` from the environment, and plenty of real environments set
    one — corporate networks, CI runners, sandboxed hosts. A proxy in the
    environment is a routing hint for outbound traffic; when the destination is
    loopback there is nothing to proxy to, and the ambient proxy answers for a
    service it has never heard of. The failure is also badly shaped: the proxy
    returns 404/502, the provider reports "simulator returned 404", and an
    operator goes looking for a missing route in the simulator instead of an
    unwanted hop out of the process.

    Only literal loopback addresses and names that resolve to loopback count as
    "here". Anything else keeps proxy behaviour untouched.
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(ipaddress.ip_address(info[4][0]).is_loopback for info in resolved)


class SimulatorInfraProvider:
    """Talks to ``opspilot-simulator`` over HTTP.

    The simulator is a stateful service modelling a small production
    environment: injecting a fault changes metrics *and* log content, and a
    restart/rollback mutates that state back. That is what makes
    "Recovery → Verification" a real closed loop instead of a scripted one.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        client: Any | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        settings = get_settings()
        target = base_url or settings.simulator_url
        self._http = ResilientHttpClient(
            target,
            timeout_s=timeout_s,
            max_retries=2,
            service_name="simulator",
            client=client,
            # Single-port hosting makes this client call its own process, and
            # local development usually talks to a companion on 127.0.0.1:8100.
            # In both cases an ambient proxy has no business being in the path.
            trust_env=not _targets_this_host(target),
        )

    # --- services -----------------------------------------------------
    async def get_status(self, service: str) -> dict[str, Any]:
        return await self._http.request("GET", f"/simulator/services/{service}")

    async def list_services(self) -> list[dict[str, Any]]:
        payload = await self._http.request("GET", "/simulator/services")
        services: list[dict[str, Any]] = payload.get("services", [])
        return services

    async def get_dependencies(self) -> dict[str, Any]:
        return await self._http.request("GET", "/simulator/dependencies")

    async def restart(self, service: str) -> dict[str, Any]:
        return await self._http.request("POST", f"/simulator/services/{service}/restart")

    async def act(
        self, action: str, service: str, **params: Any
    ) -> dict[str, Any]:
        """Perform a named recovery action.

        The simulator exposes a generic action endpoint precisely so that new
        remediations are data, not new routes on both sides of the wire.
        """
        query: dict[str, Any] = {"service": service, **params}
        return await self._http.request(
            "POST", f"/simulator/actions/{action}", params=query
        )

    async def scale(self, service: str, replicas: int) -> dict[str, Any]:
        return await self._http.request(
            "POST",
            f"/simulator/services/{service}/scale",
            params={"replicas": replicas},
        )

    # --- metrics ------------------------------------------------------
    async def query(
        self, service: str, metric_names: list[str], minutes: int = 30
    ) -> dict[str, Any]:
        series: list[dict[str, Any]] = []
        for metric in metric_names or DEFAULT_METRICS:
            try:
                payload = await self._http.request(
                    "GET",
                    "/simulator/metrics",
                    params={
                        "service": service,
                        "metric": metric,
                        "minutes_back": minutes,
                    },
                )
            except HttpError:
                # A missing metric is not a fatal condition — skip it and keep
                # whatever the environment could answer.
                continue
            points = payload.get("points", [])
            series.append(
                {
                    "metric": metric,
                    "service": service,
                    "points": points,
                    "latest": points[-1]["value"] if points else None,
                    "previous": points[-6]["value"] if len(points) >= 6 else None,
                }
            )
        if not series:
            raise HttpError(
                f"simulator returned no metric series for {service!r}", retryable=True
            )
        return {"service": service, "series": series}

    # --- logs ---------------------------------------------------------
    async def query_logs(
        self, service: str, level: str | None, minutes: int, limit: int = 50
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"service": service, "minutes_back": minutes}
        if level:
            params["level"] = level
        payload = await self._http.request("GET", "/simulator/logs", params=params)
        entries = payload.get("entries", [])
        return {"service": service, "entries": entries[:limit], "total": len(entries)}

    # --- deployments / commits ----------------------------------------
    async def list_deployments(self, service: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = await self._http.request(
            "GET", "/simulator/deployments", params={"service": service, "limit": limit}
        )
        return list(payload.get("deployments", []))

    async def recent_commits(self, repository: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = await self._http.request(
            "GET", "/simulator/commits", params={"repository": repository, "limit": limit}
        )
        return list(payload.get("commits", []))

    async def rollback(self, service: str) -> dict[str, Any]:
        return await self._http.request(
            "POST", "/simulator/deployments/rollback", params={"service": service}
        )

    # --- scenario control ---------------------------------------------
    async def inject(self, scenario: str) -> dict[str, Any]:
        return await self._http.request("POST", f"/simulator/incidents/{scenario}/inject")

    async def reset(self, scenario: str = "") -> dict[str, Any]:
        return await self._http.request("POST", f"/simulator/incidents/{scenario}/reset")

    async def list_scenarios(self) -> list[dict[str, Any]]:
        payload = await self._http.request("GET", "/simulator/scenarios")
        return list(payload.get("scenarios", []))

    # --- evaluation only ----------------------------------------------
    # Both of these are backed by endpoints the simulator only exposes under
    # EVAL_MODE, and neither is reachable from a tool: no entry in
    # ``tools/registry.py`` calls them. That separation is the point — the
    # answer key must not be one function call away from the Agent.
    async def ground_truth(self, scenario: str) -> dict[str, Any]:
        return await self._http.request("GET", f"/simulator/ground-truth/{scenario}")

    async def evaluate_criteria(
        self, criteria: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._http.request("POST", "/simulator/verify", json_body=criteria)


# ---------------------------------------------------------------------------
# Runbooks — backed by the real markdown files in the repo
# ---------------------------------------------------------------------------

def _runbook_root() -> Path:
    """Locate the repo's `runbooks/` directory without counting path levels.

    `_RUNBOOK_ROOT` used to be `parents[5] / "runbooks"`, which silently encodes
    the six-deep `apps/backend/src/opspilot_backend/infrastructure/` layout. Move
    the package anywhere else — as the deploy unit and the hosting sandbox both
    do — and the index no longer lands on the repo root: in `deploy/` it resolves
    to a directory two levels too high, and under `/workspace` it raises
    IndexError at import time, taking the whole app down before it can serve a
    request. Walking up for the directory itself is layout-independent.
    """
    override = os.environ.get("OPSPILOT_RUNBOOK_ROOT", "").strip()
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "runbooks"
        if candidate.is_dir():
            return candidate
    # Nothing found — return the natural guess so callers still get a usable
    # (empty) path instead of an exception.
    return here.parents[min(3, len(here.parents) - 1)] / "runbooks"


_RUNBOOK_ROOT = _runbook_root()


def _split_headings(text: str) -> list[tuple[str, str]]:
    """Split a markdown runbook into (heading, body) chunks."""
    chunks: list[tuple[str, str]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if current_body:
                chunks.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line.lstrip("#").strip()
            current_body = []
        else:
            current_body.append(line)
    if current_body:
        chunks.append((current_heading, "\n".join(current_body).strip()))
    return [(h, b) for h, b in chunks if b]


def _score(text: str, query: str) -> int:
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    lowered = text.lower()
    return sum(lowered.count(t) for t in terms)


class FileRunbookProvider:
    """Reads runbooks straight off disk — real content, no canned strings."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else _RUNBOOK_ROOT

    @staticmethod
    def _runbook_id(path: Path, root: Path) -> str:
        """Stable, platform-independent id.

        ``as_posix()`` is not cosmetic: on Windows ``relative_to`` yields
        ``database\\connection-pool``, so an id that came back from ``search``
        could never be passed to ``get``.
        """
        return path.relative_to(root).with_suffix("").as_posix()

    def _load(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            chunks = _split_headings(text)
            category = path.parent.name
            title = chunks[0][0] if chunks else path.stem
            out.append(
                {
                    "id": self._runbook_id(path, self.root),
                    "title": title or path.stem,
                    "category": category,
                    "path": str(path),
                    "content": text,
                    "chunks": [
                        {"heading": h, "content": b[:1200]} for h, b in chunks
                    ],
                }
            )
        return out

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        scored = [
            (rb, _score(rb["title"] + "\n" + rb["content"], query)) for rb in self._load()
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: item[1], reverse=True)
        results: list[dict[str, Any]] = []
        for rb, score in scored[:limit]:
            best_chunk = max(
                rb["chunks"],
                key=lambda c: _score(c["heading"] + c["content"], query),
                default={"heading": "", "content": ""},
            )
            results.append(
                {
                    "id": rb["id"],
                    "title": rb["title"],
                    "category": rb["category"],
                    "score": score,
                    "excerpt": best_chunk["content"][:600],
                    "heading": best_chunk["heading"],
                }
            )
        return results

    async def get(self, runbook_id: str) -> dict[str, Any] | None:
        # Accept either separator — the id is user-supplied at this point.
        wanted = runbook_id.strip().replace("\\", "/").removesuffix(".md")
        for rb in self._load():
            if rb["id"] == wanted:
                return rb
        return None


# ---------------------------------------------------------------------------
# GitHub — real when a token is configured, explicit failure when not
# ---------------------------------------------------------------------------


class GitHubAdapter:
    """Minimal GitHub issues adapter.

    With ``GITHUB_TOKEN`` + ``GITHUB_REPO`` set it performs a real API call.
    Without them it raises instead of pretending an issue was filed.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.github_token
        self._repo = settings.github_repo
        self._http = ResilientHttpClient(
            "https://api.github.com", timeout_s=8.0, service_name="github"
        )

    @property
    def configured(self) -> bool:
        return bool(self._token and self._repo)

    async def create_issue(
        self, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        if not self.configured:
            raise HttpError(
                "GitHub adapter is not configured (set GITHUB_TOKEN and GITHUB_REPO)",
                retryable=False,
            )
        return await self._post(title, body, labels)

    async def _post(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        import httpx

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"https://api.github.com/repos/{self._repo}/issues",
                headers=headers,
                json={"title": title, "body": body, "labels": labels},
            )
        if response.status_code >= 400:
            raise HttpError(
                f"GitHub returned {response.status_code}",
                status_code=response.status_code,
            )
        payload: dict[str, Any] = response.json()
        return {
            "number": payload.get("number"),
            "url": payload.get("html_url"),
            "title": payload.get("title"),
        }
