import hashlib
import math
import os
import platform
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .build_lock import workspace_file_lock
from .calibration import SANDBOX_CACHE_SCHEMA_VERSION
from .errors import ProbHubError
from .hashing import files_under
from .io import atomic_write_json, normalize_newlines, read_bounded_text, read_yaml
from .judge_qa import inspect_judge_qa
from .linting import compute_data_hash, compute_source_hash, problem_source_paths
from .problem_paths import ProblemPathError, resolve_problem_regular_file
from .process_control import (
    CANCEL_FILE_ENV,
    DEFAULT_PROCESS_LIMIT,
    OutputBudgetError,
    ProcessCancelled,
    VALIDATOR_MEMORY_LIMIT_MB,
    VALIDATOR_OUTPUT_LIMIT_BYTES,
    VALIDATOR_TIMEOUT_SECONDS,
    cancellation_requested,
    run_managed_to_files,
)
from .special_judges import execute_interactive_session, run_checker_to_files


JUDGE_QA_EVIDENCE_SCHEMA_VERSION = 1
JUDGE_QA_POLICY_VERSION = 1
JUDGE_QA_EVIDENCE_FILENAME = "judge-qa-evidence-v1.json"
JUDGE_QA_EVIDENCE_LOCK_FILENAME = "judge-qa-evidence.lock"
DEFAULT_JUDGE_QA_TIMEOUT_SECONDS = 600.0
MAX_JUDGE_QA_TIMEOUT_SECONDS = 3600.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
MIN_CHECKER_TIMEOUT_SECONDS = 5.0
COMPILER_TIMEOUT_SECONDS = 60.0
COMPILER_MEMORY_LIMIT_MB = 2048
COMPILER_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
DIAGNOSTIC_LIMIT_BYTES = 4096
ROBUSTNESS_OVERSIZED_BYTES = 1024 * 1024 + 1
MAX_JUDGE_QA_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_JUDGE_QA_TRANSCRIPT_BYTES = 1024 * 1024
MIN_JUDGE_QA_IDLE_SECONDS = 0.1


class _OverallTimeout(Exception):
    pass


class _RunCancellation:
    def __init__(self, timeout, cancel_check):
        self.deadline = time.monotonic() + max(float(timeout), 0.0)
        self.cancel_check = cancel_check
        self.cause = None

    def requested(self):
        try:
            requested = cancellation_requested()
            if self.cancel_check is not None:
                requested = requested or bool(self.cancel_check())
        except ProcessCancelled:
            raise
        except Exception as exc:
            raise ProbHubError(
                f"Judge QA cancellation check failed: {_bounded_message(exc)}",
                code="judge_qa_cancel_check_failed",
            ) from exc
        if requested:
            self.cause = "cancelled"
            return True
        if time.monotonic() >= self.deadline:
            self.cause = "overall_timeout"
            return True
        return False

    def remaining(self, maximum=None):
        if self.requested():
            if self.cause == "overall_timeout":
                raise _OverallTimeout("Judge QA overall deadline exceeded")
            raise ProcessCancelled("Judge QA cancelled")
        remaining = self.deadline - time.monotonic()
        if maximum is not None:
            remaining = min(remaining, float(maximum))
        return max(remaining, 0.001)


def _result(status, *, ok=False, applicable=True, code=None, message=None, **payload):
    return {
        "ok": bool(ok),
        "applicable": bool(applicable),
        "status": status,
        **({"code": code} if code else {}),
        **({"message": str(message)} if message else {}),
        **payload,
    }


def _bounded_message(value):
    text = str(value or "").strip()
    if len(text.encode("utf-8", errors="replace")) <= DIAGNOSTIC_LIMIT_BYTES:
        return text
    retained = text.encode("utf-8", errors="replace")[:DIAGNOSTIC_LIMIT_BYTES]
    return retained.decode("utf-8", errors="replace") + "..."


def _cleanup_summary(cleanup):
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    states = {
        key: value
        for key, value in cleanup.items()
        if key.endswith("_termination")
        or key in {"runtime_removed", "runtime_remove_attempts", "pumps_joined"}
    }
    errors = cleanup.get("errors") if isinstance(cleanup.get("errors"), list) else []
    return {
        "ok": bool(cleanup.get("ok", not errors)),
        **states,
        "error_count": len(errors),
        "errors": [
            {
                "stage": item.get("stage"),
                "actor": item.get("actor"),
                "message": _bounded_message(item.get("message")),
            }
            for item in errors[:8]
            if isinstance(item, dict)
        ],
    }


