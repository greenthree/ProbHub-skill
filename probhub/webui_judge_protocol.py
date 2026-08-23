"""Bounded Judge JSONL protocol consumption shared by WebUI adapters.

The local Judge is intentionally a line-oriented subprocess protocol.  This
module owns the pure state transitions applied to that protocol; Flask and
other adapters only provide a bounded log sink and choose the byte budgets.
No filesystem, process, or WebUI state is accessed here.
"""

from __future__ import annotations

import json
from typing import Any, MutableMapping


DEFAULT_EVENT_BYTES = 2 * 1024 * 1024
DEFAULT_FINAL_EVENT_BYTES = 64 * 1024


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def empty_sandbox_result(info: Any) -> dict[str, Any]:
    """Create the stable result shape consumed by the WebUI.

    ``info`` is retained as supplied so callers can attach the same validated
    Schema v1 problem description to queued, running, and completed results.
    """

    return {
        "problem": info,
        "limits": info.get("limits", {"time": 1, "memory": 256})
        if isinstance(info, dict)
        else {"time": 1, "memory": 256},
        "compiles": [],
        "validator": [],
        "cases": [],
        "groups": [],
        "expectations": {},
        "summaries": {},
        "cache": {},
        "transcripts": [],
        "final": None,
    }


def _failure(
    result: MutableMapping[str, Any],
    code: str,
    message: str,
    *,
    status: str = "failed",
) -> None:
    result["final"] = {
        "ok": False,
        "status": status,
        "code": code,
        "message": message,
    }


def apply_sandbox_event(result: MutableMapping[str, Any], event: dict[str, Any]) -> None:
    """Apply one validated JSONL event to the stable WebUI result shape."""

    typ = event.get("type")
    if typ == "limits":
        result["limits"] = {
            "time": event.get("time_limit", 1),
            "memory": event.get("memory_limit", 256),
            "output": event.get("output_limit", 64),
            "processes": event.get("process_limit", 32),
            "judge_type": event.get("judge_type", "standard"),
            "idle_limit": event.get("idle_limit"),
            "transcript_limit": event.get("transcript_limit"),
            "platform": event.get("platform"),
            "machine": event.get("machine"),
            "target_guarantee": event.get("target_guarantee", False),
            "measurement_note": event.get("measurement_note"),
        }
    elif typ == "groups":
        result["groups"] = event.get("groups") or []
    elif typ == "compile":
        result["compiles"].append(
            {
                "kind": event.get("kind"),
                "file": event.get("file"),
                "ok": event.get("ok"),
                "stderr": event.get("stderr", ""),
                "cached": bool(event.get("cached")),
            }
        )
    elif typ == "validator":
        result["validator"].append(
            {
                "case": event.get("case"),
                "ok": bool(event.get("ok")),
                "cached": bool(event.get("cached")),
            }
        )
    elif typ == "case":
        result["cases"].append(
            {
                "kind": event.get("kind"),
                "program": event.get("program"),
                "case": event.get("case"),
                "groups": event.get("groups") or [],
                "status": event.get("status"),
                "time": _safe_float(event.get("time")),
                "memory": event.get("memory"),
                "time_limit": event.get("time_limit"),
                "memory_limit": event.get("memory_limit"),
                "memory_enforced": event.get("memory_enforced"),
                "judge_type": event.get("judge_type", "standard"),
                "message": event.get("message", ""),
                "timeout_kind": event.get("timeout_kind"),
                "transcript_truncated": bool(event.get("transcript_truncated")),
                "output_bytes": event.get("output_bytes"),
                "termination_reason": event.get("termination_reason"),
                "cached": bool(event.get("cached")),
            }
        )
    elif typ == "transcript":
        result["transcripts"].append(
            {
                "kind": event.get("kind"),
                "program": event.get("program"),
                "case": event.get("case"),
                "entries": event.get("entries") or [],
                "truncated": bool(event.get("truncated")),
                "cached": bool(event.get("cached")),
            }
        )
    elif typ == "summary":
        program = event.get("program") or f"{event.get('kind', 'unknown')}-summary"
        result["summaries"][program] = {
            "kind": event.get("kind"),
            "program": program,
            "stats": event.get("stats", {}),
            "expectation": event.get("expectation") or {},
            "calibration": event.get("calibration") or {},
        }
    elif typ == "expectation":
        program = event.get("program") or f"{event.get('kind', 'unknown')}-expectation"
        result["expectations"][program] = {
            key: value
            for key, value in event.items()
            if key not in {"protocol", "protocol_version", "type"}
        }
    elif typ == "cache":
        result["cache"] = {
            key: value
            for key, value in event.items()
            if key not in {"protocol", "protocol_version", "type"}
        }
    elif typ == "final":
        result["final"] = {
            "ok": bool(event.get("ok")),
            "status": event.get("status", ""),
            "code": event.get("code", ""),
            "message": event.get("message", ""),
        }


