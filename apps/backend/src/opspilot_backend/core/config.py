"""Application configuration using pydantic-settings.

No hardcoded secrets — everything sensitive comes from the environment.
"""

from functools import lru_cache
from typing import List

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration."""

    # --- App ---
    app_name: str = Field(default="OpsPilot", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_debug: bool = Field(default=True, alias="APP_DEBUG")

    # --- Server ---
    backend_host: str = Field(default="0.0.0.0", alias="BACKEND_HOST")
    # Hosting platforms inject the single public port as `PORT`.
    backend_port: int = Field(
        default=8000, validation_alias=AliasChoices("BACKEND_PORT", "PORT")
    )
    backend_cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:5175",
            "http://localhost:5180",
            "http://localhost:5181",
            "http://localhost:5182",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
            "http://127.0.0.1:5175",
        ],
        alias="BACKEND_CORS_ORIGINS",
    )

    # --- Database ---
    # The driver follows the URL: postgresql+asyncpg://... really is Postgres.
    # SQLite is the zero-dependency default for local dev and tests.
    database_url: str = Field(
        default="sqlite+aiosqlite:///./opspilot.db", alias="DATABASE_URL"
    )
    database_echo: bool = Field(default=False, alias="DATABASE_ECHO")
    # Record every SQL statement as a span. On by default because "which query
    # was slow" is the question a postmortem actually asks; turn it off when
    # span volume matters more than that answer.
    db_trace_spans: bool = Field(default=True, alias="OPSPILOT_DB_TRACE_SPANS")
    # Statements faster than this are counted into a per-parent roll-up span
    # instead of being stored individually. A run issues hundreds of
    # sub-millisecond queries; keeping them all buries the ones that matter.
    db_span_min_ms: float = Field(default=5.0, alias="OPSPILOT_DB_SPAN_MIN_MS")

    # --- Redis ---
    # Optional. When unreachable the event bus falls back to an in-process
    # implementation; PostgreSQL (or SQLite) stays the source of truth either way.
    redis_url: str = Field(default="", alias="REDIS_URL")

    # --- Infrastructure providers ---
    # The simulator is a real HTTP service that models a small production
    # environment — it is not a mock API returning canned strings.
    simulator_url: str = Field(
        default="http://127.0.0.1:8100", alias="OPSPILOT_SIMULATOR_URL"
    )
    # --- Embedded simulator (single-port hosting) ---
    # When true the incident simulator is mounted in-process and the provider
    # client is pointed at this server's loopback mount, so the whole stack
    # runs behind one public port with no companion service. Used by hosting
    # sandboxes that expose exactly one HTTP port.
    embed_simulator: bool = Field(default=False, alias="OPSPILOT_EMBED_SIMULATOR")

    # --- Tool transport ---
    # "inprocess" (default): Tool Layer calls the providers directly.
    # "mcp": Tool Layer goes through the Ops MCP server over stdio.
    mcp_transport: str = Field(default="inprocess", alias="OPSPILOT_MCP_TRANSPORT")
    mcp_server_command: str = Field(default="", alias="OPSPILOT_MCP_SERVER_COMMAND")

    # --- LLM ---
    llm_provider: str = Field(default="auto", alias="OPSPILOT_LLM_PROVIDER")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        default="https://api.openai.com/v1", alias="OPENAI_BASE_URL"
    )
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_temperature: float = Field(default=0.1, alias="OPENAI_TEMPERATURE")
    openai_timeout: float = Field(default=60.0, alias="OPENAI_TIMEOUT")

    # --- GitHub ---
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_repo: str = Field(default="", alias="GITHUB_REPO")

    # --- Agent runtime ---
    agent_step_timeout_s: float = Field(default=120.0, alias="OPSPILOT_STEP_TIMEOUT_S")
    agent_max_parallel_tools: int = Field(
        default=6, alias="OPSPILOT_MAX_PARALLEL_TOOLS"
    )
    # 0 means "no automatic approval, ever" — an approval stays pending until a
    # human decides. The old 15s auto-approve grace period is gone.
    approval_auto_approve_after_s: float = Field(
        default=0.0, alias="OPSPILOT_APPROVAL_AUTO_APPROVE_S"
    )

    # --- Investigation budget ---
    # Overridable per run; these are the defaults. Tight enough that a run
    # which learns nothing escalates instead of burning the incident window.
    #
    # 32, not 24: a dependency-cascade incident legitimately costs one probe
    # per dependency for health, plus logs and deploy history on the ones that
    # turn out to be sick. At 24 the Agent ran out mid-drill and escalated with
    # INVESTIGATION_FAILED on a scenario it had actually solved.
    agent_budget_tool_calls: int = Field(
        default=32, alias="OPSPILOT_BUDGET_TOOL_CALLS"
    )
    agent_budget_seconds: float = Field(default=240.0, alias="OPSPILOT_BUDGET_SECONDS")
    agent_budget_tokens: int = Field(default=60_000, alias="OPSPILOT_BUDGET_TOKENS")
    agent_budget_retries: int = Field(default=6, alias="OPSPILOT_BUDGET_RETRIES")
    agent_budget_hypothesis_rounds: int = Field(
        default=3, alias="OPSPILOT_BUDGET_HYPOTHESIS_ROUNDS"
    )

    # --- Observability ---
    otel_service_name: str = Field(default="opspilot", alias="OTEL_SERVICE_NAME")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Demo bootstrap ---
    demo_seed: bool = Field(default=False, alias="OPSPILOT_DEMO_SEED")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
