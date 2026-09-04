"""Minimal real-time event stream protocol (protocol_schema_version: 1).

Emits single-line NDJSON frames to stdout or a provided sink for long-running
operations (stress, mutation, judge), with bounded rate-limiting to protect I/O.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable, Iterator, TextIO

PROTOCOL_SCHEMA_VERSION = 1
DEFAULT_THROTTLE_INTERVAL_SECONDS = 0.1


class EventSink:
    """Thread-safe event sink with bounded time-throttling for progress frames."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        callback: Callable[[dict[str, Any]], None] | None = None,
        min_interval_seconds: float = DEFAULT_THROTTLE_INTERVAL_SECONDS,
    ):
        self._stream = stream or sys.stdout
        self._callback = callback
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._last_progress_time: float = 0.0
        self._emitted_started = False
        self._emitted_final = False
        self._emitted_cancelled = False

    @property
    def has_emitted_final(self) -> bool:
        with self._lock:
            return self._emitted_final

    @property
    def has_emitted_cancelled(self) -> bool:
        with self._lock:
            return self._emitted_cancelled

    @property
    def has_emitted_terminal(self) -> bool:
        with self._lock:
            return self._emitted_final or self._emitted_cancelled

    def emit_raw(self, event: dict[str, Any]) -> None:
        """Serialize and emit one raw event dictionary."""
        payload = json.dumps(event, ensure_ascii=False)
        with self._lock:
            callback = self._callback
            stream = self._stream
        if callback is not None:
            callback(event)
        if stream is not None:
            stream.write(payload + "\n")
            try:
                stream.flush()
            except (OSError, ValueError):
                pass

    def emit_started(
        self,
        operation: str,
        problem_id: str,
        *,
        total: int | None = None,
        unit: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit the initial started event frame."""
        event: dict[str, Any] = {
            "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
            "type": "started",
            "operation": operation,
            "problem_id": problem_id,
            "timestamp": time.time(),
        }
        if total is not None:
            event["total"] = total
        if unit is not None:
            event["unit"] = unit
        if metadata:
            event["metadata"] = metadata
        with self._lock:
            self._emitted_started = True
            self._last_progress_time = time.monotonic()
        self.emit_raw(event)

    def emit_progress(
        self,
        operation: str,
        problem_id: str,
        completed: int,
        *,
        total: int | None = None,
        unit: str | None = None,
        detail: dict[str, Any] | None = None,
        force: bool = False,
    ) -> bool:
        """Emit a progress event if enough time has elapsed or force is True.

        Returns True if the event was emitted, False if throttled.
        """
        now = time.monotonic()
        with self._lock:
            if not force and (now - self._last_progress_time < self._min_interval_seconds):
                if total is None or completed < total:
                    return False
            self._last_progress_time = now

        event: dict[str, Any] = {
            "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
            "type": "progress",
            "operation": operation,
            "problem_id": problem_id,
            "completed": completed,
            "timestamp": time.time(),
        }
        if total is not None:
            event["total"] = total
        if unit is not None:
            event["unit"] = unit
        if detail:
            event["detail"] = detail
        self.emit_raw(event)
        return True

    def emit_final(
        self,
        operation: str,
        problem_id: str,
        ok: bool,
        status: str,
        result: dict[str, Any] | None = None,
        *,
        code: str | None = None,
    ) -> None:
        """Emit the terminal final event frame."""
        event: dict[str, Any] = {
            "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
            "type": "final",
            "operation": operation,
            "problem_id": problem_id,
            "ok": bool(ok),
            "status": status,
            "timestamp": time.time(),
        }
        if code is not None:
            event["code"] = code
        elif result is not None and "code" in result and isinstance(result["code"], str):
            event["code"] = result["code"]
        if result is not None:
            event["result"] = result
        with self._lock:
            self._emitted_final = True
        self.emit_raw(event)

    def emit_cancelled(
        self,
        operation: str,
        problem_id: str,
        *,
        completed: int | None = None,
        total: int | None = None,
        reason: str = "cancellation_requested",
    ) -> None:
        """Emit a cancellation event frame."""
        event: dict[str, Any] = {
            "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
            "type": "cancelled",
            "operation": operation,
            "problem_id": problem_id,
            "reason": reason,
            "timestamp": time.time(),
        }
        if completed is not None:
            event["completed"] = completed
        if total is not None:
            event["total"] = total
        with self._lock:
            self._emitted_cancelled = True
        self.emit_raw(event)


def parse_event_stream_line(line: str) -> dict[str, Any] | None:
    """Parse a single NDJSON event stream line.

    Returns the parsed event dict if it matches protocol_schema_version: 1,
    or None if the line is empty, invalid JSON, or not an event frame.
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("{") or not stripped.endswith("}"):
        return None
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("protocol_schema_version") != PROTOCOL_SCHEMA_VERSION:
        return None
    if not isinstance(data.get("type"), str):
        return None
    return data


def parse_event_stream(lines: Iterator[str] | list[str]) -> Iterator[dict[str, Any]]:
    """Yield all valid event stream frames from an iterable of lines."""
    for line in lines:
        frame = parse_event_stream_line(line)
        if frame is not None:
            yield frame
