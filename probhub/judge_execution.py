import hashlib
import os
import tempfile
import time

from .judge_cache import calibration_probe_cache_key
from .judge_results import (
    classify_contestant_failure,
    classify_process_status,
    infer_failed_status,
)
from .output_compare import compare_standard_output
from .process_control import (
    DEFAULT_PROCESS_LIMIT,
    OutputBudgetError,
    PROCESS_CLEANUP_FAILED,
    VALIDATOR_MEMORY_LIMIT_MB,
    VALIDATOR_OUTPUT_LIMIT_BYTES,
    VALIDATOR_TIMEOUT_SECONDS,
    run_managed_to_files,
)
from .special_judges import execute_interactive_session, run_checker_to_files


DEFAULT_TIME_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = 256
DEFAULT_OUTPUT_LIMIT = 64
MAX_TOOL_DIAGNOSTIC_BYTES = 8 * 1024 * 1024
SAMPLE_ANSWER_SCHEMA_VERSION = 1
SAMPLE_OUTPUT_PREVIEW_BYTES = 160


def normalize_sample_output(payload):
    """Normalize line endings while preserving every other output byte."""
    return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sample_output_summary(payload):
    preview = payload[:SAMPLE_OUTPUT_PREVIEW_BYTES]
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "preview": preview.decode("utf-8", errors="backslashreplace"),
        "preview_truncated": len(preview) < len(payload),
    }


def _sample_first_difference(actual, expected):
    shared = min(len(actual), len(expected))
    offset = next(
        (index for index in range(shared) if actual[index] != expected[index]),
        shared,
    )
    if offset == len(actual) == len(expected):
        return None
    return {
        "offset": offset,
        "actual_byte": actual[offset] if offset < len(actual) else None,
        "expected_byte": expected[offset] if offset < len(expected) else None,
    }


def compare_sample_answer(output_file, ans_file):
    """Compare accepted stdout and a stored sample answer as normalized bytes."""
    try:
        with open(output_file, "rb") as stream:
            actual = normalize_sample_output(stream.read())
        with open(ans_file, "rb") as stream:
            expected = normalize_sample_output(stream.read())
    except OSError as exc:
        return {
            "schema_version": SAMPLE_ANSWER_SCHEMA_VERSION,
            "normalization": "crlf_cr_to_lf",
            "matches": False,
            "actual": None,
            "expected": None,
            "first_difference": None,
            "error": str(exc),
        }
    matches = actual == expected
    return {
        "schema_version": SAMPLE_ANSWER_SCHEMA_VERSION,
        "normalization": "crlf_cr_to_lf",
        "matches": matches,
        "actual": _sample_output_summary(actual),
        "expected": _sample_output_summary(expected),
        "first_difference": None if matches else _sample_first_difference(actual, expected),
    }


def valid_sample_answer_summary(value):
    return (
        isinstance(value, dict)
        and value.get("schema_version") == SAMPLE_ANSWER_SCHEMA_VERSION
        and isinstance(value.get("matches"), bool)
        and isinstance(value.get("actual"), dict)
        and isinstance(value.get("expected"), dict)
    )


def remove_file_with_retries(path):
    for _ in range(20):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            return


def temporary_output_path(bin_path):
    descriptor, path = tempfile.mkstemp(
        prefix=".probhub-output-", suffix=".out", dir=os.path.dirname(bin_path)
    )
    os.close(descriptor)
    return path


