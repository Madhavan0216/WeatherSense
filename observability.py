"""Structured logging, request correlation, and simple Prometheus-style metrics."""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Any

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id",
    default="",
)


class JsonFormatter(logging.Formatter):
    """Serialize log records as JSON for machine-readable ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": get_correlation_id() or None,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def new_correlation_id() -> str:
    cid = str(uuid.uuid4())
    _correlation_id.set(cid)
    return cid


def set_correlation_id(correlation_id: str) -> None:
    _correlation_id.set(correlation_id)


def get_correlation_id() -> str:
    return _correlation_id.get()


class Metrics:
    """Minimal in-process metrics registry rendered in Prometheus text format."""

    def __init__(self) -> None:
        self.counters: dict[str, float] = {
            "wms_requests_total": 0,
            "wms_ingest_total": 0,
            "wms_ingest_failed_total": 0,
            "wms_payload_emitted_total": 0,
        }
        self.gauges: dict[str, float] = {
            "wms_ready": 0,
            "wms_uptime_seconds": 0,
        }
        self.start_ts = time.time()

    def inc(self, name: str, amount: float = 1.0) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def render_prometheus(self) -> str:
        self.gauges["wms_uptime_seconds"] = max(0.0, time.time() - self.start_ts)
        lines: list[str] = []
        for key, value in self.counters.items():
            lines.append(f"# TYPE {key} counter")
            lines.append(f"{key} {value}")
        for key, value in self.gauges.items():
            lines.append(f"# TYPE {key} gauge")
            lines.append(f"{key} {value}")
        return "\n".join(lines) + "\n"
