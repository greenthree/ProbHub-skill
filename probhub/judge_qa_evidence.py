"""Bounded Judge QA evidence reading and validation."""

import json
import platform
import stat
import unicodedata
from pathlib import Path, PurePosixPath

from . import __version__
from .calibration import SANDBOX_CACHE_SCHEMA_VERSION


JUDGE_QA_EVIDENCE_SCHEMA_VERSION = 1
JUDGE_QA_POLICY_VERSION = 1
JUDGE_QA_EVIDENCE_FILENAME = "judge-qa-evidence-v1.json"
JUDGE_QA_EVIDENCE_LOCK_FILENAME = "judge-qa-evidence.lock"
MAX_JUDGE_QA_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_JUDGE_QA_EVIDENCE_MESSAGE_BYTES = 4096
MAX_JUDGE_QA_EVIDENCE_NODES = 20000

_FORBIDDEN_CONTENT_KEYS = {
    "entries",
    "feedback_text",
    "stderr",
    "stderr_text",
    "stdout",
    "stdout_text",
    "transcript_entries",
    "message",
    "message_text",
}
_ACTUAL_KEYS = (
    "status",
    "timeout_kind",
    "termination_reason",
    "actor",
    "execution_status",
    "failure_kind",
)


def evidence_path(problem_dir):
    return Path(problem_dir) / ".probhub" / JUDGE_QA_EVIDENCE_FILENAME


def _fold(value):
    return unicodedata.normalize("NFC", str(value)).casefold()


def _normalise_path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return PurePosixPath(value.strip().replace("\\", "/")).as_posix()


def _bounded_detail(value):
    payload = str(value or "").strip().encode("utf-8", errors="replace")
    if len(payload) <= MAX_JUDGE_QA_EVIDENCE_MESSAGE_BYTES:
        return payload.decode("utf-8", errors="replace")
    return (
        payload[:MAX_JUDGE_QA_EVIDENCE_MESSAGE_BYTES].decode(
            "utf-8", errors="replace"
        )
        + "..."
    )


def _diagnostic(code, message, **payload):
    return {
        "code": code,
        "severity": "warning",
        "message": message,
        **payload,
    }


def _base_status(inspection, state, diagnostics=None, **payload):
    diagnostics = list(diagnostics or [])
    robustness = inspection.get("robustness") or {}
    declared_probes = list(robustness.get("probes") or [])
    return {
        "state": state,
        "configured": bool(inspection.get("configured")),
        "applicable": bool(inspection.get("applicable")),
        "judge_type": inspection.get("judge_type"),
        "target_guarantee": False,
        "declared_cases": len(inspection.get("cases") or []),
        "evidence_cases": 0,
        "matched_cases": 0,
        "declared_probes": len(declared_probes),
        "evidence_probes": 0,
        "manual_review_probes": 0,
        "cases": [],
        "probes": [],
        "diagnostics": diagnostics,
        "warnings": [
            f"[{item['code']}] {item['message']}" for item in diagnostics
        ],
        **payload,
    }


def _state_diagnostic(state, reason, detail=None):
    if state == "missing":
        return _diagnostic(
            "judge_qa_evidence_missing",
            "Judge QA evidence is missing; run probhub judge-qa",
            reason=reason,
        )
    if state == "stale":
        return _diagnostic(
            "judge_qa_evidence_stale",
            "Judge QA evidence is stale; rerun probhub judge-qa",
            reason=reason,
        )
    return _diagnostic(
        "judge_qa_evidence_invalid",
        "Judge QA evidence is invalid; rerun probhub judge-qa",
        reason=reason,
        **({"detail": _bounded_detail(detail)} if detail else {}),
    )


