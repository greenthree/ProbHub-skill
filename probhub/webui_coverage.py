"""Read-only data coverage and delivery projection for the WebUI.

This module deliberately composes the existing report and health readers.  It
does not inspect secret bytes, Judge QA transcripts, build manifests, PDF/ZIP
paths, or any other raw artifact content.  The returned shape is intended for
the coverage tab and is therefore stable, small, and metadata-only.
"""

import hashlib
import json
from pathlib import Path

from .errors import ProbHubError
from .linting import compute_source_hash
from .reporting import build_workspace_report
from .transactions import ensure_no_pending_transactions
from .webui_health import build_health_summary
from .workspace import load_problem, problem_entries, select_entries


COVERAGE_SCHEMA_VERSION = 1


def _state(value, default="unknown"):
    return value if isinstance(value, str) and value else default


def _diagnostic(value):
    if not isinstance(value, dict):
        return None
    result = {
        key: value[key]
        for key in ("code", "severity", "message", "problem_id")
        if key in value
    }
    remediation = value.get("remediation")
    if isinstance(remediation, dict):
        result["remediation"] = {
            key: remediation[key]
            for key in (
                "action_code",
                "command",
                "description",
                "documentation",
                "manual_review_required",
            )
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
            identity = repr(sorted(item.items(), key=lambda pair: pair[0]))
            if identity not in seen:
                seen.add(identity)
                result.append(item)
    return result


def _recipe_projection(recipes):
    result = []
    for item in recipes or ():
        if not isinstance(item, dict):
            continue
        args = item.get("args")
        if not isinstance(args, list):
            args = []
        # Recipe values are parsed and bounded by the schema reader.  Keep a
        # defensive cap here so a malformed report cannot create a huge API
        # response.
        identity = {
            "generator": item.get("generator"),
            "args": [str(value) for value in args],
            "manual": bool(item.get("manual")),
            "groups": list(item.get("groups") or []),
        }
        result.append({
            "recipe_hash": hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16],
            "manual": bool(item.get("manual")),
            "generator": item.get("generator"),
            "present": bool(item.get("present")),
            "groups": list(item.get("groups") or []),
        })
    return result


def _coverage_projection(report_item):
    tests = report_item.get("tests") or {}
    secret = tests.get("secret") if isinstance(tests, dict) else {}
    if not isinstance(secret, dict):
        secret = {}
    return {
        "groups": [
            {
                key: group.get(key)
                for key in (
                    "name",
                    "role",
                    "patterns",
                    "targets",
                    "sample_cases",
                    "secret_cases",
                    "total_cases",
                    "secret_ratio",
                )
                if key in group
            }
            for group in report_item.get("groups") or []
            if isinstance(group, dict)
        ],
        "ungrouped_secret_cases": int(report_item.get("ungrouped_secret_cases") or 0),
        "recipes": _recipe_projection((report_item.get("recipes") or {}).get("profiles")),
        # Counts and byte sizes are safe metadata; no case names or bytes are
        # returned for the secret suite.
        "secret_summary": {
            key: secret.get(key, 0)
            for key in ("cases", "input_bytes", "answer_bytes")
        },
    }


def _qa_probe_projection(probes):
    """Keep only Judge QA fields sanctioned by the evidence projection."""
    result = []
    for item in probes or ():
        if not isinstance(item, dict):
            continue
        expected = item.get("expected")
        actual = item.get("actual")
        result.append({
            "id": item.get("id"),
            "purpose": item.get("purpose"),
            "expected": {
                key: expected[key]
                for key in ("status", "timeout_kind", "termination_reason")
                if isinstance(expected, dict) and key in expected
            },
            "actual": {
                key: actual[key]
                for key in ("status", "timeout_kind", "termination_reason")
                if isinstance(actual, dict) and key in actual
            },
            "matched": bool(item.get("matched")),
            "manual_review_required": bool(item.get("manual_review_required")),
        })
    return result