def _file_identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode)


def _copy_regular_file(source, destination, problem_dir, cancellation=None):
    relative = source.relative_to(problem_dir).as_posix()
    try:
        verified = resolve_problem_regular_file(problem_dir, relative)
    except ProblemPathError as exc:
        raise ProbHubError(
            f"Judge QA snapshot source is unsafe: {relative} ({exc.reason})",
            code="snapshot_failed",
        ) from exc
    before = _file_identity(verified)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with verified.open("rb") as source_stream, destination.open("wb") as target_stream:
        while True:
            if cancellation is not None:
                cancellation.remaining()
            chunk = source_stream.read(1024 * 1024)
            if not chunk:
                break
            target_stream.write(chunk)
    if os.name != "nt":
        os.chmod(destination, before[4] & 0o777)
    after = _file_identity(verified)
    if before != after or destination.stat().st_size != before[3]:
        raise ProbHubError(
            f"Judge QA input changed while creating the snapshot: {relative}",
            code="inputs_changed",
        )


def _snapshot_paths(problem_dir, config, report, cancellation):
    problem_dir = Path(problem_dir).resolve()
    paths = set()
    for path in problem_source_paths(
        problem_dir, config, check=cancellation.remaining
    ):
        if path.exists():
            paths.add(path.resolve())
    for item in report.get("files") or []:
        cancellation.remaining()
        paths.add(resolve_problem_regular_file(problem_dir, item.get("path")).resolve())
    data = config.get("data") if isinstance(config.get("data"), dict) else {}
    for key, default in (("sample_dir", "data/sample"), ("secret_dir", "data/secret")):
        directory = problem_dir / str(data.get(key, default))
        for path in files_under(
            directory, {".in", ".ans"}, check=cancellation.remaining
        ):
            paths.add(path.resolve())
    cancellation.remaining()
    return sorted(paths, key=lambda path: path.relative_to(problem_dir).as_posix())


def _create_snapshot(problem_dir, destination, config, report, identity, cancellation):
    problem_dir = Path(problem_dir).resolve()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for source in _snapshot_paths(problem_dir, config, report, cancellation):
        _copy_regular_file(
            source,
            destination / source.relative_to(problem_dir),
            problem_dir,
            cancellation,
        )
    cancellation.remaining()
    copied_config = read_yaml(destination / "probhub.yaml")
    cancellation.remaining()
    copied_report = inspect_judge_qa(
        destination, copied_config, check=cancellation.remaining
    )
    copied_identity = (
        compute_source_hash(destination, copied_config, check=cancellation.remaining),
        compute_data_hash(destination, copied_config, check=cancellation.remaining),
        copied_report.get("fixture_hash"),
    )
    if not copied_report.get("ok") or copied_identity != identity:
        raise ProbHubError(
            "Judge QA inputs changed while creating the snapshot",
            code="inputs_changed",
        )
    return copied_config, copied_report


def _remove_snapshot(path):
    shutil.rmtree(path)


def _compiler_command(source, output, include_dir, role):
    command = [
        "g++",
        os.fspath(source),
        "-o",
        os.fspath(output),
        "-O2",
        "-std=c++17",
        "-I",
        os.fspath(include_dir),
    ]
    if platform.system() == "Windows":
        command.append("-static")
        if role == "validator":
            command.append("-DFOR_LINUX")
    return command


def _compiler_identity(cancellation):
    with tempfile.TemporaryDirectory(prefix="probhub-judge-qa-compiler-") as temp:
        stdout_path = Path(temp) / "stdout"
        stderr_path = Path(temp) / "stderr"
        try:
            execution = run_managed_to_files(
                ["g++", "--version"],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=cancellation.remaining(DEFAULT_TOOL_TIMEOUT_SECONDS),
                memory_limit_mb=COMPILER_MEMORY_LIMIT_MB,
                output_limit_bytes=64 * 1024,
                process_limit=DEFAULT_PROCESS_LIMIT,
                cancel_check=cancellation.requested,
            )
        except (OSError, OutputBudgetError) as exc:
            raise ProbHubError(
                f"failed to identify the Judge QA compiler: {_bounded_message(exc)}",
                code="compiler_identity_failed",
            ) from exc
        if execution.get("reason") != "completed" or execution.get("returncode") != 0:
            raise ProbHubError(
                "failed to identify the Judge QA compiler: "
                + _bounded_message(execution.get("message") or execution.get("reason")),
                code="compiler_identity_failed",
            )
        text = read_bounded_text(stdout_path, 64 * 1024).get("text", "")
        identity = (text.splitlines() or [""])[0].strip()
        if not identity:
            raise ProbHubError(
                "Judge QA compiler identity output is empty",
                code="compiler_identity_failed",
            )
        return identity


