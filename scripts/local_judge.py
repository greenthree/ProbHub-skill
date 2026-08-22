import json
import os
import platform
import re
import shutil
import signal
import sys

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
    infer_failed_status,
)
from probhub import judge_execution as _judge_execution
from probhub import judge_planning as _judge_planning
from probhub import judge_runtime as _judge_runtime
from probhub.output_compare import compare_standard_output
from probhub.problem_paths import ProblemPathError, resolve_problem_regular_file
from probhub.special_judges import execute_interactive_session, run_checker_to_files
from probhub.process_control import (
    DEFAULT_PROCESS_LIMIT,
    OutputBudgetError,
    ProcessCancelled,
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
)

REFERENCES_DIR = os.path.join(PACKAGE_ROOT, "references")
DEFAULT_TIME_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = 256
DEFAULT_OUTPUT_LIMIT = 64
PROTOCOL_NAME = "probhub.local_judge"
PROTOCOL_VERSION = 1
SAMPLE_ANSWER_SCHEMA_VERSION = 1
_COMPILER_IDENTITY = None


def _cancel_signal_handler(_signum, _frame):
    raise ProcessCancelled("execution cancelled")


def install_cancellation_handlers():
    signal.signal(signal.SIGTERM, _cancel_signal_handler)
    if platform.system() == "Windows" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _cancel_signal_handler)


BinaryChangedError = _judge_runtime.BinaryChangedError


def binary_identity(path):
    """Compatibility wrapper for the Core binary identity primitive."""
    return _judge_runtime.binary_identity(path)


def verify_binary_identities(entries):
    """Compatibility wrapper for the Core binary identity fence."""
    return _judge_runtime.verify_binary_identities(entries, identity_fn=binary_identity)


def publish_compiled_binary(staged_binary, out_bin, staged_identity):
    """Compatibility wrapper retaining local identity injection for tests."""
    return _judge_runtime.publish_compiled_binary(
        staged_binary,
        out_bin,
        staged_identity,
        identity_fn=binary_identity,
    )


def normalize_sample_output(payload):
    """Compatibility wrapper for strict sample output normalization."""
    return _judge_execution.normalize_sample_output(payload)


def _sample_output_summary(payload):
    return _judge_execution._sample_output_summary(payload)


def _sample_first_difference(actual, expected):
    return _judge_execution._sample_first_difference(actual, expected)


def compare_sample_answer(output_file, ans_file):
    return _judge_execution.compare_sample_answer(output_file, ans_file)


def valid_sample_answer_summary(value):
    return _judge_execution.valid_sample_answer_summary(value)


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


def compiler_identity():
    """Compatibility wrapper for the Core compiler identity probe."""
    global _COMPILER_IDENTITY
    if _COMPILER_IDENTITY is None:
        _COMPILER_IDENTITY = _judge_runtime.compiler_identity(
            run_managed_fn=run_managed_to_files
        )
    return _COMPILER_IDENTITY


def source_fingerprint(src_file, prob_dir, kind=None):
    """Compatibility wrapper for the Core source/cache identity function."""
    return _judge_runtime.source_fingerprint(
        src_file,
        prob_dir,
        kind,
        reference_dir=REFERENCES_DIR,
        compiler_identity_fn=compiler_identity,
    )


def compile_cpp(
    src_file,
    out_bin,
    reporter=None,
    kind=None,
    display_file=None,
    cache=None,
    fingerprint=None,
):
    """Compatibility wrapper preserving the local Judge's callback surface."""
    if reporter is None:
        reporter = Reporter(False)
    return _judge_runtime.compile_cpp(
        src_file,
        out_bin,
        reporter=reporter,
        kind=kind,
        display_file=display_file,
        cache=cache,
        fingerprint=fingerprint,
        reference_dir=REFERENCES_DIR,
        source_fingerprint_fn=source_fingerprint,
        binary_identity_fn=binary_identity,
        publish_fn=publish_compiled_binary,
        run_managed_fn=run_managed_to_files,
        process_limit=DEFAULT_PROCESS_LIMIT,
    )


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
    return _judge_execution.remove_file_with_retries(path)