def _wrong_projection(report_item, health_item):
    matrix = report_item.get("kill_matrix") or {}
    rows = matrix.get("rows") or []
    evidence_by_program = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get("cells") or {}
        matched = []
        for cell in cells.values():
            if not isinstance(cell, dict):
                continue
            matched.extend(str(case) for case in cell.get("matched_cases") or [])
        matched = len(set(matched))
        overall = _state(row.get("overall"), "unknown")
        # The actual status is deliberately bounded to a verdict token.  A
        # passed expectation means the expected non-AC outcome was observed;
        # details remain in the Core report and are not exposed here.
        expected_statuses = list(row.get("expected_statuses") or [])
        actual_status = row.get("actual_status")
        if actual_status not in {"AC", "WA", "TLE", "MLE", "OLE", "RE", "FAIL"}:
            actual_status = (
                expected_statuses[0]
                if overall == "passed" and expected_statuses
                else ("AC" if overall == "failed" else None)
            )
        expected_groups = [
            value for value in (row.get("scope") or []) if value != "__all__"
        ]
        evidence_by_program[str(row.get("program"))] = {
            "program": row.get("program"),
            "expected_statuses": expected_statuses,
            "expected_groups": expected_groups,
            "actual_status": actual_status,
            # Secret case identifiers are intentionally reduced to a count;
            # the cockpit must not disclose names or paths.
            "matched_case_count": matched,
            "state": overall,
        }
    return list(evidence_by_program.values())


def _requires_for_problem(report_item, health_item):
    requirements = []
    artifacts = health_item.get("artifacts") or {}
    stale = list(artifacts.get("stale_fields") or [])
    checkpoint = health_item.get("checkpoint") or {}
    qa = (health_item.get("checks") or {}).get("judge_qa") or {}
    evidence = (health_item.get("evidence") or {}).get("calibration") or {}
    if stale:
        requirements.append("build")
    if any(value in {"source_hash", "data_hash", "workspace_hash", "collection_hash"} for value in stale):
        requirements.extend(("judge", "checkpoint", "seal"))
    if checkpoint.get("state") in {"missing", "stale", "invalid"}:
        requirements.extend(("checkpoint", "seal"))
    if qa.get("state") in {"missing", "stale", "invalid", "not-configured"} and qa.get("applicable"):
        requirements.append("judge-qa")
    if evidence.get("state") in {"missing", "stale", "invalid"}:
        requirements.append("judge")
    if any(artifacts.get(key) in {"missing", "stale", "invalid"} for key in ("pdf", "zip", "manifest")):
        requirements.append("build")
    # Preserve the first occurrence order for a predictable UI checklist.
    return list(dict.fromkeys(requirements))


def _problem_projection(report_item, health_item):
    checkpoint = health_item.get("checkpoint") or {}
    artifacts = health_item.get("artifacts") or {}
    checks = health_item.get("checks") or {}
    qa = checks.get("judge_qa") or {}
    # The report's Judge QA profile is already a bounded projection of
    # validated evidence.  Prefer its probe summaries (status tokens only)
    # while retaining health's current/stale state and counters.
    report_qa = report_item.get("judge_qa") or {}
    evidence = (health_item.get("evidence") or {}).get("calibration") or {}
    stale_fields = list(artifacts.get("stale_fields") or [])
    requires = _requires_for_problem(report_item, health_item)
    source_revision = checkpoint.get("revision_id") or health_item.get("source_hash")
    delivery = {
        "sealed": checkpoint.get("state") == "sealed",
        "qa": qa.get("state") if qa.get("configured") or qa.get("applicable") else "not-configured",
        "evidence": evidence.get("state", "missing"),
        "pdf": artifacts.get("pdf", "missing"),
        "zip": artifacts.get("zip", "missing"),
        "manifest": artifacts.get("manifest", "missing"),
        "requires": requires,
    }
    if stale_fields:
        impact_state = "stale"
    elif requires:
        impact_state = "needs-action"
    else:
        impact_state = "current"
    return {
        "id": report_item.get("id"),
        "name": report_item.get("name") or report_item.get("id"),
        "coverage": _coverage_projection(report_item),
        "wrong": _wrong_projection(report_item, health_item),
        "judge_qa": {
            **{
                key: qa.get(key)
                for key in (
                    "state",
                    "configured",
                    "declared_cases",
                    "evidence_cases",
                    "manual_review_probes",
                )
                if key in qa
            },
            "matched_case_count": qa.get("matched_cases"),
            "probes": _qa_probe_projection(report_qa.get("probes") or qa.get("probes")),
        },
        "impact": {
            "state": impact_state,
            "stale_fields": stale_fields,
            "requires": requires,
            "source_revision": source_revision,
        },
        "delivery": delivery,
    }


