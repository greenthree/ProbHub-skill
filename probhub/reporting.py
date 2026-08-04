"""Read-only workspace health reports for authors and reviewers."""

import re
from collections import Counter
from pathlib import Path

from .datagen import recipe_coverage, resolve_data_dir
from .errors import ProbHubError
from .linting import lint_workspace
from .solutions import (
    case_group_names,
    normalize_data_groups,
    normalize_solution_entries,
    targeted_group_names,
)
from .workspace import load_problem, problem_entries


REPORT_SCHEMA_VERSION = 1
RANDOM_RATIO_WARNING_THRESHOLD = 0.5
_RANDOM_SIGNALS = ("random", "rand", "rnd", "随机")
_BOUNDARY_SIGNALS = (
    "boundary",
    "bound",
    "limit",
    "maximum",
    "minimum",
    "max",
    "min",
    "upper",
    "lower",
    "extreme",
    "edge",
    "边界",
    "上界",
    "下界",
    "极限",
    "最大",
    "最小",
)
_TARGET_SIGNALS = ("killer", "target", "counterexample", "hack", "反例", "定向", "卡")


def _diagnostic(code, message, *, severity="warning", **payload):
    return {
        "code": code,
        "severity": severity,
        "message": message,
        **payload,
    }


def _problem_label(position):
    value = int(position)
    result = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _ratio(part, whole):
    if not whole:
        return None
    return round(float(part) / float(whole), 6)


def _contains_signal(values, signals):
    text = " ".join(str(value).casefold() for value in values if value is not None)
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return any(
        (signal in text if any(ord(character) > 127 for character in signal) else any(
            token == signal or token.startswith(signal)
            for token in tokens
        ))
        for signal in signals
    )


def _file_size(path, diagnostics, problem_id, case_name, kind):
    try:
        return path.stat().st_size
    except OSError as exc:
        diagnostics.append(_diagnostic(
            "report_file_stat_failed",
            f"cannot read {kind} size for {case_name}: {exc}",
            problem_id=problem_id,
            case=case_name,
            path=str(path),
        ))
        return 0


def _collect_cases(problem_dir, config, groups, problem_id, diagnostics):
    cases = []
    suites = {}
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        try:
            directory = resolve_data_dir(problem_dir, config, f"{suite}_dir", default)
        except ProbHubError as exc:
            diagnostics.append(_diagnostic(
                "report_data_directory_invalid",
                str(exc),
                severity="error",
                problem_id=problem_id,
                suite=suite,
            ))
            directory = None
        suite_cases = []
        if directory is not None and directory.is_dir():
            for input_path in sorted(directory.glob("*.in"), key=lambda item: item.name):
                base_name = input_path.stem
                case_name = f"{suite}/{base_name}"
                answer_path = input_path.with_suffix(".ans")
                record = {
                    "suite": suite,
                    "name": base_name,
                    "case": case_name,
                    "groups": case_group_names(case_name, suite, base_name, groups),
                    "input_bytes": _file_size(
                        input_path, diagnostics, problem_id, case_name, "input"
                    ),
                    "answer_bytes": (
                        _file_size(answer_path, diagnostics, problem_id, case_name, "answer")
                        if answer_path.is_file()
                        else 0
                    ),
                }
                suite_cases.append(record)
                cases.append(record)
        max_case = max(suite_cases, key=lambda item: item["input_bytes"], default=None)
        suites[suite] = {
            "cases": len(suite_cases),
            "input_bytes": sum(item["input_bytes"] for item in suite_cases),
            "answer_bytes": sum(item["answer_bytes"] for item in suite_cases),
            "max_input_bytes": max_case["input_bytes"] if max_case else 0,
            "max_input_case": max_case["case"] if max_case else None,
        }
    max_case = max(cases, key=lambda item: item["input_bytes"], default=None)
    return cases, {
        **suites,
        "total": {
            "cases": len(cases),
            "input_bytes": sum(item["input_bytes"] for item in cases),
            "answer_bytes": sum(item["answer_bytes"] for item in cases),
            "max_input_bytes": max_case["input_bytes"] if max_case else 0,
            "max_input_case": max_case["case"] if max_case else None,
        },
    }