def run_program_to_file(
    bin_path,
    in_file,
    output_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    return _judge_execution.run_program_to_file(
        bin_path,
        in_file,
        output_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        run_managed_fn=run_managed_to_files,
        infer_failed_status_fn=infer_failed_status,
        cleanup_fn=_remove_file_with_retries,
    )


def _temporary_output_path(bin_path):
    return _judge_execution.temporary_output_path(bin_path)


def _probe_status(result, memory_limit):
    status = _judge_execution.classify_process_status(result, memory_limit)
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
    return _judge_execution.probe_tle_margin(
        bin_path,
        in_file,
        case_name,
        program_fingerprint,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        probe_factor,
        cache,
        run_managed_fn=run_managed_to_files,
        cache_key_fn=calibration_probe_cache_key,
        output_path_fn=_temporary_output_path,
        cleanup_fn=_remove_file_with_retries,
    )


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
    return _judge_execution.run_standard_testcase(
        bin_path,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        capture_sample_answer,
        run_program_fn=run_program_to_file,
        sample_compare_fn=compare_sample_answer,
        output_path_fn=_temporary_output_path,
        cleanup_fn=_remove_file_with_retries,
    )


def run_sample_answer_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    return _judge_execution.run_sample_answer_testcase(
        bin_path,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        run_program_fn=run_program_to_file,
        sample_compare_fn=compare_sample_answer,
        output_path_fn=_temporary_output_path,
        cleanup_fn=_remove_file_with_retries,
    )


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
    return _judge_execution.run_custom_testcase(
        bin_path,
        checker_bin,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        capture_sample_answer,
        run_program_fn=run_program_to_file,
        sample_compare_fn=compare_sample_answer,
        checker_fn=run_checker_to_files,
        output_path_fn=_temporary_output_path,
        cleanup_fn=_remove_file_with_retries,
    )


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
    return _judge_execution.run_interactive_testcase(
        bin_path,
        interactor_bin,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        idle_limit,
        transcript_limit,
        output_limit,
        process_limit,
        interactive_fn=execute_interactive_session,
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
    return _judge_execution.run_testcase(
        bin_path,
        in_file,
        ans_file,
        time_limit,
        memory_limit,
        output_limit,
        process_limit,
        judge_type,
        judge_bin,
        idle_limit,
        transcript_limit,
        capture_sample_answer,
        standard_fn=run_standard_testcase,
        custom_fn=run_custom_testcase,
        interactive_fn=run_interactive_testcase,
    )

def run_validator(val_bin, in_file, timeout=VALIDATOR_TIMEOUT_SECONDS):
    return _judge_execution.run_validator(
        val_bin,
        in_file,
        timeout,
        run_managed_fn=run_managed_to_files,
    )


def _data_groups(config=None):
    return normalize_data_groups(config or {})


def _testcase_groups(case_name, suite, base_name, groups):
    return case_group_names(case_name, suite, base_name, groups)


def collect_testcases(prob_dir, config=None):
    return _judge_planning.collect_testcases(prob_dir, config)


def _select_expectation_cases(kind, entry, testcases, group_definitions):
    return _judge_planning.select_solution_expectation_cases(
        kind, entry, testcases, group_definitions
    )


def _expectation_cases(kind, testcases, expected, group_definitions, program_name):
    return _judge_planning.expectation_cases(
        kind, testcases, expected, group_definitions, program_name
    )


def _solution_execution_plan(source_groups, testcases, group_definitions):
    return _judge_planning.solution_execution_plan(
        source_groups, testcases, group_definitions
    )


def _result_snapshot(item):
    return _judge_planning.result_snapshot(item)


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
    return _judge_planning.evaluate_expectation(
        kind,
        program_name,
        expected,
        case_results,
        testcases,
        group_definitions,
        run_on,
        execution_cases,
        skipped_cases,
    )


def _status_only_resource_kill(status, case_result, limits):
    return _judge_planning.status_only_resource_kill(status, case_result, limits)


def build_solution_calibration(
    kind,
    case_results,
    time_limit,
    policy,
    resource_kills,
):
    return _judge_planning.build_solution_calibration(
        kind, case_results, time_limit, policy, resource_kills
    )


def cached_case_result(cache, key, require_sample_answer=False):
    return _judge_planning.cached_case_result(
        cache,
        key,
        require_sample_answer,
        sample_validator=valid_sample_answer_summary,
    )


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
    return _judge_planning.save_case_result(
        cache,
        key,
        status,
        elapsed,
        memory,
        memory_enforced,
        message,
        details,
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
