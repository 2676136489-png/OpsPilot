"""OpsPilot Backend — FastAPI application entry point.

Start with:
    uv run uvicorn opspilot_backend.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import time

# --- Bootstrap: give the whole process one config source ---
# Two different mechanisms read configuration at two different times, and they
# used to disagree. `Settings` declares `env_file=".env"`, which pydantic only
# consults when `get_settings()` runs. The embed-simulator flag right below is
# read from `os.environ` at *import* time, which is earlier. So a value that
# existed only in `.env` was invisible to the one reader that needed it most:
# the simulator never got mounted, `/__sim/*` fell through to the SPA catch-all,
# and every provider-backed endpoint answered 503 while the logs pointed at a
# companion process that was never supposed to exist.
#
# Loading the file here first makes both readers agree. Real environment
# variables keep precedence over the file, so platform-injected values (a secret
# set in the dashboard, $PORT) still win.
def _candidate_env_files() -> list[Path]:
    """Where a ``.env`` may live, most specific first.

    An explicit override always wins. After that the process working directory
    — hosting platforms usually start the server from the root they uploaded —
    and finally a walk up from this module, because a sandbox is free to start
    the server from anywhere it likes: in the deploy unit this file is
    ``<root>/src/opspilot_backend/main.py``, so three steps up is the same root
    that carries ``.env`` regardless of where the process was launched from.
    """
    override = os.environ.get("OPSPILOT_ENV_FILE", "").strip()
    if override:
        return [Path(override)]
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    candidates.extend(parent / ".env" for parent in here.parents[:4])
    return candidates


def _bootstrap_env_file() -> Path | None:
    """Load the first available .env into os.environ, live values winning."""
    for candidate in _candidate_env_files():
        if not candidate.is_file():
            continue
        try:
            from dotenv import dotenv_values  # type: ignore[import-not-found]

            values = dotenv_values(str(candidate), encoding="utf-8")
        except ImportError:  # pragma: no cover - python-dotenv absent
            values = _parse_env_file(candidate)
        for key, value in values.items():
            if value is not None:
                os.environ.setdefault(key, value)
        return candidate
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser, used only when python-dotenv is unavailable.

    Duplicating a third of `dotenv` is cheaper than letting an unstated
    dependency turn into a silently-unconfigured deployment.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        for quote in ("'", '"'):
            if len(value) >= 2 and value[0] == quote and value[-1] == quote:
                value = value[1:-1]
                break
        values[key.strip()] = value
    return values


_bootstrap_env_file()

# --- Embedded simulator (single-port hosting) ---
# This MUST run before any other import that could trigger ``get_settings()``:
# the provider reads ``OPSPILOT_SIMULATOR_URL`` from cached settings, and that
# function is ``@lru_cache``d, so if a submodule reads it during import (before
# this block runs) the loopback URL is lost forever and the agent talks to the
# dead default port instead of the in-process mount.
EMBED_SIMULATOR = os.environ.get("OPSPILOT_EMBED_SIMULATOR", "").lower() in {
    "1",
    "true",
    "yes",
}
if EMBED_SIMULATOR:
    _embed_port = os.environ.get("PORT", os.environ.get("BACKEND_PORT", "8000"))
    os.environ.setdefault(
        "OPSPILOT_SIMULATOR_URL", f"http://127.0.0.1:{_embed_port}/__sim"
    )

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from opspilot_backend.api.v1.router import api_v1_router
from opspilot_backend.core.config import get_settings
from opspilot_backend.core.logging import log_event
from opspilot_backend.core.observability import metrics
from opspilot_backend.domain.errors import AppError
from opspilot_backend.core.tracing import (
    aspan,
    bind,
    current_context,
    drain_spans,
    new_span_id,
    parse_traceparent,
    suspend_recording,
    unbind,
)
from opspilot_backend.db.session import async_engine
from opspilot_backend.models import Base  # noqa: F401 — ensure all models are registered

settings = get_settings()

# --- Built frontend (single-port hosting) ---
# Deploy targets expose exactly one HTTP port, so the API also serves the
# compiled SPA instead of requiring a second process. `apps/frontend` builds
# into `webroot/`; when it is absent the app stays a pure API (dev mode).
WEB_ROOT = Path(
    os.environ.get("OPSPILOT_WEB_ROOT")
    or Path(__file__).resolve().parent.parent.parent / "webroot"
).resolve()

HAS_WEB = (WEB_ROOT / "index.html").is_file()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: D401 — FastAPI lifespan signature
    """Create tables on SQLite dev database at startup."""
    # Import all models so Base.metadata is populated before create_all
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if settings.demo_seed:
        await _seed_demo_incidents()

    yield
    # Dispose engine on shutdown
    await async_engine.dispose()


async def _seed_demo_incidents() -> None:
    """Seed one canonical incident so a fresh deploy isn't an empty dashboard.

    The simulator can only hold a single active scenario at a time, so we seed
    exactly one representative incident (payment-bad-deployment) and set the
    simulator's live state to match it. Operators inject more scenarios from the
    UI, which opens a new incident and resets the simulator to that scenario.

    Only runs when ``OPSPILOT_DEMO_SEED`` is set and the incidents table is
    empty, so it never overwrites real records and never fires in tests.
    """
    from sqlalchemy import func, select

    from opspilot_backend.db.session import async_session_factory
    from opspilot_backend.models import Incident, Service
    from opspilot_backend.repositories.incident import IncidentRepository
    from opspilot_simulator.engine import get_simulator
    from opspilot_simulator.scenarios import get_scenario

    scenario_name = "payment-bad-deployment"
    try:
        async with async_session_factory() as session:
            if await session.scalar(select(func.count()).select_from(Incident)):
                return
            scenario = get_scenario(scenario_name)
            if scenario is None:
                return
            # Point the simulator's live state at this scenario so the agent's
            # tool calls observe the fault it is about to investigate.
            get_simulator().inject(scenario.name)
            service = (
                await session.execute(
                    select(Service).where(Service.name == scenario.alert_service)
                )
            ).scalars().first()
            if service is None:
                service = Service(
                    name=scenario.alert_service,
                    description=f"{scenario.alert_service} (simulated)",
                    tier="application",
                    owner="opspilot-simulator",
                )
                session.add(service)
                await session.flush()
            incident = await IncidentRepository(session).create(
                title=scenario.title,
                service_id=service.id,
                severity=scenario.severity,
                description=scenario.description,
                scenario=scenario.name,
            )
            await session.commit()
            log_event("demo.seed", scenario=scenario_name, incident_id=str(incident.id))
    except Exception:  # noqa: BLE001 — seeding is best-effort, never fatal
        pass


app = FastAPI(
    title=settings.app_name,
    description="OpsPilot — AI-Powered Incident Investigation & Recovery Platform",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# --- Domain errors → HTTP ---
# `domain/errors.py` documents the API layer as the single place AppError
# subclasses become responses, but nothing ever registered the handler. Every
# raised ConflictError / NotFoundError therefore escaped FastAPI as a bare 500
# with an empty body: repeat a running investigation and the caller gets no way
# to tell "this incident already has a run" from "the server is broken". The
# classes already carry their own status and code — this just stops discarding
# them.
@app.exception_handler(AppError)
async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log_event(
        "api.domain_error",
        code=exc.code,
        status=exc.http_status,
        path=request.url.path,
    )
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


# --- Embedded simulator (single-port hosting) ---
# Mount the simulator as a sub-application under /__sim. The provider client is
# already pointed at http://127.0.0.1:<port>/__sim, so investigation and
# recovery talk to it over the same single public port. Registered before the
# SPA catch-all route so /__sim/* is never mistaken for a SPA path.
if EMBED_SIMULATOR:
    from opspilot_simulator.server import app as _simulator_app

    app.mount("/__sim", _simulator_app)

# --- Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.backend_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def instrument_requests(request: Request, call_next):
    """Record request count and latency for every HTTP call.

    Without this the metrics collector has no producers and ``/api/v1/metrics``
    is permanently empty — an endpoint that looks live but reports nothing is
    worse than no endpoint at all.

    Metrics are labelled by the *route template* (``/api/v1/incidents/{id}``)
    rather than the resolved path, otherwise every incident id would create a
    new time series and the collector would grow without bound.
    """
    started = time.perf_counter()
    status_code = 500
    # Key the in-flight gauge on the raw path: the route template is not known
    # until `call_next` has routed the request, and the increment must happen
    # before the handler runs for the gauge to mean anything.
    inflight_key = request.url.path

    metrics.inc_gauge("opspilot_http_in_flight", 1, {"path": inflight_key})
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - started
        # `request.scope["route"]` is only populated once routing has run, which
        # happens inside `call_next`. Reading it here gives the route *template*
        # (`/api/v1/incidents/{id}`) rather than the resolved path — otherwise
        # one time series would be created per incident id.
        route = request.scope.get("route")
        path = getattr(route, "path", None) or request.url.path
        labels = {"method": request.method, "path": path, "status": str(status_code)}

        metrics.increment("opspilot_http_requests_total", 1, labels)
        metrics.observe_histogram("opspilot_http_request_duration_seconds", elapsed, labels)
        metrics.inc_gauge("opspilot_http_in_flight", -1, {"path": inflight_key})


@app.middleware("http")
async def trace_context(request: Request, call_next):
    """Bind request_id / trace_id / span_id for the whole request.

    Registered after the metrics middleware so it is the outermost layer: by
    the time anything else runs, the identifiers exist.

    Inbound ``traceparent`` is honoured rather than overwritten, which is what
    lets a run started by an HTTP call share one trace with the frontend's own
    request. The triple is echoed back so the browser can correlate what it
    just did with what the backend logged.
    """
    incoming = request.headers.get("traceparent", "")
    token = bind(
        trace_id=parse_traceparent(incoming)
        or request.headers.get("x-trace-id", "")
        or None,
        request_id=request.headers.get("x-request-id", "") or None,
        span_id=new_span_id(),
    )
    # Captured now, while it is still bound: `unbind` below restores the
    # enclosing context, so reading it afterwards would yield empty ids.
    ctx = current_context()
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    try:
        async with aspan(
            "http.request", kind="http.server", method=request.method, path=path
        ):
            response = await call_next(request)
    finally:
        unbind(token)

    response.headers["X-Request-Id"] = ctx.request_id
    response.headers["X-Trace-Id"] = ctx.trace_id
    response.headers["traceparent"] = f"00-{ctx.trace_id}-{ctx.span_id}-01"
    # Let the SPA read the correlation id off a cross-origin response. Merged
    # rather than assigned: this middleware sits outside CORSMiddleware, and
    # clobbering its header would silently drop whatever it had exposed.
    exposed = {
        h.strip()
        for h in response.headers.get("Access-Control-Expose-Headers", "").split(",")
        if h.strip()
    }
    exposed |= {"X-Request-Id", "X-Trace-Id", "traceparent"}
    response.headers["Access-Control-Expose-Headers"] = ", ".join(sorted(exposed))

    # Persist the HTTP leg only for requests that *cause* something. Reads are
    # the polling traffic — a dashboard refreshing every two seconds would
    # write more trace rows than the incident it is watching — whereas a POST
    # is what a postmortem reconstructs: who clicked restart, and what did it
    # trigger.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        await _persist_request_spans(request, ctx.span_id)
    return response


async def _persist_request_spans(request: Request, span_id: str) -> None:
    """Store this request's own span as the root of its trace.

    Deliberately best-effort — observability must never be able to fail a
    request that otherwise succeeded.
    """
    spans = [
        s
        for s in drain_spans()
        # Only this request's span: a state-changing handler may also have run
        # traced work inline (a synchronous run), and that work is persisted
        # by the runtime under its own run_id.
        if s.get("span_id") == span_id
    ]
    if not spans:
        return
    try:
        from opspilot_backend.db.session import async_session_factory
        from opspilot_backend.repositories.agent_run import AgentRunRepository

        with suspend_recording():  # do not trace the act of writing the trace
            async with async_session_factory() as session:
                await AgentRunRepository(session).save_spans(None, spans)
                await session.commit()
    except Exception as exc:  # noqa: BLE001 - see docstring
        log_event("http.trace.persist_failed", error=f"{type(exc).__name__}: {exc}")


# --- Routers ---
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/", tags=["root"], include_in_schema=not HAS_WEB)
async def root(request: Request):
    """Root endpoint — the SPA when it is built, service info otherwise."""
    if HAS_WEB:
        return FileResponse(WEB_ROOT / "index.html")
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/v1/health",
    }


if HAS_WEB:

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """Serve the SPA for every non-API path, including deep links.

        Client-side routes like ``/incidents/<id>`` have no file behind them, so
        without this the app 404s on refresh. Assets that *do* exist are served
        directly; anything unknown falls back to ``index.html`` and lets the
        router decide.
        """
        # Resolve inside WEB_ROOT and reject anything that escapes it — a
        # naive join here would turn `/../../etc/passwd` into a file read.
        candidate = (WEB_ROOT / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(WEB_ROOT):
            return FileResponse(candidate)
        return FileResponse(WEB_ROOT / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "opspilot_backend.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=settings.app_env == "development",
    )
