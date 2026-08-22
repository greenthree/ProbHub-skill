"""Pure verdict and execution-result normalization shared by Judge adapters.

The command-line local judge and the special-Judge runner both observe the
same process-control result shape.  Keeping the mapping in Core prevents a
script or a future WebUI/Judge-QA adapter from inventing a subtly different
interpretation of resource limits, startup failures, or Checker exit codes.

This module deliberately has no filesystem, subprocess, or UI dependencies.
Its functions are deterministic and safe to use from tests, workers, and
protocol adapters.
"""

from __future__ import annotations

from typing import Any, Mapping


PROCESS_CLEANUP_FAILED = "process_cleanup_failed"

RESOURCE_LIMIT_REASONS = frozenset(
    ("time_limit", "memory_limit", "output_limit", "process_limit")
)
CONTROL_FAILURE_REASONS = frozenset(
    ("output_control_error", PROCESS_CLEANUP_FAILED)
)


def infer_failed_status(
    returncode: int | None,
    stderr: str | bytes | None,
    memory_enforced: bool,
    peak_memory_mb: float | int | None,
    memory_limit_mb: float | int,
) -> str:
    """Classify a non-zero process exit using only positive MLE evidence.

    A process that exits non-zero is ``RE`` unless the supervisor enforced a
    memory limit and observed it reaching at least 98% of that limit.  The
    ``stderr`` and ``returncode`` arguments are accepted for a stable adapter
    API and future diagnostics, but are intentionally not treated as proof of
    a resource verdict.
    """

    del returncode, stderr
    try:
        limit = float(memory_limit_mb)
        observed = float(peak_memory_mb) if peak_memory_mb is not None else None
    except (TypeError, ValueError):
        limit = 0.0
        observed = None
    if memory_enforced and observed is not None and observed >= limit * 0.98:
        return "MLE"
    return "RE"


def classify_process_status(
    execution: Mapping[str, Any], memory_limit_mb: float | int
) -> str:
    """Map one shared process-control result to a local Judge status."""

    reason = execution.get("reason")
    if reason == "time_limit":
        return "TLE"
    if reason == "output_limit":
        return "OLE"
    if reason == "memory_limit":
        return "MLE"
    if reason == "process_limit":
        return "RE"
    if reason == PROCESS_CLEANUP_FAILED:
        return "FAIL"
    if execution.get("returncode") != 0:
        return infer_failed_status(
            execution.get("returncode"),
            None,
            bool(execution.get("memory_enforced")),
            execution.get("memory"),
            memory_limit_mb,
        )
    return "AC"


def protocol_outcome(returncode: int | None, message: str | None, actor: str) -> dict[str, Any]:
    """Normalize DOMjudge/testlib Checker or Interactor exit codes."""

    clean_message = (message or "").strip()
    if returncode in {0, 42}:
        return {
            "status": "AC",
            "verdict": "AC",
            "execution_status": "completed",
            "failure_kind": None,
            "actor": "session",
            "termination_reason": "completed",
            "message": clean_message,
        }
    if returncode in {1, 2, 43}:
        return {
            "status": "WA",
            "verdict": "WA",
            "execution_status": "completed",
            "failure_kind": "wrong_answer",
            "actor": "contestant",
            "termination_reason": "completed",
            "message": clean_message,
        }
    return {
        "status": "FAIL",
        "verdict": None,
        "execution_status": "completed",
        "failure_kind": "judge_failure",
        "actor": actor,
        "termination_reason": "completed",
        "message": clean_message or f"{actor} exited with code {returncode}",
    }


def classify_contestant_failure(status: str, termination_reason: str | None) -> dict[str, Any]:
    """Build the stable detail fields for a contestant-side failure."""

    reason = termination_reason or "start_error"
    execution_status = (
        "memory_limit"
        if reason == "inferred_memory_limit"
        else reason
        if reason in {
            "time_limit",
            "memory_limit",
            "output_limit",
            "process_limit",
            "output_control_error",
            PROCESS_CLEANUP_FAILED,
            "start_error",
        }
        else "completed"
    )
    failure_kind = (
        "control_failure"
        if status == "FAIL"
        else "startup_failure"
        if reason == "start_error"
        else "resource_limit"
        if status in {"TLE", "MLE", "OLE"} or reason == "process_limit"
        else "runtime_error"
    )
    return {
        "verdict": status if status != "FAIL" else None,
        "execution_status": execution_status,
        "failure_kind": failure_kind,
        "actor": "supervisor" if status == "FAIL" else "contestant",
        "termination_reason": reason,
    }