def run_program_to_file(
    bin_path,
    in_file,
    output_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    *,
    run_managed_fn=run_managed_to_files,
    infer_failed_status_fn=infer_failed_status,
    cleanup_fn=remove_file_with_retries,
):
    """Run one contestant process with tree-wide resource controls."""
    stderr_path = output_file + ".stderr"
    try:
        result = run_managed_fn(
            [bin_path],
            input_path=in_file,
            stdout_path=output_file,
            stderr_path=stderr_path,
            timeout=time_limit,
            memory_limit_mb=memory_limit,
            output_limit_bytes=int(output_limit * 1024 * 1024),
            process_limit=process_limit,
        )
        try:
            with open(stderr_path, "r", encoding="utf-8", errors="replace") as stream:
                stderr = stream.read().strip()
        except OSError:
            stderr = ""
        details = {
            "output_bytes": int(result.get("output_bytes") or 0),
            "termination_reason": result.get("reason"),
        }
        reason = result["reason"]
        status_by_reason = {
            "time_limit": "TLE",
            "output_limit": "OLE",
            "memory_limit": "MLE",
            "process_limit": "RE",
            PROCESS_CLEANUP_FAILED: "FAIL",
        }
        if reason in status_by_reason:
            return (
                status_by_reason[reason],
                result["time"],
                result["memory"],
                result["memory_enforced"],
                result["message"],
                details,
            )
        if result["returncode"] != 0:
            status = infer_failed_status_fn(
                result["returncode"],
                stderr,
                result["memory_enforced"],
                result["memory"],
                memory_limit,
            )
            if status == "MLE" and reason != "memory_limit":
                details["termination_reason"] = "inferred_memory_limit"
            return (
                status,
                result["time"],
                result["memory"],
                result["memory_enforced"],
                stderr,
                details,
            )
        return (
            "AC",
            result["time"],
            result["memory"],
            result["memory_enforced"],
            "",
            details,
        )
    except OutputBudgetError as exc:
        return "FAIL", 0.0, None, False, str(exc), {
            "output_bytes": 0,
            "termination_reason": "output_control_error",
        }
    except OSError as exc:
        return "RE", 0.0, None, False, str(exc), {"output_bytes": 0}
    finally:
        cleanup_fn(stderr_path)


def probe_tle_margin(
    bin_path,
    in_file,
    case_name,
    program_fingerprint,
    time_limit,
    memory_limit,
    output_limit,
    process_limit,
    probe_factor,
    cache=None,
    *,
    run_managed_fn=run_managed_to_files,
    classify_process_status_fn=classify_process_status,
    cache_key_fn=calibration_probe_cache_key,
    output_path_fn=temporary_output_path,
    cleanup_fn=remove_file_with_retries,
):
    """Measure one expected-TLE case beyond the official time limit."""
    probe_factor = max(float(probe_factor), 1.0)
    probe_limit = float(time_limit) * probe_factor
    key = cache_key_fn(
        program_fingerprint,
        in_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        probe_factor,
    )
    cached_result = cache.get("probe", key) if cache is not None else None
    if cached_result is not None:
        return {**dict(cached_result), "cached": True}

    output_file = output_path_fn(bin_path)
    stderr_path = output_file + ".stderr"
    try:
        try:
            run = run_managed_fn(
                [bin_path],
                input_path=in_file,
                stdout_path=output_file,
                stderr_path=stderr_path,
                timeout=probe_limit,
                memory_limit_mb=memory_limit,
                output_limit_bytes=int(output_limit * 1024 * 1024),
                process_limit=process_limit,
            )
        except OSError as exc:
            result = {
                "status": "TLE",
                "case": case_name,
                "limit": float(time_limit),
                "observed": None,
                "limit_ratio": None,
                "probe_limit": probe_limit,
                "process_status": "RE",
                "evidence_kind": "unavailable",
                "censored": False,
                "supported": True,
                "message": str(exc),
            }
        else:
            observed = float(run.get("time") or 0.0)
            process_status = classify_process_status_fn(run, memory_limit)
            result = {
                "status": "TLE",
                "case": case_name,
                "limit": float(time_limit),
                "observed": round(observed, 6),
                "limit_ratio": round(observed / float(time_limit), 6),
                "probe_limit": probe_limit,
                "process_status": "completed" if process_status == "AC" else process_status,
                "evidence_kind": (
                    "lower_bound" if run.get("reason") == "time_limit" else "exact"
                ),
                "censored": run.get("reason") == "time_limit",
                "supported": True,
                "message": run.get("message", ""),
            }
        if cache is not None:
            cache.set("probe", key, result)
        return {**result, "cached": False}
    finally:
        cleanup_fn(output_file)
        cleanup_fn(stderr_path)


