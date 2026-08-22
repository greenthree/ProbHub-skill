import hashlib
import json
import os
import platform
import re
import shutil
import signal
import sys
import tempfile
import time

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(SCRIPT_DIR)
if PACKAGE_ROOT in sys.path:
    sys.path.remove(PACKAGE_ROOT)
sys.path.insert(0, PACKAGE_ROOT)

from probhub.calibration import (
    MEASUREMENT_NOTE,
    calibration_policy,
)
from probhub.build_lock import workspace_file_lock
from probhub.errors import ProbHubError
from probhub.io import read_bounded_text
from probhub.judge_cache import (
    CACHE_FILENAME,
    CACHE_LIMITS,
    CACHE_SCHEMA_VERSION,
    SandboxCache,
    calibration_probe_cache_key,
    file_digest as _file_digest,
    hash_parts as _hash_parts,
    testcase_cache_key,
    validator_cache_key,
)
from probhub.judge_results import (
    classify_contestant_failure,
    classify_process_status,
    infer_failed_status,
)
from probhub.output_compare import compare_standard_output
from probhub.problem_paths import ProblemPathError, resolve_problem_regular_file
from probhub.special_judges import execute_interactive_session, run_checker_to_files
from probhub.process_control import (
    DEFAULT_PROCESS_LIMIT,
    OutputBudgetError,
    PROCESS_CLEANUP_FAILED,
    ProcessCancelled,
    VALIDATOR_MEMORY_LIMIT_MB,
    VALIDATOR_OUTPUT_LIMIT_BYTES,
    VALIDATOR_TIMEOUT_SECONDS,
    cancellation_requested,
    run_managed_to_files,
)
from probhub.solutions import (
    analyze_solution_verification,
    case_group_names,
    normalize_data_groups,
    normalize_expected,
    normalize_solution_entries,
    resolve_solution_source,
    select_expectation_cases,
    select_run_cases,
    select_target_cases,
)

REFERENCES_DIR = os.path.join(PACKAGE_ROOT, "references")
DEFAULT_TIME_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = 256
DEFAULT_OUTPUT_LIMIT = 64
MAX_TOOL_DIAGNOSTIC_BYTES = 8 * 1024 * 1024
PROTOCOL_NAME = "probhub.local_judge"
PROTOCOL_VERSION = 1
SAMPLE_ANSWER_SCHEMA_VERSION = 1
SAMPLE_OUTPUT_PREVIEW_BYTES = 160
_COMPILER_IDENTITY = None


class BinaryChangedError(RuntimeError):
    pass


def binary_identity(path):
    """Return the published binary identity used by one Judge session."""
    absolute = os.path.abspath(path)
    digest = hashlib.sha256()
    with open(absolute, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        stat = os.fstat(stream.fileno())
    return {
        "path": absolute,
        "size": int(stat.st_size),
        "digest": digest.hexdigest(),
    }


def verify_binary_identities(entries):
    """Check compiled binaries once before the first Judge execution.

    The caller supplies identities captured immediately after compilation (or
    from a validated cache hit).  A single changed/missing file invalidates the
    whole Judge session and must not become a contestant verdict.
    """
    for entry in entries:
        expected = entry.get("identity") or {}
        path = expected.get("path")
        try:
            actual = binary_identity(path)
        except (OSError, TypeError, ValueError) as exc:
            return {
                "code": "binary_changed",
                "failure_kind": "infrastructure",
                "role": entry.get("role"),
                "program": entry.get("program"),
                "path": path,
                "expected_digest": expected.get("digest"),
                "actual_digest": None,
                "message": f"compiled binary is unavailable before first execution: {path} ({exc})",
            }
        if (
            actual.get("digest") != expected.get("digest")
            or actual.get("size") != expected.get("size")
        ):
            return {
                "code": "binary_changed",
                "failure_kind": "infrastructure",
                "role": entry.get("role"),
                "program": entry.get("program"),
                "path": path,
                "expected_digest": expected.get("digest"),
                "actual_digest": actual.get("digest"),
                "expected_size": expected.get("size"),
                "actual_size": actual.get("size"),
                "message": f"compiled binary changed before first execution: {path}",
            }
    return None


def publish_compiled_binary(staged_binary, out_bin, staged_identity):
    """Atomically publish a compiled binary and restore the previous file on mismatch."""
    previous = staged_binary + ".previous"
    had_previous = os.path.isfile(out_bin)
    if had_previous:
        shutil.copyfile(out_bin, previous)
    published = False
    try:
        os.replace(staged_binary, out_bin)
        published = True
        actual = binary_identity(out_bin)
        if (
            actual["digest"] != staged_identity["digest"]
            or actual["size"] != staged_identity["size"]
        ):
            raise BinaryChangedError(
                "published binary identity differs from staged binary"
            )
        return actual
    except Exception:
        if not published:
            raise
        try:
            if had_previous and os.path.isfile(previous):
                os.replace(previous, out_bin)
            elif not had_previous and os.path.lexists(out_bin):
                os.remove(out_bin)
        except OSError as rollback_error:
            raise BinaryChangedError(
                "compiled binary publish failed and the previous binary could not be restored: "
                f"{rollback_error}"
            ) from rollback_error
        raise


def _cancel_signal_handler(_signum, _frame):
    raise ProcessCancelled("execution cancelled")


def install_cancellation_handlers():
    signal.signal(signal.SIGTERM, _cancel_signal_handler)
    if platform.system() == "Windows" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _cancel_signal_handler)


def normalize_sample_output(payload):
    """Normalize CRLF and bare CR line endings while preserving every other byte."""
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
    """Compare accepted stdout and the stored sample answer as normalized bytes."""
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


def compiler_identity():
    global _COMPILER_IDENTITY
    if _COMPILER_IDENTITY is not None:
        return _COMPILER_IDENTITY
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-compiler-version-") as temp:
            stdout_path = os.path.join(temp, "stdout")
            stderr_path = os.path.join(temp, "stderr")
            result = run_managed_to_files(
                ["g++", "--version"],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=10,
                memory_limit_mb=512,
                output_limit_bytes=1024 * 1024,
                process_limit=8,
            )
            with open(stdout_path, "r", encoding="utf-8", errors="replace") as stream:
                stdout = stream.read()
            with open(stderr_path, "r", encoding="utf-8", errors="replace") as stream:
                stderr = stream.read()
        if result["reason"] != "completed" or result["returncode"] != 0:
            raise RuntimeError(result.get("message") or result["reason"])
        first_line = (stdout or stderr).splitlines()[0]
        _COMPILER_IDENTITY = first_line.strip()
    except Exception as exc:
        _COMPILER_IDENTITY = f"g++-unknown:{exc}"
    return _COMPILER_IDENTITY


def source_fingerprint(src_file, prob_dir, kind=None):
    src_file = os.path.abspath(src_file)
    prob_dir = os.path.abspath(prob_dir)
    dependencies = [src_file]
    source_text = ""
    try:
        with open(src_file, "r", encoding="utf-8", errors="replace") as stream:
            source_text = stream.read()
    except OSError:
        pass

    for root, directories, files in os.walk(prob_dir):
        directories[:] = [name for name in directories if name != ".probhub"]
        for name in files:
            if name.endswith((".h", ".hpp")):
                dependencies.append(os.path.join(root, name))
    if "testlib.h" in source_text:
        dependencies.append(os.path.join(REFERENCES_DIR, "testlib.h"))

    parts = [
        CACHE_SCHEMA_VERSION,
        platform.system(),
        platform.machine(),
        compiler_identity(),
        kind or "unknown",
        "-O2",
        "-std=c++17",
        "-static" if platform.system() == "Windows" else "dynamic",
        "-DFOR_LINUX" if platform.system() == "Windows" and kind == "validator" else "native-lines",
    ]
    for path in sorted(set(os.path.abspath(item) for item in dependencies)):
        if os.path.isfile(path):
            try:
                label = os.path.relpath(path, prob_dir).replace(os.sep, "/")
            except ValueError:
                label = path.replace(os.sep, "/")
            parts.extend((label, _file_digest(path)))
    return _hash_parts(*parts)


