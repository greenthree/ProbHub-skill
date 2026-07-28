import math
import platform
from pathlib import Path

from .build_lock import workspace_file_lock
from .io import atomic_write_json
from .solutions import (
    case_group_names,
    normalize_data_groups,
    normalize_solution_entries,
    select_run_cases,
)


CALIBRATION_SCHEMA_VERSION = 2
CALIBRATION_STRATEGY_VERSION = 2
SANDBOX_CACHE_SCHEMA_VERSION = 4
EVIDENCE_FILENAME = "judge-evidence-v2.json"
EVIDENCE_LOCK_FILENAME = "judge-evidence.lock"
DEFAULT_ACCEPTED_TIME_MULTIPLIER = 3.0
DEFAULT_EXPECTED_TLE_TIME_MULTIPLIER = 1.5
MEASUREMENT_NOTE = (
    "Local sandbox timings and resource measurements are machine-specific. "
    "Windows and Linux/DOMjudge differ in process startup, linking, scheduling, "
    "timing, and memory accounting; recalibrate limits on the target Linux judge."
)


def _finite_number(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def calibration_policy(config):
    raw = config.get("calibration") or {}
    accepted = raw.get(
        "accepted_time_multiplier", DEFAULT_ACCEPTED_TIME_MULTIPLIER
    ) if isinstance(raw, dict) else DEFAULT_ACCEPTED_TIME_MULTIPLIER
    tle = raw.get(
        "expected_tle_time_multiplier", DEFAULT_EXPECTED_TLE_TIME_MULTIPLIER
    ) if isinstance(raw, dict) else DEFAULT_EXPECTED_TLE_TIME_MULTIPLIER
    if not _finite_number(accepted) or float(accepted) < 1.0:
        accepted = DEFAULT_ACCEPTED_TIME_MULTIPLIER
    if not _finite_number(tle) or float(tle) <= 1.0:
        tle = DEFAULT_EXPECTED_TLE_TIME_MULTIPLIER
    return {
        "accepted_time_multiplier": float(accepted),
        "expected_tle_time_multiplier": float(tle),
    }


def validate_calibration_config(config):
    raw = config.get("calibration")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["calibration must be a mapping"]
    errors = []
    accepted = raw.get("accepted_time_multiplier")
    if accepted is not None and (
        not _finite_number(accepted) or float(accepted) < 1.0
    ):
        errors.append(
            "calibration.accepted_time_multiplier must be a finite number at least 1"
        )
    tle = raw.get("expected_tle_time_multiplier")
    if tle is not None and (
        not _finite_number(tle) or float(tle) <= 1.0
    ):
        errors.append(
            "calibration.expected_tle_time_multiplier must be a finite number greater than 1"
        )
    return errors


def evidence_path(problem_dir):
    return Path(problem_dir) / ".probhub" / EVIDENCE_FILENAME


def write_calibration_evidence(problem_dir, evidence):
    problem_dir = Path(problem_dir)
    with workspace_file_lock(
        problem_dir,
        Path(".probhub") / EVIDENCE_LOCK_FILENAME,
        busy_code="calibration_busy",
        busy_message="another calibration evidence publisher is running",
        wait_timeout=30,
    ):
        current = read_calibration_evidence(problem_dir)
        current_time = str((current or {}).get("published_at") or "")
        incoming_time = str(evidence.get("published_at") or "")
        if current_time and incoming_time and current_time > incoming_time:
            return False
        atomic_write_json(evidence_path(problem_dir), evidence)
        return True


def read_calibration_evidence(problem_dir):
    path = evidence_path(problem_dir)
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def build_judge_evidence(events, source_hash, data_hash, measured_at):
    limits = next((item for item in events if item.get("type") == "limits"), {})
    cache = next(
        (item for item in reversed(events) if item.get("type") == "cache"),
        {},
    )
    summaries = [
        item
        for item in events
        if item.get("type") == "summary" and isinstance(item.get("calibration"), dict)
    ]
    if not summaries:
        return None
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "strategy_version": CALIBRATION_STRATEGY_VERSION,
        "sandbox_cache_schema_version": SANDBOX_CACHE_SCHEMA_VERSION,
        "source_hash": source_hash,
        "data_hash": data_hash,
        "published_at": measured_at,
        "observed_at": (
            measured_at
            if cache.get("case_misses", 0) or not cache.get("case_hits", 0)
            else None
        ),
        "measurement": {
            "scope": "local",
            "platform": limits.get("platform"),
            "machine": limits.get("machine"),
            "compiler": limits.get("compiler"),
            "cache_mode": cache.get("mode"),
            "case_hits": cache.get("case_hits", 0),
            "case_misses": cache.get("case_misses", 0),
            "probe_hits": cache.get("probe_hits", 0),
            "probe_misses": cache.get("probe_misses", 0),
            "target_guarantee": False,
            "note": MEASUREMENT_NOTE,
        },
        "limits": {
            key: limits.get(key)
            for key in ("time_limit", "memory_limit", "output_limit", "process_limit")
        },
        "thresholds": limits.get("calibration_policy") or {},
        "solutions": [
            {
                "kind": item.get("kind"),
                "program": item.get("program"),
                "stats": item.get("stats", {}),
                "expectation": item.get("expectation", {}),
                "execution_domain": {
                    key: item.get(key)
                    for key in (
                        "run_on",
                        "executed_cases",
                        "skipped_cases",
                        "coverage_ok",
                        "unexecuted_expected_cases",
                    )
                },
                "calibration": item.get("calibration", {}),
            }
            for item in summaries
        ],
    }