def run_standard_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    capture_sample_answer=False,
    *,
    run_program_fn=run_program_to_file,
    sample_compare_fn=compare_sample_answer,
    output_compare_fn=compare_standard_output,
    output_path_fn=temporary_output_path,
    cleanup_fn=remove_file_with_retries,
):
    output_file = output_path_fn(bin_path)
    try:
        result = run_program_fn(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        status, elapsed, memory, memory_enforced, message, details = result
        if capture_sample_answer:
            details["sample_answer"] = sample_compare_fn(output_file, ans_file)
        if status != "AC":
            return result
        with open(ans_file, "r", encoding="utf-8", errors="replace") as answer, open(
            output_file, "r", encoding="utf-8", errors="replace"
        ) as actual:
            matched, message = output_compare_fn(answer.read(), actual.read())
        return (
            "AC" if matched else "WA",
            elapsed,
            memory,
            memory_enforced,
            message,
            details,
        )
    finally:
        cleanup_fn(output_file)


def run_sample_answer_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    *,
    run_program_fn=run_program_to_file,
    sample_compare_fn=compare_sample_answer,
    output_path_fn=temporary_output_path,
    cleanup_fn=remove_file_with_retries,
):
    """Run the primary accepted solution and compare raw sample bytes only."""
    output_file = output_path_fn(bin_path)
    try:
        result = run_program_fn(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        status, elapsed, memory, memory_enforced, message, details = result
        details["sample_answer"] = sample_compare_fn(output_file, ans_file)
        return status, elapsed, memory, memory_enforced, message, details
    finally:
        cleanup_fn(output_file)


def run_custom_testcase(
    bin_path,
    checker_bin,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    capture_sample_answer=False,
    *,
    run_program_fn=run_program_to_file,
    sample_compare_fn=compare_sample_answer,
    checker_fn=run_checker_to_files,
    classify_failure_fn=classify_contestant_failure,
    output_path_fn=temporary_output_path,
    cleanup_fn=remove_file_with_retries,
):
    """Run a contestant and adapt the shared Checker result to the tuple API."""
    output_file = output_path_fn(bin_path)
    try:
        result = run_program_fn(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        status, elapsed, memory, memory_enforced, message, details = result
        if capture_sample_answer:
            details["sample_answer"] = sample_compare_fn(output_file, ans_file)
        if status != "AC":
            termination_reason = details.get("termination_reason")
            if not termination_reason:
                termination_reason = "start_error"
                details["termination_reason"] = termination_reason
            details.update(classify_failure_fn(status, termination_reason))
            return status, elapsed, memory, memory_enforced, message, details

        checker = checker_fn(
            [checker_bin],
            in_file,
            ans_file,
            output_file,
            timeout=max(5.0, float(time_limit)),
            cwd=os.path.dirname(bin_path),
            memory_limit_mb=memory_limit,
            output_limit_bytes=min(
                int(output_limit * 1024 * 1024), MAX_TOOL_DIAGNOSTIC_BYTES
            ),
            process_limit=process_limit,
        )
        checker_status = checker.get("verdict") or "FAIL"
        actor = checker.get("actor")
        failure_kind = checker.get("failure_kind")
        if checker_status == "AC":
            actor = "session"
        elif checker_status == "WA":
            actor = "contestant"
            failure_kind = "wrong_answer"
        details.update({
            "verdict": checker.get("verdict"),
            "execution_status": checker.get("execution_status"),
            "failure_kind": failure_kind,
            "actor": actor,
            "termination_reason": checker.get("termination_reason"),
            "checker_termination_reason": checker.get("termination_reason"),
            "cleanup": checker.get("cleanup"),
        })
        checker_message = checker.get("message") or ""
        if checker_status == "FAIL":
            if checker.get("execution_status") == "output_control_error":
                checker_message = f"checker output control failed: {checker_message}"
            elif checker.get("execution_status") == "start_error":
                checker_message = f"checker failed to start: {checker_message}"
            elif checker.get("termination_reason") != "completed":
                checker_message = f"checker {checker_message}"
        return (
            checker_status,
            elapsed,
            memory,
            memory_enforced,
            checker_message,
            details,
        )
    finally:
        cleanup_fn(output_file)


def run_interactive_testcase(
    bin_path,
    interactor_bin,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    idle_limit=None,
    transcript_limit=65536,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    *,
    interactive_fn=execute_interactive_session,
):
    """Connect contestant and interactor with transcript, idle and total deadlines."""
    result = interactive_fn(
        [bin_path],
        [interactor_bin],
        in_file,
        ans_file,
        work_dir=os.path.dirname(bin_path),
        time_limit=time_limit,
        memory_limit_mb=memory_limit,
        idle_limit=idle_limit,
        transcript_limit=transcript_limit,
        output_limit_bytes=int(output_limit * 1024 * 1024),
        process_limit=process_limit,
    )
    details = {
        "transcript": result.get("transcript", []),
        "transcript_truncated": result.get("transcript_truncated", False),
        "output_bytes": result.get("output_bytes"),
        "termination_reason": result.get("termination_reason"),
        "verdict": result.get("verdict"),
        "execution_status": result.get("execution_status"),
        "failure_kind": result.get("failure_kind"),
        "actor": result.get("actor"),
        "cleanup": result.get("cleanup"),
        "exit_codes": result.get("exit_codes"),
        "traffic": result.get("traffic"),
        "resources": result.get("resources"),
    }
    if result.get("timeout_kind"):
        details["timeout_kind"] = result["timeout_kind"]
    return (
        result.get("status") or "FAIL",
        float(result.get("time") or 0.0),
        result.get("memory"),
        bool(result.get("memory_enforced")),
        result.get("message") or "",
        details,
    )


def run_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    judge_type="standard",
    judge_bin=None,
    idle_limit=None,
    transcript_limit=65536,
    capture_sample_answer=False,
    *,
    standard_fn=run_standard_testcase,
    custom_fn=run_custom_testcase,
    interactive_fn=run_interactive_testcase,
):
    if judge_type == "custom":
        return custom_fn(
            bin_path,
            judge_bin,
            in_file,
            ans_file,
            time_limit,
            memory_limit,
            output_limit,
            process_limit,
            capture_sample_answer=capture_sample_answer,
        )
    if judge_type == "interactive":
        return interactive_fn(
            bin_path,
            judge_bin,
            in_file,
            ans_file,
            time_limit,
            memory_limit,
            idle_limit=idle_limit,
            transcript_limit=transcript_limit,
            output_limit=output_limit,
            process_limit=process_limit,
        )
    return standard_fn(
        bin_path,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        capture_sample_answer=capture_sample_answer,
    )


def run_validator(
    val_bin,
    in_file,
    timeout=VALIDATOR_TIMEOUT_SECONDS,
    *,
    run_managed_fn=run_managed_to_files,
):
    """Validate LF-normalized input under the shared process controller."""
    try:
        with open(in_file, "rb") as stream:
            data = stream.read().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory(prefix="probhub-validator-") as temp:
            stdout_path = os.path.join(temp, "validator.out")
            stderr_path = os.path.join(temp, "validator.stderr")
            result = run_managed_fn(
                [val_bin],
                input_data=data,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=timeout,
                memory_limit_mb=VALIDATOR_MEMORY_LIMIT_MB,
                output_limit_bytes=VALIDATOR_OUTPUT_LIMIT_BYTES,
                process_limit=DEFAULT_PROCESS_LIMIT,
            )
            try:
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as stream:
                    stderr = stream.read().strip()
            except OSError:
                stderr = ""
            if result["reason"] != "completed":
                return False, result["message"] or result["reason"].replace("_", " ")
            return result["returncode"] == 0, stderr
    except OSError as exc:
        return False, str(exc)