def sandbox_log_line(event: dict[str, Any]) -> str:
    """Render one bounded, human-readable event line for the WebUI log."""

    typ = event.get("type")
    if typ == "limits":
        return (
            f"[limits:{event.get('judge_type', 'standard')}] "
            f"{_safe_float(event.get('time_limit', 1), 1):g}s / {event.get('memory_limit', 256)}MB"
        )
    if typ == "groups":
        names = [
            group.get("name")
            for group in (event.get("groups") or [])
            if isinstance(group, dict)
        ]
        return f"[groups] {', '.join(name for name in names if name) or 'none'}"
    if typ == "compile":
        ok = event.get("ok")
        status = "SKIP" if ok is None else ("OK" if ok else "FAIL")
        return f"[compile:{status}] {event.get('kind')} · {event.get('file')}"
    if typ == "validator":
        return f"[validator:{'OK' if event.get('ok') else 'FAIL'}] {event.get('case')}"
    if typ == "case":
        detail = f" · {event.get('message')}" if event.get("message") else ""
        return (
            f"[{event.get('kind')}:{event.get('judge_type', 'standard')}] "
            f"{event.get('program')} / {event.get('case')} -> {event.get('status')} "
            f"({_safe_float(event.get('time')):.3f}s){detail}"
        )
    if typ == "transcript":
        suffix = " truncated" if event.get("truncated") else ""
        return (
            f"[transcript{suffix}] {event.get('program')} / {event.get('case')}: "
            f"{len(event.get('entries') or [])} chunks"
        )
    if typ == "summary":
        return f"[summary] {event.get('program')}: {event.get('stats')}"
    if typ == "expectation":
        state = "PASS" if event.get("ok") else "FAIL"
        first = (
            event.get("first_forbidden")
            or event.get("first_expected_match")
            or event.get("first_non_ac")
        )
        suffix = (
            f" · first={first.get('case')}:{first.get('status')}"
            if isinstance(first, dict)
            else ""
        )
        return (
            f"[expectation:{state}] {event.get('program')} "
            f"status={event.get('expected_statuses') or []} "
            f"groups={event.get('groups') or ['all']}{suffix}"
        )
    if typ == "final":
        return f"[final:{'OK' if event.get('ok') else 'FAIL'}] {event.get('message')}"
    return json.dumps(event, ensure_ascii=False)


