"""Incident aggregate repository — the only place incidents are persisted."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opspilot_backend.domain.enums import (
    ActorType,
    IncidentStatus,
    ALLOWED_TRANSITIONS,
)
from opspilot_backend.domain.errors import InvalidStateTransitionError
from opspilot_backend.models import (
    AgentRun,
    Approval,
    Deployment,
    Evidence,
    Hypothesis,
    Incident,
    IncidentEvent,
    Postmortem,
    RecoveryAction,
    RecoveryPlan,
    Service,
    VerificationResult,
)
from opspilot_backend.repositories.base import (
    Page,
    apply_filters,
    apply_sorting,
    paginate,
)


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class IncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- reads ---------------------------------------------------------
    async def get(self, incident_id: Any) -> Incident | None:
        key = _as_uuid(incident_id)
        # populate_existing: the agent mutates incidents from its own session,
        # so an identity-map hit must not serve stale status/root cause.
        stmt = (
            select(Incident)
            .where(Incident.id == key)
            .options(selectinload(Incident.service))
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(stmt)

    async def get_or_raise(self, incident_id: Any) -> Incident:
        incident = await self.get(incident_id)
        if incident is None:
            from opspilot_backend.domain.errors import NotFoundError

            raise NotFoundError("Incident", incident_id)
        return incident

    async def list(
        self,
        *,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
    ) -> Page[Incident]:
        stmt = select(Incident).options(selectinload(Incident.service))
        stmt = apply_filters(stmt, Incident, filters or {})
        stmt = apply_sorting(stmt, Incident, sort)
        items, total = await paginate(
            self.session, stmt, page=page, page_size=page_size
        )
        return Page(items=list(items), total=total, page=page, page_size=page_size)

    async def update(self, incident: Incident, **changes: Any) -> Incident:
        """Apply a partial update.

        ``resolved_at=None`` must reach the database as NULL — plain setattr
        on an ORM instance does exactly that, unlike ``exclude_unset``
        payloads that silently drop the key.
        """
        for field, value in changes.items():
            setattr(incident, field, value)
        await self.session.flush()
        return incident

    async def delete(self, incident: Incident) -> None:
        await self.session.delete(incident)
        await self.session.flush()

    async def count_all(self) -> int:
        from sqlalchemy import func

        return int(await self.session.scalar(select(func.count()).select_from(Incident)) or 0)

    # -- writes --------------------------------------------------------
    async def create(
        self,
        *,
        title: str,
        service_id: Any,
        severity: str,
        description: str | None = None,
        scenario: str | None = None,
    ) -> Incident:
        incident = Incident(
            title=title,
            description=description,
            service_id=_as_uuid(service_id),
            severity=severity,
            status=IncidentStatus.CREATED.value,
            scenario=scenario,
            detected_at=datetime.utcnow(),
        )
        self.session.add(incident)
        await self.session.flush()
        self._append_event_sync(incident, "incident.created", f"Incident created: {title}")
        await self.session.flush()
        return incident

    def _append_event_sync(
        self,
        incident: Incident,
        event_type: str,
        summary: str,
        *,
        actor: str = "system",
        actor_type: ActorType = ActorType.SYSTEM,
        data: dict[str, Any] | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> IncidentEvent:
        event = IncidentEvent(
            incident_id=incident.id,
            event_type=event_type,
            summary=summary,
            actor=actor,
            actor_type=actor_type,
            from_status=from_status,
            to_status=to_status,
            data=data,
            created_at=datetime.utcnow(),
        )
        self.session.add(event)
        return event

    async def append_event(
        self,
        incident_id: Any,
        event_type: str,
        summary: str,
        *,
        actor: str = "system",
        actor_type: ActorType = ActorType.SYSTEM,
        data: dict[str, Any] | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> IncidentEvent:
        event = IncidentEvent(
            incident_id=_as_uuid(incident_id),
            event_type=event_type,
            summary=summary,
            actor=actor,
            actor_type=actor_type,
            from_status=from_status,
            to_status=to_status,
            data=data,
            created_at=datetime.utcnow(),
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def transition(
        self,
        incident: Incident,
        target: IncidentStatus,
        *,
        actor: str = "system",
        actor_type: ActorType = ActorType.SYSTEM,
        summary: str = "",
        data: dict[str, Any] | None = None,
    ) -> Incident:
        current = IncidentStatus(incident.status)
        if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise InvalidStateTransitionError(current, target)

        previous = current.value
        incident.status = target.value
        if target == IncidentStatus.RESOLVED and incident.resolved_at is None:
            incident.resolved_at = datetime.utcnow()
        if target == IncidentStatus.CLOSED and incident.closed_at is None:
            incident.closed_at = datetime.utcnow()
        if previous == IncidentStatus.RESOLVED.value and target != IncidentStatus.CLOSED:
            # Re-opening a resolved incident (re-investigation): the previous
            # resolution timestamp no longer describes this episode.
            incident.resolved_at = None

        await self.append_event(
            incident.id,
            "incident.status_changed",
            summary or f"{previous} → {target.value}",
            actor=actor,
            actor_type=actor_type,
            data=data,
            from_status=previous,
            to_status=target.value,
        )
        await self.session.flush()
        return incident

    # -- related collections -------------------------------------------
    async def timeline(self, incident_id: Any) -> list[IncidentEvent]:
        stmt = (
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == _as_uuid(incident_id))
            .order_by(IncidentEvent.created_at.asc(), IncidentEvent.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def evidence(self, incident_id: Any) -> list[Evidence]:
        stmt = (
            select(Evidence)
            .where(Evidence.incident_id == _as_uuid(incident_id))
            .order_by(Evidence.ref.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def hypotheses(self, incident_id: Any) -> list[Hypothesis]:
        stmt = (
            select(Hypothesis)
            .where(Hypothesis.incident_id == _as_uuid(incident_id))
            .order_by(Hypothesis.ref.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def agent_runs(self, incident_id: Any) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.incident_id == _as_uuid(incident_id))
            .order_by(AgentRun.started_at.desc().nullslast(), AgentRun.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def recovery_plans(self, incident_id: Any) -> list[RecoveryPlan]:
        stmt = (
            select(RecoveryPlan)
            .options(selectinload(RecoveryPlan.actions))
            .where(RecoveryPlan.incident_id == _as_uuid(incident_id))
            .order_by(RecoveryPlan.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def latest_recovery_plan(self, incident_id: Any) -> RecoveryPlan | None:
        plans = await self.recovery_plans(incident_id)
        return plans[0] if plans else None

    async def verification_results(self, incident_id: Any) -> list[VerificationResult]:
        stmt = (
            select(VerificationResult)
            .where(VerificationResult.incident_id == _as_uuid(incident_id))
            .order_by(VerificationResult.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def approvals(self, incident_id: Any) -> list[Approval]:
        stmt = (
            select(Approval)
            .where(Approval.incident_id == _as_uuid(incident_id))
            .order_by(Approval.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def postmortem(self, incident_id: Any) -> Postmortem | None:
        stmt = select(Postmortem).where(
            Postmortem.incident_id == _as_uuid(incident_id)
        )
        return await self.session.scalar(stmt)

    async def recovery_actions(self, plan_id: Any) -> list[RecoveryAction]:
        stmt = (
            select(RecoveryAction)
            .where(RecoveryAction.plan_id == _as_uuid(plan_id))
            .order_by(RecoveryAction.order.asc())
        )
        return list((await self.session.execute(stmt)).scalars())


class ServiceRepository:
    """Catalogue of monitored services."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, service_id: Any) -> Service | None:
        try:
            key = _as_uuid(service_id)
        except (ValueError, AttributeError, TypeError):
            return None
        return await self.session.get(Service, key)

    async def get_by_name(self, name: str) -> Service | None:
        stmt = select(Service).where(Service.name == name)
        return await self.session.scalar(stmt)

    async def list_all(self) -> list[Service]:
        stmt = select(Service).order_by(Service.name.asc())
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, *, name: str, description: str | None = None) -> Service:
        service = Service(name=name, description=description)
        self.session.add(service)
        await self.session.flush()
        return service


class DeploymentRepository:
    """Deployment records — the agent's deployment-correlation input."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        service_id: Any,
        version: str,
        previous_version: str | None = None,
        status: str = "DEPLOYING",
        deployer: str | None = None,
    ) -> Deployment:
        deployment = Deployment(
            service_id=_as_uuid(service_id),
            version=version,
            previous_version=previous_version,
            status=status,
            deployer=deployer,
        )
        self.session.add(deployment)
        await self.session.flush()
        return deployment

    async def get_by_service(self, service_id: Any) -> list[Deployment]:
        stmt = (
            select(Deployment)
            .where(Deployment.service_id == _as_uuid(service_id))
            .order_by(Deployment.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_latest_by_service(self, service_id: Any) -> Deployment | None:
        rows = await self.get_by_service(service_id)
        return rows[0] if rows else None
