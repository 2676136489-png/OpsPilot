"""Service layer for the Incident domain.

Services orchestrate repositories and own business validation /
cross-object rules.  They never talk to FastAPI — routers handle HTTP and
services raise :class:`AppError` subclasses only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.domain.enums import join_values
from opspilot_backend.domain.errors import BadRequestError, NotFoundError
from opspilot_backend.models import Deployment, Incident, Service
from opspilot_backend.repositories.incident import (
    DeploymentRepository,
    IncidentRepository,
    ServiceRepository,
)
from opspilot_backend.schemas.incident import (
    API_STATUS_BUCKETS,
    DeploymentCreate,
    DOMAIN_SEVERITIES,
    DOMAIN_STATUSES,
    IncidentCreate,
    IncidentUpdate,
    SEVERITY_TO_DOMAIN,
    ServiceCreate,
    STATUS_TO_DOMAIN,
    VALID_DEPLOYMENT_STATUSES,
    VALID_SEVERITIES,
    VALID_STATUSES,
)

# Columns a PATCH payload may touch — everything else the client sends is
# ignored rather than written blind onto the row.
_UPDATABLE_FIELDS = frozenset(
    {"title", "description", "severity", "status", "root_cause", "confidence", "resolved_at"}
)


class ServiceService:
    """Business operations for :class:`Service`."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = ServiceRepository(session)
        self.session = session

    async def create_service(self, payload: ServiceCreate) -> Service:
        existing = await self.repo.get_by_name(payload.name)
        if existing is not None:
            raise BadRequestError(
                f"名为 '{payload.name}' 的服务已经存在。"
            )
        return await self.repo.create(name=payload.name, description=payload.description)

    async def list_services(self) -> list[Service]:
        return list(await self.repo.list_all())

    async def get_service(self, service_id: uuid.UUID) -> Service:
        svc = await self.repo.get_by_id(service_id)
        if svc is None:
            raise NotFoundError("Service", service_id)
        return svc


