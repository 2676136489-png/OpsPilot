"""Provider container.

One place to swap the simulator for real Grafana / GitHub / Kubernetes.
Tests override :func:`set_providers` with ASGI-backed or stub adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opspilot_backend.infrastructure.providers import (
    FileRunbookProvider,
    GitHubAdapter,
    SimulatorInfraProvider,
)


@dataclass
class Providers:
    services: Any = None
    metrics: Any = None
    logs: Any = None
    deployments: Any = None
    runbooks: Any = None
    github: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


_default_providers: Providers | None = None


def get_providers() -> Providers:
    global _default_providers
    if _default_providers is None:
        infra = SimulatorInfraProvider()
        _default_providers = Providers(
            services=infra,
            metrics=infra,
            logs=infra,
            deployments=infra,
            runbooks=FileRunbookProvider(),
            github=GitHubAdapter(),
            extra={"infra": infra},
        )
    return _default_providers


def set_providers(providers: Providers | None) -> None:
    """Replace the container contents (used by tests and bootstrap)."""
    global _default_providers
    _default_providers = providers


def reset_providers() -> None:
    global _default_providers
    _default_providers = None