def _group_profile(groups, cases, secret_count):
    result = []
    for group in groups:
        matched = [case for case in cases if group["name"] in case["groups"]]
        sample_count = sum(case["suite"] == "sample" for case in matched)
        group_secret_count = sum(case["suite"] == "secret" for case in matched)
        result.append({
            "name": group["name"],
            "role": group.get("role"),
            "patterns": list(group.get("patterns") or []),
            "targets": list(group.get("targets") or []),
            "sample_cases": sample_count,
            "secret_cases": group_secret_count,
            "total_cases": len(matched),
            "secret_ratio": _ratio(group_secret_count, secret_count),
        })
    grouped_secret = {
        case["case"]
        for case in cases
        if case["suite"] == "secret" and case["groups"]
    }
    return result, secret_count - len(grouped_secret)


def _upper_bound_values(constraint_report):
    values = []
    for item in (constraint_report or {}).get("statement_constraints") or []:
        if item.get("bound") != "upper" or item.get("dynamic"):
            continue
        value = item.get("value_normalized")
        if isinstance(value, str) and value.strip():
            values.append(value.strip().casefold())
    return sorted(set(values))


def _recipe_profile(
    problem_dir,
    config,
    cases,
    groups,
    constraint_report,
    wrong_programs,
    wrong_target_groups,
):
    diagnostics = []
    try:
        recipes, uncovered = recipe_coverage(problem_dir, config)
    except ProbHubError as exc:
        return {
            "analysis_state": "invalid",
            "analysis_note": "Recipe profiling stopped because data.recipes is invalid.",
            "total": 0,
            "manual": 0,
            "generated": 0,
            "covered_secret_cases": 0,
            "uncovered_secret_cases": [],
            "coverage_ratio": None,
            "random": 0,
            "random_ratio": None,
            "targeted": 0,
            "near_boundary": 0,
            "upper_bounds": [],
            "profiles": [],
            "diagnostics": [_diagnostic(
                "report_recipe_invalid",
                str(exc),
                severity="error",
            )],
        }

    secret_cases = [case for case in cases if case["suite"] == "secret"]
    secret_names = {case["name"].casefold() for case in secret_cases}
    upper_bounds = _upper_bound_values(constraint_report)
    group_by_name = {group["name"]: group for group in groups}
    profiles = []
    for recipe in recipes:
        case_name = recipe["case"]
        logical_case = f"secret/{case_name}"
        matched_groups = case_group_names(logical_case, "secret", case_name, groups)
        group_values = []
        targeted = bool(set(matched_groups).intersection(wrong_target_groups))
        for name in matched_groups:
            group = group_by_name.get(name) or {}
            group_values.extend((name, group.get("role")))
            targets = set(group.get("targets") or [])
            if targets.intersection(wrong_programs) or _contains_signal(
                (name, group.get("role")), _TARGET_SIGNALS
            ):
                targeted = True

        signal_values = [case_name, recipe.get("generator"), *(recipe.get("args") or []), *group_values]
        random_case = _contains_signal(signal_values, _RANDOM_SIGNALS)
        boundary_signals = []
        if _contains_signal(signal_values, _BOUNDARY_SIGNALS):
            boundary_signals.append("name_or_argument")
        normalized_args = {str(value).strip().casefold() for value in recipe.get("args") or []}
        matched_bounds = sorted(normalized_args.intersection(upper_bounds))
        if matched_bounds:
            boundary_signals.append("declared_upper_bound")
        profiles.append({
            "case": case_name,
            "manual": bool(recipe.get("manual")),
            "generator": recipe.get("generator"),
            "args": list(recipe.get("args") or []),
            "groups": matched_groups,
            "present": case_name.casefold() in secret_names,
            "random": random_case,
            "targeted": targeted,
            "near_boundary": bool(boundary_signals),
            "boundary_signals": boundary_signals,
            "matched_upper_bounds": matched_bounds,
        })

    total = len(recipes)
    random_count = sum(item["random"] for item in profiles)
    targeted_count = sum(item["targeted"] for item in profiles)
    boundary_count = sum(item["near_boundary"] for item in profiles)
    absent = [item["case"] for item in profiles if not item["present"]]
    if secret_cases and not recipes:
        diagnostics.append(_diagnostic(
            "report_recipe_missing",
            f"all {len(secret_cases)} secret case(s) lack data.recipes provenance",
            secret_cases=len(secret_cases),
        ))
    elif uncovered:
        diagnostics.append(_diagnostic(
            "report_recipe_uncovered",
            f"{len(uncovered)} secret case(s) lack data.recipes provenance",
            cases=uncovered,
        ))
    if absent:
        diagnostics.append(_diagnostic(
            "report_recipe_case_missing",
            f"{len(absent)} declared recipe case(s) have no current secret input",
            cases=absent,
        ))
    random_ratio = _ratio(random_count, total)
    if random_ratio is not None and random_ratio > RANDOM_RATIO_WARNING_THRESHOLD:
        diagnostics.append(_diagnostic(
            "report_random_ratio_high",
            f"random-like recipes account for {random_ratio:.0%}; recommended maximum is "
            f"{RANDOM_RATIO_WARNING_THRESHOLD:.0%}",
            random_recipes=random_count,
            total_recipes=total,
            ratio=random_ratio,
            threshold=RANDOM_RATIO_WARNING_THRESHOLD,
        ))
    if total and wrong_programs and not targeted_count:
        diagnostics.append(_diagnostic(
            "report_targeted_recipe_missing",
            "no recipe is mapped to a targeted wrong-solution data group",
            wrong_solutions=len(wrong_programs),
        ))
    if total and upper_bounds and not boundary_count:
        diagnostics.append(_diagnostic(
            "report_near_boundary_recipe_missing",
            "no recipe has a near-boundary signal or an argument equal to a declared upper bound",
            upper_bounds=upper_bounds,
        ))
    return {
        "analysis_state": "heuristic",
        "analysis_note": (
            "Random/targeted/near-boundary labels use recipe names and arguments, "
            "data-group roles/targets, and exact declared upper-bound arguments; "
            "they are author-review signals, not semantic proof of generator coverage."
        ),
        "total": total,
        "manual": sum(item["manual"] for item in profiles),
        "generated": sum(not item["manual"] for item in profiles),
        "covered_secret_cases": len(secret_cases) - len(uncovered),
        "uncovered_secret_cases": uncovered,
        "coverage_ratio": _ratio(len(secret_cases) - len(uncovered), len(secret_cases)),
        "random": random_count,
        "random_ratio": random_ratio,
        "targeted": targeted_count,
        "near_boundary": boundary_count,
        "upper_bounds": upper_bounds,
        "profiles": profiles,
        "diagnostics": diagnostics,
    }


