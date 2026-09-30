"""Deployment endpoints."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.db.session import async_session
from opspilot_backend.models import Deployment as DeploymentModel
from opspilot_backend.schemas.incident import (
    DeploymentCreate,
    DeploymentRead,
    ListResponse,
)
from opspilot_backend.services.incident import DeploymentService

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.post("", response_model=DeploymentRead, status_code=status.HTTP_201_CREATED)
async def create_deployment(
    payload: DeploymentCreate,
    session: AsyncSession = Depends(async_session),
) -> DeploymentRead:
    svc = DeploymentService(session)
    dep = await svc.create_deployment(payload)
    return DeploymentRead.model_validate(dep)


@router.get("", response_model=ListResponse[DeploymentRead])
async def list_deployments(
    service_id: Optional[uuid.UUID] = Query(default=None),
    session: AsyncSession = Depends(async_session),
) -> ListResponse[DeploymentRead]:
    if service_id is not None:
        svc = DeploymentService(session)
        items = await svc.list_by_service(service_id)
    else:
        # No filter — latest 50 across all services
        stmt = (
            select(DeploymentModel)
            .order_by(DeploymentModel.created_at.desc())
            .limit(50)
        )
        result = await session.execute(stmt)
        items = list(result.scalars().all())
    return ListResponse(
        items=[DeploymentRead.model_validate(d) for d in items],
        total=len(items),
        offset=0,
        limit=len(items) or 50,
    )


@router.get("/latest", response_model=Optional[DeploymentRead])
async def get_latest_deployment(
    service_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(async_session),
) -> Optional[DeploymentRead]:
    svc = DeploymentService(session)
    dep = await svc.get_latest(service_id)
    if dep is None:
        return None
    return DeploymentRead.model_validate(dep)
