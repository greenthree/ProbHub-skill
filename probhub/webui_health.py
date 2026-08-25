"""Bounded, read-only health summaries for the local WebUI."""

import json
from pathlib import Path

from .builder_fingerprint import compute_builder_fingerprint
from .errors import ProbHubError
from .generations import generation_status, latest_checkpoint
from .linting import (
    compute_collection_hash,
    compute_workspace_hash,
    lint_workspace,
    problem_status,
)
from .reporting import build_workspace_report
from .transactions import ensure_no_pending_transactions
from .workspace import load_problem, problem_entries, select_entries


HEALTH_SCHEMA_VERSION = 1
_DIAGNOSTIC_KEYS = ("code", "severity", "message")
_REMEDIATION_KEYS = (
    "action_code",
    "command",
    "description",
    "documentation",
    "manual_review_required",
    "target_path",
)


def _diagnostic(value):
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in _DIAGNOSTIC_KEYS if key in value}
    remediation = value.get("remediation")
    if isinstance(remediation, dict):
        result["remediation"] = {
            key: remediation[key]
            for key in _REMEDIATION_KEYS
            if key in remediation
        }
    return result or None


def _diagnostics(*collections):
    result = []
    seen = set()
    for collection in collections:
        for value in collection or ():
            item = _diagnostic(value)
            if item is None:
                continue
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                result.append(item)
    return result


def _remediations(diagnostics):
    return [
        item["remediation"]
        for item in diagnostics
        if isinstance(item.get("remediation"), dict)
    ]


def _state(value, default="unknown"):
    return value if isinstance(value, str) and value else default


def _checkpoint(root, problem_id, status):
    try:
        value = latest_checkpoint(root, problem_id)
    except ProbHubError as exc:
        return {
            "state": "invalid",
            "revision_id": None,
            "created_at": None,
            "error_code": exc.code or "checkpoint_invalid",
        }, {}
    if not isinstance(value, dict):
        return {"state": "missing", "revision_id": None, "created_at": None}, {}
    checkpoint_state = _state(value.get("state"), "invalid")
    if (
        value.get("source_hash") != status.get("source_hash")
        or value.get("data_hash") != status.get("data_hash")
    ):
        checkpoint_state = "stale"
    evidence = value.get("evidence")
    return {
        "state": checkpoint_state,
        "revision_id": value.get("revision_id"),
        "created_at": value.get("created_at"),
    }, evidence if isinstance(evidence, dict) else {}


def _sealed_check(evidence, key, checkpoint_state):
    value = evidence.get(key)
    if checkpoint_state == "stale" and isinstance(value, dict):
        return {"state": "stale"}
    if value is None:
        return {"state": "not-run"}
    if not isinstance(value, dict):
        return {"state": "invalid"}
    if key == "stress":
        return {
            "state": "passed" if value.get("ok") is True else "failed",
            "rounds_completed": value.get("rounds_completed"),
            "seed": value.get("seed"),
            **({"reason": value.get("reason")} if value.get("reason") else {}),
        }
    final = value.get("final") if isinstance(value.get("final"), dict) else {}
    return {
        "state": "passed" if value.get("ok") is True else "failed",
        "returncode": value.get("returncode"),
        "final_status": final.get("status"),
    }


def _judge_qa(status):
    inspection = status.get("judge_qa") or {}
    evidence = inspection.get("evidence") or {}
    return {
        "state": _state(evidence.get("state"), "not-configured"),
        "configured": bool(evidence.get("configured")),
        "applicable": bool(evidence.get("applicable")),
        "declared_cases": evidence.get("declared_cases"),
        "matched_cases": evidence.get("matched_cases"),
        "declared_probes": evidence.get("declared_probes"),
        "evidence_probes": evidence.get("evidence_probes"),
        "manual_review_probes": evidence.get("manual_review_probes"),
    }


def _calibration(status, report_item):
    evaluated = status.get("calibration") or {}
    evidence = evaluated.get("evidence") or {}
    resource_kills = []
    if evaluated.get("state") == "current":
        for solution in evidence.get("solutions") or []:
            if not isinstance(solution, dict):
                continue
            for kill in (solution.get("calibration") or {}).get("resource_kills") or []:
                if not isinstance(kill, dict):
                    continue
                resource_kills.append({
                    key: value
                    for key, value in {
                        "program": solution.get("program"),
                        "kind": solution.get("kind"),
                        "status": kill.get("status"),
                        "case": kill.get("case"),
                        "limit_ratio": kill.get("limit_ratio"),
                        "evidence_kind": kill.get("evidence_kind"),
                        "process_status": kill.get("process_status"),
                    }.items()
                    if value is not None
                })
    profile = report_item.get("calibration") or {}
    return {
        "state": _state(profile.get("state"), "missing"),
        "primary_headroom": profile.get("primary_headroom"),
        "accepted": [
            {
                key: item.get(key)
                for key in (
                    "program",
                    "max_time",
                    "time_limit_ratio",
                    "headroom_factor",
                    "headroom_ok",
                )
                if key in item
            }
            for item in profile.get("accepted") or []
            if isinstance(item, dict)
        ],
        "resource_kills": resource_kills,
        "measurement_note": profile.get("measurement_note"),
    }


def _artifact_state(status, path):
    if not Path(path).is_file():
        return "missing"
    return "current" if status.get("state") == "current" else _state(status.get("state"))