class IncidentService:
    """Business operations for :class:`Incident`."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = IncidentRepository(session)
        self.session = session
        self.service_repo = ServiceRepository(session)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_domain_severity(severity: str) -> str:
        """Accept either vocabulary, return the domain value.

        Both vocabularies reach this layer: request bodies are translated by
        the schema validators, but query parameters and internal callers arrive
        raw. Accepting only the domain form meant `?severity=critical` was
        rejected as an unknown *domain* value even though the identical string
        was accepted in a JSON body — the operator saw a 400 for the value the
        UI itself had just put in the URL.
        """
        if severity in DOMAIN_SEVERITIES:
            return severity
        domain = SEVERITY_TO_DOMAIN.get(severity)
        if domain is None:
            raise BadRequestError(
                f"severity 取值 '{severity}' 不合法，必须是 "
                f"{join_values(VALID_SEVERITIES | DOMAIN_SEVERITIES)} 之一。"
            )
        return domain

    @staticmethod
    def _to_domain_status(st: str) -> str:
        """Accept either vocabulary, return the domain value."""
        if st in DOMAIN_STATUSES:
            return st
        domain = STATUS_TO_DOMAIN.get(st)
        if domain is None:
            raise BadRequestError(
                f"status 取值 '{st}' 不合法，必须是 "
                f"{join_values(VALID_STATUSES | DOMAIN_STATUSES)} 之一。"
            )
        return domain

    @classmethod
    def _status_filter_values(cls, st: str) -> list[str]:
        """Domain states covered by one UI status bucket.

        `open` is not a state, it is a bucket: CREATED, TRIAGING and FAILED all
        present as "open". Filtering on the single mapped value would hide the
        two thirds of matching incidents that happen to be in another member of
        the bucket.
        """
        return API_STATUS_BUCKETS.get(st) or [cls._to_domain_status(st)]

    # -- CRUD --------------------------------------------------------------

    async def create_incident(self, payload: IncidentCreate) -> Incident:
        # Validate service exists
        svc = await self.service_repo.get_by_id(payload.service_id)
        if svc is None:
            raise BadRequestError(f"服务 {payload.service_id} 不存在。")
        severity = self._to_domain_severity(payload.severity)
        status = self._to_domain_status(payload.status or "CREATED")

        incident = await self.repo.create(
            title=payload.title,
            service_id=payload.service_id,
            severity=severity,
            description=payload.description,
        )
        # The repository's create() is the agent-domain entry point (fixed
        # CREATED status); API callers may additionally seed status, root
        # cause and confidence directly.
        extras: dict[str, Any] = {"status": status}
        extras.update(
            {
                field: value
                for field in ("root_cause", "confidence", "resolved_at")
                if (value := getattr(payload, field)) is not None
            }
        )
        return await self.repo.update(incident, **extras)

    async def get_incident(self, incident_id: uuid.UUID) -> Incident:
        inc = await self.repo.get(incident_id)
        if inc is None:
            raise NotFoundError("Incident", incident_id)
        return inc

    async def list_incidents(
        self,
        status_filter: Optional[str] = None,
        severity_filter: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Incident], int]:
        filters: dict[str, Any] = {}
        if status_filter is not None:
            filters["status"] = self._status_filter_values(status_filter)
        if severity_filter is not None:
            filters["severity"] = self._to_domain_severity(severity_filter)
        # The repository paginates by page/page_size; translate the API's
        # offset/limit so deep slices keep their meaning.
        page = (offset // limit) + 1 if limit else 1
        result = await self.repo.list(
            filters=filters or None, page=page, page_size=limit, sort="-created_at"
        )
        # `result.total` counts the *filtered* rows; `count_all()` counts the
        # table. Returning the latter made the UI's page count describe a
        # different query than the rows it was rendering.
        return result.items, result.total

    async def update_incident(
        self, incident_id: uuid.UUID, payload: IncidentUpdate
    ) -> Incident:
        inc = await self.repo.get(incident_id)
        if inc is None:
            raise NotFoundError("Incident", incident_id)

        update_data = payload.model_dump(exclude_unset=True)
        if "severity" in update_data and update_data["severity"] is not None:
            update_data["severity"] = self._to_domain_severity(update_data["severity"])
        if "status" in update_data and update_data["status"] is not None:
            update_data["status"] = self._to_domain_status(update_data["status"])

        # Auto-set resolved_at when status flips to RESOLVED or FAILED
        new_status = update_data.get("status")
        if new_status in {"RESOLVED", "FAILED"}:
            if inc.resolved_at is None:
                update_data["resolved_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        elif new_status is not None:
            # Reverting out of a terminal state clears resolved_at. This must be
            # an explicit None-aware write (see IncidentRepository.update).
            update_data["resolved_at"] = None

        # Only columns the payload actually carried may change.
        return await self.repo.update(
            inc, **{k: v for k, v in update_data.items() if k in _UPDATABLE_FIELDS}
        )

    async def delete_incident(self, incident_id: uuid.UUID) -> None:
        inc = await self.repo.get(incident_id)
        if inc is None:
            raise NotFoundError("Incident", incident_id)
        await self.repo.delete(inc)


class DeploymentService:
    """Business operations for :class:`Deployment`."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = DeploymentRepository(session)
        self.session = session
        self.service_repo = ServiceRepository(session)

    async def create_deployment(self, payload: DeploymentCreate) -> Deployment:
        svc = await self.service_repo.get_by_id(payload.service_id)
        if svc is None:
            raise BadRequestError(f"服务 {payload.service_id} 不存在。")
        if payload.status not in VALID_DEPLOYMENT_STATUSES:
            raise BadRequestError(
                f"部署状态 '{payload.status}' 不合法，"
                f"必须是 {join_values(VALID_DEPLOYMENT_STATUSES)} 之一。"
            )
        return await self.repo.create(
            service_id=payload.service_id,
            version=payload.version,
            previous_version=payload.previous_version,
            status=payload.status,
            deployer=payload.deployer,
        )

    async def get_latest(self, service_id: uuid.UUID) -> Optional[Deployment]:
        return await self.repo.get_latest_by_service(service_id)

    async def list_by_service(self, service_id: uuid.UUID) -> list[Deployment]:
        svc = await self.service_repo.get_by_id(service_id)
        if svc is None:
            raise BadRequestError(f"服务 {service_id} 不存在。")
        return list(await self.repo.get_by_service(service_id))
