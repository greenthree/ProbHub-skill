import os

from .solutions import (
    case_group_names,
    normalize_data_groups,
    normalize_expected,
    select_expectation_cases,
    select_run_cases,
    select_target_cases,
)


def collect_testcases(prob_dir, config=None):
    """Collect sample and secret cases in the stable local Judge order."""
    testcases = []
    data_config = (config or {}).get("data") or {}
    groups = normalize_data_groups(config or {})
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        data_dir = os.path.join(prob_dir, data_config.get(f"{suite}_dir", default))
        if not os.path.isdir(data_dir):
            continue
        input_names = sorted(name for name in os.listdir(data_dir) if name.endswith(".in"))
        for input_name in input_names:
            base_name = input_name[:-3]
            case_name = f"{suite}/{base_name}"
            testcases.append({
                "suite": suite,
                "name": base_name,
                "case": case_name,
                "groups": case_group_names(
                    case_name, suite, base_name, groups
                ),
                "in_path": os.path.join(data_dir, input_name),
                "ans_path": os.path.join(data_dir, f"{base_name}.ans"),
            })
    return testcases


def select_solution_expectation_cases(kind, entry, testcases, group_definitions):
    """Select the cases whose verdicts establish one solution expectation."""
    if kind == "std" and entry.get("run_on") is None:
        return list(testcases), []
    return select_expectation_cases(kind, entry, testcases, group_definitions)


def expectation_cases(kind, testcases, expected, group_definitions, program_name):
    """Compatibility adapter for callers with a split expectation value."""
    return select_solution_expectation_cases(
        kind,
        {"file": program_name, "expected": expected, "run_on": None},
        testcases,
        group_definitions,
    )


def solution_execution_plan(source_groups, testcases, group_definitions):
    """Build per-solution execution domains and report deterministic gaps."""
    group_names = {group["name"] for group in group_definitions}
    plans = {"std": [], "brute": [], "wrong": []}
    errors = []
    for kind, entries in source_groups.items():
        for entry in entries:
            program = str(entry.get("file") or entry.get("path") or "").replace("\\", "/")
            normalized = {
                "file": program,
                "expected": entry.get("expected") or normalize_expected(kind, None),
                "run_on": entry.get("run_on"),
            }
            run_on = normalized["run_on"]
            executed, skipped = select_run_cases(normalized, testcases)
            selected, selected_groups = select_solution_expectation_cases(
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


def result_snapshot(item):
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
    """Aggregate case verdicts against one normalized expectation."""
    entry = {"file": program_name, "expected": expected, "run_on": run_on}
    selected_cases, groups = select_solution_expectation_cases(
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
        "first_non_ac": result_snapshot(first_non_ac),
        "first_expected_match": result_snapshot(first_expected_match),
        "first_forbidden": result_snapshot(first_forbidden),
    }


def status_only_resource_kill(status, case_result, limits):
    """Summarize MLE/OLE evidence without inventing missing measurements."""
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
    """Build one solution's deterministic local calibration summary."""
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


def cached_case_result(cache, key, require_sample_answer=False, *, sample_validator):
    """Return a cached case unless strict sample evidence is incomplete."""
    cached = cache.get("case", key)
    if (
        cached is not None
        and require_sample_answer
        and not sample_validator((cached.get("details") or {}).get("sample_answer"))
    ):
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
    """Store the stable per-case cache payload."""
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