class Reporter:
    def __init__(self, jsonl=False):
        self.jsonl = jsonl

    def text(self, msg=""):
        if not self.jsonl:
            print(msg, flush=True)

    def event(self, type_, **payload):
        if self.jsonl:
            event = {
                "protocol": PROTOCOL_NAME,
                "protocol_version": PROTOCOL_VERSION,
                "type": type_,
                **payload,
            }
            print(json.dumps(event, ensure_ascii=False), flush=True)


def compile_cpp(
    src_file,
    out_bin,
    reporter=None,
    kind=None,
    display_file=None,
    cache=None,
    fingerprint=None,
):
    if reporter is None:
        reporter = Reporter(False)
    if not os.path.exists(src_file):
        return False
    file_name = display_file or os.path.basename(src_file)
    fingerprint = fingerprint or source_fingerprint(src_file, os.path.dirname(src_file), kind)
    cache_key = str(file_name).replace(os.sep, "/")

    if cache and cache.enabled:
        cached = cache.get("compile", cache_key)
        binary_matches = False
        if cached and cached.get("fingerprint") == fingerprint and os.path.isfile(out_bin):
            try:
                current_identity = binary_identity(out_bin)
                binary_matches = (
                    cached.get("binary_digest") == current_identity["digest"]
                    and (
                        cached.get("binary_size") is None
                        or cached.get("binary_size") == current_identity["size"]
                    )
                )
            except OSError:
                binary_matches = False
        if binary_matches:
            reporter.text(f"[*] Compiling {file_name}... cached")
            reporter.event(
                "compile",
                kind=kind or "unknown",
                file=file_name,
                ok=True,
                stderr="",
                cached=True,
                binary_digest=current_identity["digest"],
                binary_size=current_identity["size"],
            )
            return current_identity
        if cached is not None:
            cache.stats["compile_hits"] -= 1
            cache.stats["compile_misses"] += 1
            cache.delete("compile", cache_key)

    reporter.text(f"[*] Compiling {file_name}...")
    source_dir = os.path.dirname(os.path.abspath(src_file))
    compile_root = os.path.join(source_dir, ".probhub", "compile")
    try:
        os.makedirs(compile_root, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=compile_root) as temp:
            # MinGW's narrow argv handling cannot reliably open absolute paths
            # containing non-ASCII characters. The process cwd is established
            # through the wide Windows API, so compiler path arguments remain
            # ASCII and relative. Python publishes the binary after success.
            staged_binary = os.path.join(
                temp,
                "program.exe" if platform.system() == "Windows" else "program",
            )
            shutil.copyfile(
                os.path.join(REFERENCES_DIR, "testlib.h"),
                os.path.join(temp, "testlib.h"),
            )
            source_arg = os.path.relpath(os.path.abspath(src_file), source_dir)
            output_arg = os.path.relpath(staged_binary, source_dir)
            include_arg = os.path.relpath(temp, source_dir)
            flags = [
                "g++", source_arg, "-o", output_arg,
                "-O2", "-std=c++17", "-I", include_arg,
            ]
            if platform.system() == "Windows":
                flags.insert(4, "-static")
                if kind == "validator":
                    # DOMjudge validators run on Linux and accept LF line endings. Emulate
                    # that behavior so Git-normalized test data is validated consistently.
                    flags.append("-DFOR_LINUX")
            stdout_path = os.path.join(temp, "compiler.out")
            stderr_path = os.path.join(temp, "compiler.stderr")
            ret = run_managed_to_files(
                flags,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=60.0,
                memory_limit_mb=2048,
                output_limit_bytes=8 * 1024 * 1024,
                process_limit=DEFAULT_PROCESS_LIMIT,
                cwd=source_dir,
            )
            try:
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as stream:
                    stderr = stream.read()
            except OSError:
                stderr = ""
            ok = ret["reason"] == "completed" and ret["returncode"] == 0
            staged_identity = None
            if ok:
                staged_identity = binary_identity(staged_binary)
                published_identity = publish_compiled_binary(
                    staged_binary,
                    out_bin,
                    staged_identity,
                )
            else:
                published_identity = None
        if ret["reason"] != "completed":
            stderr = (stderr + "\n" + (ret["message"] or ret["reason"])).strip()
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=ok,
            stderr=stderr,
            cached=False,
            **(
                {
                    "binary_digest": published_identity["digest"],
                    "binary_size": published_identity["size"],
                }
                if published_identity is not None
                else {}
            ),
        )
        if not ok:
            if cache:
                cache.delete("compile", cache_key)
            reporter.text(f"[-] Compile failed:\n{stderr}")
            return False
        if cache:
            cache.set(
                "compile",
                cache_key,
                {
                    "fingerprint": fingerprint,
                    "binary_digest": published_identity["digest"],
                    "binary_size": published_identity["size"],
                },
            )
        return published_identity
    except BinaryChangedError as e:
        if cache:
            cache.delete("compile", cache_key)
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=False,
            stderr=str(e),
            cached=False,
            code="binary_changed",
            failure_kind="infrastructure",
        )
        reporter.text(f"[-] Compile error: {e}")
        raise
    except Exception as e:
        if cache:
            cache.delete("compile", cache_key)
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=False,
            stderr=str(e),
            cached=False,
        )
        reporter.text(f"[-] Compile error: {e}")
        return False


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def read_probhub_config(prob_dir):
    config_path = os.path.join(prob_dir, "probhub.yaml")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read probhub.yaml: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("probhub.yaml must contain a mapping")
    return config


def _entry_file(entry):
    if isinstance(entry, dict):
        return entry.get("file")
    return entry


def resolve_problem_path(prob_dir, entry):
    relative = _entry_file(entry)
    if not relative:
        return None
    try:
        return os.fspath(resolve_problem_regular_file(prob_dir, str(relative)))
    except ProblemPathError:
        return None


def display_problem_path(prob_dir, path):
    try:
        return os.path.relpath(path, prob_dir).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def binary_path_for_source(source_path):
    return os.path.splitext(source_path)[0] + ".exe"


def _normalize_expected(kind, entry):
    return normalize_expected(kind, entry)


def _require_schema_config(config):
    if not isinstance(config, dict):
        raise ValueError("Workspace Schema v1 problem config is required")
    if config.get("schema_version") != 1:
        raise ValueError("unsupported problem schema_version; migrate to Workspace Schema v1")
    return config


def discover_solution_entries(prob_dir, config=None):
    config = _require_schema_config(config)
    solutions = {"std": [], "brute": [], "wrong": []}
    for kind, entries in normalize_solution_entries(config).items():
        for entry in entries:
            source_path = resolve_problem_path(prob_dir, entry.get("file"))
            if source_path:
                solutions[kind].append({
                    "path": source_path,
                    "file": entry.get("file"),
                    "expected": entry.get("expected"),
                    "run_on": entry.get("run_on"),
                    "run_on_error": entry.get("run_on_error"),
                    "expected_groups_configured": entry.get("expected_groups_configured", False),
                    "run_on_configured": isinstance(entry.get("raw"), dict)
                    and "run_on" in entry["raw"],
                    "index": entry.get("index"),
                })
    return solutions


def discover_solution_sources(prob_dir, config=None):
    """Compatibility view used by existing callers/tests."""
    return {
        kind: [entry["path"] for entry in entries]
        for kind, entries in discover_solution_entries(prob_dir, config).items()
    }

def discover_validator_source(prob_dir, config=None):
    config = _require_schema_config(config)
    return resolve_problem_path(prob_dir, ((config.get("judge") or {}).get("validator")))


def configured_judge_type(config=None):
    config = _require_schema_config(config)
    value = str(((config.get("judge") or {}).get("type", "standard"))).strip().lower()
    return "custom" if value == "checker" else value


def discover_judge_source(prob_dir, config, key):
    config = _require_schema_config(config)
    return resolve_problem_path(prob_dir, ((config.get("judge") or {}).get(key)))


def read_problem_limits(prob_dir, config=None):
    config = _require_schema_config(config)
    time_limit = DEFAULT_TIME_LIMIT
    memory_limit = DEFAULT_MEMORY_LIMIT
    limits = config.get("limits") or {}
    if "time" in limits:
        time_limit = _safe_float(limits.get("time"), time_limit)
    if "memory" in limits:
        memory_limit = _safe_int(limits.get("memory"), memory_limit)

    return max(time_limit, 0.1), max(memory_limit, 1)


