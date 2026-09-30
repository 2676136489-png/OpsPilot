"""Pydantic v2 schemas for the Incident domain.

Literal types are used instead of Python enums to match the string-storage
choice in ORM models and keep JSON round-trips trivial.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

# ---------------------------------------------------------------------------
# Domain constants — exposed as Literal aliases for reuse
# ---------------------------------------------------------------------------

# The API speaks the frontend vocabulary (``critical`` / ``open`` / ...) while
# the domain layer and DB columns use ``SEV1`` / ``CREATED`` style enums. The
# validators below translate in both directions so neither side sees the
# other's vocabulary.

Severity = Literal["critical", "high", "medium", "low"]
IncidentStatus = Literal["open", "investigating", "mitigated", "resolved", "closed"]
DeploymentStatus = Literal["DEPLOYING", "SUCCESSFUL", "FAILED", "ROLLED_BACK"]

VALID_SEVERITIES: set[str] = {"critical", "high", "medium", "low"}
VALID_STATUSES: set[str] = {"open", "investigating", "mitigated", "resolved", "closed"}
VALID_DEPLOYMENT_STATUSES: set[str] = {"DEPLOYING", "SUCCESSFUL", "FAILED", "ROLLED_BACK"}

# API vocabulary -> domain enum values (models/incident.py).
SEVERITY_TO_DOMAIN: dict[str, str] = {
    "critical": "SEV1",
    "high": "SEV2",
    "medium": "SEV3",
    "low": "SEV4",
}
STATUS_TO_DOMAIN: dict[str, str] = {
    "open": "CREATED",
    "investigating": "INVESTIGATING",
    "mitigated": "VERIFYING",
    "resolved": "RESOLVED",
    "closed": "CLOSED",
}
# Domain enum values -> API vocabulary (superset map: every state the agent
# state machine can produce lands in the closest UI bucket).
SEVERITY_TO_API: dict[str, str] = {v: k for k, v in SEVERITY_TO_DOMAIN.items()}

STATUS_TO_API: dict[str, str] = {
    "CREATED": "open",
    "TRIAGING": "open",
    "INVESTIGATING": "investigating",
    "DIAGNOSING": "investigating",
    "WAITING_APPROVAL": "investigating",
    "RECOVERING": "mitigated",
    "ROLLING_BACK": "mitigated",
    "VERIFYING": "mitigated",
    "RESOLVED": "resolved",
    "FAILED": "open",
    "ESCALATED": "investigating",
    "CLOSED": "closed",
}

# Inverse of the above: one UI bucket covers several domain states.
#
# Filtering on the bucket alone (`?status=open` → `CREATED` only) would hide
# every incident that is mid-triage or has failed, which is most of the ones an
# operator is looking for.
API_STATUS_BUCKETS: dict[str, list[str]] = {}
for _domain, _api in STATUS_TO_API.items():
    API_STATUS_BUCKETS.setdefault(_api, []).append(_domain)

# Domain-side membership sets, for services that receive already-converted
# values (post-validator) and need to double-check before persisting.
DOMAIN_SEVERITIES: set[str] = set(SEVERITY_TO_DOMAIN.values())
DOMAIN_STATUSES: set[str] = set(STATUS_TO_DOMAIN.values())


def _validate_severity(v: str) -> str:
    """Validate API severity and translate it to the domain value."""
    if v not in VALID_SEVERITIES:
        raise ValueError(
            f"Invalid severity '{v}'. Must be one of {sorted(VALID_SEVERITIES)}."
        )
    return SEVERITY_TO_DOMAIN[v]


def _validate_status(v: str) -> str:
    """Validate API status and translate it to the domain value."""
    if v not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{v}'. Must be one of {sorted(VALID_STATUSES)}."
        )
    return STATUS_TO_DOMAIN[v]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ServiceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class ServiceCreate(ServiceBase):
    pass


class ServiceRead(ServiceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Live health telemetry. Only simulator-sourced services carry it today;
    # DB-backed rows leave these unset rather than inventing zeroes that would
    # read as "healthy" on the dashboard.
    health: Optional[str] = None
    error_rate: Optional[float] = None
    latency_p95: Optional[float] = None
    latency_p99: Optional[float] = None
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    version: Optional[str] = None


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


class IncidentBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    service_id: uuid.UUID
    severity: str
    status: str = "open"
    root_cause: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    resolved_at: Optional[datetime] = None


class IncidentCreate(BaseModel):
    """Schema for creating a new incident via the API.

    ``severity`` and ``status`` are the only required business fields beyond
    the title and service FK — everything else can be filled in later.
    """

    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    service_id: uuid.UUID
    severity: str
    # `validate_default` is load-bearing, not decoration: pydantic does not run
    # validators on defaults unless asked, so an omitted `status` used to keep
    # the raw literal `"open"` and the service layer then rejected it as an
    # unknown *domain* state — `POST /incidents` returned 500 for exactly the
    # payload its own schema documents as the minimum.
    status: Optional[str] = Field(default="open", validate_default=True)
    root_cause: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    resolved_at: Optional[datetime] = None

    @field_validator("severity")
    @classmethod
    def _check_severity(cls, v: str) -> str:
        return _validate_severity(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_status(v)


class IncidentUpdate(BaseModel):
    """Partial update — every field is optional."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    root_cause: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    resolved_at: Optional[datetime] = None

    @field_validator("severity")
    @classmethod
    def _check_severity(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_severity(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_status(v)


class IncidentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: Optional[str] = None
    service_id: uuid.UUID
    severity: str
    status: str
    root_cause: Optional[str] = None
    confidence: Optional[float] = None
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None

    # Presentation fields. The UI renders the affected service by name and needs
    # the scenario to pass investigation context back to the agent; without them
    # it can only show an opaque service_id. `service_name` (not `service`) is
    # used because the ORM already has a `service` relationship.
    service_name: Optional[str] = None
    scenario: Optional[str] = None

    @field_serializer("severity")
    def _severity_out(self, v: str) -> str:
        return SEVERITY_TO_API.get(v, v)

    @field_serializer("status")
    def _status_out(self, v: str) -> str:
        return STATUS_TO_API.get(v, v)


# ---------------------------------------------------------------------------
# IncidentEvent
# ---------------------------------------------------------------------------


class IncidentEventBase(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=64)
    details: Optional[dict] = None


class IncidentEventCreate(IncidentEventBase):
    pass


class IncidentEventRead(IncidentEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


class DeploymentBase(BaseModel):
    version: str = Field(..., min_length=1, max_length=128)
    previous_version: Optional[str] = None
    status: str
    deployer: Optional[str] = None


class DeploymentCreate(DeploymentBase):
    service_id: uuid.UUID

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in VALID_DEPLOYMENT_STATUSES:
            raise ValueError(
                f"Invalid deployment status '{v}'. "
                f"Must be one of {sorted(VALID_DEPLOYMENT_STATUSES)}."
            )
        return v


class DeploymentRead(DeploymentBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_id: uuid.UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# Generic list wrapper
# ---------------------------------------------------------------------------

T = TypeVar("T")


class ListResponse(BaseModel, Generic[T]):
    """Standard envelope for paginated list responses."""

    items: List[T]
    total: int
    offset: int
    limit: int
