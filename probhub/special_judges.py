"""Shared execution primitives for custom and interactive judges."""

import codecs
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .io import read_bounded_text
from .process_control import (
    DEFAULT_PROCESS_LIMIT,
    OutputBudgetError,
    ProcessCancelled,
    cancellation_requested,
    output_path_size,
    run_managed_to_files,
    spawn_managed,
)


MIB = 1024 * 1024
MAX_CHECKER_DIAGNOSTIC_BYTES = 8 * MIB
_FEEDBACK_NAMES = ("judgemessage.txt", "teammessage.txt")
_RESOURCE_LIMIT_REASONS = frozenset(
    ("time_limit", "memory_limit", "output_limit", "process_limit")
)
_CLEANUP_ERROR_BYTES = 4096


def _cleanup_error(stage, actor, exc):
    message = str(exc).encode("utf-8", errors="replace")[:_CLEANUP_ERROR_BYTES]
    return {
        "stage": stage,
        "actor": actor,
        "message": message.decode("utf-8", errors="replace"),
    }


def _remove_runtime_directory(path):
    if path is None:
        return True, 0, None
    last_error = None
    for attempt in range(1, 21):
        try:
            shutil.rmtree(path)
            return True, attempt, None
        except FileNotFoundError:
            return True, attempt, None
        except OSError as exc:
            last_error = exc
            if attempt < 20:
                time.sleep(0.05)
    return False, 20, last_error


def _apply_cleanup_failure(result, cleanup):
    result["cleanup"] = cleanup
    if cleanup["ok"]:
        return result
    result["pre_cleanup_result"] = {
        key: result.get(key)
        for key in (
            "verdict",
            "execution_status",
            "failure_kind",
            "actor",
            "termination_reason",
        )
    }
    result.update({
        "verdict": None,
        "execution_status": "cleanup_error",
        "failure_kind": "cleanup_failure",
        "actor": "supervisor",
        "termination_reason": "cleanup_error",
        "message": cleanup["errors"][0]["message"] if cleanup["errors"] else "cleanup failed",
    })
    if "status" in result:
        result["status"] = "FAIL"
    return result


def _feedback_message(feedback_dir, fallback="", limit_bytes=MAX_CHECKER_DIAGNOSTIC_BYTES):
    for name in _FEEDBACK_NAMES:
        path = Path(feedback_dir) / name
        try:
            result = read_bounded_text(path, limit_bytes)
        except OSError:
            continue
        message = result["text"].strip()
        if message:
            result["source"] = name
            return message, result
    encoded = (fallback or "").encode("utf-8", errors="replace")
    retained = encoded[: max(int(limit_bytes), 0)]
    return retained.decode("utf-8", errors="replace").strip(), {
        "source": "stderr",
        "observed_bytes": len(encoded),
        "retained_bytes": len(retained),
        "truncated": len(retained) < len(encoded),
        "exists": bool(encoded),
    }


def _failed_checker_result(reason, message, diagnostic_limit_bytes):
    stderr = str(message).encode("utf-8", errors="replace")
    retained = stderr[: max(int(diagnostic_limit_bytes), 0)]
    bounded_message = retained.decode("utf-8", errors="replace").strip()
    feedback = {
        "source": "stderr",
        "observed_bytes": len(stderr),
        "retained_bytes": len(retained),
        "truncated": len(retained) < len(stderr),
        "exists": bool(stderr),
    }
    return {
        "verdict": None,
        "execution_status": reason,
        "failure_kind": (
            "control_failure" if reason == "output_control_error" else "startup_failure"
        ),
        "actor": "supervisor" if reason == "output_control_error" else "checker",
        "termination_reason": reason,
        "message": bounded_message,
        "feedback_message": bounded_message,
        "returncode": None,
        "time": 0.0,
        "memory": None,
        "memory_enforced": False,
        "process_limit_enforced": False,
        "output_bytes": 0,
        "retained_output_bytes": 0,
        "stdout_retained_bytes": 0,
        "stderr_retained_bytes": 0,
        "output_truncated": False,
        "stdout": b"",
        "stderr": stderr,
        "feedback": feedback,
    }