def read_problem_output_limit(prob_dir, config=None):
    config = _require_schema_config(config)
    limits = config.get("limits") or {}
    output_limit = _safe_int(limits.get("output"), DEFAULT_OUTPUT_LIMIT)
    process_limit = _safe_int(limits.get("processes"), DEFAULT_PROCESS_LIMIT)
    return max(output_limit, 1), max(process_limit, 1)


def _failed_status(returncode, stderr, memory_enforced, peak_memory_mb, memory_limit):
    """Compatibility wrapper for the Core result normalizer."""
    return infer_failed_status(
        returncode, stderr, memory_enforced, peak_memory_mb, memory_limit
    )


def _remove_file_with_retries(path):
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

def run_program_to_file(
    bin_path,
    in_file,
    output_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    """Run one contestant process with tree-wide time, memory and output controls."""
    stderr_path = output_file + ".stderr"
    try:
        result = run_managed_to_files(
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
            stderr = open(stderr_path, "r", encoding="utf-8", errors="replace").read().strip()
        except OSError:
            stderr = ""
        details = {
            "output_bytes": int(result.get("output_bytes") or 0),
            "termination_reason": result.get("reason"),
        }
        reason = result["reason"]
        if reason == "time_limit":
            return "TLE", result["time"], result["memory"], result["memory_enforced"], result["message"], details
        if reason == "output_limit":
            return "OLE", result["time"], result["memory"], result["memory_enforced"], result["message"], details
        if reason == "memory_limit":
            return "MLE", result["time"], result["memory"], result["memory_enforced"], result["message"], details
        if reason == "process_limit":
            return "RE", result["time"], result["memory"], result["memory_enforced"], result["message"], details
        if reason == PROCESS_CLEANUP_FAILED:
            return "FAIL", result["time"], result["memory"], result["memory_enforced"], result["message"], details
        if result["returncode"] != 0:
            status = infer_failed_status(
                result["returncode"],
                stderr,
                result["memory_enforced"],
                result["memory"],
                memory_limit,
            )
            if status == "MLE" and result.get("reason") != "memory_limit":
                details["termination_reason"] = "inferred_memory_limit"
            return status, result["time"], result["memory"], result["memory_enforced"], stderr, details
        return "AC", result["time"], result["memory"], result["memory_enforced"], "", details
    except OutputBudgetError as exc:
        return "FAIL", 0.0, None, False, str(exc), {
            "output_bytes": 0,
            "termination_reason": "output_control_error",
        }
    except OSError as exc:
        return "RE", 0.0, None, False, str(exc), {"output_bytes": 0}
    finally:
        _remove_file_with_retries(stderr_path)


def _temporary_output_path(bin_path):
    descriptor, path = tempfile.mkstemp(
        prefix=".probhub-output-", suffix=".out", dir=os.path.dirname(bin_path)
    )
    os.close(descriptor)
    return path


def _probe_status(result, memory_limit):
    status = classify_process_status(result, memory_limit)
    return "completed" if status == "AC" else status


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
):
    """Measure one expected-TLE case beyond the official TL without changing verdicts."""
    probe_factor = max(float(probe_factor), 1.0)
    probe_limit = float(time_limit) * probe_factor
    key = calibration_probe_cache_key(
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
        result = dict(cached_result)
        result["cached"] = True
        return result

    output_file = _temporary_output_path(bin_path)
    stderr_path = output_file + ".stderr"
    try:
        try:
            run = run_managed_to_files(
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
            result = {
                "status": "TLE",
                "case": case_name,
                "limit": float(time_limit),
                "observed": round(observed, 6),
                "limit_ratio": round(observed / float(time_limit), 6),
                "probe_limit": probe_limit,
                "process_status": _probe_status(run, memory_limit),
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
        _remove_file_with_retries(output_file)
        _remove_file_with_retries(stderr_path)


def run_standard_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    capture_sample_answer=False,
):
    output_file = _temporary_output_path(bin_path)
    try:
        status, elapsed, memory, memory_enforced, message, details = run_program_to_file(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        if capture_sample_answer:
            details["sample_answer"] = compare_sample_answer(output_file, ans_file)
        if status != "AC":
            return status, elapsed, memory, memory_enforced, message, details
        with open(ans_file, "r", encoding="utf-8", errors="replace") as answer, open(
            output_file, "r", encoding="utf-8", errors="replace"
        ) as actual:
            matched, message = compare_standard_output(answer.read(), actual.read())
        status = "AC" if matched else "WA"
        return status, elapsed, memory, memory_enforced, message, details
    finally:
        for _ in range(20):
            try:
                os.remove(output_file)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05)


def run_sample_answer_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    """Run the first accepted solution and compare only normalized raw stdout.

    Sample-check deliberately does not invoke a custom Checker. Its sole
    invariant is that the program reproduces the stored sample answer bytes
    after newline normalization.
    """
    output_file = _temporary_output_path(bin_path)
    try:
        status, elapsed, memory, memory_enforced, message, details = run_program_to_file(
            bin_path,
            in_file,
            output_file,
            time_limit,
            memory_limit,
            output_limit,
            process_limit,
        )
        details["sample_answer"] = compare_sample_answer(output_file, ans_file)
        return status, elapsed, memory, memory_enforced, message, details
    finally:
        _remove_file_with_retries(output_file)


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
):
    """Run a contestant and adapt the shared Checker result to the tuple API."""
    output_file = _temporary_output_path(bin_path)
    try:
        status, elapsed, memory, memory_enforced, message, details = run_program_to_file(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        if capture_sample_answer:
            details["sample_answer"] = compare_sample_answer(output_file, ans_file)
        if status != "AC":
            termination_reason = details.get("termination_reason")
            if not termination_reason:
                termination_reason = "start_error"
                details["termination_reason"] = termination_reason
            details.update(classify_contestant_failure(status, termination_reason))
            return status, elapsed, memory, memory_enforced, message, details
        checker = run_checker_to_files(
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
        _remove_file_with_retries(output_file)


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
):
    """Connect contestant and interactor with transcript, idle and total deadlines."""
    result = execute_interactive_session(
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
):
    if judge_type == "custom":
        status, elapsed, memory, memory_enforced, message, details = run_custom_testcase(
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
        return status, elapsed, memory, memory_enforced, message, details
    if judge_type == "interactive":
        return run_interactive_testcase(
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
    status, elapsed, memory, memory_enforced, message, details = run_standard_testcase(
        bin_path,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        capture_sample_answer=capture_sample_answer,
    )
    return status, elapsed, memory, memory_enforced, message, details

def run_validator(val_bin, in_file, timeout=VALIDATOR_TIMEOUT_SECONDS):
    """Validate LF-normalized input under the shared process-tree controller."""
    try:
        with open(in_file, "rb") as fin:
            data = fin.read().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory(prefix="probhub-validator-") as temp:
            stdout_path = os.path.join(temp, "validator.out")
            stderr_path = os.path.join(temp, "validator.stderr")
            result = run_managed_to_files(
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
                stderr = open(stderr_path, "r", encoding="utf-8", errors="replace").read().strip()
            except OSError:
                stderr = ""
            if result["reason"] != "completed":
                return False, result["message"] or result["reason"].replace("_", " ")
            return result["returncode"] == 0, stderr
    except OSError as exc:
        return False, str(exc)


def _data_groups(config=None):
    return normalize_data_groups(config or {})


def _testcase_groups(case_name, suite, base_name, groups):
    return case_group_names(case_name, suite, base_name, groups)


def collect_testcases(prob_dir, config=None):
    testcases = []
    data_config = (config or {}).get("data") or {}
    groups = _data_groups(config)
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        data_dir = os.path.join(prob_dir, data_config.get(f"{suite}_dir", default))
        if not os.path.isdir(data_dir):
            continue
        for in_name in sorted([f for f in os.listdir(data_dir) if f.endswith(".in")]):
            base_name = in_name[:-3]
            case_name = f"{suite}/{base_name}"
            testcases.append({
                "suite": suite,
                "name": base_name,
                "case": case_name,
                "groups": _testcase_groups(case_name, suite, base_name, groups),
                "in_path": os.path.join(data_dir, in_name),
                "ans_path": os.path.join(data_dir, f"{base_name}.ans"),
            })
    return testcases


def _select_expectation_cases(kind, entry, testcases, group_definitions):
    # A full-range accepted remains the correctness baseline even if a
    # configuration carries expected.groups. Partial accepted entries are
    # distinguishable by their explicit run_on domain.
    if kind == "std" and entry.get("run_on") is None:
        return list(testcases), []
    return select_expectation_cases(kind, entry, testcases, group_definitions)


def _expectation_cases(kind, testcases, expected, group_definitions, program_name):
    """Compatibility adapter for callers that still pass a split expectation."""
    return _select_expectation_cases(
        kind,
        {"file": program_name, "expected": expected, "run_on": None},
        testcases,
        group_definitions,
    )


def _solution_execution_plan(source_groups, testcases, group_definitions):
    group_names = {group["name"] for group in group_definitions}
    plans = {"std": [], "brute": [], "wrong": []}
    errors = []
    for kind, entries in source_groups.items():
        for entry in entries:
            program = str(entry.get("file") or entry.get("path") or "").replace("\\", "/")
            normalized = {
                "file": program,
                "expected": entry.get("expected") or _normalize_expected(kind, None),
                "run_on": entry.get("run_on"),
            }
            run_on = normalized["run_on"]
            executed, skipped = select_run_cases(normalized, testcases)
            selected, selected_groups = _select_expectation_cases(
                kind, normalized, testcases, group_definitions
            )
            targeted, target_groups = select_target_cases(
                normalized, testcases, group_definitions
            )
            executed_names = {case["case"] for case in executed}
            missing = [case["case"] for case in selected if case["case"] not in executed_names]
            selected_names = {case["case"] for case in selected}
            missing_targets = [
                case["case"]
                for case in targeted
                if case["case"] not in executed_names
                and case["case"] not in selected_names
            ]
            unknown = sorted(set(run_on or []) - group_names)
            empty = sorted(
                name
                for name in (run_on or [])
                if not any(name in (case.get("groups") or []) for case in testcases)
            )
            reason = None
            result_code = None
            if entry.get("run_on_error"):
                reason = entry["run_on_error"]
                result_code = "invalid_solution_run_on"
            elif kind == "std" and entry.get("index") == 0 and run_on is not None:
                reason = "first accepted solution must run on all cases"
                result_code = "primary_accepted_run_on_forbidden"
            elif (
                kind == "std"
                and entry.get("index", 0) > 0
                and run_on is not None
                and not entry.get("expected_groups_configured")
            ):
                reason = "partial accepted solution must explicitly declare expected.groups"
                result_code = "partial_accepted_expected_groups_required"
            elif unknown:
                reason = "run_on references unknown groups: " + ", ".join(unknown)
                result_code = "solution_run_on_unknown_group"
            elif empty:
                reason = "run_on groups match no test cases: " + ", ".join(empty)
                result_code = "solution_run_on_has_no_cases"
            elif missing:
                reason = f"run_on skips {len(missing)} case(s) selected by expectation"
                result_code = "solution_run_on_expectation_gap"
            elif missing_targets:
                reason = (
                    f"run_on skips {len(missing_targets)} case(s) required by "
                    "data.groups.targets"
                )
                result_code = "solution_run_on_target_gap"
            plan = {
                **entry,
                "program": program,
                "execution_cases": executed,
                "skipped_cases": skipped,
                "expectation_cases": selected,
                "expectation_groups": selected_groups,
                "coverage_ok": reason is None,
                "unexecuted_expected_cases": missing,
                "unexecuted_target_cases": missing_targets,
                "target_groups": target_groups,
            }
            plans[kind].append(plan)
            if reason is not None:
                errors.append({
                    "kind": kind,
                    "program": program,
                    "run_on": list(run_on or []),
                    "reason": reason,
                    "result_code": result_code,
                    "coverage_ok": False,
                    "unexecuted_expected_cases": missing,
                    "unexecuted_target_cases": missing_targets,
                    "target_groups": target_groups,
                })
    return plans, errors


def _result_snapshot(item):
    if item is None:
        return None
    snapshot = {
        "case": item.get("case"),
        "groups": list(item.get("groups") or []),
        "status": item.get("status"),
        "message": item.get("message", ""),
    }
    for key in (
        "time",
        "memory",
        "memory_enforced",
        "output_bytes",
        "termination_reason",
        "cached",
    ):
        if key in item:
            snapshot[key] = item.get(key)
    return snapshot


def evaluate_expectation(
    kind,
    program_name,
    expected,
    case_results,
    testcases,
    group_definitions,
    run_on=None,
    execution_cases=None,
    skipped_cases=None,
):
    entry = {"file": program_name, "expected": expected, "run_on": run_on}
    selected_cases, groups = _select_expectation_cases(
        kind, entry, testcases, group_definitions
    )
    if execution_cases is None:
        execution_cases, inferred_skipped = select_run_cases(entry, testcases)
        if skipped_cases is None:
            skipped_cases = inferred_skipped
    if skipped_cases is None:
        executed_names = {case["case"] for case in execution_cases}
        skipped_cases = [case for case in testcases if case["case"] not in executed_names]
    execution_names = {case["case"] for case in execution_cases}
    unexecuted_expected = [
        case["case"] for case in selected_cases if case["case"] not in execution_names
    ]
    selected_names = {case["case"] for case in selected_cases}
    selected_results = [item for item in case_results if item["case"] in selected_names]
    statuses = set(expected.get("status") or [])
    forbidden_statuses = set(expected.get("forbid") or [])
    all_required = bool(expected.get("all"))
    matched = [item for item in selected_results if item["status"] in statuses]
    forbidden = [item for item in case_results if item["status"] in forbidden_statuses]
    status_ok = bool(selected_results) and (
        len(matched) == len(selected_results) if all_required else bool(matched)
    )
    first_non_ac = next((item for item in case_results if item["status"] != "AC"), None)
    first_expected_match = matched[0] if matched else None
    first_forbidden = forbidden[0] if forbidden else None
    return {
        "kind": kind,
        "program": program_name,
        "expected_statuses": sorted(statuses),
        "forbidden_statuses": sorted(forbidden_statuses),
        "groups": groups,
        "run_on": list(run_on or []),
        "executed_cases": [case["case"] for case in execution_cases],
        "skipped_cases": [case["case"] for case in skipped_cases],
        "coverage_ok": not unexecuted_expected,
        "unexecuted_expected_cases": unexecuted_expected,
        "all": all_required,
        "ok": status_ok and not forbidden and not unexecuted_expected,
        "matched_cases": [item["case"] for item in matched],
        "selected_cases": [item["case"] for item in selected_results],
        "forbidden_cases": [item["case"] for item in forbidden],
        "first_non_ac": _result_snapshot(first_non_ac),
        "first_expected_match": _result_snapshot(first_expected_match),
        "first_forbidden": _result_snapshot(first_forbidden),
    }


def _status_only_resource_kill(status, case_result, limits):
    if case_result is None:
        return {
            "status": status,
            "case": None,
            "groups": [],
            "observed": None,
            "limit": limits[status],
            "limit_ratio": None,
            "evidence_kind": "unavailable",
            "supported": True,
            "censored": True,
        }
    if status == "MLE":
        observed = case_result.get("memory")
        termination_reason = case_result.get("termination_reason")
        supported = (
            termination_reason == "memory_limit"
            and bool(case_result.get("memory_enforced"))
            and observed is not None
        )
        evidence_kind = (
            "lower_bound" if supported
            else "inferred" if termination_reason == "inferred_memory_limit"
            else "unavailable"
        )
    else:
        output_bytes = case_result.get("output_bytes")
        observed = (
            float(output_bytes) / (1024 * 1024)
            if isinstance(output_bytes, (int, float)) and output_bytes > 0
            else None
        )
        supported = (
            case_result.get("termination_reason") == "output_limit"
            and observed is not None
        )
        evidence_kind = "lower_bound" if supported else "unavailable"
    limit = float(limits[status])
    return {
        "status": status,
        "case": case_result.get("case"),
        "groups": list(case_result.get("groups") or []),
        "observed": round(float(observed), 6) if observed is not None else None,
        "limit": limit,
        "limit_ratio": (
            round(float(observed) / limit, 6)
            if supported and limit > 0
            else None
        ),
        "evidence_kind": evidence_kind,
        "supported": supported,
        "censored": True,
    }


def build_solution_calibration(
    kind,
    case_results,
    time_limit,
    policy,
    resource_kills,
):
    timed = [item for item in case_results if isinstance(item.get("time"), (int, float))]
    max_item = max(timed, key=lambda item: item["time"]) if timed else None
    max_time = float(max_item["time"]) if max_item is not None else None
    ratio = (
        max_time / float(time_limit)
        if max_time is not None and float(time_limit) > 0
        else None
    )
    headroom = (
        float(time_limit) / max_time
        if max_time is not None and max_time > 0
        else None
    )
    required = policy["accepted_time_multiplier"]
    return {
        "schema_version": 1,
        "target_guarantee": False,
        "max_time": round(max_time, 6) if max_time is not None else None,
        "max_time_case": max_item.get("case") if max_item is not None else None,
        "time_limit": float(time_limit),
        "time_limit_ratio": round(ratio, 6) if ratio is not None else None,
        "headroom_factor": round(headroom, 6) if headroom is not None else None,
        "fresh_cases": sum(1 for item in case_results if not item.get("cached")),
        "cached_cases": sum(1 for item in case_results if item.get("cached")),
        "accepted_check": {
            "applicable": kind == "std",
            "required_multiplier": required,
            "ok": (
                kind != "std"
                or (headroom is not None and headroom + 1e-12 >= required)
            ),
        },
        "resource_kills": resource_kills,
    }


def cached_case_result(cache, key, require_sample_answer=False):
    cached = cache.get("case", key)
    if (
        cached is not None
        and require_sample_answer
        and not valid_sample_answer_summary(
            (cached.get("details") or {}).get("sample_answer")
        )
    ):
        # A manually edited or partially written entry must never bypass the
        # strict sample invariant. Count it as a miss and refresh this key.
        cache.stats["case_hits"] -= 1
        cache.stats["case_misses"] += 1
        cache.delete("case", key)
        return None
    return cached


def save_case_result(
    cache,
    key,
    status,
    elapsed,
    memory,
    memory_enforced,
    message,
    details,
):
    cache.set(
        "case",
        key,
        {
            "status": status,
            "time": round(elapsed, 6),
            "memory": memory,
            "memory_enforced": memory_enforced,
            "message": message,
            "details": details,
        },
    )


def sample_check_event_payload(
    program_name,
    testcase,
    run_status,
    run_message,
    details,
    cached,
    judge_type,
):
    comparison = (details or {}).get("sample_answer") or {
        "schema_version": SAMPLE_ANSWER_SCHEMA_VERSION,
        "normalization": "crlf_cr_to_lf",
        "matches": False,
        "actual": None,
        "expected": None,
        "first_difference": None,
        "error": "sample output summary is unavailable",
    }
    matches = comparison.get("matches") is True
    execution_completed = (details or {}).get("termination_reason") == "completed"
    mismatch = (
        execution_completed
        and run_status in {"AC", "WA"}
        and not matches
    )
    return {
        "applicable": True,
        "ok": matches and execution_completed and run_status == "AC",
        "program": program_name,
        "case": testcase["case"],
        "judge_type": judge_type,
        "run_status": run_status,
        "execution_completed": execution_completed,
        "message": run_message,
        "matches": matches,
        "mismatch": mismatch,
        "normalization": comparison.get("normalization"),
        "actual": comparison.get("actual"),
        "expected": comparison.get("expected"),
        "first_difference": comparison.get("first_difference"),
        **(
            {"error": comparison.get("error")}
            if comparison.get("error")
            else {}
        ),
        "cached": cached,
    }


def run_sample_check_mode(
    prob_dir,
    config,
    testcases,
    reporter,
    cache,
    time_limit,
    memory_limit,
    output_limit,
    process_limit,
    judge_type,
    judge_options_fingerprint,
):
    sample_cases = [item for item in testcases if item.get("suite") == "sample"]
    if judge_type == "interactive":
        reporter.event(
            "sample_check",
            applicable=False,
            ok=True,
            judge_type=judge_type,
            reason="interactive answers are defined by the interactor protocol",
        )
        finish(
            reporter,
            True,
            "sample answer consistency is not applicable to interactive problems",
            0,
            "sample_check_not_applicable",
            applicable=False,
            judge_type=judge_type,
            checked_cases=0,
            mismatches=0,
        )
    if judge_type not in {"standard", "custom"}:
        finish(
            reporter,
            False,
            f"unsupported judge.type: {judge_type}",
            1,
            "invalid_judge_type",
            applicable=False,
            judge_type=judge_type,
        )
    if not sample_cases:
        finish(
            reporter,
            False,
            "No .in test cases found under data/sample",
            1,
            "sample_cases_missing",
            applicable=True,
            judge_type=judge_type,
            checked_cases=0,
            mismatches=0,
        )

    # Sample-only case results cannot share a full standard/custom Judge key:
    # this mode intentionally runs no Checker, and a later full Judge must not
    # mistake that execution result for a completed Checker verdict. Compile
    # cache entries remain shared between both commands.
    sample_cache_kind = f"sample-answer-v{SAMPLE_ANSWER_SCHEMA_VERSION}"

    accepted = normalize_solution_entries(config).get("std") or []
    if not accepted:
        finish(
            reporter,
            False,
            "accepted solution not configured",
            1,
            "accepted_missing",
            applicable=True,
            judge_type=judge_type,
        )
    first = accepted[0]
    program_name = first.get("file") or "<missing file>"
    source_path, path_state, reason = resolve_solution_source(prob_dir, first.get("file"))
    if source_path is None:
        result_code = "accepted_missing" if path_state == "missing" else "solution_source_invalid"
        message = (
            f"first accepted source file not found: {program_name}"
            if path_state == "missing"
            else f"first accepted source must be a regular non-link file inside the problem directory: "
            f"{program_name} ({reason})"
        )
        finish(
            reporter,
            False,
            message,
            1,
            result_code,
            applicable=True,
            judge_type=judge_type,
            program=program_name,
            reason=reason,
        )
    bin_path = binary_path_for_source(source_path)
    fingerprint = source_fingerprint(source_path, prob_dir, "std")
    accepted_identity = compile_cpp(
        source_path,
        bin_path,
        reporter,
        "std",
        program_name,
        cache=cache,
        fingerprint=fingerprint,
    )
    if not accepted_identity:
        finish(
            reporter,
            False,
            f"first accepted solution failed to compile: {program_name}",
            1,
            "accepted_compile_failed",
            applicable=True,
            judge_type=judge_type,
            program=program_name,
        )
    identity_error = verify_binary_identities([{
        "role": "accepted",
        "program": program_name,
        "identity": accepted_identity,
    }])
    if identity_error:
        finish(
            reporter,
            False,
            identity_error["message"],
            1,
            "binary_changed",
            failure_kind="infrastructure",
            role=identity_error.get("role"),
            program=identity_error.get("program"),
            path=identity_error.get("path"),
            expected_digest=identity_error.get("expected_digest"),
            actual_digest=identity_error.get("actual_digest"),
        )

    checks = []
    for testcase in sample_cases:
        if not os.path.exists(testcase["ans_path"]):
            finish(
                reporter,
                False,
                f"{testcase['case']}.ans not found",
                1,
                "sample_answer_missing",
                applicable=True,
                judge_type=judge_type,
                program=program_name,
            )
        key = testcase_cache_key(
            fingerprint,
            testcase["in_path"],
            testcase["ans_path"],
            time_limit,
            memory_limit,
            judge_type=sample_cache_kind,
            judge_fingerprint=sample_cache_kind,
            judge_options_fingerprint=judge_options_fingerprint,
        )
        cached_result = cached_case_result(
            cache, key, require_sample_answer=True
        )
        if cached_result is not None:
            status = cached_result["status"]
            elapsed = float(cached_result.get("time") or 0)
            memory = cached_result.get("memory")
            memory_enforced = bool(cached_result.get("memory_enforced"))
            message = cached_result.get("message", "")
            details = cached_result.get("details", {})
            cached = True
        else:
            status, elapsed, memory, memory_enforced, message, details = run_sample_answer_testcase(
                bin_path,
                testcase["in_path"],
                testcase["ans_path"],
                time_limit,
                memory_limit,
                output_limit,
                process_limit,
            )
            save_case_result(
                cache,
                key,
                status,
                elapsed,
                memory,
                memory_enforced,
                message,
                details,
            )
            cached = False
        payload = sample_check_event_payload(
            program_name,
            testcase,
            status,
            message,
            details,
            cached,
            judge_type,
        )
        checks.append(payload)
        reporter.event("sample_check", **payload)
        reporter.text(
            f"  - {testcase['case'].ljust(16)} : "
            f"{'MATCH' if payload['matches'] else 'MISMATCH'} "
            f"({status}){' [cached]' if cached else ''}"
        )

    mismatches = sum(
        1
        for item in checks
        if item["mismatch"]
    )
    failures = [item for item in checks if not item["ok"]]
    if failures:
        result_code = (
            "sample_answer_mismatch" if mismatches else "sample_check_failed"
        )
        first_failure = failures[0]
        finish(
            reporter,
            False,
            f"sample answer check failed for {first_failure['case']} "
            f"({first_failure['run_status']})",
            1,
            result_code,
            applicable=True,
            judge_type=judge_type,
            program=program_name,
            checked_cases=len(checks),
            mismatches=mismatches,
        )
    finish(
        reporter,
        True,
        f"all {len(checks)} sample answer(s) match the first accepted solution",
        0,
        "sample_answers_match",
        applicable=True,
        judge_type=judge_type,
        program=program_name,
        checked_cases=len(checks),
        mismatches=0,
    )

def finish(reporter, ok, message, exit_code, result_code=None, **result_payload):
    cache = getattr(reporter, "cache", None)
    if cache is not None:
        cache.save()
        reporter.event("cache", **cache.stats)
        reporter.text(
            "Cache: "
            f"compile {cache.stats['compile_hits']} hit/{cache.stats['compile_misses']} miss, "
            f"validator {cache.stats['validator_hits']} hit/{cache.stats['validator_misses']} miss, "
            f"case {cache.stats['case_hits']} hit/{cache.stats['case_misses']} miss, "
            f"probe {cache.stats['probe_hits']} hit/{cache.stats['probe_misses']} miss"
        )
    result_code = result_code or ("all_expectations_met" if ok else "validation_failed")
    reporter.event(
        "final",
        ok=ok,
        status="passed" if ok else "failed",
        code=result_code,
        exit_code=exit_code,
        message=message,
        **result_payload,
    )
    reporter.text(message)
    sys.exit(exit_code)


def _main_unlocked():
    jsonl = "--jsonl" in sys.argv
    no_cache = "--no-cache" in sys.argv
    cancellable = "--cancellable" in sys.argv
    sample_check_only = "--sample-check" in sys.argv
    args = [
        arg
        for arg in sys.argv[1:]
        if arg not in {"--jsonl", "--no-cache", "--cancellable", "--sample-check"}
    ]
    reporter = Reporter(jsonl)
    if cancellable:
        install_cancellation_handlers()

    if len(args) != 1:
        reporter.text(
            "Usage: python scripts/local_judge.py <problem_dir> "
            "[--jsonl] [--no-cache] [--cancellable] [--sample-check]"
        )
        finish(reporter, False, "Invalid arguments", 1)

    prob_dir = args[0]
    cache = SandboxCache(
        prob_dir,
        enabled=True,
        read_enabled=not no_cache,
        preserve_existing=sample_check_only and no_cache,
    )
    reporter.cache = cache
    try:
        config = read_probhub_config(prob_dir)
    except ValueError as exc:
        finish(reporter, False, str(exc), 1)
    if config is None:
        finish(
            reporter,
            False,
            "Workspace Schema v1 problem config is required; Legacy workflow is no longer supported",
            1,
            "migration_required",
        )
    if config.get("schema_version") != 1:
        finish(
            reporter,
            False,
            "unsupported problem schema_version; migrate to Workspace Schema v1",
            1,
            "unsupported_schema",
        )

    judge_type = configured_judge_type(config)
    data_config = (config or {}).get("data") or {}
    sample_dir = os.path.join(prob_dir, data_config.get("sample_dir", "data/sample"))
    secret_dir = os.path.join(prob_dir, data_config.get("secret_dir", "data/secret"))
    if (
        not sample_check_only
        and not os.path.isdir(sample_dir)
        and not os.path.isdir(secret_dir)
    ):
        finish(reporter, False, "configured sample and secret data directories not found", 1)

    time_limit, memory_limit = read_problem_limits(prob_dir, config)
    output_limit, process_limit = read_problem_output_limit(prob_dir, config)
    policy = calibration_policy(config)
    testcases = collect_testcases(prob_dir, config)
    if not testcases and not sample_check_only:
        finish(reporter, False, "No .in test cases found under data/sample or data/secret", 1)

    reporter.text("\n" + "=" * 50)
    reporter.text("ProbHub Validation Sandbox")
    reporter.text(
        f"Limits: {time_limit:g}s / {memory_limit}MB / {output_limit}MB output / "
        f"{process_limit} processes"
    )
    reporter.text(
        f"Cases: {sum(1 for t in testcases if t['suite'] == 'sample')} sample / "
        f"{sum(1 for t in testcases if t['suite'] == 'secret')} secret"
    )
    reporter.text("Calibration note: local measurements are not a target Linux judge guarantee.")
    reporter.text("=" * 50)
    judge_config = (config or {}).get("judge") or {}
    interactive_config = judge_config.get("interactive") or {}
    idle_limit = _safe_float(interactive_config.get("idle_limit"), min(time_limit, 2.0))
    idle_limit = max(idle_limit, 0.1)
    transcript_limit = max(_safe_int(interactive_config.get("transcript_limit"), 65536), 0)
    judge_options_fingerprint = _hash_parts(
        judge_type,
        f"{idle_limit:.9g}",
        transcript_limit,
        output_limit,
        process_limit,
    )
    reporter.event(
        "limits",
        time_limit=time_limit,
        memory_limit=memory_limit,
        output_limit=output_limit,
        process_limit=process_limit,
        judge_type=judge_type,
        idle_limit=idle_limit if judge_type == "interactive" else None,
        transcript_limit=transcript_limit if judge_type == "interactive" else None,
        platform=platform.system(),
        machine=platform.machine(),
        compiler=compiler_identity(),
        target_guarantee=False,
        measurement_note=MEASUREMENT_NOTE,
        calibration_policy=policy,
    )

    if sample_check_only:
        run_sample_check_mode(
            prob_dir,
            config,
            testcases,
            reporter,
            cache,
            time_limit,
            memory_limit,
            output_limit,
            process_limit,
            judge_type,
            judge_options_fingerprint,
        )
        return

    verification = analyze_solution_verification(prob_dir, config)
    verification_errors = [
        item
        for item in (verification.get("diagnostics") or [])
        if item.get("severity") == "error"
    ]
    if verification_errors:
        first = verification_errors[0]
        finish(
            reporter,
            False,
            first.get("message") or "solution verification preflight failed",
            1,
            first.get("code") or "solution_verification_failed",
            diagnostic=first,
        )

    configured_accepted = normalize_solution_entries(config).get("std") or []
    if not configured_accepted:
        finish(
            reporter,
            False,
            "accepted solution not configured",
            1,
            "accepted_missing",
        )
    primary_file = configured_accepted[0].get("file")
    primary_path = resolve_problem_path(prob_dir, primary_file)
    if not primary_path or not os.path.isfile(primary_path):
        finish(
            reporter,
            False,
            f"first accepted source file not found: {primary_file or '<missing file>'}",
            1,
            "accepted_missing",
            program=primary_file,
        )

    source_groups = discover_solution_entries(prob_dir, config)
    first_accepted_program = (
        display_problem_path(prob_dir, source_groups["std"][0]["path"])
        if source_groups.get("std")
        else None
    )
    group_definitions = _data_groups(config)
    reporter.event("groups", groups=group_definitions)
    solution_plans, run_on_errors = _solution_execution_plan(
        source_groups, testcases, group_definitions
    )
    if run_on_errors:
        first = run_on_errors[0]
        failure_payload = {
            key: value for key, value in first.items() if key != "result_code"
        }
        finish(
            reporter,
            False,
            f"invalid run_on domain for {first['program']}: {first['reason']}",
            1,
            first["result_code"],
            **failure_payload,
        )

    # 1. Compile every binary before executing any official tool or solution.
    compiled_identities = []
    validator_entry = ((config or {}).get("judge") or {}).get("validator") if config is not None else None
    val_cpp = discover_validator_source(prob_dir, config)
    val_bin = None
    val_name = None
    val_fingerprint = None
    if val_cpp and os.path.exists(val_cpp):
        val_bin = binary_path_for_source(val_cpp)
        val_name = display_problem_path(prob_dir, val_cpp)
        val_fingerprint = source_fingerprint(val_cpp, prob_dir, "validator")
        val_identity = compile_cpp(
            val_cpp,
            val_bin,
            reporter,
            "validator",
            val_name,
            cache=cache,
            fingerprint=val_fingerprint,
        )
        if val_identity:
            compiled_identities.append({
                "role": "validator",
                "program": val_name,
                "identity": val_identity,
            })
    elif jsonl and (validator_entry or val_cpp):
        validator_name = _entry_file(validator_entry) or display_problem_path(prob_dir, val_cpp)
        reporter.event(
            "compile",
            kind="validator",
            file=str(validator_name).replace(os.sep, "/"),
            ok=None,
            stderr=f"{validator_name} not found",
        )

    if judge_type not in {"standard", "custom", "interactive"}:
        finish(reporter, False, f"unsupported judge.type: {judge_type}", 1, "invalid_judge_type")

    judge_bin = None
    judge_identity = None
    judge_fingerprint = "standard-v2-ignore-line-trailing-whitespace"
    if judge_type in {"custom", "interactive"}:
        source_key = "checker" if judge_type == "custom" else "interactor"
        judge_source = discover_judge_source(prob_dir, config, source_key)
        judge_name = display_problem_path(prob_dir, judge_source) if judge_source else source_key
        if not judge_source or not os.path.isfile(judge_source):
            finish(reporter, False, f"{source_key} not found: {judge_name}", 1, f"{source_key}_missing")
        judge_bin = binary_path_for_source(judge_source)
        judge_fingerprint = source_fingerprint(judge_source, prob_dir, source_key)
        judge_identity = compile_cpp(
            judge_source,
            judge_bin,
            reporter,
            source_key,
            judge_name,
            cache=cache,
            fingerprint=judge_fingerprint,
        )
        if not judge_identity:
            finish(reporter, False, f"{source_key} failed to compile", 1, f"{source_key}_compile_failed")
        compiled_identities.append({
            "role": source_key,
            "program": judge_name,
            "identity": judge_identity,
        })

    # 2. Discover and compile all solutions
    solutions = {"std": [], "brute": [], "wrong": []}
    for kind, source_entries in solution_plans.items():
        for source_entry in source_entries:
            source_path = source_entry["path"]
            expected = source_entry["expected"]
            run_on = source_entry.get("run_on")
            execution_cases = source_entry["execution_cases"]
            skipped_cases = source_entry["skipped_cases"]
            program_name = display_problem_path(prob_dir, source_path)
            if not os.path.isfile(source_path):
                reporter.event("compile", kind=kind, file=program_name, ok=False, stderr="source file not found")
                reporter.text(f"[-] Source file not found: {program_name}")
                continue
            bin_name = binary_path_for_source(source_path)
            fingerprint = source_fingerprint(source_path, prob_dir, kind)
            solution_identity = compile_cpp(
                source_path,
                bin_name,
                reporter,
                kind,
                program_name,
                cache=cache,
                fingerprint=fingerprint,
            )
            if solution_identity:
                compiled_identities.append({
                    "role": kind,
                    "program": program_name,
                    "identity": solution_identity,
                })
                solutions[kind].append((
                    program_name,
                    bin_name,
                    fingerprint,
                    expected,
                    run_on,
                    execution_cases,
                    skipped_cases,
                ))

    if not solutions["std"]:
        finish(reporter, False, "accepted solution not found or failed to compile. Aborting.", 1)
    if first_accepted_program and not any(
        item[0] == first_accepted_program for item in solutions["std"]
    ):
        finish(
            reporter,
            False,
            f"first accepted solution failed to compile: {first_accepted_program}",
            1,
            "accepted_compile_failed",
        )

    identity_error = verify_binary_identities(compiled_identities)
    if identity_error:
        finish(
            reporter,
            False,
            identity_error["message"],
            1,
            "binary_changed",
            failure_kind="infrastructure",
            role=identity_error.get("role"),
            program=identity_error.get("program"),
            path=identity_error.get("path"),
            expected_digest=identity_error.get("expected_digest"),
            actual_digest=identity_error.get("actual_digest"),
            expected_size=identity_error.get("expected_size"),
            actual_size=identity_error.get("actual_size"),
        )

    # 3. Run Validator only after the whole Judge binary snapshot passed.
    if val_bin and val_fingerprint and any(
        entry.get("role") == "validator" for entry in compiled_identities
    ):
        reporter.text("\nRunning validator...")
        all_ok = True
        for testcase in testcases:
            key = validator_cache_key(val_fingerprint, testcase["in_path"])
            cached_result = cache.get("validator", key)
            if cached_result is not None:
                ok = bool(cached_result.get("ok"))
                validator_message = cached_result.get("message", "")
                cached = True
            else:
                ok, validator_message = run_validator(val_bin, testcase["in_path"])
                cache.set("validator", key, {"ok": ok, "message": validator_message})
                cached = False
            reporter.event(
                "validator",
                case=testcase["case"],
                ok=ok,
                message=validator_message,
                cached=cached,
            )
            if not ok:
                all_ok = False
                detail = f" ({validator_message})" if validator_message else ""
                reporter.text(
                    f"[-] FATAL: {testcase['case']} violates validator constraints{detail}! "
                    "Fix the data generator."
                )
                finish(
                    reporter,
                    False,
                    f"{testcase['case']} violates validator constraints{detail}",
                    1,
                )
        if all_ok:
            reporter.text("[+] Validator: ALL PASS")

    # 4. Evaluate all solutions.
    for kind, progs in solutions.items():
        for (
            prog_name,
            bin_path,
            fingerprint,
            expected,
            run_on,
            execution_cases,
            skipped_cases,
        ) in progs:
            reporter.text(f"\nEvaluating: {prog_name}")
            stats = {"AC": 0, "WA": 0, "TLE": 0, "RE": 0, "MLE": 0, "OLE": 0, "FAIL": 0}
            case_results = []

            for testcase in execution_cases:
                if not os.path.exists(testcase["ans_path"]):
                    finish(reporter, False, f"{testcase['case']}.ans not found", 1)

                requires_sample_answer = (
                    kind == "std"
                    and prog_name == first_accepted_program
                    and testcase.get("suite") == "sample"
                    and judge_type != "interactive"
                )

                key = testcase_cache_key(
                    fingerprint,
                    testcase["in_path"],
                    testcase["ans_path"],
                    time_limit,
                    memory_limit,
                    judge_type=judge_type,
                    judge_fingerprint=judge_fingerprint,
                    judge_options_fingerprint=judge_options_fingerprint,
                )
                cached_result = cached_case_result(
                    cache,
                    key,
                    require_sample_answer=requires_sample_answer,
                )
                if cached_result is not None:
                    status = cached_result["status"]
                    t = float(cached_result.get("time") or 0)
                    memory = cached_result.get("memory")
                    memory_enforced = bool(cached_result.get("memory_enforced"))
                    judge_message = cached_result.get("message", "")
                    judge_details = cached_result.get("details", {})
                    cached = True
                else:
                    status, t, memory, memory_enforced, judge_message, judge_details = run_testcase(
                        bin_path,
                        testcase["in_path"],
                        testcase["ans_path"],
                        time_limit,
                        memory_limit,
                        output_limit,
                        process_limit,
                        judge_type=judge_type,
                        judge_bin=judge_bin,
                        idle_limit=idle_limit,
                        transcript_limit=transcript_limit,
                        capture_sample_answer=requires_sample_answer,
                    )
                    save_case_result(
                        cache,
                        key,
                        status,
                        t,
                        memory,
                        memory_enforced,
                        judge_message,
                        judge_details,
                    )
                    cached = False
                stats[status] += 1
                reporter.event(
                    "case",
                    kind=kind,
                    program=prog_name,
                    case=testcase["case"],
                    groups=testcase.get("groups", []),
                    status=status,
                    time=round(t, 6),
                    time_limit=time_limit,
                    memory=memory,
                    memory_limit=memory_limit,
                    memory_enforced=memory_enforced,
                    judge_type=judge_type,
                    message=judge_message,
                    timeout_kind=judge_details.get("timeout_kind"),
                    transcript_truncated=judge_details.get("transcript_truncated", False),
                    output_bytes=judge_details.get("output_bytes"),
                    termination_reason=judge_details.get("termination_reason"),
                    verdict=judge_details.get("verdict"),
                    execution_status=judge_details.get("execution_status"),
                    failure_kind=judge_details.get("failure_kind"),
                    actor=judge_details.get("actor"),
                    cleanup=judge_details.get("cleanup"),
                    cached=cached,
                )
                case_results.append({
                    "case": testcase["case"],
                    "groups": testcase.get("groups", []),
                    "status": status,
                    "message": judge_message,
                    "time": round(t, 6),
                    "memory": memory,
                    "memory_enforced": memory_enforced,
                    "output_bytes": judge_details.get("output_bytes"),
                    "termination_reason": judge_details.get("termination_reason"),
                    "verdict": judge_details.get("verdict"),
                    "execution_status": judge_details.get("execution_status"),
                    "failure_kind": judge_details.get("failure_kind"),
                    "actor": judge_details.get("actor"),
                    "cleanup": judge_details.get("cleanup"),
                    "cached": cached,
                })
                if requires_sample_answer:
                    sample_payload = sample_check_event_payload(
                        prog_name,
                        testcase,
                        status,
                        judge_message,
                        judge_details,
                        cached,
                        judge_type,
                    )
                    reporter.event("sample_check", **sample_payload)
                    if sample_payload["mismatch"]:
                        finish(
                            reporter,
                            False,
                            f"sample answer mismatch for {testcase['case']} "
                            f"using {prog_name}",
                            1,
                            "sample_answer_mismatch",
                            applicable=True,
                            judge_type=judge_type,
                            program=prog_name,
                            case=testcase["case"],
                        )
                if judge_type == "interactive" and judge_details.get("transcript"):
                    reporter.event(
                        "transcript",
                        kind=kind,
                        program=prog_name,
                        case=testcase["case"],
                        entries=judge_details.get("transcript", []),
                        truncated=judge_details.get("transcript_truncated", False),
                        cached=cached,
                    )
                if status != "AC" or kind == "std":
                    suffix = " [cached]" if cached else ""
                    detail = f" - {judge_message}" if judge_message else ""
                    reporter.text(f"  - {testcase['case'].ljust(16)} : {status} ({t:.3f}s){suffix}{detail}")
                if status == "FAIL":
                    finish(
                        reporter,
                        False,
                        f"{judge_type} judge failed on {testcase['case']}: {judge_message or 'unknown failure'}",
                        1,
                        "judge_failed",
                    )

            expectation = evaluate_expectation(
                kind,
                prog_name,
                expected,
                case_results,
                testcases,
                group_definitions,
                run_on=run_on,
                execution_cases=execution_cases,
                skipped_cases=skipped_cases,
            )
            selected_names = set(expectation.get("selected_cases") or [])
            selected_results = [
                item for item in case_results if item.get("case") in selected_names
            ]
            resource_kills = []
            expected_resource_statuses = set(expectation.get("expected_statuses") or []) & {
                "TLE", "MLE", "OLE"
            }
            for resource_status in ("TLE", "MLE", "OLE"):
                if resource_status not in expected_resource_statuses:
                    continue
                matched = next(
                    (
                        item for item in selected_results
                        if item.get("status") == resource_status
                    ),
                    None,
                )
                # expected.status is a set of acceptable alternatives, not a
                # requirement that every listed resource status must occur.
                if matched is None:
                    continue
                if resource_status == "TLE":
                    if judge_type == "interactive":
                        resource_kills.append({
                            "status": "TLE",
                            "case": matched.get("case"),
                            "groups": list(matched.get("groups") or []),
                            "observed": None,
                            "limit": float(time_limit),
                            "limit_ratio": None,
                            "evidence_kind": "unavailable",
                            "supported": False,
                            "censored": True,
                            "reason": "interactive probe requires the interactor harness",
                            "required_ratio": policy["expected_tle_time_multiplier"],
                        })
                    else:
                        testcase = next(
                            item for item in testcases
                            if item["case"] == matched["case"]
                        )
                        required_ratio = policy["expected_tle_time_multiplier"]
                        probe_factor = max(2.0, required_ratio * 1.25)
                        margin = probe_tle_margin(
                            bin_path,
                            testcase["in_path"],
                            testcase["case"],
                            fingerprint,
                            time_limit,
                            memory_limit,
                            output_limit,
                            process_limit,
                            probe_factor,
                            cache=cache,
                        )
                        margin["groups"] = list(matched.get("groups") or [])
                        margin["required_ratio"] = required_ratio
                        resource_kills.append(margin)
                else:
                    margin = _status_only_resource_kill(
                        resource_status,
                        matched,
                        {"MLE": memory_limit, "OLE": output_limit},
                    )
                    resource_kills.append(margin)

            calibration = build_solution_calibration(
                kind,
                case_results,
                time_limit,
                policy,
                resource_kills,
            )
            reporter.event(
                "summary",
                kind=kind,
                program=prog_name,
                stats=stats,
                expectation=expectation,
                calibration=calibration,
                run_on=expectation["run_on"],
                executed_cases=expectation["executed_cases"],
                skipped_cases=expectation["skipped_cases"],
                coverage_ok=expectation["coverage_ok"],
                unexecuted_expected_cases=expectation["unexecuted_expected_cases"],
            )
            reporter.event("expectation", **expectation)
            reporter.text(f"  Summary: {stats}")
            if (
                calibration["max_time"] is not None
                and calibration["time_limit_ratio"] is not None
                and calibration["headroom_factor"] is not None
            ):
                reporter.text(
                    "  Calibration: "
                    f"max={calibration['max_time']:.3f}s "
                    f"({calibration['time_limit_ratio']:.3f}x TL), "
                    f"headroom={calibration['headroom_factor']:.3f}x"
                )
            reporter.text(
                "  Expectation: "
                f"{'PASS' if expectation['ok'] else 'FAIL'} "
                f"statuses={expectation['expected_statuses']} "
                f"forbid={expectation['forbidden_statuses']} "
                f"groups={expectation['groups'] or ['all']}"
            )

            if not expectation["selected_cases"]:
                finish(
                    reporter,
                    False,
                    f"expectation for {prog_name} selected no test cases",
                    1,
                    "expectation_has_no_cases",
                )
            if not expectation["ok"]:
                failure = expectation.get("first_forbidden") or expectation.get("first_non_ac")
                detail = f"; first relevant case: {failure}" if failure else ""
                finish(
                    reporter,
                    False,
                    f"expectation not met for {prog_name}{detail}",
                    1,
                    "expectation_not_met",
                )

    reporter.text("\n" + "=" * 50)
    finish(reporter, True, "[+] 恭喜！所有代码均符合预期宿命", 0)


def main():
    args = [
        arg
        for arg in sys.argv[1:]
        if arg not in {"--jsonl", "--no-cache", "--cancellable", "--sample-check"}
    ]
    if len(args) != 1:
        return _main_unlocked()
    problem_dir = os.path.abspath(args[0])
    if not os.path.isdir(problem_dir):
        return _main_unlocked()
    try:
        with workspace_file_lock(
            problem_dir,
            ".probhub/judge.lock",
            busy_code="judge_busy",
            busy_message="another Judge is already running for this problem",
            no_follow=True,
        ):
            return _main_unlocked()
    except BinaryChangedError as exc:
        reporter = Reporter("--jsonl" in sys.argv)
        finish(
            reporter,
            False,
            str(exc),
            1,
            "binary_changed",
            failure_kind="infrastructure",
        )
    except ProbHubError as exc:
        reporter = Reporter("--jsonl" in sys.argv)
        finish(reporter, False, str(exc), 1, exc.code)


if __name__ == "__main__":
    try:
        main()
    except ProcessCancelled:
        reporter = Reporter("--jsonl" in sys.argv)
        reporter.event(
            "final",
            ok=False,
            status="cancelled",
            code="cancelled",
            exit_code=130,
            message="Submission judging cancelled",
        )
        reporter.text("Submission judging cancelled")
        sys.exit(130)