def _problem_summary(root, workspace, entry, lint_item, report_item, status):
    problem_id = entry["id"]
    problem_dir, config = load_problem(root, entry)
    checkpoint, sealed_evidence = _checkpoint(root, problem_id, status)
    diagnostics = _diagnostics(
        lint_item.get("diagnostics"),
        status.get("diagnostics"),
        report_item.get("diagnostics"),
    )
    constraint = lint_item.get("constraint_reconciliation") or {}
    aggregate = constraint.get("aggregate_constraints") or {}
    sample_cases = ((report_item.get("tests") or {}).get("sample") or {}).get("cases", 0)
    judge = _sealed_check(sealed_evidence, "judge", checkpoint["state"])
    sample_state = "covered-by-judge" if judge["state"] == "passed" else judge["state"]
    mutation = report_item.get("mutation") or {}
    manifest = status.get("manifest")
    return {
        "id": problem_id,
        "name": report_item.get("name") or config.get("display_name") or problem_id,
        "number": report_item.get("number"),
        "lint": {
            "ok": bool(lint_item.get("ok")),
            "errors": list(lint_item.get("errors") or []),
            "warnings": list(lint_item.get("warnings") or []),
            "diagnostics": _diagnostics(lint_item.get("diagnostics")),
        },
        "constraints": {
            "state": _state(constraint.get("analysis_state"), "unknown"),
            "confidence": constraint.get("confidence"),
            "requires_review": bool(constraint.get("requires_review")),
            "summary": dict(constraint.get("summary") or {}),
            "aggregate_state": _state(aggregate.get("state"), "not-detected"),
            "diagnostics": _diagnostics(constraint.get("diagnostics")),
        },
        "checks": {
            "sample": {"state": sample_state, "cases": sample_cases},
            "judge": judge,
            "judge_qa": _judge_qa(status),
            "stress": _sealed_check(sealed_evidence, "stress", checkpoint["state"]),
            "mutation": {
                "state": _state(mutation.get("state"), "not-run"),
                "summary": dict(mutation.get("summary") or {}),
                "planning": dict(mutation.get("planning") or {}),
            },
        },
        "evidence": {
            "calibration": _calibration(status, report_item),
            "diagnostics": diagnostics,
        },
        "checkpoint": checkpoint,
        "artifacts": {
            "state": _state(status.get("state")),
            "manifest": "missing" if not isinstance(manifest, dict) else _state(status.get("state")),
            "pdf": _artifact_state(status, problem_dir / "problem.pdf"),
            "zip": _artifact_state(status, problem_dir.parent / f"{problem_id}.zip"),
            "stale_fields": list(status.get("stale_fields") or []),
            "sealed_revision_id": (manifest or {}).get("sealed_revision_id"),
        },
        "remediations": _remediations(diagnostics),
    }


def _generation_summary(root):
    value = generation_status(root)
    manifest = value.get("manifest") if isinstance(value.get("manifest"), dict) else {}
    return {
        "state": _state(value.get("state"), "none"),
        "ok": bool(value.get("ok")),
        "generation_id": value.get("generation_id"),
        "complete": manifest.get("complete"),
        "all_sealed": manifest.get("all_sealed"),
        "missing": [
            item.get("problem_id")
            for item in manifest.get("missing") or []
            if isinstance(item, dict) and item.get("problem_id")
        ],
        "stale_fields": list(value.get("stale_fields") or []),
    }


def build_health_summary(root, workspace, selected_ids=None):
    """Combine existing Core results without reading or publishing raw artifacts."""
    root = Path(root).resolve()
    ensure_no_pending_transactions(root, workspace)
    all_entries = problem_entries(workspace)
    entries = select_entries(workspace, selected_ids) if selected_ids else all_entries
    if not entries:
        contest = workspace.get("contest") or {}
        return {
            "success": True,
            "schema_version": HEALTH_SCHEMA_VERSION,
            "workspace": {
                "title": contest.get("title") or root.name,
                "subtitle": contest.get("subtitle"),
                "problem_count": len(all_entries),
                "selected_count": 0,
            },
            "summary": {"problems": 0, "warnings": 0, "errors": 0},
            "problems": [],
            "generation": _generation_summary(root),
            "diagnostics": [],
        }

    lint = lint_workspace(root, workspace, entries)
    report = build_workspace_report(root, workspace, entries)
    lint_by_id = {
        item.get("id"): item
        for item in lint.get("problems") or []
        if isinstance(item, dict)
    }
    report_by_id = {
        item.get("id"): item
        for item in report.get("problems") or []
        if isinstance(item, dict)
    }
    workspace_hash = compute_workspace_hash(root, workspace)
    collection_hash = compute_collection_hash(root, workspace)
    builder = None
    builder_error = None
    try:
        builder = compute_builder_fingerprint(root, workspace)
    except ProbHubError as exc:
        builder_error = {"code": exc.code or "builder_fingerprint_failed", "error": str(exc)}

    problems = []
    for entry in entries:
        problem_dir, config = load_problem(root, entry)
        status = problem_status(
            problem_dir,
            config,
            root,
            workspace,
            workspace_hash=workspace_hash,
            collection_hash=collection_hash,
            builder_fingerprint=builder,
            builder_fingerprint_error=builder_error,
        )
        problems.append(_problem_summary(
            root,
            workspace,
            entry,
            lint_by_id.get(entry["id"], {}),
            report_by_id.get(entry["id"], {}),
            status,
        ))
    return {
        "success": True,
        "schema_version": HEALTH_SCHEMA_VERSION,
        "workspace": report.get("workspace") or {},
        "summary": report.get("summary") or {},
        "problems": problems,
        "generation": _generation_summary(root),
        "diagnostics": _diagnostics(report.get("diagnostics")),
    }