def _calibration_profile(calibration):
    state = (calibration or {}).get("state", "missing")
    evidence = (calibration or {}).get("evidence") or {}
    accepted = []
    if state == "current":
        for solution in evidence.get("solutions") or []:
            if solution.get("kind") != "std":
                continue
            measured = solution.get("calibration") or {}
            check = measured.get("accepted_check") or {}
            accepted.append({
                "program": solution.get("program"),
                "max_time": measured.get("max_time"),
                "max_time_case": measured.get("max_time_case"),
                "time_limit_ratio": measured.get("time_limit_ratio"),
                "headroom_factor": measured.get("headroom_factor"),
                "required_headroom": check.get("required_multiplier"),
                "headroom_ok": check.get("ok"),
                "target_guarantee": False,
            })
    return {
        "state": state,
        "target_guarantee": False,
        "accepted": accepted,
        "primary_headroom": accepted[0].get("headroom_factor") if accepted else None,
        "measurement_note": (calibration or {}).get("measurement_note"),
    }


def _kill_matrix(config, groups, cases, calibration):
    wrong_entries = normalize_solution_entries(config)["wrong"]
    evidence_state = (calibration or {}).get("state", "missing")
    evidence_solutions = {
        (item.get("kind"), str(item.get("program") or "").replace("\\", "/")): item
        for item in ((calibration or {}).get("evidence") or {}).get("solutions") or []
        if isinstance(item, dict)
    }
    scopes = []
    for entry in wrong_entries:
        expected_groups = list((entry.get("expected") or {}).get("groups") or [])
        target_groups = targeted_group_names(groups, entry.get("file"))
        scopes.append(expected_groups or target_groups or ["__all__"])
    columns = [group["name"] for group in groups]
    for scope in scopes:
        for name in scope:
            if name != "__all__" and name not in columns:
                columns.append(name)
    if any("__all__" in scope for scope in scopes):
        columns.append("__all__")

    rows = []
    for entry, scope in zip(wrong_entries, scopes):
        program = entry.get("file")
        evidence = evidence_solutions.get(("wrong", str(program or "").replace("\\", "/")))
        expectation = (evidence or {}).get("expectation") or {}
        matched_names = set(expectation.get("matched_cases") or [])
        cells = {}
        for column in columns:
            declared = column in scope
            selected = [
                case for case in cases
                if column == "__all__" or column in case["groups"]
            ]
            matched = [case["case"] for case in selected if case["case"] in matched_names]
            if not declared:
                state = "not-targeted"
            elif evidence_state != "current" or evidence is None:
                state = "unknown"
            elif matched:
                state = "killed"
            else:
                state = "missed"
            cells[column] = {
                "declared": declared,
                "state": state,
                "selected_cases": len(selected),
                "matched_cases": matched,
            }
        rows.append({
            "program": program,
            "expected_statuses": list((entry.get("expected") or {}).get("status") or []),
            "scope": scope,
            "overall": (
                "unknown"
                if evidence_state != "current" or evidence is None
                else "passed" if expectation.get("ok") else "failed"
            ),
            "cells": cells,
        })
    return {
        "evidence_state": evidence_state,
        "columns": columns,
        "rows": rows,
    }