def _read_evidence(problem_dir):
    path = evidence_path(problem_dir)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, "missing", None
    except OSError as exc:
        return None, "unreadable", exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(before.st_mode) or (
        reparse and getattr(before, "st_file_attributes", 0) & reparse
    ):
        return None, "unsafe_path", "evidence path is a link or reparse point"
    if not stat.S_ISREG(before.st_mode):
        return None, "unsafe_path", "evidence path is not a regular file"
    if before.st_size > MAX_JUDGE_QA_EVIDENCE_BYTES:
        return None, "too_large", (
            f"evidence is {before.st_size} bytes; limit is "
            f"{MAX_JUDGE_QA_EVIDENCE_BYTES}"
        )
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_JUDGE_QA_EVIDENCE_BYTES + 1)
        after = path.lstat()
    except OSError as exc:
        return None, "unreadable", exc
    if len(payload) > MAX_JUDGE_QA_EVIDENCE_BYTES:
        return None, "too_large", "evidence grew beyond the read limit"
    if (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_size,
    ):
        return None, "unreadable", "evidence changed while being read"
    try:
        decoded = payload.decode("utf-8")
        evidence = json.loads(decoded)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        return None, "invalid_json", exc
    if not isinstance(evidence, dict):
        return None, "invalid_json", "evidence root must be a JSON object"
    return evidence, None, None


def _payload_structure_error(evidence):
    stack = [evidence]
    visited = 0
    while stack:
        value = stack.pop()
        visited += 1
        if visited > MAX_JUDGE_QA_EVIDENCE_NODES:
            return "evidence structure exceeds the fixed node limit"
        if isinstance(value, dict):
            forbidden = sorted(set(value).intersection(_FORBIDDEN_CONTENT_KEYS))
            if forbidden:
                return "evidence contains forbidden stream content: " + ", ".join(
                    forbidden
                )
            if "feedback" in value and (
                isinstance(value["feedback"], bool)
                or not isinstance(value["feedback"], (int, float))
            ):
                return "evidence contains feedback content instead of a byte count"
            transcript = value.get("transcript")
            if isinstance(transcript, dict) and not set(transcript).issubset(
                {"bytes", "limit", "truncated"}
            ):
                return "evidence transcript contains unsupported fields"
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def _index_records(records, label):
    if not isinstance(records, list):
        return None, f"{label} must be a list"
    indexed = {}
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None, f"{label} contains an invalid record"
        folded = _fold(item["id"])
        if folded in indexed:
            return None, f"{label} contains duplicate ID {item['id']}"
        indexed[folded] = item
    return indexed, None


def _diagnostic_error(item, label):
    diagnostic = item.get("diagnostic")
    if not isinstance(diagnostic, dict):
        return f"{label} diagnostic must be a mapping"
    if set(diagnostic) != {"present", "bytes", "truncated"}:
        return f"{label} diagnostic contains unsupported fields"
    if not isinstance(diagnostic.get("present"), bool):
        return f"{label} diagnostic.present must be boolean"
    count = diagnostic.get("bytes")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return f"{label} diagnostic.bytes must be a non-negative integer"
    if count > MAX_JUDGE_QA_EVIDENCE_MESSAGE_BYTES:
        return f"{label} diagnostic.bytes exceeds the fixed byte limit"
    if diagnostic["present"] != (count > 0):
        return f"{label} diagnostic presence does not match byte count"
    if not isinstance(diagnostic.get("truncated"), bool):
        return f"{label} diagnostic.truncated must be boolean"
    if diagnostic["truncated"] and count != MAX_JUDGE_QA_EVIDENCE_MESSAGE_BYTES:
        return f"{label} truncated diagnostic must use the fixed byte limit"
    return None


def _expected_compilers(config, inspection):
    judge = config.get("judge") if isinstance(config.get("judge"), dict) else {}
    expected = []
    for role in ("validator", inspection.get("judge_type") == "custom" and "checker" or "interactor"):
        source = _normalise_path(judge.get(role))
        if source:
            expected.append((role, source))
    for case in inspection.get("cases") or []:
        contestant = case.get("contestant") or {}
        source = _normalise_path(contestant.get("source"))
        if source:
            expected.append(("contestant", source))
    return set(expected)