def _compile_program(source, build_dir, role, cancellation):
    source = Path(source)
    try:
        display_source = source.relative_to(Path(build_dir).parent).as_posix()
    except ValueError:
        display_source = source.name
    suffix = source.suffix.lower()
    if suffix == ".py":
        if role != "contestant":
            raise ProbHubError(
                f"Judge QA {role} must be a reproducible .cpp source: {display_source}",
                code="judge_qa_program_type_unsupported",
            )
        return [sys.executable, "-I", str(source)], {
            "role": role,
            "source": display_source,
            "kind": "python",
            "interpreter": platform.python_implementation(),
            "version": platform.python_version(),
        }
    if suffix != ".cpp":
        raise ProbHubError(
            f"Judge QA programs must be reproducible .cpp or .py sources: {display_source}",
            code="judge_qa_program_type_unsupported",
        )

    digest = hashlib.sha256(f"{role}\0{source}".encode("utf-8")).hexdigest()[:16]
    output = Path(build_dir) / (f"{role}-{digest}.exe" if os.name == "nt" else f"{role}-{digest}")
    include_dir = Path(build_dir) / "include"
    include_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "references" / "testlib.h",
        include_dir / "testlib.h",
    )
    source_dir = source.parent
    command = _compiler_command(
        os.path.relpath(source, source_dir),
        os.path.relpath(output, source_dir),
        os.path.relpath(include_dir, source_dir),
        role,
    )
    compile_dir = Path(build_dir) / "compile-output"
    compile_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = compile_dir / f"{digest}.stdout"
    stderr_path = compile_dir / f"{digest}.stderr"
    try:
        execution = run_managed_to_files(
            command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=cancellation.remaining(COMPILER_TIMEOUT_SECONDS),
            memory_limit_mb=COMPILER_MEMORY_LIMIT_MB,
            output_limit_bytes=COMPILER_OUTPUT_LIMIT_BYTES,
            process_limit=DEFAULT_PROCESS_LIMIT,
            cwd=source_dir,
            cancel_check=cancellation.requested,
        )
    except OutputBudgetError as exc:
        raise ProbHubError(str(exc), code="compile_output_control_failed") from exc
    stderr = read_bounded_text(stderr_path, DIAGNOSTIC_LIMIT_BYTES).get("text", "")
    if execution.get("reason") != "completed" or execution.get("returncode") != 0:
        detail = stderr.strip() or execution.get("message") or execution.get("reason")
        raise ProbHubError(
            f"Judge QA {role} failed to compile: {_bounded_message(detail)}",
            code="compile_failed",
        )
    return [str(output)], {
        "role": role,
        "source": display_source,
        "kind": "cpp17",
        "compiler": "g++",
        "compiler_path": shutil.which("g++"),
    }


def _program_path(problem_dir, config, key):
    judge = config.get("judge") if isinstance(config.get("judge"), dict) else {}
    value = judge.get(key)
    try:
        return resolve_problem_regular_file(problem_dir, value)
    except ProblemPathError as exc:
        raise ProbHubError(
            f"judge.{key} must be a problem-local regular file ({exc.reason})",
            code="judge_qa_program_invalid",
        ) from exc


