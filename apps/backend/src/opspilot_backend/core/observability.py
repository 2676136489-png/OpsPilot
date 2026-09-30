"""Lightweight in-process metrics collector for OpsPilot.

This module provides a zero-dependency metrics system that mirrors the
core concepts of OpenTelemetry Metrics (counters, gauges, histograms).
When an OpenTelemetry SDK is later installed this shim can be swapped in
for OTel's real ``MeterProvider`` / ``Counter`` / ``Histogram`` APIs
without touching the call sites — the public surface stays identical.

Thread safety: every public method acquires a :class:`threading.Lock`,
so the collector is safe to call from FastAPI request handlers, the
agent streaming task, or any other coroutine / thread.

Prometheus exposition
---------------------
:meth:`MetricsCollector.prometheus_exposition` serialises all registered
metrics in the Prometheus text exposition format that the bundled
``/metrics`` endpoint serves.
"""

from __future__ import annotations

import functools
import threading
import time
from collections import defaultdict
from typing import Any, Callable


def _sanitize_label(value: Any) -> str:
    """Escape a label value for Prometheus exposition format."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return text


def _label_key(labels: dict[str, Any] | None) -> str:
    """Return a deterministic string key for a labels dict."""
    if not labels:
        return ""
    return ",".join(f"{k}={_sanitize_label(v)}" for k, v in sorted(labels.items()))


class MetricsCollector:
    """Collects in-process counters/gauges/histograms that the Prometheus
    endpoint exposes."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        # name -> label_key -> value
        self.counters: dict[str, dict[str, int]] = defaultdict(dict)
        self.gauges: dict[str, dict[str, float]] = defaultdict(dict)
        self.histograms: dict[str, dict[str, list[float]]] = defaultdict(dict)

    # ----- Counters ----------------------------------------------------

    def increment(
        self,
        name: str,
        value: int = 1,
        labels: dict[str, Any] | None = None,
    ) -> None:
        """Atomically add ``value`` to counter ``name``."""
        if value < 0:
            raise ValueError(f"Counter increment must be non-negative (got {value})")
        key = _label_key(labels)
        with self._lock:
            self.counters[name][key] = self.counters[name].get(key, 0) + value

    # ----- Gauges ------------------------------------------------------

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, Any] | None = None,
    ) -> None:
        """Set gauge ``name`` to ``value``."""
        key = _label_key(labels)
        with self._lock:
            self.gauges[name][key] = float(value)

    def inc_gauge(
        self,
        name: str,
        value: float = 1.0,
        labels: dict[str, Any] | None = None,
    ) -> None:
        """Add ``value`` to gauge ``name``."""
        key = _label_key(labels)
        with self._lock:
            self.gauges[name][key] = self.gauges[name].get(key, 0.0) + float(value)

    def dec_gauge(
        self,
        name: str,
        value: float = 1.0,
        labels: dict[str, Any] | None = None,
    ) -> None:
        """Subtract ``value`` from gauge ``name``."""
        self.inc_gauge(name, -abs(value), labels)

    # ----- Histograms --------------------------------------------------

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, Any] | None = None,
    ) -> None:
        """Record a sample in histogram ``name``."""
        key = _label_key(labels)
        with self._lock:
            bucket = self.histograms[name].setdefault(key, [])
            if len(bucket) >= 10_000:
                # Drop oldest samples to bound memory usage
                del bucket[: len(bucket) - 10_000 + 1]
            bucket.append(float(value))

    # ----- Helpers -----------------------------------------------------

    def counter_value(self, name: str, labels: dict[str, Any] | None = None) -> int:
        """Return the current value of counter ``name`` with ``labels``."""
        key = _label_key(labels)
        with self._lock:
            return self.counters.get(name, {}).get(key, 0)

    def gauge_value(self, name: str, labels: dict[str, Any] | None = None) -> float:
        """Return the current value of gauge ``name`` with ``labels``."""
        key = _label_key(labels)
        with self._lock:
            return self.gauges.get(name, {}).get(key, 0.0)

    def histogram_samples(
        self, name: str, labels: dict[str, Any] | None = None
    ) -> list[float]:
        """Return a copy of recorded histogram samples for ``name``."""
        key = _label_key(labels)
        with self._lock:
            return list(self.histograms.get(name, {}).get(key, []))

    # ----- Prometheus exposition ---------------------------------------

    def prometheus_exposition(self) -> str:
        """Format counters/gauges/histograms in Prometheus text exposition format."""
        lines: list[str] = []

        with self._lock:
            # Counters
            for name, label_map in sorted(self.counters.items()):
                lines.append(f"# HELP {name} {name}")
                lines.append(f"# TYPE {name} counter")
                for key, value in sorted(label_map.items()):
                    suffix = f"{{{key}}}" if key else ""
                    lines.append(f"{name}{suffix} {value}")

            # Gauges
            for name, label_map in sorted(self.gauges.items()):
                lines.append(f"# HELP {name} {name}")
                lines.append(f"# TYPE {name} gauge")
                for key, value in sorted(label_map.items()):
                    suffix = f"{{{key}}}" if key else ""
                    lines.append(f"{name}{suffix} {value}")

            # Histograms (emit as _count + _sum)
            for name, label_map in sorted(self.histograms.items()):
                lines.append(f"# HELP {name} {name}")
                lines.append(f"# TYPE {name} histogram")
                for key, samples in sorted(label_map.items()):
                    count = len(samples)
                    total = sum(samples)
                    suffix = f"{{{key}}}" if key else ""
                    lines.append(f"{name}_count{suffix} {count}")
                    lines.append(f"{name}_sum{suffix} {total:.6f}")

        return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# Singleton collector + decorator
# ---------------------------------------------------------------------------

metrics = MetricsCollector()
"""Application-wide metrics collector — import this instance anywhere."""


def timed(name: str, labels_fn: Callable[[tuple, dict], dict[str, Any] | None] | None = None) -> Callable[..., Any]:
    """Decorator that records execution duration of a function in a histogram.

    Parameters
    ----------
    name:
        Metric name (Prometheus naming conventions apply).
    labels_fn:
        Optional callable that receives ``(args, kwargs)`` from the decorated
        function and returns a ``dict`` of labels or ``None``.

    Example
    -------

    .. code-block:: python

        @timed("agent_duration_seconds", lambda a, kw: {"scenario": kw.get("scenario")})
        async def run_agent(scenario: str | None = None): ...

    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.monotonic() - start
                labels = labels_fn(args, kwargs) if labels_fn else None
                metrics.observe_histogram(name, duration, labels)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.monotonic() - start
                labels = labels_fn(args, kwargs) if labels_fn else None
                metrics.observe_histogram(name, duration, labels)

        # Pick wrapper based on whether the original is async
        import inspect

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


__all__ = [
    "MetricsCollector",
    "metrics",
    "timed",
]