def consume_jsonl_chunk(
    result: MutableMapping[str, Any],
    log: Any,
    state: MutableMapping[str, Any],
    chunk: bytes,
    *,
    final: bool = False,
    event_limit_bytes: int = DEFAULT_EVENT_BYTES,
    final_event_limit_bytes: int = DEFAULT_FINAL_EVENT_BYTES,
) -> None:
    """Consume a possibly partial Judge JSONL byte chunk.

    The parser is deliberately fail-closed for malformed JSON, non-object
    events, invalid UTF-8, duplicate/oversized final events, and any later
    event after a sticky protocol error.  Event details are bounded separately
    from the final event so a verbose Judge cannot replace its verdict with an
    unbounded result document.
    """

    event_limit_bytes = int(event_limit_bytes)
    final_event_limit_bytes = int(final_event_limit_bytes)
    if event_limit_bytes <= 0 or final_event_limit_bytes <= 0:
        raise ValueError("JSONL event limits must be positive")
    if not isinstance(chunk, (bytes, bytearray, memoryview)):
        raise TypeError("JSONL chunks must be bytes-like")

    state.setdefault("buffer", b"")
    state.setdefault("event_bytes", 0)
    state.setdefault("final_seen", False)
    state.setdefault("protocol_error", None)
    state["buffer"] += bytes(chunk)
    lines = state["buffer"].split(b"\n")
    state["buffer"] = b"" if final else lines.pop()
    if final and lines and not lines[-1]:
        lines.pop()

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        event_size = len(raw_line) + 1
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_line.decode("utf-8", errors="replace")
            if not state.get("protocol_error"):
                state["protocol_error"] = "invalid_utf8"
                _failure(result, "judge_protocol_error", "judge emitted invalid UTF-8 JSONL")
                log.append(text)
                log.append("[final:FAIL] judge emitted invalid UTF-8 JSONL")
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            if not state.get("protocol_error"):
                state["protocol_error"] = "malformed_json"
                _failure(result, "judge_protocol_error", "judge emitted malformed JSONL")
                log.append(text)
                log.append("[final:FAIL] judge emitted malformed JSONL")
            continue
        if not isinstance(event, dict):
            if not state.get("protocol_error"):
                state["protocol_error"] = "event_not_object"
                _failure(result, "judge_protocol_error", "judge JSONL event must be an object")
                log.append("[final:FAIL] judge JSONL event must be an object")
            continue

        if event.get("type") == "final":
            if state.get("protocol_error"):
                continue
            if state.get("final_seen"):
                state["protocol_error"] = "duplicate_final_event"
                _failure(result, "judge_protocol_error", "judge emitted more than one final event")
                log.append("[final:FAIL] judge emitted more than one final event")
                continue
            state["final_seen"] = True
            if event_size > final_event_limit_bytes:
                state["protocol_error"] = "task_event_limit"
                result["details_truncated"] = True
                _failure(result, "task_event_limit", "judge final event exceeded its limit")
                log.append("[final:FAIL] judge final event exceeded its limit")
                continue
            apply_sandbox_event(result, event)
            log.append(sandbox_log_line(event))
            continue

        if state.get("protocol_error"):
            continue
        if state["event_bytes"] + event_size <= event_limit_bytes:
            apply_sandbox_event(result, event)
            state["event_bytes"] += event_size
            log.append(sandbox_log_line(event))
        else:
            result["details_truncated"] = True
            if not state.get("detail_limit_reported"):
                state["detail_limit_reported"] = True
                log.append("[details truncated: event budget exceeded]")


def finalize_judge_protocol(
    result: MutableMapping[str, Any],
    state: MutableMapping[str, Any],
    return_code: int | None,
    *,
    read_error: bool = False,
) -> str | None:
    """Validate the terminal Judge protocol state after stdout is drained.

    Process cancellation, deadlines, output flooding, and cleanup failures are
    supervisor concerns and must be resolved before this function is called.
    A non-``None`` return value is the stable supervisor stop reason.
    """

    if read_error:
        _failure(result, "judge_protocol_error", "failed to read judge protocol output")
        return "protocol_error"
    if state.get("protocol_error"):
        return "protocol_error"
    final = result.get("final")
    if not isinstance(final, dict):
        _failure(result, "missing_final_event", "judge exited without a final event")
        return "protocol_error"
    if bool(final.get("ok")) != (return_code == 0):
        _failure(result, "judge_protocol_error", "judge final event disagreed with its exit code")
        return "protocol_error"
    return None


def submission_verdict(result: MutableMapping[str, Any]) -> str:
    """Map a completed sandbox result to the WebUI submission verdict."""

    submission_compiles = [
        item for item in (result.get("compiles") or []) if item.get("kind") == "std"
    ]
    if submission_compiles and not submission_compiles[-1].get("ok"):
        return "CE"
    statuses = [
        item.get("status") for item in (result.get("cases") or []) if item.get("status")
    ]
    if statuses and all(status == "AC" for status in statuses):
        return "AC"
    for status in ("FAIL", "RE", "MLE", "OLE", "TLE", "WA"):
        if status in statuses:
            return status
    return "FAIL" if result.get("final") else "PENDING"