def _problem_report(root, workspace, entry, position, lint_result):
    problem_dir, config = load_problem(root, entry)
    groups = normalize_data_groups(config)
    diagnostics = []
    cases, test_profile = _collect_cases(
        problem_dir, config, groups, entry["id"], diagnostics
    )
    group_profile, ungrouped_secret = _group_profile(
        groups, cases, test_profile["secret"]["cases"]
    )
    wrong_entries = normalize_solution_entries(config)["wrong"]
    wrong_programs = {entry.get("file") for entry in wrong_entries if entry.get("file")}
    wrong_target_groups = set()
    for wrong_entry in wrong_entries:
        expected_groups = set((wrong_entry.get("expected") or {}).get("groups") or [])
        wrong_target_groups.update(
            expected_groups or targeted_group_names(groups, wrong_entry.get("file"))
        )
    targeted_secret_cases = {
        case["case"]
        for case in cases
        if case["suite"] == "secret"
        and set(case["groups"]).intersection(wrong_target_groups)
    }
    if wrong_programs and not targeted_secret_cases:
        diagnostics.append(_diagnostic(
            "report_targeted_data_missing",
            "no secret case belongs to a data group targeting a configured wrong solution",
            wrong_solutions=sorted(wrong_programs),
        ))

    constraint_report = (lint_result or {}).get("constraint_reconciliation") or {}
    recipes = _recipe_profile(
        problem_dir,
        config,
        cases,
        groups,
        constraint_report,
        wrong_programs,
        wrong_target_groups,
    )
    diagnostics.extend(recipes.pop("diagnostics"))
    calibration = (lint_result or {}).get("calibration") or {"state": "missing"}
    report_diagnostics = [
        *(lint_result or {}).get("diagnostics", []),
        *diagnostics,
    ]
    return {
        "id": entry["id"],
        "number": position,
        "label": _problem_label(position),
        "name": config.get("display_name") or config.get("name") or entry["id"],
        "difficulty": config.get("difficulty"),
        "tags": list(config.get("tags") or []),
        "limits": dict(config.get("limits") or {}),
        "lint": {
            "ok": bool((lint_result or {}).get("ok")),
            "errors": list((lint_result or {}).get("errors") or []),
            "warnings": list((lint_result or {}).get("warnings") or []),
        },
        "tests": test_profile,
        "groups": group_profile,
        "ungrouped_secret_cases": ungrouped_secret,
        "targeted_secret_cases": len(targeted_secret_cases),
        "recipes": recipes,
        "aggregate_constraints": constraint_report.get("aggregate_constraints") or {
            "analysis_state": "partial",
            "multi_case_detected": False,
            "state": "not_detected",
            "matched": [],
            "statement_only": [],
            "validator_only": [],
            "dynamic": [],
            "statement_constraints": [],
            "validator_constraints": [],
            "summary": {
                "matched": 0,
                "statement_only": 0,
                "validator_only": 0,
                "dynamic": 0,
            },
        },
        "calibration": _calibration_profile(calibration),
        "solution_verification": (lint_result or {}).get("solution_verification") or {},
        "kill_matrix": _kill_matrix(config, groups, cases, calibration),
        "diagnostics": report_diagnostics,
    }


