"""Structured JSON logging with request/trace context."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")
_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")
_RUN_ID: ContextVar[str] = ContextVar("run_id", default="-")


def get_request_id() -> str:
    return _REQUEST_ID.get()


def set_request_id(value: str) -> None:
    _REQUEST_ID.set(value)


def get_trace_id() -> str:
    return _TRACE_ID.get()


def set_trace_id(value: str) -> None:
    _TRACE_ID.set(value)


def set_run_id(value: str) -> None:
    _RUN_ID.set(value)


def get_run_id() -> str:
    return _RUN_ID.get()


def new_trace_id() -> str:
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    """One JSON object per line — greppable, ingestible, no multi-line stacks."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _REQUEST_ID.get(),
            "trace_id": _TRACE_ID.get(),
            "run_id": _RUN_ID.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # uvicorn/httpx would otherwise double-print in plain text
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx"):
        lg = logging.getLogger(noisy)
        lg.handlers.clear()
        lg.propagate = True


def log_event(message: str, **fields: Any) -> None:
    """Emit a structured line with arbitrary extra fields."""
    logging.getLogger("opspilot").info(message, extra={"extra_fields": fields})