def _delivery_state(items, generation):
    if not items:
        return "empty"
    if any(item.get("requires") for item in items):
        return "needs-action"
    required = ("sealed", "qa", "evidence", "pdf", "zip", "manifest")
    if all(
        item.get("sealed") is True
        and item.get("qa") in {"current", "passed", "not-configured"}
        and item.get("evidence") in {"current", "passed"}
        and all(item.get(field) in {"current", "passed"} for field in required[3:])
        for item in items
    ) and generation.get("complete") is not False:
        return "ready"
    return "incomplete"


def build_coverage_summary(root, workspace, selected_ids=None):
    """Return the bounded coverage projection for a Schema v1 workspace."""
    root = Path(root).resolve()
    # Match health/report fail-closed behavior even for an empty workspace;
    # pending recovery journals must never be hidden by an empty result.
    ensure_no_pending_transactions(root, workspace)
    all_entries = problem_entries(workspace)
    entries = select_entries(workspace, selected_ids) if selected_ids else all_entries
    if not entries:
        contest = workspace.get("contest") or {}
        return {
            "success": True,
            "schema_version": COVERAGE_SCHEMA_VERSION,
            "source_revision": None,
            "workspace": {
                "title": contest.get("title") or root.name,
                "subtitle": contest.get("subtitle"),
                "problem_count": 0,
                "selected_count": 0,
            },
            "problems": [],
            "delivery": {"state": "empty", "items": [], "remediations": []},
            "diagnostics": [],
        }

    report = build_workspace_report(root, workspace, entries)
    health = build_health_summary(root, workspace, [entry["id"] for entry in entries])
    report_by_id = {item.get("id"): item for item in report.get("problems") or []}
    health_by_id = {item.get("id"): item for item in health.get("problems") or []}
    problems = []
    remediations = []
    for entry in entries:
        # A sealed checkpoint revision is preferred.  For draft/unsealed
        # problems expose the current source hash as a stable revision token;
        # computing it uses only canonical source metadata and never secret
        # data bytes.
        if entry["id"] in health_by_id and not (
            (health_by_id[entry["id"]].get("checkpoint") or {}).get("revision_id")
        ):
            try:
                problem_dir, config = load_problem(root, entry)
                health_by_id[entry["id"]]["source_hash"] = compute_source_hash(problem_dir, config)
            except (OSError, ProbHubError, KeyError, TypeError, ValueError):
                pass
        item = _problem_projection(
            report_by_id.get(entry["id"], {}),
            health_by_id.get(entry["id"], {}),
        )
        problems.append(item)
        remediations.extend(
            value
            for value in (health_by_id.get(entry["id"], {}).get("remediations") or [])
            if isinstance(value, dict)
        )
    source_revision = next(
        (item["impact"].get("source_revision") for item in problems if item["impact"].get("source_revision")),
        None,
    )
    delivery_items = [
        {"id": item["id"], **item["delivery"]}
        for item in problems
    ]
    generation = health.get("generation") or {}
    return {
        "success": True,
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "source_revision": source_revision,
        "workspace": report.get("workspace") or {},
        "problems": problems,
        "delivery": {
            "state": _delivery_state(delivery_items, generation),
            "items": delivery_items,
            "remediations": remediations[:32],
        },
        "diagnostics": _diagnostics(
            report.get("diagnostics"),
            health.get("diagnostics"),
            *(item.get("diagnostics") for item in health.get("problems") or []),
        ),
    }


__all__ = ["COVERAGE_SCHEMA_VERSION", "build_coverage_summary"]