def _structure_error(evidence, config, inspection):
    if (error := _payload_structure_error(evidence)) is not None:
        return error
    measurement = evidence.get("measurement")
    if not isinstance(measurement, dict) or measurement.get("target_guarantee") is not False:
        return "measurement.target_guarantee must be false"
    cleanup = evidence.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("ok") is not True or cleanup.get("snapshot_removed") is not True:
        return "cleanup must confirm successful snapshot removal"

    declared_cases, error = _index_records(inspection.get("cases") or [], "declared cases")
    if error:
        return error
    observed_cases, error = _index_records(evidence.get("cases"), "evidence cases")
    if error:
        return error
    if set(observed_cases) != set(declared_cases):
        return "evidence case IDs do not match current Judge QA configuration"
    for folded, declared in declared_cases.items():
        observed = observed_cases[folded]
        if observed.get("purpose") != declared.get("purpose"):
            return f"case purpose changed for {declared.get('id')}"
        if observed.get("expected") != declared.get("expected"):
            return f"case expectation changed for {declared.get('id')}"
        if observed.get("matched") is not True or observed.get("infrastructure_failed") is not False:
            return f"case did not pass cleanly: {declared.get('id')}"
        actual = observed.get("actual")
        if not isinstance(actual, dict):
            return f"case actual result is missing: {declared.get('id')}"
        if any(actual.get(key) != value for key, value in declared.get("expected", {}).items()):
            return f"case actual result does not satisfy expectation: {declared.get('id')}"
        if (error := _diagnostic_error(observed, f"case {declared.get('id')}")) is not None:
            return error

    declared_probe_ids = list((inspection.get("robustness") or {}).get("probes") or [])
    declared_probes = {_fold(item): item for item in declared_probe_ids}
    observed_probes, error = _index_records(evidence.get("probes"), "evidence probes")
    if error:
        return error
    if set(observed_probes) != set(declared_probes):
        return "evidence probe IDs do not match current Judge QA configuration"
    for folded, declared_id in declared_probes.items():
        observed = observed_probes[folded]
        if observed.get("matched") is not True or observed.get("infrastructure_failed") is not False:
            return f"robustness probe did not complete cleanly: {declared_id}"
        actual = observed.get("actual")
        if not isinstance(actual, dict) or actual.get("status") not in {"AC", "WA"}:
            return f"robustness probe actual result is invalid: {declared_id}"
        expected_manual = actual.get("status") == "AC"
        if observed.get("manual_review_required") is not expected_manual:
            return f"robustness probe review flag is invalid: {declared_id}"
        if (error := _diagnostic_error(observed, f"probe {declared_id}")) is not None:
            return error

    expected_inputs = {
        _fold(case.get("input")): case.get("input")
        for case in inspection.get("cases") or []
        if case.get("input")
    }
    validators = evidence.get("validators")
    if not isinstance(validators, list):
        return "validator summaries must be a list"
    observed_inputs = {}
    for item in validators:
        if not isinstance(item, dict) or not isinstance(item.get("input"), str):
            return "validator summary is invalid"
        folded = _fold(item["input"])
        if folded in observed_inputs:
            return f"validator input is duplicated: {item['input']}"
        if item.get("ok") is not True:
            return f"validator did not accept Judge QA input: {item['input']}"
        if (error := _diagnostic_error(item, f"validator {item['input']}")) is not None:
            return error
        observed_inputs[folded] = item["input"]
    if set(observed_inputs) != set(expected_inputs):
        return "validator summaries do not cover current Judge QA inputs"

    compilers = evidence.get("compilers")
    if not isinstance(compilers, list):
        return "compiler summaries must be a list"
    expected_compilers = _expected_compilers(config, inspection)
    observed_compilers = set()
    for item in compilers:
        if not isinstance(item, dict):
            return "compiler summary is invalid"
        role = item.get("role")
        source = _normalise_path(item.get("source"))
        identity = (role, source)
        if not isinstance(role, str) or source is None or identity in observed_compilers:
            return "compiler identity is invalid or duplicated"
        expected_kind = "python" if PurePosixPath(source).suffix.casefold() == ".py" else "cpp17"
        if item.get("kind") != expected_kind:
            return f"compiler kind is invalid for {source}"
        if expected_kind == "cpp17" and not isinstance(item.get("compiler_identity"), str):
            return f"compiler identity is missing for {source}"
        observed_compilers.add(identity)
    if observed_compilers != expected_compilers:
        return "compiler summaries do not match current Judge QA programs"
    return None