def _diagnostic(code, message, **payload):
    return {
        "code": code,
        "severity": "warning",
        "message": message,
        **payload,
    }


def _configured_solutions(config):
    configured = set()
    solutions = config.get("solutions") or {}
    for config_kind, event_kind in (
        ("accepted", "std"),
        ("brute", "brute"),
        ("wrong", "wrong"),
    ):
        entries = solutions.get(config_kind) or []
        entries = entries if isinstance(entries, list) else [entries]
        for entry in entries:
            name = entry.get("file") if isinstance(entry, dict) else entry
            if name:
                configured.add((event_kind, str(name).replace("\\", "/")))
    return configured


def _configured_execution_domains(problem_dir, config):
    configured = _configured_solutions(config)
    if not configured:
        return {}
    problem_dir = Path(problem_dir)
    groups = normalize_data_groups(config)
    data = config.get("data") or {}
    testcases = []
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        directory = problem_dir / data.get(f"{suite}_dir", default)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.in"), key=lambda item: item.name):
            case_name = f"{suite}/{path.stem}"
            testcases.append({
                "suite": suite,
                "name": path.stem,
                "case": case_name,
                "groups": case_group_names(case_name, suite, path.stem, groups),
            })
    result = {}
    for kind, entries in normalize_solution_entries(config).items():
        for entry in entries:
            program = entry.get("file")
            if not program:
                continue
            executed, skipped = select_run_cases(entry, testcases)
            result[(kind, program.replace("\\", "/"))] = {
                "run_on": list(entry.get("run_on") or []),
                "executed_cases": [case["case"] for case in executed],
                "skipped_cases": [case["case"] for case in skipped],
            }
    return result


def _evidence_structure_error(evidence, config, problem_dir=None):
    if evidence.get("strategy_version") != CALIBRATION_STRATEGY_VERSION:
        return "calibration strategy changed"
    if evidence.get("sandbox_cache_schema_version") != SANDBOX_CACHE_SCHEMA_VERSION:
        return "sandbox cache measurement strategy changed"
    solutions = evidence.get("solutions")
    if not isinstance(solutions, list) or not solutions:
        return "solution summaries are missing"
    observed = {}
    for solution in solutions:
        if not isinstance(solution, dict):
            return "solution summary is invalid"
        kind = solution.get("kind")
        program = solution.get("program")
        expectation = solution.get("expectation")
        execution_domain = solution.get("execution_domain")
        calibration = solution.get("calibration")
        if not isinstance(kind, str) or not isinstance(program, str):
            return "solution identity is invalid"
        if not isinstance(expectation, dict):
            return f"solution expectation is missing for {program}"
        if not isinstance(execution_domain, dict):
            return f"solution execution domain is missing for {program}"
        for key in ("run_on", "executed_cases", "skipped_cases", "unexecuted_expected_cases"):
            value = expectation.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return f"solution expectation {key} is invalid for {program}"
            if execution_domain.get(key) != value:
                return f"solution execution domain disagrees with expectation for {program}"
        if not isinstance(expectation.get("coverage_ok"), bool):
            return f"solution expectation coverage_ok is invalid for {program}"
        if execution_domain.get("coverage_ok") != expectation.get("coverage_ok"):
            return f"solution execution domain disagrees with expectation for {program}"
        if not isinstance(calibration, dict) or not _finite_number(
            calibration.get("max_time")
        ):
            return f"solution calibration is incomplete for {program}"
        identity = (kind, program.replace("\\", "/"))
        if identity in observed:
            return f"solution summary is duplicated for {program}"
        observed[identity] = solution
    configured = _configured_solutions(config)
    if configured and set(observed) != configured:
        missing = sorted(configured - set(observed))
        extra = sorted(set(observed) - configured)
        details = []
        if missing:
            details.append("missing " + ", ".join(f"{kind}:{program}" for kind, program in missing))
        if extra:
            details.append("unexpected " + ", ".join(f"{kind}:{program}" for kind, program in extra))
        return "solution identities do not match current config: " + "; ".join(details)
    if configured and problem_dir is not None:
        domains = _configured_execution_domains(problem_dir, config)
        for identity, current in domains.items():
            solution = observed.get(identity) or {}
            recorded = solution.get("execution_domain") or {}
            for key in ("run_on", "executed_cases", "skipped_cases"):
                if recorded.get(key) != current[key]:
                    return (
                        f"solution execution domain does not match current config for "
                        f"{identity[1]} ({key})"
                    )
            if recorded.get("coverage_ok") is not True or recorded.get("unexecuted_expected_cases"):
                return f"solution execution coverage is incomplete for {identity[1]}"
    return None