def run_checker_to_files(
    checker_command,
    input_path,
    answer_path,
    contestant_output_path,
    *,
    timeout,
    cwd=None,
    memory_limit_mb=256,
    output_limit_bytes=64 * MIB,
    process_limit=DEFAULT_PROCESS_LIMIT,
    diagnostic_limit_bytes=None,
    env=None,
):
    """Run a DOMjudge/testlib Checker and return protocol and execution evidence.

    The Checker receives ``<input> <answer> <feedback-dir>`` as arguments and
    reads contestant output from stdin. Its stdout, stderr, and both standard
    feedback files share one bounded output budget.
    """
    effective_output_limit = min(
        max(int(output_limit_bytes), 0),
        MAX_CHECKER_DIAGNOSTIC_BYTES,
    )
    if diagnostic_limit_bytes is None:
        diagnostic_limit_bytes = effective_output_limit
    else:
        diagnostic_limit_bytes = min(
            max(int(diagnostic_limit_bytes), 0),
            effective_output_limit,
        )

    runtime_dir = None
    result = None
    cancelled_error = None
    cleanup = {
        "ok": True,
        "checker_tree_termination": "not-started",
        "runtime_removed": False,
        "runtime_remove_attempts": 0,
        "errors": [],
    }
    try:
        runtime_dir = Path(
            tempfile.mkdtemp(prefix=".probhub-checker-", dir=cwd)
        )
        feedback_dir = runtime_dir / "feedback"
        feedback_dir.mkdir()
        stdout_path = runtime_dir / "checker.stdout"
        stderr_path = runtime_dir / "checker.stderr"
        feedback_paths = tuple(feedback_dir / name for name in _FEEDBACK_NAMES)
        try:
            cleanup["checker_tree_termination"] = "pending"
            execution = run_managed_to_files(
                [
                    *checker_command,
                    os.fspath(input_path),
                    os.fspath(answer_path),
                    os.fspath(feedback_dir),
                ],
                input_path=contestant_output_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                additional_output_paths=feedback_paths,
                timeout=timeout,
                memory_limit_mb=memory_limit_mb,
                output_limit_bytes=effective_output_limit,
                process_limit=process_limit,
                cwd=cwd,
                env=env,
            )
            cleanup["checker_tree_termination"] = "completed"
        except OutputBudgetError as exc:
            cleanup["checker_tree_termination"] = "completed"
            result = _failed_checker_result(
                "output_control_error", str(exc), diagnostic_limit_bytes
            )
            return result
        except OSError as exc:
            cleanup["checker_tree_termination"] = "not-started"
            result = _failed_checker_result("start_error", str(exc), diagnostic_limit_bytes)
            return result

        stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
        stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
        feedback_message, feedback = _feedback_message(
            feedback_dir,
            stderr.decode("utf-8", errors="replace"),
            diagnostic_limit_bytes,
        )
        message = feedback_message
        termination_reason = execution.get("reason") or "completed"
        if termination_reason == "cancelled":
            raise ProcessCancelled(execution.get("message") or "execution cancelled")
        returncode = execution.get("returncode")
        verdict = None
        failure_kind = None
        actor = "checker"
        if termination_reason == "completed" and returncode in {0, 42}:
            verdict = "AC"
            execution_status = "completed"
            actor = "session"
        elif termination_reason == "completed" and returncode in {1, 2, 43}:
            verdict = "WA"
            execution_status = "completed"
            failure_kind = "wrong_answer"
            actor = "contestant"
        elif termination_reason == "completed":
            execution_status = "completed"
            failure_kind = "judge_failure"
            message = message or f"checker exited with code {returncode}"
        elif termination_reason in _RESOURCE_LIMIT_REASONS:
            execution_status = termination_reason
            failure_kind = "resource_limit"
            message = execution.get("message") or termination_reason.replace("_", " ")
        else:
            execution_status = "completed"
            failure_kind = "judge_failure"
            message = execution.get("message") or termination_reason.replace("_", " ")

        result = {
            "verdict": verdict,
            "execution_status": execution_status,
            "failure_kind": failure_kind,
            "actor": actor,
            "termination_reason": termination_reason,
            "message": message,
            "feedback_message": feedback_message,
            "returncode": returncode,
            "time": execution.get("time", 0.0),
            "memory": execution.get("memory"),
            "memory_enforced": execution.get("memory_enforced", False),
            "process_limit_enforced": execution.get("process_limit_enforced", False),
            "output_bytes": execution.get("output_bytes", 0),
            "retained_output_bytes": execution.get("retained_output_bytes", 0),
            "stdout_retained_bytes": execution.get("stdout_retained_bytes", 0),
            "stderr_retained_bytes": execution.get("stderr_retained_bytes", 0),
            "output_truncated": execution.get("output_truncated", False),
            "stdout": stdout,
            "stderr": stderr,
            "feedback": feedback,
        }
        return result
    except ProcessCancelled as exc:
        cancelled_error = exc
        result = _failed_checker_result(
            "cancelled", str(exc), diagnostic_limit_bytes
        )
        result.update({
            "execution_status": "cancelled",
            "failure_kind": "cancelled",
            "actor": "supervisor",
            "termination_reason": "cancelled",
        })
    except OutputBudgetError as exc:
        result = _failed_checker_result(
            "output_control_error", str(exc), diagnostic_limit_bytes
        )
        return result
    except OSError as exc:
        result = _failed_checker_result("start_error", str(exc), diagnostic_limit_bytes)
        result["actor"] = "supervisor"
        return result
    finally:
        removed, attempts, error = _remove_runtime_directory(runtime_dir)
        cleanup["runtime_removed"] = removed
        cleanup["runtime_remove_attempts"] = attempts
        if error is not None:
            cleanup["errors"].append(
                _cleanup_error("runtime_remove", "supervisor", error)
            )
        cleanup["ok"] = not cleanup["errors"]
        if result is not None:
            _apply_cleanup_failure(result, cleanup)
    if cancelled_error is not None:
        if result is not None and result.get("failure_kind") == "cleanup_failure":
            return result
        raise cancelled_error
    return result