def build_workspace_report(root, workspace, selected=None):
    """Build a deterministic, read-only workspace report."""
    root = Path(root).resolve()
    all_entries = problem_entries(workspace)
    entries = list(selected or all_entries)
    selected_ids = {entry["id"] for entry in entries}
    lint = lint_workspace(root, workspace, entries)
    lint_by_id = {
        item.get("id"): item for item in lint.get("problems") or []
        if isinstance(item, dict)
    }
    positions = {entry["id"]: index for index, entry in enumerate(all_entries, start=1)}
    problems = [
        _problem_report(
            root,
            workspace,
            entry,
            positions[entry["id"]],
            lint_by_id.get(entry["id"], {}),
        )
        for entry in all_entries
        if entry["id"] in selected_ids
    ]
    difficulty = Counter(
        "unknown" if problem["difficulty"] is None else str(problem["difficulty"])
        for problem in problems
    )
    diagnostics = [
        {**item, "problem_id": problem["id"]}
        for problem in problems
        for item in problem["diagnostics"]
        if isinstance(item, dict)
    ]
    contest = workspace.get("contest") or {}
    summary = {
        "problems": len(problems),
        "difficulty_distribution": dict(sorted(difficulty.items())),
        "sample_cases": sum(problem["tests"]["sample"]["cases"] for problem in problems),
        "secret_cases": sum(problem["tests"]["secret"]["cases"] for problem in problems),
        "input_bytes": sum(problem["tests"]["total"]["input_bytes"] for problem in problems),
        "data_groups": sum(len(problem["groups"]) for problem in problems),
        "wrong_solutions": sum(len(problem["kill_matrix"]["rows"]) for problem in problems),
        "calibration_states": dict(Counter(
            problem["calibration"]["state"] for problem in problems
        )),
        "warnings": sum(item.get("severity") == "warning" for item in diagnostics),
        "errors": sum(item.get("severity") == "error" for item in diagnostics)
        + sum(len(problem["lint"]["errors"]) for problem in problems),
    }
    return {
        "ok": bool(lint.get("ok")),
        "schema_version": REPORT_SCHEMA_VERSION,
        "analysis_state": "declared_inputs_and_local_evidence",
        "workspace": {
            "title": contest.get("title") or root.name,
            "subtitle": contest.get("subtitle"),
            "problem_count": len(all_entries),
            "selected_count": len(problems),
        },
        "summary": summary,
        "problems": problems,
        "diagnostics": diagnostics,
        "errors": list(lint.get("errors") or []),
    }


def _markdown_escape(value):
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _format_ratio(value):
    return "—" if value is None else f"{float(value):.0%}"


def _format_headroom(value):
    return "—" if value is None else f"{float(value):.3g}x"


