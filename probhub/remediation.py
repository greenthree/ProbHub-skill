"""Stable, bounded remediation metadata for structured diagnostics."""

from __future__ import annotations

from pathlib import Path

from .problem_paths import ProblemPathError, resolve_problem_regular_file


KNOWN_ACTION_CODES = frozenset({
    "refresh_calibration_evidence",
    "review_constraint_contract",
    "repair_judge_qa_fixtures",
    "refresh_judge_qa_evidence",
    "refresh_sealed_revision",
})

_CALIBRATION_CODES = {
    "calibration_evidence_missing",
    "calibration_evidence_stale",
    "calibration_evidence_invalid",
}
_CONSTRAINT_CODES = {
    "constraint_mismatch",
    "constraint_statement_only",
    "aggregate_constraint_mismatch",
    "aggregate_constraint_statement_only",
    "aggregate_constraint_dynamic",
    "aggregate_constraint_review_required",
}
_JUDGE_QA_FIXTURE_CODES = {
    "judge_qa_cases_empty",
    "judge_qa_case_reference_incomplete",
    "judge_qa_fixture_changed",
    "judge_qa_fixture_file_too_large",
    "judge_qa_fixture_path_invalid",
    "judge_qa_fixture_path_link",
    "judge_qa_fixture_path_missing",
    "judge_qa_fixture_path_non_regular",
    "judge_qa_fixture_path_outside",
    "judge_qa_fixture_path_scope",
    "judge_qa_fixture_total_bytes_exceeded",
}
_JUDGE_QA_EVIDENCE_CODES = {
    "judge_qa_evidence_missing",
    "judge_qa_evidence_stale",
    "judge_qa_evidence_invalid",
}
_BUILD_STATUS_CODES = {
    "build_manifest_missing",
    "build_manifest_invalid",
    "build_manifest_stale",
}


def _safe_relative_target(problem_dir, value):
    if problem_dir is None or not isinstance(value, str):
        return None
    root = Path(problem_dir).resolve()
    try:
        target = resolve_problem_regular_file(root, value)
    except ProblemPathError:
        return None
    return target.relative_to(root).as_posix()


def _command(name, problem_id, *options):
    if not isinstance(problem_id, str) or not problem_id:
        return None
    return ["probhub", name, problem_id, *options]


def _build_remediation(
    action_code,
    description,
    documentation,
    *,
    command=None,
    target_path=None,
    manual_review_required=False,
):
    result = {
        "action_code": action_code,
        "description": description,
        "documentation": documentation,
        "manual_review_required": bool(manual_review_required),
    }
    if command is not None:
        result["command"] = list(command)
    if target_path is not None:
        result["target_path"] = target_path
    return result


def remediation_for_diagnostic(
    diagnostic,
    *,
    problem_id=None,
    problem_dir=None,
    config=None,
):
    """Return remediation for one known diagnostic, otherwise ``None``."""

    if not isinstance(diagnostic, dict):
        return None
    code = diagnostic.get("code")
    if code in _CALIBRATION_CODES:
        return _build_remediation(
            "refresh_calibration_evidence",
            "Rerun the complete local Judge calibration after reviewing the diagnostic.",
            "references/cli.md#judge",
            command=_command("judge", problem_id, "--no-cache"),
        )
    if code in _CONSTRAINT_CODES:
        judge = config.get("judge") if isinstance(config, dict) else None
        validator = judge.get("validator") if isinstance(judge, dict) else None
        target = _safe_relative_target(problem_dir, validator)
        return _build_remediation(
            "review_constraint_contract",
            "Review the statement and Validator constraint contract, then lint again.",
            "references/workspace-schema-v1.md#problem-file",
            command=_command("lint", problem_id),
            target_path=target,
            manual_review_required=True,
        )
    if code in _JUDGE_QA_FIXTURE_CODES:
        return _build_remediation(
            "repair_judge_qa_fixtures",
            "Correct the Judge QA fixture declaration and its problem-local files.",
            "references/checker-interactor.md#judge-qa-active-testing",
            command=_command("lint", problem_id),
            target_path=_safe_relative_target(problem_dir, "probhub.yaml"),
            manual_review_required=True,
        )
    if code in _JUDGE_QA_EVIDENCE_CODES:
        return _build_remediation(
            "refresh_judge_qa_evidence",
            "Rerun Judge QA from current fixtures and review every unmatched result.",
            "references/cli.md#judge-qa",
            command=_command("judge-qa", problem_id, "--no-cache"),
            target_path=_safe_relative_target(problem_dir, "probhub.yaml"),
            manual_review_required=True,
        )
    if code in _BUILD_STATUS_CODES:
        return _build_remediation(
            "refresh_sealed_revision",
            "Refresh the sealed revision before publishing a new formal build.",
            "references/cli.md#checkpoint-seal-assemble",
            command=_command("seal", problem_id, "--no-cache", "--seed", "12345"),
        )
    return None


def attach_remediations(
    diagnostics,
    *,
    problem_id=None,
    problem_dir=None,
    config=None,
):
    """Copy diagnostics and add metadata only for known action codes."""

    result = []
    for diagnostic in diagnostics or []:
        if not isinstance(diagnostic, dict):
            result.append(diagnostic)
            continue
        item = dict(diagnostic)
        remediation = remediation_for_diagnostic(
            item,
            problem_id=problem_id,
            problem_dir=problem_dir,
            config=config,
        )
        if remediation is not None:
            item["remediation"] = remediation
        result.append(item)
    return result


def remediation_for_error(code, *, problem_id=None):
    """Return safe top-level remediation for selected stable Core errors."""

    if code not in {"sealed_revision_required", "sealed_revision_changed"}:
        return None
    return _build_remediation(
        "refresh_sealed_revision",
        "Refresh the affected sealed revision, then retry the formal multi-problem build.",
        "references/cli.md#13-build",
        command=_command("seal", problem_id, "--no-cache", "--seed", "12345"),
    )


def remediation_summary(remediation):
    """Render known remediation metadata; unknown actions are ignored."""

    if not isinstance(remediation, dict):
        return None
    if remediation.get("action_code") not in KNOWN_ACTION_CODES:
        return None
    description = remediation.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    parts = [description.strip()]
    command = remediation.get("command")
    if isinstance(command, list) and command and all(
        isinstance(item, str) and item for item in command
    ):
        parts.append("Run `" + " ".join(command) + "`.")
    if remediation.get("manual_review_required") is True:
        parts.append("Manual review is required.")
    return " ".join(parts)