def _project_current(evidence, inspection, diagnostics):
    cases = [
        {
            "id": item.get("id"),
            "purpose": item.get("purpose"),
            "expected": dict(item.get("expected") or {}),
            "actual": {
                key: (item.get("actual") or {}).get(key)
                for key in _ACTUAL_KEYS
                if key in (item.get("actual") or {})
            },
            "matched": True,
            "diagnostic": dict(item.get("diagnostic") or {}),
        }
        for item in evidence.get("cases") or []
    ]
    probes = [
        {
            "id": item.get("id"),
            "purpose": item.get("purpose"),
            "expected": dict(item.get("expected") or {}),
            "actual": {
                key: (item.get("actual") or {}).get(key)
                for key in _ACTUAL_KEYS
                if key in (item.get("actual") or {})
            },
            "matched": True,
            "manual_review_required": bool(item.get("manual_review_required")),
            "diagnostic": dict(item.get("diagnostic") or {}),
        }
        for item in evidence.get("probes") or []
    ]
    manual = sorted(
        str(item.get("id")) for item in probes if item["manual_review_required"]
    )
    if manual:
        diagnostics.append(_diagnostic(
            "judge_qa_probe_manual_review_required",
            "one or more automatic Judge QA probes returned AC and require author review",
            probe_ids=manual,
        ))
    measurement = evidence.get("measurement") or {}
    return _base_status(
        inspection,
        "current",
        diagnostics,
        published_at=evidence.get("published_at"),
        probhub_version=evidence.get("probhub_version"),
        platform={
            "system": measurement.get("platform"),
            "machine": measurement.get("machine"),
        },
        evidence_cases=len(cases),
        matched_cases=len(cases),
        evidence_probes=len(probes),
        manual_review_probes=len(manual),
        cases=cases,
        probes=probes,
    )


def validate_judge_qa_evidence_document(
    evidence,
    config,
    inspection,
    source_hash,
    data_hash,
):
    """Validate an already-decoded evidence document and return its bounded view."""

    if not inspection.get("configured"):
        return _base_status(inspection, "not-configured")
    if not inspection.get("ok"):
        return _base_status(inspection, "invalid")
    if not isinstance(evidence, dict):
        diagnostic = _state_diagnostic("invalid", "invalid_json")
        return _base_status(inspection, "invalid", [diagnostic], reason="invalid_json")

    stale_checks = (
        (evidence.get("schema_version") != JUDGE_QA_EVIDENCE_SCHEMA_VERSION, "schema"),
        (evidence.get("policy_version") != JUDGE_QA_POLICY_VERSION, "policy"),
        (evidence.get("sandbox_schema_version") != SANDBOX_CACHE_SCHEMA_VERSION, "sandbox"),
        (evidence.get("probhub_version") != __version__, "core"),
        (
            evidence.get("source_hash") != source_hash
            or evidence.get("data_hash") != data_hash
            or evidence.get("fixture_hash") != inspection.get("fixture_hash"),
            "inputs",
        ),
    )
    for changed, reason in stale_checks:
        if changed:
            diagnostic = _state_diagnostic("stale", reason)
            return _base_status(
                inspection,
                "stale",
                [diagnostic],
                reason=reason,
                published_at=evidence.get("published_at"),
            )
    measurement = evidence.get("measurement")
    if not isinstance(measurement, dict):
        structure_error = "measurement summary is missing"
    elif (
        measurement.get("platform") != platform.system()
        or (
            measurement.get("machine")
            and measurement.get("machine") != platform.machine()
        )
    ):
        diagnostic = _state_diagnostic("stale", "platform")
        return _base_status(
            inspection,
            "stale",
            [diagnostic],
            reason="platform",
            published_at=evidence.get("published_at"),
        )
    else:
        structure_error = _structure_error(evidence, config, inspection)
    if structure_error is not None:
        diagnostic = _state_diagnostic("invalid", "structure", structure_error)
        return _base_status(
            inspection,
            "invalid",
            [diagnostic],
            reason="structure",
            published_at=evidence.get("published_at"),
        )
    return _project_current(evidence, inspection, [])


def evaluate_judge_qa_evidence(
    problem_dir,
    config,
    inspection,
    source_hash,
    data_hash,
):
    """Read and evaluate Judge QA evidence without running external tools."""

    if not inspection.get("configured"):
        return _base_status(inspection, "not-configured")
    if not inspection.get("ok"):
        return _base_status(inspection, "invalid")
    evidence, reason, detail = _read_evidence(problem_dir)
    if reason is not None:
        state = "missing" if reason == "missing" else "invalid"
        diagnostic = _state_diagnostic(state, reason, detail)
        return _base_status(
            inspection,
            state,
            [diagnostic],
            reason=reason,
        )
    return validate_judge_qa_evidence_document(
        evidence,
        config,
        inspection,
        source_hash,
        data_hash,
    )