def _command_list(command):
    if isinstance(command, (str, bytes, os.PathLike)):
        return [os.fspath(command)]
    return [os.fspath(item) for item in command]


def _protocol_outcome(returncode, message, actor):
    message = (message or "").strip()
    if returncode in {0, 42}:
        return {
            "status": "AC",
            "verdict": "AC",
            "execution_status": "completed",
            "failure_kind": None,
            "actor": "session",
            "termination_reason": "completed",
            "message": message,
        }
    if returncode in {1, 2, 43}:
        return {
            "status": "WA",
            "verdict": "WA",
            "execution_status": "completed",
            "failure_kind": "wrong_answer",
            "actor": "contestant",
            "termination_reason": "completed",
            "message": message,
        }
    return {
        "status": "FAIL",
        "verdict": None,
        "execution_status": "completed",
        "failure_kind": "judge_failure",
        "actor": actor,
        "termination_reason": "completed",
        "message": message or f"{actor} exited with code {returncode}",
    }


def _record_interactive_transcript(transcript, direction, payload, decoder, *, final=False):
    """Reserve the shared byte budget and decode one direction incrementally."""
    with transcript["_lock"]:
        remaining = max(transcript["limit"] - transcript["bytes"], 0)
        saved = payload[:remaining]
        transcript["bytes"] += len(saved)
        if len(saved) < len(payload):
            transcript["truncated"] = True
        text = decoder.decode(saved, final=final)
        if text:
            transcript["entries"].append({"direction": direction, "data": text})


