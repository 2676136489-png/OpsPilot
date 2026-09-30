"""Prometheus-compatible ``/metrics`` endpoint."""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from opspilot_backend.core.observability import metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
async def get_metrics() -> PlainTextResponse:
    """Expose application metrics in Prometheus text exposition format."""
    body = metrics.prometheus_exposition()
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
