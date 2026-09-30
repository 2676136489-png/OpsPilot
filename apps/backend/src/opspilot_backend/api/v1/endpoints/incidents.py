"""Incident endpoints — create, read, list, update, delete."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.db.session import async_session
from opspilot_backend.models import Incident
from opspilot_backend.repositories.incident import IncidentRepository
from opspilot_backend.schemas.incident import (
    IncidentCreate,
    IncidentRead,
    IncidentUpdate,
    ListResponse,
)
from opspilot_backend.services.incident import IncidentService

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.post("", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
async def create_incident(
    payload: IncidentCreate,
    session: AsyncSession = Depends(async_session),
) -> IncidentRead:
    svc = IncidentService(session)
    inc = await svc.create_incident(payload)
    return IncidentRead.model_validate(inc)


@router.get("", response_model=ListResponse[IncidentRead])
async def list_incidents(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    severity: Optional[str] = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(async_session),
) -> ListResponse[IncidentRead]:
    """List incidents, newest first.

    The list is the database, full stop. It used to be the database *plus* a
    set of in-memory "synthetic" incidents held by the simulator module, which
    meant an injected fault appeared here while being invisible to every other
    endpoint — including the one the Agent reads. Injection now writes a real
    row, so this endpoint has nothing to merge.
    """
    svc = IncidentService(session)
    items, total = await svc.list_incidents(
        status_filter=status_filter,
        severity_filter=severity,
        offset=offset,
        limit=limit,
    )
    return ListResponse(
        items=[IncidentRead.model_validate(i) for i in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{incident_id}", response_model=IncidentRead)
async def get_incident(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(async_session),
) -> IncidentRead:
    svc = IncidentService(session)
    inc = await svc.get_incident(incident_id)
    return IncidentRead.model_validate(inc)


@router.get("/{incident_id}/events")
async def get_incident_timeline(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(async_session),
) -> dict:
    """The incident's business timeline, oldest first.

    Every status change, diagnosis, approval and resolution appends a row
    here. The dashboard used to assemble its "what happened" list out of
    ``created_at`` / ``resolved_at`` and a couple of run timestamps, which
    could only ever describe the handful of moments that happen to have their
    own column — the intermediate transitions were invisible.
    """
    inc = await session.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(
            status_code=404, detail=f"Incident {incident_id} not found"
        )
    rows = await IncidentRepository(session).timeline(incident_id)
    return {
        "incident_id": str(incident_id),
        "count": len(rows),
        "events": [
            {
                "id": str(row.id),
                "event_type": row.event_type,
                "summary": row.summary,
                "actor": row.actor,
                "actor_type": getattr(row.actor_type, "value", str(row.actor_type)),
                "from_status": row.from_status,
                "to_status": row.to_status,
                "data": row.data or {},
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.patch("/{incident_id}", response_model=IncidentRead)
async def update_incident(
    incident_id: uuid.UUID,
    payload: IncidentUpdate,
    session: AsyncSession = Depends(async_session),
) -> IncidentRead:
    svc = IncidentService(session)
    inc = await svc.update_incident(incident_id, payload)
    return IncidentRead.model_validate(inc)


@router.delete("/{incident_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_incident(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(async_session),
) -> None:
    svc = IncidentService(session)
    await svc.delete_incident(incident_id)
