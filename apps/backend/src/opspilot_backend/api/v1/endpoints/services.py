"""Service endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.db.session import async_session
from opspilot_backend.infrastructure.container import get_providers
from opspilot_backend.infrastructure.http_client import HttpError
from opspilot_backend.schemas.incident import (
    ListResponse,
    ServiceCreate,
    ServiceRead,
)
from opspilot_backend.services.incident import ServiceService

router = APIRouter(prefix="/services", tags=["services"])


@router.post("", response_model=ServiceRead, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ServiceCreate,
    session: AsyncSession = Depends(async_session),
) -> ServiceRead:
    svc = ServiceService(session)
    obj = await svc.create_service(payload)
    return ServiceRead.model_validate(obj)


@router.get("", response_model=ListResponse[ServiceRead])
async def list_services(
    session: AsyncSession = Depends(async_session),
) -> ListResponse[ServiceRead]:
    """The service catalogue, joined with live health from the simulator.

    The DB owns identity (a stable UUID per service, which incidents reference);
    the simulator owns telemetry. Returning only DB rows left every card
    showing "未知" for a service the simulator was happily measuring, and the
    old fallback — used only when the DB was empty — read its numbers out of a
    backend-local mock module rather than the simulator the Agent actually
    queries.

    A simulator that cannot be reached is not an error here: the catalogue is
    still correct, it just has no telemetry to show.
    """
    svc = ServiceService(session)
    rows = list(await svc.list_services())

    try:
        live = await get_providers().extra["infra"].list_services()
    except HttpError:
        live = []
    telemetry = {str(r.get("service")): r for r in live}

    items: list[ServiceRead] = []
    seen: set[str] = set()
    for row in rows:
        seen.add(row.name)
        items.append(_with_telemetry(row.id, row.name, row.description, telemetry.get(row.name)))

    # Services the simulator knows about but the catalogue has never persisted
    # (nothing has referenced them yet) are still part of the environment.
    for name, card in telemetry.items():
        if name in seen:
            continue
        items.append(
            _with_telemetry(
                uuid.uuid5(uuid.NAMESPACE_URL, f"svc://{name}"),
                name,
                None,
                card,
            )
        )

    items.sort(key=lambda s: s.name)
    return ListResponse(items=items, total=len(items), offset=0, limit=len(items) or 50)


def _with_telemetry(
    service_id: uuid.UUID,
    name: str,
    description: str | None,
    card: dict | None,
) -> ServiceRead:
    """Merge one catalogue row with whatever the simulator reports for it."""
    if card is None:
        return ServiceRead(
            id=service_id,
            name=name,
            description=description,
            # No verdict recorded. Left unset rather than defaulted to
            # "healthy" so the UI can say "—" instead of inventing a green dot.
            health=None,
        )

    memory_mb = card.get("memory_mb")
    memory_limit = card.get("memory_limit_mb")
    memory_pct: float | None = None
    if isinstance(memory_mb, int | float) and isinstance(memory_limit, int | float):
        if memory_limit:
            memory_pct = round(memory_mb / memory_limit * 100, 1)

    return ServiceRead(
        id=service_id,
        name=name,
        description=description or _describe(card),
        health=card.get("health"),
        error_rate=card.get("error_rate"),
        latency_p95=card.get("latency_p95_ms"),
        latency_p99=None,
        cpu_usage=card.get("cpu_percent"),
        memory_usage=memory_pct,
        version=card.get("version"),
    )


def _describe(card: dict) -> str:
    parts: list[str] = []
    kind = card.get("kind")
    if kind:
        parts.append(str(kind))
    replicas = card.get("replicas")
    if replicas:
        parts.append(f"{replicas} 副本")
    faults = card.get("active_faults") or []
    if faults:
        parts.append("故障：" + "、".join(str(f) for f in faults))
    if card.get("circuit_breaker_open"):
        parts.append("熔断已打开")
    return " · ".join(parts)


@router.get("/{service_id}", response_model=ServiceRead)
async def get_service(
    service_id: uuid.UUID,
    session: AsyncSession = Depends(async_session),
) -> ServiceRead:
    svc = ServiceService(session)
    obj = await svc.get_service(service_id)
    return ServiceRead.model_validate(obj)