def _runtime_limits(config):
    raw = config.get("limits") if isinstance(config.get("limits"), dict) else {}
    time_limit = raw.get("time")
    memory_limit = raw.get("memory")
    output_limit = raw.get("output", 64)
    process_limit = raw.get("processes", DEFAULT_PROCESS_LIMIT)
    if isinstance(time_limit, bool) or not isinstance(time_limit, int) or time_limit <= 0:
        raise ProbHubError(
            "limits.time must be a positive integer before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    if (
        isinstance(memory_limit, bool)
        or not isinstance(memory_limit, int)
        or memory_limit < 256
        or memory_limit & (memory_limit - 1)
    ):
        raise ProbHubError(
            "limits.memory must be a power of two and at least 256 before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or output_limit <= 0:
        raise ProbHubError(
            "limits.output must be a positive integer before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    if isinstance(process_limit, bool) or not isinstance(process_limit, int) or process_limit <= 0:
        raise ProbHubError(
            "limits.processes must be a positive integer before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    configured_output_bytes = output_limit * 1024 * 1024
    return {
        "time": float(time_limit),
        "memory": memory_limit,
        "output_bytes": min(configured_output_bytes, MAX_JUDGE_QA_OUTPUT_BYTES),
        "configured_output_bytes": configured_output_bytes,
        "processes": process_limit,
    }


def _interactive_limits(config, limits):
    interactive = (config.get("judge") or {}).get("interactive") or {}
    if not isinstance(interactive, dict):
        raise ProbHubError(
            "judge.interactive must be a mapping before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    idle_limit = interactive.get("idle_limit", min(limits["time"], 2.0))
    if (
        isinstance(idle_limit, bool)
        or not isinstance(idle_limit, (int, float))
        or not math.isfinite(float(idle_limit))
        or float(idle_limit) <= 0
    ):
        raise ProbHubError(
            "judge.interactive.idle_limit must be a positive finite number before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    transcript_limit = interactive.get("transcript_limit", 65536)
    if (
        isinstance(transcript_limit, bool)
        or not isinstance(transcript_limit, int)
        or transcript_limit < 0
    ):
        raise ProbHubError(
            "judge.interactive.transcript_limit must be a non-negative integer before running Judge QA",
            code="judge_qa_limits_invalid",
        )
    return {
        "idle_limit": max(float(idle_limit), MIN_JUDGE_QA_IDLE_SECONDS),
        "configured_idle_limit": float(idle_limit),
        "transcript_limit": min(
            transcript_limit,
            MAX_JUDGE_QA_TRANSCRIPT_BYTES,
        ),
        "configured_transcript_limit": transcript_limit,
    }


def _validate_inputs(validator_command, problem_dir, cases, cancellation):
    seen = set()
    results = []
    runtime = Path(problem_dir) / ".judge-qa-validator"
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        for case in cases:
            relative = case.get("input")
            if not relative or relative.casefold() in seen:
                continue
            seen.add(relative.casefold())
            input_path = resolve_problem_regular_file(problem_dir, relative)
            stdout_path = runtime / f"{len(results)}.stdout"
            stderr_path = runtime / f"{len(results)}.stderr"
            execution = run_managed_to_files(
                validator_command,
                input_data=normalize_newlines(input_path.read_bytes()),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=cancellation.remaining(VALIDATOR_TIMEOUT_SECONDS),
                memory_limit_mb=VALIDATOR_MEMORY_LIMIT_MB,
                output_limit_bytes=VALIDATOR_OUTPUT_LIMIT_BYTES,
                process_limit=DEFAULT_PROCESS_LIMIT,
                cwd=problem_dir,
                cancel_check=cancellation.requested,
            )
            message = read_bounded_text(stderr_path, DIAGNOSTIC_LIMIT_BYTES).get("text", "")
            summary = {
                "input": relative,
                "ok": execution.get("reason") == "completed" and execution.get("returncode") == 0,
                "execution_status": execution.get("reason"),
                "returncode": execution.get("returncode"),
                "time": execution.get("time"),
                "message": _bounded_message(message or execution.get("message")),
            }
            results.append(summary)
            if not summary["ok"]:
                raise ProbHubError(
                    f"Validator rejected Judge QA input {relative}: {summary['message']}",
                    code="judge_qa_validator_failed",
                )
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    return results


def _checker_summary(case, result):
    actual = result.get("verdict")
    infrastructure = actual not in {"AC", "WA"} or not (result.get("cleanup") or {}).get("ok", True)
    expected = case.get("expected") or {}
    matched = not infrastructure and actual == expected.get("status")
    return {
        "id": case.get("id"),
        "purpose": case.get("purpose"),
        "expected": expected,
        "actual": {
            "status": actual,
            "actor": result.get("actor"),
            "execution_status": result.get("execution_status"),
            "failure_kind": result.get("failure_kind"),
            "termination_reason": result.get("termination_reason"),
        },
        "matched": matched,
        "infrastructure_failed": infrastructure,
        "message": _bounded_message(result.get("message")),
        "resources": {
            key: result.get(key)
            for key in (
                "time", "memory", "memory_enforced", "process_limit_enforced",
                "output_bytes", "retained_output_bytes", "output_truncated",
            )
        },
        "cleanup": _cleanup_summary(result.get("cleanup")),
    }


def _interactor_summary(case, result):
    infrastructure = (
        result.get("status") == "FAIL"
        or result.get("actor") in {"interactor", "supervisor"}
        and result.get("failure_kind") not in {None, "cancelled"}
        or not (result.get("cleanup") or {}).get("ok", True)
    )
    expected = case.get("expected") or {}
    actual = {
        "status": result.get("status"),
        "timeout_kind": result.get("timeout_kind"),
        "termination_reason": result.get("termination_reason"),
        "actor": result.get("actor"),
        "execution_status": result.get("execution_status"),
        "failure_kind": result.get("failure_kind"),
    }
    matched = not infrastructure and all(actual.get(key) == value for key, value in expected.items())
    return {
        "id": case.get("id"),
        "purpose": case.get("purpose"),
        "expected": expected,
        "actual": actual,
        "matched": matched,
        "infrastructure_failed": infrastructure,
        "message": _bounded_message(result.get("message")),
        "resources": result.get("resources") or {},
        "traffic": result.get("traffic") or {},
        "transcript": {
            "bytes": result.get("transcript_bytes", 0),
            "limit": None,
            "truncated": bool(result.get("transcript_truncated")),
        },
        "cleanup": _cleanup_summary(result.get("cleanup")),
    }


def _checker_probe_payload(name, baseline):
    if name == "empty":
        return b""
    if name == "truncated":
        stripped = baseline.rstrip()
        if not stripped:
            return b""
        split = stripped.rfind(b" ")
        return stripped[:split] if split > 0 else stripped[:-1]
    if name == "extra-token":
        return baseline + (b"" if baseline.endswith(b"\n") else b"\n") + b"probhub-extra-token\n"
    if name == "oversized":
        seed = baseline + (b"" if baseline.endswith(b"\n") else b"\n")
        repetitions = (ROBUSTNESS_OVERSIZED_BYTES - len(seed) + 1) // 2
        return (seed + b"0 " * max(repetitions, 0))[:ROBUSTNESS_OVERSIZED_BYTES]
    raise ProbHubError(f"unsupported Judge QA probe: {name}", code="judge_qa_probe_invalid")


def _run_checker_qa(problem_dir, report, checker_command, limits, cancellation):
    checker_timeout = max(MIN_CHECKER_TIMEOUT_SECONDS, float(limits["time"]))
    limits["checker_timeout"] = checker_timeout
    summaries = []
    case_by_id = {}
    for case in report["cases"]:
        result = run_checker_to_files(
            checker_command,
            resolve_problem_regular_file(problem_dir, case["input"]),
            resolve_problem_regular_file(problem_dir, case["jury_answer"]),
            resolve_problem_regular_file(problem_dir, case["contestant_output"]),
            timeout=cancellation.remaining(checker_timeout),
            cwd=problem_dir,
            memory_limit_mb=limits["memory"],
            output_limit_bytes=limits["output_bytes"],
            process_limit=limits["processes"],
            cancel_check=cancellation.requested,
        )
        summary = _checker_summary(case, result)
        summaries.append(summary)
        case_by_id[str(case.get("id", "")).casefold()] = case
        if summary["infrastructure_failed"]:
            return summaries, [], "infrastructure-failed"

    probes = []
    robustness = report.get("robustness")
    if robustness:
        baseline = case_by_id.get(str(robustness.get("baseline", "")).casefold())
        if baseline is None:
            raise ProbHubError("Judge QA robustness baseline is missing", code="judge_qa_probe_invalid")
        baseline_output = resolve_problem_regular_file(
            problem_dir, baseline["contestant_output"]
        ).read_bytes()
        probe_dir = Path(problem_dir) / ".judge-qa-probes"
        probe_dir.mkdir(parents=True, exist_ok=True)
        try:
            for index, name in enumerate(robustness.get("probes") or []):
                output_path = probe_dir / f"{index}.out"
                output_path.write_bytes(_checker_probe_payload(name, baseline_output))
                raw = run_checker_to_files(
                    checker_command,
                    resolve_problem_regular_file(problem_dir, baseline["input"]),
                    resolve_problem_regular_file(problem_dir, baseline["jury_answer"]),
                    output_path,
                    timeout=cancellation.remaining(checker_timeout),
                    cwd=problem_dir,
                    memory_limit_mb=limits["memory"],
                    output_limit_bytes=limits["output_bytes"],
                    process_limit=limits["processes"],
                    cancel_check=cancellation.requested,
                )
                summary = _checker_summary(
                    {"id": name, "purpose": "robustness-probe", "expected": {}}, raw
                )
                summary["matched"] = not summary["infrastructure_failed"]
                summary["manual_review_required"] = raw.get("verdict") == "AC"
                probes.append(summary)
                if summary["infrastructure_failed"]:
                    return summaries, probes, "infrastructure-failed"
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
    if any(not item["matched"] for item in summaries):
        return summaries, probes, "expectation-failed"
    return summaries, probes, "passed"


def _builtin_contestant_command(behavior, limits):
    if behavior == "early-eof":
        code = "raise SystemExit(0)"
    elif behavior == "idle":
        code = f"import time; time.sleep({max(float(limits['time']) * 4, 10.0)!r})"
    elif behavior == "output-flood":
        code = "import os; b=b'0'*65536\nwhile True: os.write(1,b)"
    else:
        raise ProbHubError(
            f"unsupported built-in contestant behavior: {behavior}",
            code="judge_qa_behavior_invalid",
        )
    return [sys.executable, "-I", "-c", code]


def _run_interactor_qa(
    problem_dir,
    report,
    interactor_command,
    contestant_commands,
    config,
    limits,
    cancellation,
):
    interactive_limits = (
        {
            "idle_limit": limits["idle_limit"],
            "configured_idle_limit": limits["configured_idle_limit"],
            "transcript_limit": limits["transcript_limit"],
            "configured_transcript_limit": limits["configured_transcript_limit"],
        }
        if "transcript_limit" in limits
        else _interactive_limits(config, limits)
    )
    idle_limit = interactive_limits["idle_limit"]
    transcript_limit = interactive_limits["transcript_limit"]
    summaries = []
    for case in report["cases"]:
        contestant = case.get("contestant") or {}
        if contestant.get("source"):
            command = contestant_commands[contestant["source"].casefold()]
        else:
            command = _builtin_contestant_command(contestant.get("behavior"), limits)
        result = execute_interactive_session(
            command,
            interactor_command,
            resolve_problem_regular_file(problem_dir, case["input"]),
            resolve_problem_regular_file(problem_dir, case["jury_answer"]),
            work_dir=problem_dir,
            time_limit=min(limits["time"], cancellation.remaining()),
            memory_limit_mb=limits["memory"],
            idle_limit=idle_limit,
            transcript_limit=transcript_limit,
            output_limit_bytes=limits["output_bytes"],
            process_limit=limits["processes"],
            cancel_check=cancellation.requested,
        )
        summary = _interactor_summary(case, result)
        summary["transcript"]["limit"] = transcript_limit
        summaries.append(summary)
        if summary["infrastructure_failed"]:
            return summaries, "infrastructure-failed"
    if any(not item["matched"] for item in summaries):
        return summaries, "expectation-failed"
    return summaries, "passed"


def _live_identity(problem_dir, cancellation=None):
    check = cancellation.remaining if cancellation is not None else None
    if check is not None:
        check()
    config = read_yaml(Path(problem_dir) / "probhub.yaml")
    if check is not None:
        check()
    report = inspect_judge_qa(problem_dir, config, check=check)
    if not report.get("ok"):
        raise ProbHubError("Judge QA configuration changed or became invalid", code="inputs_changed")
    return config, report, (
        compute_source_hash(problem_dir, config, check=check),
        compute_data_hash(problem_dir, config, check=check),
        report.get("fixture_hash"),
    )


def _evidence_path(problem_dir):
    return Path(problem_dir) / ".probhub" / JUDGE_QA_EVIDENCE_FILENAME


def _publish_evidence(problem_dir, evidence, expected_identity, cancellation):
    with workspace_file_lock(
        problem_dir,
        Path(".probhub") / JUDGE_QA_EVIDENCE_LOCK_FILENAME,
        busy_code="judge_qa_evidence_busy",
        busy_message="another Judge QA evidence publisher is running",
        wait_timeout=0,
        no_follow=True,
    ):
        cancellation.remaining()
        _, _, current_identity = _live_identity(problem_dir, cancellation)
        if current_identity != expected_identity:
            raise ProbHubError(
                "Judge QA inputs changed before evidence publication",
                code="inputs_changed",
            )
        cancellation.remaining()
        atomic_write_json(_evidence_path(problem_dir), evidence)


def _build_evidence(
    identity,
    limits,
    validators,
    compilers,
    cases,
    probes,
    elapsed,
    overall_timeout,
):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": JUDGE_QA_EVIDENCE_SCHEMA_VERSION,
        "policy_version": JUDGE_QA_POLICY_VERSION,
        "sandbox_schema_version": SANDBOX_CACHE_SCHEMA_VERSION,
        "probhub_version": __version__,
        "source_hash": identity[0],
        "data_hash": identity[1],
        "fixture_hash": identity[2],
        "published_at": now,
        "measurement": {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "target_guarantee": False,
        },
        "limits": {
            "time": limits["time"],
            "memory": limits["memory"],
            "output_bytes": limits["output_bytes"],
            "configured_output_bytes": limits["configured_output_bytes"],
            "processes": limits["processes"],
            "overall_timeout": float(overall_timeout),
            "validator": {
                "timeout": VALIDATOR_TIMEOUT_SECONDS,
                "memory": VALIDATOR_MEMORY_LIMIT_MB,
                "output_bytes": VALIDATOR_OUTPUT_LIMIT_BYTES,
                "processes": DEFAULT_PROCESS_LIMIT,
            },
            **(
                {"checker_timeout": limits["checker_timeout"]}
                if "checker_timeout" in limits
                else {}
            ),
            **(
                {
                    "idle_limit": limits["idle_limit"],
                    "configured_idle_limit": limits["configured_idle_limit"],
                    "transcript_limit": limits["transcript_limit"],
                    "configured_transcript_limit": limits[
                        "configured_transcript_limit"
                    ],
                }
                if "transcript_limit" in limits
                else {}
            ),
        },
        "elapsed": elapsed,
        "compilers": compilers,
        "validators": validators,
        "cases": cases,
        "probes": probes,
        "cleanup": {"ok": True, "snapshot_removed": True},
    }


def _execute_snapshot(snapshot_problem, config, report, cancellation):
    limits = _runtime_limits(config)
    build_dir = Path(snapshot_problem) / ".judge-qa-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    compiled = []
    validator_command, compile_info = _compile_program(
        _program_path(snapshot_problem, config, "validator"), build_dir, "validator", cancellation
    )
    compiled.append(compile_info)
    validators = _validate_inputs(
        validator_command, snapshot_problem, report["cases"], cancellation
    )

    if report["judge_type"] == "custom":
        checker_command, compile_info = _compile_program(
            _program_path(snapshot_problem, config, "checker"), build_dir, "checker", cancellation
        )
        compiled.append(compile_info)
        cases, probes, status = _run_checker_qa(
            snapshot_problem, report, checker_command, limits, cancellation
        )
    else:
        limits.update(_interactive_limits(config, limits))
        interactor_command, compile_info = _compile_program(
            _program_path(snapshot_problem, config, "interactor"), build_dir, "interactor", cancellation
        )
        compiled.append(compile_info)
        contestant_commands = {}
        for case in report["cases"]:
            source = (case.get("contestant") or {}).get("source")
            if not source or source.casefold() in contestant_commands:
                continue
            command, compile_info = _compile_program(
                resolve_problem_regular_file(snapshot_problem, source),
                build_dir,
                "contestant",
                cancellation,
            )
            contestant_commands[source.casefold()] = command
            compiled.append(compile_info)
        cases, status = _run_interactor_qa(
            snapshot_problem,
            report,
            interactor_command,
            contestant_commands,
            config,
            limits,
            cancellation,
        )
        probes = []
    if any(item.get("kind") == "cpp17" for item in compiled):
        compiler_identity = _compiler_identity(cancellation)
        for item in compiled:
            if item.get("kind") == "cpp17":
                item["compiler_identity"] = compiler_identity
    return limits, validators, compiled, cases, probes, status


def judge_qa_problem(
    root,
    problem_dir,
    *,
    timeout=DEFAULT_JUDGE_QA_TIMEOUT_SECONDS,
    cancel_check=None,
):
    """Execute configured Checker/Interactor fixtures without mutating formal artifacts."""

    del root  # Reserved for the P2 CLI/workspace adapter.
    problem_dir = Path(problem_dir).resolve()
    started = time.monotonic()
    try:
        timeout = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        return _result(
            "infrastructure-failed",
            code="judge_qa_timeout_invalid",
            message=exc,
        )
    if (
        not math.isfinite(timeout)
        or timeout < 0
        or timeout > MAX_JUDGE_QA_TIMEOUT_SECONDS
    ):
        return _result(
            "infrastructure-failed",
            code="judge_qa_timeout_invalid",
            message=(
                "Judge QA timeout must be a finite non-negative number no greater than "
                f"{MAX_JUDGE_QA_TIMEOUT_SECONDS:g} seconds"
            ),
        )
    cancellation = _RunCancellation(timeout, cancel_check)
    try:
        with workspace_file_lock(
            problem_dir,
            ".probhub/judge.lock",
            busy_code="judge_busy",
            busy_message="another Judge is already running for this problem",
            no_follow=True,
        ):
            cancellation.remaining()
            config = read_yaml(problem_dir / "probhub.yaml")
            cancellation.remaining()
            report = inspect_judge_qa(
                problem_dir, config, check=cancellation.remaining
            )
            if not report.get("configured"):
                return _result(
                    "not-configured",
                    ok=True,
                    applicable=False,
                    judge_type=report.get("judge_type"),
                )
            cancellation.remaining()
            if not report.get("ok"):
                return _result(
                    "infrastructure-failed",
                    code="judge_qa_schema_invalid",
                    message="Judge QA configuration is invalid",
                    diagnostics=report.get("diagnostics") or [],
                )
            identity = (
                compute_source_hash(
                    problem_dir, config, check=cancellation.remaining
                ),
                compute_data_hash(
                    problem_dir, config, check=cancellation.remaining
                ),
                report.get("fixture_hash"),
            )
            temporary_root = Path(tempfile.mkdtemp(prefix="probhub-judge-qa-"))
            snapshot_problem = temporary_root / "problem"
            execution = None
            execution_error = None
            cleanup_error = None
            try:
                copied_config, copied_report = _create_snapshot(
                    problem_dir,
                    snapshot_problem,
                    config,
                    report,
                    identity,
                    cancellation,
                )
                execution = _execute_snapshot(
                    snapshot_problem, copied_config, copied_report, cancellation
                )
                _, _, snapshot_identity = _live_identity(
                    snapshot_problem, cancellation
                )
                if snapshot_identity != identity:
                    raise ProbHubError(
                        "Judge QA snapshot inputs changed during execution",
                        code="inputs_changed",
                    )
            except Exception as exc:
                execution_error = exc
            finally:
                try:
                    _remove_snapshot(temporary_root)
                except Exception as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                return _result(
                    "infrastructure-failed",
                    code="judge_qa_snapshot_cleanup_failed",
                    message=cleanup_error,
                )
            if execution_error is not None:
                if not isinstance(execution_error, (ProcessCancelled, _OverallTimeout)):
                    _, _, current_identity = _live_identity(
                        problem_dir, cancellation
                    )
                    if current_identity != identity:
                        return _result(
                            "infrastructure-failed",
                            code="inputs_changed",
                            message="Judge QA inputs changed during execution",
                        )
                raise execution_error
            limits, validators, compilers, cases, probes, status = execution
            cancellation.remaining()
            _, _, current_identity = _live_identity(problem_dir, cancellation)
            if current_identity != identity:
                return _result(
                    "infrastructure-failed",
                    code="inputs_changed",
                    message="Judge QA inputs changed during execution",
                    cases=cases,
                    probes=probes,
                )
            if status != "passed":
                return _result(
                    status,
                    code=(
                        "judge_qa_infrastructure_failed"
                        if status == "infrastructure-failed"
                        else "judge_qa_expectation_failed"
                    ),
                    cases=cases,
                    probes=probes,
                    validators=validators,
                )
            elapsed = time.monotonic() - started
            evidence = _build_evidence(
                identity,
                limits,
                validators,
                compilers,
                cases,
                probes,
                elapsed,
                timeout,
            )
            _publish_evidence(problem_dir, evidence, identity, cancellation)
            return _result(
                "passed",
                ok=True,
                code="judge_qa_passed",
                cases=cases,
                probes=probes,
                validators=validators,
                evidence=evidence,
                evidence_path=str(_evidence_path(problem_dir)),
                evidence_published=True,
            )
    except ProcessCancelled as exc:
        if cancellation.cause == "overall_timeout":
            return _result(
                "infrastructure-failed",
                code="judge_qa_overall_timeout",
                message="Judge QA overall deadline exceeded",
            )
        return _result("cancelled", code="cancelled", message=exc)
    except _OverallTimeout as exc:
        return _result(
            "infrastructure-failed",
            code="judge_qa_overall_timeout",
            message=exc,
        )
    except (ProbHubError, OSError, OutputBudgetError, ValueError, TypeError) as exc:
        return _result(
            "infrastructure-failed",
            code=getattr(exc, "code", None) or "judge_qa_infrastructure_failed",
            message=exc,
        )