def evaluate_calibration(problem_dir, config, source_hash, data_hash):
    policy = calibration_policy(config)
    evidence = read_calibration_evidence(problem_dir)
    diagnostics = []
    state = "current"
    if evidence is None:
        state = "missing"
        diagnostics.append(_diagnostic(
            "calibration_evidence_missing",
            "no complete local judge calibration evidence; run probhub judge",
        ))
    elif evidence.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        state = "stale"
        diagnostics.append(_diagnostic(
            "calibration_evidence_stale",
            "local judge calibration evidence uses an unsupported schema; rerun probhub judge",
        ))
    elif (
        evidence.get("source_hash") != source_hash
        or evidence.get("data_hash") != data_hash
    ):
        state = "stale"
        diagnostics.append(_diagnostic(
            "calibration_evidence_stale",
            "local judge calibration evidence does not match current source/data; rerun probhub judge",
        ))
    elif (
        (evidence.get("measurement") or {}).get("platform") != platform.system()
        or (
            (evidence.get("measurement") or {}).get("machine")
            and (evidence.get("measurement") or {}).get("machine") != platform.machine()
        )
    ):
        state = "stale"
        diagnostics.append(_diagnostic(
            "calibration_evidence_stale",
            "local judge calibration evidence was measured on a different platform; rerun probhub judge",
        ))
    elif (structure_error := _evidence_structure_error(evidence, config, problem_dir)) is not None:
        state = "invalid"
        diagnostics.append(_diagnostic(
            "calibration_evidence_invalid",
            f"local judge calibration evidence is incomplete ({structure_error}); rerun probhub judge",
        ))
    else:
        accepted_required = policy["accepted_time_multiplier"]
        tle_required = policy["expected_tle_time_multiplier"]
        for solution in evidence.get("solutions") or []:
            if not isinstance(solution, dict):
                continue
            program = solution.get("program")
            kind = solution.get("kind")
            calibration = solution.get("calibration") or {}
            if kind == "std":
                headroom = calibration.get("headroom_factor")
                if not _finite_number(headroom):
                    diagnostics.append(_diagnostic(
                        "resource_margin_unavailable",
                        f"accepted timing margin is unavailable for {program}",
                        program=program,
                        resource="time",
                    ))
                elif float(headroom) + 1e-12 < accepted_required:
                    diagnostics.append(_diagnostic(
                        "accepted_time_margin_low",
                        f"accepted solution {program} has {float(headroom):.3g}x TL headroom; "
                        f"recommended minimum is {accepted_required:g}x",
                        program=program,
                        case=calibration.get("max_time_case"),
                        observed_headroom=float(headroom),
                        required_headroom=accepted_required,
                    ))

            expectation = solution.get("expectation") or {}
            expected_statuses = set(expectation.get("expected_statuses") or [])
            if "TLE" not in expected_statuses:
                continue
            resource_kills = calibration.get("resource_kills") or []
            tle = next(
                (item for item in resource_kills if item.get("status") == "TLE"),
                None,
            )
            if not isinstance(tle, dict):
                # expected.status may list alternative acceptable outcomes
                # (for example [TLE, MLE]); only an actually observed TLE needs
                # a timing-margin diagnostic.
                continue
            if not _finite_number(tle.get("limit_ratio")):
                diagnostics.append(_diagnostic(
                    "resource_margin_unavailable",
                    f"expected TLE timing margin is unavailable for {program}",
                    program=program,
                    resource="time",
                ))
                continue
            ratio = float(tle["limit_ratio"])
            process_status = tle.get("process_status")
            if process_status not in {"completed", "TLE"}:
                diagnostics.append(_diagnostic(
                    "expected_tle_probe_changed_status",
                    f"extended timing probe for {program} ended as {process_status or 'unknown'}",
                    program=program,
                    case=tle.get("case"),
                    process_status=process_status,
                ))
            if ratio + 1e-12 < tle_required:
                diagnostics.append(_diagnostic(
                    "expected_tle_margin_low",
                    f"expected TLE solution {program} was observed for only {ratio:.3g}x TL; "
                    f"required minimum is {tle_required:g}x",
                    program=program,
                    case=tle.get("case"),
                    observed_ratio=ratio,
                    required_ratio=tle_required,
                    evidence_kind=tle.get("evidence_kind"),
                ))

    return {
        "state": state,
        "target_guarantee": False,
        "policy": policy,
        "measurement_note": MEASUREMENT_NOTE,
        "diagnostics": diagnostics,
        "warnings": [f"[{item['code']}] {item['message']}" for item in diagnostics],
        **({"evidence": evidence} if evidence is not None else {}),
    }
