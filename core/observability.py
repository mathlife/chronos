"""Structured logging and in-process metrics for Chronos."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any


class MetricsRegistry:
    """Thread-safe in-memory metrics registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, value: int = 1) -> None:
        if value == 0:
            return
        with self._lock:
            self._counters[name] = int(self._counters.get(name, 0)) + int(value)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counters": counters,
            "gauges": gauges,
        }


METRICS = MetricsRegistry()


def emit_log(event: str, *, level: str = "INFO", **fields: Any) -> None:
    """Emit one structured JSON log line."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "level": level.upper(),
        "event": event,
    }
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