def _pump_interactive_stream(source, destination, direction, transcript, activity, traffic):
    """Forward bytes while recording traffic and a bounded transcript."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            now = time.monotonic()
            with transcript["_lock"]:
                activity["last"] = max(activity["last"], now)
                traffic[direction] = traffic.get(direction, 0) + len(chunk)
                limit_exceeded = traffic[direction] > traffic["limit"]
            if limit_exceeded:
                break
            _record_interactive_transcript(transcript, direction, chunk, decoder)
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        _record_interactive_transcript(transcript, direction, b"", decoder, final=True)
        for stream in (destination, source):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _interactive_evidence(
    traffic,
    solution_stderr,
    interactor_stderr,
    feedback_dir=None,
):
    solution_stderr_bytes = output_path_size(solution_stderr, required=True)[0]
    interactor_stderr_bytes = output_path_size(interactor_stderr, required=True)[0]
    feedback_bytes = 0
    if feedback_dir:
        for name in _FEEDBACK_NAMES:
            feedback_bytes += output_path_size(Path(feedback_dir) / name)[0]
    lock = traffic.get("_lock")
    if lock is None:
        solution_protocol_bytes = traffic.get("solution_to_interactor", 0)
        interactor_protocol_bytes = traffic.get("interactor_to_solution", 0)
        limit = int(traffic["limit"])
    else:
        with lock:
            solution_protocol_bytes = traffic.get("solution_to_interactor", 0)
            interactor_protocol_bytes = traffic.get("interactor_to_solution", 0)
            limit = int(traffic["limit"])
    return {
        "limit_bytes": limit,
        "solution_to_interactor": int(solution_protocol_bytes),
        "interactor_to_solution": int(interactor_protocol_bytes),
        "solution_stderr": int(solution_stderr_bytes),
        "interactor_stderr": int(interactor_stderr_bytes),
        "feedback": int(feedback_bytes),
    }


def _interactive_output_classification(evidence, diagnostic_limit_bytes):
    limit = evidence["limit_bytes"]
    if evidence["interactor_to_solution"] > limit:
        return {
            "status": "FAIL",
            "verdict": None,
            "execution_status": "output_limit",
            "failure_kind": "resource_limit",
            "actor": "interactor",
            "termination_reason": "output_limit",
            "message": "interactor output limit exceeded",
        }
    if evidence["interactor_stderr"] + evidence["feedback"] > int(diagnostic_limit_bytes):
        return {
            "status": "FAIL",
            "verdict": None,
            "execution_status": "output_limit",
            "failure_kind": "resource_limit",
            "actor": "interactor",
            "termination_reason": "output_limit",
            "message": "interactor diagnostic output limit exceeded",
        }
    if evidence["solution_to_interactor"] + evidence["solution_stderr"] > limit:
        return {
            "status": "OLE",
            "verdict": "OLE",
            "execution_status": "output_limit",
            "failure_kind": "resource_limit",
            "actor": "contestant",
            "termination_reason": "output_limit",
            "message": "interactive output limit exceeded",
        }
    return None


def _interactive_result(
    outcome,
    *,
    elapsed,
    memory,
    memory_enforced,
    transcript,
    traffic_evidence,
    cleanup,
    exit_codes,
    timeout_kind=None,
    resources=None,
):
    lock = transcript.get("_lock")
    if lock is None:
        entries = [dict(entry) for entry in transcript["entries"]]
        transcript_truncated = bool(transcript["truncated"])
        transcript_bytes = int(transcript.get("bytes", 0))
    else:
        with lock:
            entries = [dict(entry) for entry in transcript["entries"]]
            transcript_truncated = bool(transcript["truncated"])
            transcript_bytes = int(transcript.get("bytes", 0))
    result = dict(outcome)
    result.update({
        "time": float(elapsed),
        "memory": memory,
        "memory_enforced": bool(memory_enforced),
        "exit_codes": dict(exit_codes),
        "traffic": dict(traffic_evidence or {}),
        "transcript": entries,
        "transcript_bytes": transcript_bytes,
        "transcript_truncated": transcript_truncated,
        "output_bytes": (
            int(traffic_evidence.get("solution_to_interactor", 0))
            + int(traffic_evidence.get("solution_stderr", 0))
            if traffic_evidence else 0
        ),
        "cleanup": cleanup,
    })
    if resources is not None:
        result["resources"] = resources
    if timeout_kind:
        result["timeout_kind"] = timeout_kind
    return result


def _terminate_interactive_actor(managed, actor, cleanup):
    if managed is None:
        return
    key = f"{actor}_tree_termination"
    try:
        managed.terminate()
        cleanup[key] = "completed"
    except BaseException as exc:
        cleanup[key] = "failed"
        cleanup["errors"].append(_cleanup_error("tree_termination", actor, exc))


def _join_interactive_pumps(threads, cleanup):
    for thread in threads:
        thread.join(timeout=1)
    cleanup["pumps_joined"] = all(not thread.is_alive() for thread in threads)
    if not cleanup["pumps_joined"] and not any(
        item["stage"] == "pump_join" for item in cleanup["errors"]
    ):
        cleanup["errors"].append({
            "stage": "pump_join",
            "actor": "supervisor",
            "message": "interactive pump thread did not stop",
        })


def execute_interactive_session(
    contestant_command,
    interactor_command,
    input_path,
    answer_path,
    *,
    work_dir=None,
    time_limit=1.0,
    memory_limit_mb=256,
    idle_limit=None,
    transcript_limit=65536,
    output_limit_bytes=64 * MIB,
    process_limit=DEFAULT_PROCESS_LIMIT,
    env=None,
):
    """Run one contestant/Interactor session with explicit responsibility evidence."""
    monotonic_start = time.monotonic()
    wall_start = time.time()
    idle_limit = max(
        float(idle_limit if idle_limit is not None else min(time_limit, 2.0)),
        0.1,
    )
    state_lock = threading.RLock()
    transcript = {
        "entries": [],
        "bytes": 0,
        "limit": max(int(transcript_limit), 0),
        "truncated": False,
        "_lock": state_lock,
    }
    activity = {"last": monotonic_start}
    traffic = {"limit": max(int(output_limit_bytes), 0), "_lock": state_lock}
    cleanup = {
        "ok": True,
        "contestant_tree_termination": "not-started",
        "interactor_tree_termination": "not-started",
        "pumps_joined": True,
        "runtime_removed": False,
        "runtime_remove_attempts": 0,
        "errors": [],
    }
    runtime_dir = None
    solution_stderr = None
    interactor_stderr = None
    feedback_dir = None
    solution_managed = None
    interactor_managed = None
    solution_proc = None
    interactor_proc = None
    memory_enforced = False
    peak_memory_mb = None
    solution_process_limit_enforced = False
    interactor_memory_enforced = False
    interactor_peak_memory_mb = None
    interactor_process_limit_enforced = False
    threads = []
    result = None
    cancelled_error = None
    spawn_actor = "supervisor"
    diagnostic_limit_bytes = min(max(int(output_limit_bytes), 0), MAX_CHECKER_DIAGNOSTIC_BYTES)

    def current_exit_codes():
        return {
            "contestant": getattr(solution_proc, "returncode", None),
            "interactor": getattr(interactor_proc, "returncode", None),
        }

    def current_evidence():
        if solution_stderr is None or interactor_stderr is None:
            return {}
        return _interactive_evidence(
            traffic,
            solution_stderr,
            interactor_stderr,
            feedback_dir,
        )

    def finish(outcome, *, timeout_kind=None, traffic_evidence=None):
        nonlocal result
        actor = outcome.get("actor")
        reported_memory = (
            interactor_peak_memory_mb if actor == "interactor" else peak_memory_mb
        )
        reported_memory_enforced = (
            interactor_memory_enforced if actor == "interactor" else memory_enforced
        )
        result = _interactive_result(
            outcome,
            elapsed=time.time() - wall_start,
            memory=reported_memory,
            memory_enforced=reported_memory_enforced,
            transcript=transcript,
            traffic_evidence=(
                current_evidence() if traffic_evidence is None else traffic_evidence
            ),
            cleanup=cleanup,
            exit_codes=current_exit_codes(),
            timeout_kind=timeout_kind,
            resources={
                "contestant": {
                    "memory": peak_memory_mb,
                    "memory_enforced": bool(memory_enforced),
                    "process_limit_enforced": bool(solution_process_limit_enforced),
                },
                "interactor": {
                    "memory": interactor_peak_memory_mb,
                    "memory_enforced": bool(interactor_memory_enforced),
                    "process_limit_enforced": bool(interactor_process_limit_enforced),
                },
            },
        )
        return result

    try:
        runtime_dir = Path(tempfile.mkdtemp(prefix=".probhub-interactive-", dir=work_dir))
        solution_stderr = runtime_dir / "solution.stderr"
        interactor_stderr = runtime_dir / "interactor.stderr"
        feedback_dir = runtime_dir / "feedback"
        feedback_dir.mkdir()
        with solution_stderr.open("wb") as solution_err, interactor_stderr.open("wb") as interactor_err:
            spawn_actor = "contestant"
            cleanup["contestant_tree_termination"] = "pending"
            solution_managed = spawn_managed(
                _command_list(contestant_command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=solution_err,
                bufsize=0,
                memory_limit_mb=memory_limit_mb,
                process_limit=process_limit,
                cwd=work_dir,
                env=env,
            )
            solution_proc = solution_managed.proc
            memory_enforced = solution_managed.memory_enforced
            solution_process_limit_enforced = bool(
                getattr(solution_managed, "process_limit_enforced", False)
            )
            spawn_actor = "interactor"
            cleanup["interactor_tree_termination"] = "pending"
            interactor_managed = spawn_managed(
                [
                    *_command_list(interactor_command),
                    os.fspath(input_path),
                    os.fspath(answer_path),
                    os.fspath(feedback_dir),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=interactor_err,
                bufsize=0,
                memory_limit_mb=memory_limit_mb,
                process_limit=process_limit,
                cwd=work_dir,
                env=env,
            )
            interactor_proc = interactor_managed.proc
            interactor_memory_enforced = interactor_managed.memory_enforced
            interactor_process_limit_enforced = bool(
                getattr(interactor_managed, "process_limit_enforced", False)
            )
            spawn_actor = "supervisor"
            threads = [
                threading.Thread(
                    target=_pump_interactive_stream,
                    args=(
                        solution_proc.stdout,
                        interactor_proc.stdin,
                        "solution_to_interactor",
                        transcript,
                        activity,
                        traffic,
                    ),
                    daemon=True,
                ),
                threading.Thread(
                    target=_pump_interactive_stream,
                    args=(
                        interactor_proc.stdout,
                        solution_proc.stdin,
                        "interactor_to_solution",
                        transcript,
                        activity,
                        traffic,
                    ),
                    daemon=True,
                ),
            ]
            cleanup["pumps_joined"] = False
            for thread in threads:
                thread.start()

            monotonic_start = time.monotonic()
            wall_start = time.time()
            activity["last"] = max(activity["last"], monotonic_start)
            last_resource_sample = monotonic_start
            deadline = monotonic_start + float(time_limit)
            while solution_proc.poll() is None or interactor_proc.poll() is None:
                if cancellation_requested():
                    raise ProcessCancelled("execution cancelled")
                now = time.monotonic()
                evidence = current_evidence()
                resource_outcome = _interactive_output_classification(
                    evidence, diagnostic_limit_bytes
                )
                if now - last_resource_sample >= 0.05:
                    last_resource_sample = now
                    solution_count, solution_memory = solution_managed.sample()
                    peak_memory_mb = solution_managed.peak_memory_mb
                    if solution_memory is not None and solution_memory >= memory_limit_mb:
                        resource_outcome = {
                            "status": "MLE", "verdict": "MLE",
                            "execution_status": "memory_limit", "failure_kind": "resource_limit",
                            "actor": "contestant", "termination_reason": "memory_limit",
                            "message": "memory limit exceeded",
                        }
                    elif solution_count is not None and solution_count > process_limit:
                        resource_outcome = {
                            "status": "RE", "verdict": "RE",
                            "execution_status": "process_limit", "failure_kind": "resource_limit",
                            "actor": "contestant", "termination_reason": "process_limit",
                            "message": "process limit exceeded",
                        }
                    interactor_count, interactor_memory = interactor_managed.sample()
                    interactor_peak_memory_mb = interactor_managed.peak_memory_mb
                    if interactor_memory is not None and interactor_memory >= memory_limit_mb:
                        resource_outcome = {
                            "status": "FAIL", "verdict": None,
                            "execution_status": "memory_limit", "failure_kind": "resource_limit",
                            "actor": "interactor", "termination_reason": "memory_limit",
                            "message": "interactor memory limit exceeded",
                        }
                    elif interactor_count is not None and interactor_count > process_limit:
                        resource_outcome = {
                            "status": "FAIL", "verdict": None,
                            "execution_status": "process_limit", "failure_kind": "resource_limit",
                            "actor": "interactor", "termination_reason": "process_limit",
                            "message": "interactor process limit exceeded",
                        }
                    elif interactor_proc.poll() is not None:
                        protocol = _protocol_outcome(interactor_proc.returncode, "", "interactor")
                        if protocol["status"] == "FAIL":
                            resource_outcome = protocol
                if resource_outcome:
                    _terminate_interactive_actor(solution_managed, "contestant", cleanup)
                    solution_managed = None
                    _terminate_interactive_actor(interactor_managed, "interactor", cleanup)
                    interactor_managed = None
                    _join_interactive_pumps(threads, cleanup)
                    return finish(resource_outcome)

                timeout_kind = None
                if now >= deadline:
                    timeout_kind = "total"
                else:
                    with state_lock:
                        idle_elapsed = now - activity["last"]
                    if idle_elapsed >= idle_limit:
                        timeout_kind = "idle"
                if timeout_kind:
                    solution_managed.sample()
                    peak_memory_mb = solution_managed.peak_memory_mb
                    _terminate_interactive_actor(solution_managed, "contestant", cleanup)
                    solution_managed = None
                    _terminate_interactive_actor(interactor_managed, "interactor", cleanup)
                    interactor_managed = None
                    _join_interactive_pumps(threads, cleanup)
                    return finish({
                        "status": "TLE",
                        "verdict": "TLE",
                        "execution_status": "time_limit",
                        "failure_kind": "resource_limit",
                        "actor": "session",
                        "termination_reason": "time_limit",
                        "message": (
                            "interactive idle timeout exceeded"
                            if timeout_kind == "idle"
                            else "interactive time limit exceeded"
                        ),
                    }, timeout_kind=timeout_kind)
                time.sleep(0.005)

            solution_managed.sample()
            peak_memory_mb = solution_managed.peak_memory_mb
            interactor_managed.sample()
            interactor_peak_memory_mb = interactor_managed.peak_memory_mb
            _terminate_interactive_actor(solution_managed, "contestant", cleanup)
            solution_managed = None
            _terminate_interactive_actor(interactor_managed, "interactor", cleanup)
            interactor_managed = None
            _join_interactive_pumps(threads, cleanup)
            evidence = current_evidence()
            resource_outcome = _interactive_output_classification(
                evidence, diagnostic_limit_bytes
            )
            if (
                interactor_proc.returncode != 0
                and interactor_memory_enforced
                and interactor_peak_memory_mb is not None
                and interactor_peak_memory_mb >= memory_limit_mb * 0.98
            ):
                resource_outcome = {
                    "status": "FAIL", "verdict": None,
                    "execution_status": "memory_limit", "failure_kind": "resource_limit",
                    "actor": "interactor", "termination_reason": "memory_limit",
                    "message": "interactor memory limit exceeded",
                }

        solution_message = ""
        interactor_message = ""
        try:
            solution_message = read_bounded_text(
                solution_stderr, max(int(output_limit_bytes), 0)
            )["text"].strip()
        except OSError:
            pass
        try:
            interactor_message = read_bounded_text(
                interactor_stderr, diagnostic_limit_bytes
            )["text"].strip()
        except OSError:
            pass
        interactor_message = _feedback_message(
            feedback_dir, interactor_message, diagnostic_limit_bytes
        )[0]
        if resource_outcome:
            return finish(resource_outcome)
        if solution_proc.returncode != 0:
            inferred_memory = (
                memory_enforced
                and peak_memory_mb is not None
                and peak_memory_mb >= memory_limit_mb * 0.98
            )
            if inferred_memory:
                outcome = {
                    "status": "MLE", "verdict": "MLE",
                    "execution_status": "memory_limit", "failure_kind": "resource_limit",
                    "actor": "contestant", "termination_reason": "inferred_memory_limit",
                    "message": solution_message,
                }
            else:
                outcome = {
                    "status": "RE", "verdict": "RE",
                    "execution_status": "completed", "failure_kind": "runtime_error",
                    "actor": "contestant", "termination_reason": "completed",
                    "message": solution_message,
                }
            return finish(outcome)
        return finish(_protocol_outcome(
            interactor_proc.returncode, interactor_message, "interactor"
        ))
    except ProcessCancelled as exc:
        cancelled_error = exc
        finish({
            "status": "cancelled",
            "verdict": None,
            "execution_status": "cancelled",
            "failure_kind": "cancelled",
            "actor": "supervisor",
            "termination_reason": "cancelled",
            "message": str(exc),
        }, traffic_evidence={})
    except OutputBudgetError as exc:
        return finish({
            "status": "FAIL",
            "verdict": None,
            "execution_status": "output_control_error",
            "failure_kind": "control_failure",
            "actor": "supervisor",
            "termination_reason": "output_control_error",
            "message": str(exc),
        }, traffic_evidence={})
    except OSError as exc:
        return finish({
            "status": "FAIL",
            "verdict": None,
            "execution_status": "start_error",
            "failure_kind": "startup_failure",
            "actor": spawn_actor,
            "termination_reason": "start_error",
            "message": str(exc),
        }, traffic_evidence={})
    finally:
        _terminate_interactive_actor(solution_managed, "contestant", cleanup)
        _terminate_interactive_actor(interactor_managed, "interactor", cleanup)
        _join_interactive_pumps(threads, cleanup)
        removed, attempts, error = _remove_runtime_directory(runtime_dir)
        cleanup["runtime_removed"] = removed
        cleanup["runtime_remove_attempts"] = attempts
        if error is not None:
            cleanup["errors"].append(
                _cleanup_error("runtime_remove", "supervisor", error)
            )
        cleanup["ok"] = not cleanup["errors"]
        if result is not None:
            _apply_cleanup_failure(result, cleanup)
    if cancelled_error is not None:
        if result is not None and result.get("failure_kind") == "cleanup_failure":
            return result
        raise cancelled_error
    return result