def render_markdown_report(report):
    workspace = report["workspace"]
    summary = report["summary"]
    lines = [
        f"# ProbHub 工作区报告：{_markdown_escape(workspace['title'])}",
        "",
        f"- 题目：{summary['problems']}",
        f"- 测试点：sample {summary['sample_cases']} / secret {summary['secret_cases']}",
        f"- 输入总量：{summary['input_bytes']} bytes",
        f"- Warning / Error：{summary['warnings']} / {summary['errors']}",
        "",
        "## 题目概览",
        "",
        "| 题号 | ID | 题名 | 难度 | 标签 | Sample | Secret | Recipe 覆盖 | TL 余量 |",
        "|---|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for problem in report["problems"]:
        lines.append(
            "| {label} | {id} | {name} | {difficulty} | {tags} | {sample} | {secret} | {recipes} | {headroom} |".format(
                label=_markdown_escape(problem["label"]),
                id=_markdown_escape(problem["id"]),
                name=_markdown_escape(problem["name"]),
                difficulty=_markdown_escape(problem["difficulty"]),
                tags=_markdown_escape(", ".join(problem["tags"]) or "—"),
                sample=problem["tests"]["sample"]["cases"],
                secret=problem["tests"]["secret"]["cases"],
                recipes=_format_ratio(problem["recipes"]["coverage_ratio"]),
                headroom=_format_headroom(problem["calibration"]["primary_headroom"]),
            )
        )

    for problem in report["problems"]:
        lines.extend(["", f"## {problem['label']}. {_markdown_escape(problem['name'])} (`{problem['id']}`)", ""])
        recipes = problem["recipes"]
        lines.extend([
            f"- 测试规模：sample {problem['tests']['sample']['cases']} / secret {problem['tests']['secret']['cases']}；"
            f"输入 {problem['tests']['total']['input_bytes']} bytes；最大输入 "
            f"{problem['tests']['total']['max_input_bytes']} bytes（{_markdown_escape(problem['tests']['total']['max_input_case'])}）",
            f"- Recipe：覆盖 {_format_ratio(recipes['coverage_ratio'])}；random {recipes['random']}/{recipes['total']}；"
            f"targeted {recipes['targeted']}/{recipes['total']}；near-boundary {recipes['near_boundary']}/{recipes['total']}",
            f"- 校准：{problem['calibration']['state']}；primary accepted TL 余量 "
            f"{_format_headroom(problem['calibration']['primary_headroom'])}；`target_guarantee: false`",
            f"- 累计约束：{problem['aggregate_constraints']['state']}；"
            f"matched {problem['aggregate_constraints']['summary']['matched']}；"
            f"statement-only {problem['aggregate_constraints']['summary']['statement_only']}；"
            f"Validator-only {problem['aggregate_constraints']['summary']['validator_only']}；"
            f"dynamic {problem['aggregate_constraints']['summary']['dynamic']}",
            "",
        ])
        if problem["groups"]:
            lines.extend([
                "### 数据组画像",
                "",
                "| 数据组 | 角色 | Sample | Secret | Secret 占比 | Targets |",
                "|---|---|---:|---:|---:|---|",
            ])
            for group in problem["groups"]:
                lines.append(
                    f"| {_markdown_escape(group['name'])} | {_markdown_escape(group['role'])} | "
                    f"{group['sample_cases']} | {group['secret_cases']} | "
                    f"{_format_ratio(group['secret_ratio'])} | "
                    f"{_markdown_escape(', '.join(group['targets']) or '—')} |"
                )
        matrix = problem["kill_matrix"]
        if matrix["rows"]:
            columns = matrix["columns"]
            lines.extend(["", "### 错解击杀矩阵", ""])
            header = ["Solution", *[("all" if column == "__all__" else column) for column in columns], "Evidence"]
            lines.append("| " + " | ".join(_markdown_escape(item) for item in header) + " |")
            lines.append("|" + "|".join("---" for _ in header) + "|")
            marks = {"killed": "✓", "missed": "✗", "unknown": "?", "not-targeted": "·"}
            for row in matrix["rows"]:
                values = [row["program"]]
                values.extend(marks[row["cells"][column]["state"]] for column in columns)
                values.append(row["overall"])
                lines.append("| " + " | ".join(_markdown_escape(item) for item in values) + " |")
        warning_items = [
            item for item in problem["diagnostics"]
            if item.get("severity") in {"warning", "error"}
        ]
        if warning_items or problem["lint"]["errors"]:
            lines.extend(["", "### 诊断", ""])
            for message in problem["lint"]["errors"]:
                lines.append(f"- **error** `{problem['id']}`: {_markdown_escape(message)}")
            for item in warning_items:
                lines.append(
                    f"- **{item.get('severity')}** `{item.get('code')}`: "
                    f"{_markdown_escape(item.get('message'))}"
                )
    lines.append("")
    return "\n".join(lines)


def render_text_report(report):
    workspace = report["workspace"]
    summary = report["summary"]
    lines = [
        f"ProbHub 工作区报告: {workspace['title']}",
        f"题目 {summary['problems']} | sample {summary['sample_cases']} | secret {summary['secret_cases']} | "
        f"warning {summary['warnings']} | error {summary['errors']}",
        "",
    ]
    for problem in report["problems"]:
        tags = ", ".join(problem["tags"]) or "—"
        lines.append(
            f"[{problem['label']}] {problem['id']} {problem['name']} | 难度 {problem['difficulty'] if problem['difficulty'] is not None else '—'} "
            f"| 标签 {tags} | sample {problem['tests']['sample']['cases']} / secret {problem['tests']['secret']['cases']} "
            f"| recipe {_format_ratio(problem['recipes']['coverage_ratio'])} | TL {_format_headroom(problem['calibration']['primary_headroom'])}"
        )
        recipes = problem["recipes"]
        lines.append(
            f"  规模: input={problem['tests']['total']['input_bytes']}B "
            f"max={problem['tests']['total']['max_input_bytes']}B({problem['tests']['total']['max_input_case'] or '—'})"
        )
        lines.append(
            f"  Recipe: random={recipes['random']}/{recipes['total']} "
            f"targeted={recipes['targeted']}/{recipes['total']} "
            f"near-boundary={recipes['near_boundary']}/{recipes['total']} "
            f"analysis={recipes['analysis_state']}"
        )
        aggregate = problem["aggregate_constraints"]
        lines.append(
            f"  累计约束: {aggregate['state']} "
            f"matched={aggregate['summary']['matched']} "
            f"statement-only={aggregate['summary']['statement_only']} "
            f"validator-only={aggregate['summary']['validator_only']} "
            f"dynamic={aggregate['summary']['dynamic']}"
        )
        if problem["groups"]:
            lines.append("  数据组:")
            for group in problem["groups"]:
                lines.append(
                    f"    - {group['name']} ({group.get('role') or '—'}): "
                    f"sample={group['sample_cases']} secret={group['secret_cases']} "
                    f"share={_format_ratio(group['secret_ratio'])}"
                )
        matrix = problem["kill_matrix"]
        if matrix["rows"]:
            columns = matrix["columns"]
            names = [("all" if column == "__all__" else column) for column in columns]
            lines.append("  击杀矩阵: solution | " + " | ".join(names) + " | evidence")
            marks = {"killed": "KILL", "missed": "MISS", "unknown": "?", "not-targeted": "-"}
            for row in matrix["rows"]:
                cells = [marks[row["cells"][column]["state"]] for column in columns]
                lines.append(
                    f"    {row['program']} | " + " | ".join(cells) + f" | {row['overall']}"
                )
        for message in problem["lint"]["errors"]:
            lines.append(f"  ERROR lint: {message}")
        for item in problem["diagnostics"]:
            if item.get("severity") in {"warning", "error"}:
                lines.append(
                    f"  {item.get('severity', 'warning').upper()} {item.get('code')}: {item.get('message')}"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_workspace_report(report, output_format="text"):
    if output_format == "markdown":
        return render_markdown_report(report)
    if output_format == "text":
        return render_text_report(report)
    raise ValueError(f"unsupported report format: {output_format}")
