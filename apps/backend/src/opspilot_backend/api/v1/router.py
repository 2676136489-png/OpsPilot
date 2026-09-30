"""API v1 router — aggregates all endpoint routers under /api/v1."""

from fastapi import APIRouter

from opspilot_backend.api.v1.endpoints import (
    agent,
    deployments,
    health,
    incidents,
    metrics as metrics_endpoint,
    services,
    simulator,
)

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(incidents.router)
api_v1_router.include_router(services.router)
api_v1_router.include_router(deployments.router)
api_v1_router.include_router(agent.router)
api_v1_router.include_router(simulator.router)
api_v1_router.include_router(metrics_endpoint.router)
