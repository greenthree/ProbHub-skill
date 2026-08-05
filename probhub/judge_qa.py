import hashlib
import re
import unicodedata
from pathlib import Path, PurePosixPath

from .problem_paths import ProblemPathError, resolve_problem_regular_file


JUDGE_QA_SCHEMA_VERSION = 1
MAX_JUDGE_QA_CASES = 128
MAX_JUDGE_QA_FILES = 256
MAX_JUDGE_QA_FILE_BYTES = 16 * 1024 * 1024
MAX_JUDGE_QA_TOTAL_BYTES = 64 * 1024 * 1024
MAX_JUDGE_QA_ROBUSTNESS_PROBES = 16
MAX_JUDGE_QA_DIAGNOSTICS = 128

_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TERMINATION_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CHECKER_STATUSES = {"AC", "WA"}
_INTERACTOR_STATUSES = {"AC", "WA", "RE", "TLE", "MLE", "OLE"}
_INTERACTOR_BEHAVIORS = {"early-eof", "idle", "output-flood"}
_ROBUSTNESS_PROBES = {"empty", "truncated", "extra-token", "oversized"}
_PATH_REASON_CODES = {
    "invalid": "judge_qa_fixture_path_invalid",
    "outside": "judge_qa_fixture_path_outside",
    "link": "judge_qa_fixture_path_link",
    "missing": "judge_qa_fixture_path_missing",
    "non_regular": "judge_qa_fixture_path_non_regular",
}


def _normalise_judge_type(config):
    judge = config.get("judge")
    if not isinstance(judge, dict):
        return "standard"
    value = str(judge.get("type", "standard")).strip().lower()
    return "custom" if value == "checker" else value


def _fold_id(value):
    return unicodedata.normalize("NFC", value).casefold()


def _normalise_relative_path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return PurePosixPath(value.strip().replace("\\", "/")).as_posix()


def _under_prefix(relative, prefix):
    path_parts = PurePosixPath(relative).parts
    prefix_parts = PurePosixPath(prefix).parts
    return path_parts[:len(prefix_parts)] == prefix_parts and len(path_parts) > len(prefix_parts)


def judge_fixture_tree_paths(problem_dir):
    """Return regular non-link files stored under judge-fixtures/."""

    return _judge_qa_tree_paths(problem_dir, "judge-fixtures")


def _judge_qa_tree_paths(problem_dir, relative_root):
    root = Path(problem_dir).resolve() / relative_root
    if not root.is_dir() or root.is_symlink():
        return []
    paths = []
    for candidate in root.rglob("*"):
        try:
            relative = candidate.relative_to(Path(problem_dir).resolve()).as_posix()
            resolved = resolve_problem_regular_file(problem_dir, relative)
        except (ProblemPathError, ValueError):
            continue
        paths.append(resolved)
    return paths


def _hash_fixture_files(files):
    digest = hashlib.sha256()
    digest.update(b"probhub-judge-qa-fixtures-v1\0")
    for item in sorted(files, key=lambda value: value["path"]):
        relative = item["path"].encode("utf-8")
        expected_size = item["size"]
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(expected_size.to_bytes(8, "big"))
        observed_size = 0
        with item["absolute"].open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                observed_size += len(chunk)
                digest.update(chunk)
        after = item["absolute"].stat()
        if (
            observed_size != expected_size
            or after.st_size != expected_size
            or (
                item.get("stat")
                and (
                    after.st_dev,
                    after.st_ino,
                    after.st_mtime_ns,
                    after.st_size,
                )
                != item["stat"]
            )
        ):
            raise OSError(f"fixture changed while hashing: {item['path']}")
    return digest.hexdigest()


def inspect_judge_qa(problem_dir, config):
    """Parse Judge QA Schema v1 without executing any fixture."""

    problem_dir = Path(problem_dir).resolve()
    judge = config.get("judge")
    if not isinstance(judge, dict) or "qa" not in judge:
        return {
            "configured": False,
            "applicable": _normalise_judge_type(config) in {"custom", "interactive"},
            "ok": True,
            "schema_version": None,
            "judge_type": _normalise_judge_type(config),
            "fixture_hash": None,
            "cases": [],
            "robustness": None,
            "files": [],
            "stats": {"cases": 0, "files": 0, "total_bytes": 0},
            "diagnostics": [],
        }

    judge_type = _normalise_judge_type(config)
    diagnostics = []
    resolved_files = {}
    parsed_cases = []

    def add(code, message, **details):
        if len(diagnostics) >= MAX_JUDGE_QA_DIAGNOSTICS:
            return
        if len(diagnostics) == MAX_JUDGE_QA_DIAGNOSTICS - 1:
            diagnostics.append({
                "code": "judge_qa_diagnostics_truncated",
                "severity": "error",
                "message": (
                    "Judge QA diagnostics reached the fixed limit of "
                    f"{MAX_JUDGE_QA_DIAGNOSTICS}"
                ),
                "limit": MAX_JUDGE_QA_DIAGNOSTICS,
            })
            return
        diagnostics.append({
            "code": code,
            "severity": "error",
            "message": message,
            **details,
        })

    def unknown_fields(value, allowed, field, *, case_id=None):
        if not isinstance(value, dict):
            return
        for key in sorted(set(value) - set(allowed), key=str):
            add(
                "judge_qa_unknown_field",
                f"{field} contains unsupported field: {key}",
                field=f"{field}.{key}",
                **({"case_id": case_id} if case_id else {}),
            )

    def register_file(field, path, *, case_id=None):
        try:
            canonical_path = path.resolve(strict=True)
            canonical_relative = canonical_path.relative_to(problem_dir).as_posix()
            stat_before = path.stat()
        except (OSError, RuntimeError, ValueError):
            add(
                "judge_qa_fixture_changed",
                f"fixture path changed or became unreadable while resolving: {path}",
                field=field,
                path=str(path),
                **({"case_id": case_id} if case_id else {}),
            )
            return None
        folded = unicodedata.normalize("NFC", canonical_relative).casefold()
        previous = resolved_files.get(folded)
        if previous and previous["path"] != canonical_relative:
            add(
                "judge_qa_fixture_path_collision",
                "fixture paths collide case-insensitively on Windows: "
                f"{previous['path']} and {canonical_relative}",
                field=field,
                paths=[previous["path"], canonical_relative],
                **({"case_id": case_id} if case_id else {}),
            )
            return None
        if previous:
            return canonical_relative
        size = stat_before.st_size
        resolved_files[folded] = {
            "path": canonical_relative,
            "absolute": path,
            "size": size,
            "stat": (
                stat_before.st_dev,
                stat_before.st_ino,
                stat_before.st_mtime_ns,
                stat_before.st_size,
            ),
        }
        if size > MAX_JUDGE_QA_FILE_BYTES:
            add(
                "judge_qa_fixture_file_too_large",
                f"fixture file exceeds {MAX_JUDGE_QA_FILE_BYTES} bytes: {canonical_relative}",
                field=field,
                path=canonical_relative,
                size=size,
                limit=MAX_JUDGE_QA_FILE_BYTES,
                **({"case_id": case_id} if case_id else {}),
            )
        return canonical_relative

    def resolve_file(field, value, *, required_prefix=None, case_id=None):
        relative = _normalise_relative_path(value)
        try:
            path = resolve_problem_regular_file(problem_dir, value)
        except ProblemPathError as exc:
            code = _PATH_REASON_CODES.get(exc.reason, "judge_qa_fixture_path_invalid")
            add(
                code,
                f"{field} must be a problem-local regular non-link file: {value!r}",
                field=field,
                path=value,
                reason=exc.reason,
                **({"case_id": case_id} if case_id else {}),
            )
            return None
        if required_prefix and (relative is None or not _under_prefix(relative, required_prefix)):
            add(
                "judge_qa_fixture_path_scope",
                f"{field} must stay under {required_prefix}/: {value!r}",
                field=field,
                path=value,
                required_prefix=required_prefix,
                **({"case_id": case_id} if case_id else {}),
            )
            return None

        return register_file(field, path, case_id=case_id)

    raw_qa = judge.get("qa")
    if not isinstance(raw_qa, dict):
        add("judge_qa_schema_invalid", "judge.qa must be a mapping", field="judge.qa")
        raw_qa = {}
    unknown_fields(raw_qa, {"schema_version", "robustness", "cases"}, "judge.qa")
    schema_version = raw_qa.get("schema_version")
    if type(schema_version) is not int or schema_version != JUDGE_QA_SCHEMA_VERSION:
        add(
            "judge_qa_schema_version_unsupported",
            f"judge.qa.schema_version must be {JUDGE_QA_SCHEMA_VERSION}",
            field="judge.qa.schema_version",
            actual=schema_version,
        )
    if judge_type not in {"custom", "interactive"}:
        add(
            "judge_qa_judge_type_unsupported",
            "judge.qa is only valid for custom or interactive judging",
            field="judge.type",
            judge_type=judge_type,
        )

    for tree_name in ("judge-fixtures", "code/judge-qa"):
        for path in _judge_qa_tree_paths(problem_dir, tree_name):
            register_file("judge.qa.fixture_tree", path)

    raw_cases = raw_qa.get("cases")
    if not isinstance(raw_cases, list):
        add("judge_qa_cases_invalid", "judge.qa.cases must be a list", field="judge.qa.cases")
        raw_cases = []
    elif not raw_cases:
        add("judge_qa_cases_empty", "judge.qa.cases must not be empty", field="judge.qa.cases")
    if len(raw_cases) > MAX_JUDGE_QA_CASES:
        add(
            "judge_qa_case_limit_exceeded",
            f"judge.qa.cases exceeds the fixed limit of {MAX_JUDGE_QA_CASES}",
            field="judge.qa.cases",
            count=len(raw_cases),
            limit=MAX_JUDGE_QA_CASES,
        )

    seen_ids = {}
    expected_by_id = {}
    for index, raw_case in enumerate(raw_cases[:MAX_JUDGE_QA_CASES]):
        field = f"judge.qa.cases[{index}]"
        if not isinstance(raw_case, dict):
            add("judge_qa_case_invalid", f"{field} must be a mapping", field=field)
            continue
        allowed = {
            "id", "purpose", "case", "input", "jury_answer", "expected",
            "contestant_output" if judge_type == "custom" else "contestant",
        }
        unknown_fields(raw_case, allowed, field)

        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not _CASE_ID_PATTERN.fullmatch(case_id):
            add(
                "judge_qa_case_id_invalid",
                f"{field}.id must match {_CASE_ID_PATTERN.pattern}",
                field=f"{field}.id",
                actual=case_id,
            )
            case_id = None
        else:
            folded_id = _fold_id(case_id)
            if folded_id in seen_ids:
                add(
                    "judge_qa_case_id_duplicate",
                    f"Judge QA case IDs collide case-insensitively: "
                    f"{seen_ids[folded_id]} and {case_id}",
                    field=f"{field}.id",
                    case_id=case_id,
                    conflicts_with=seen_ids[folded_id],
                )
            else:
                seen_ids[folded_id] = case_id

        purpose = raw_case.get("purpose")
        if (
            not isinstance(purpose, str)
            or not purpose.strip()
            or len(purpose) > 256
            or any(ord(character) < 32 for character in purpose)
        ):
            add(
                "judge_qa_purpose_invalid",
                f"{field}.purpose is required and must be a non-empty string of at most 256 characters",
                field=f"{field}.purpose",
                **({"case_id": case_id} if case_id else {}),
            )

        parsed = {
            "id": case_id,
            "purpose": purpose.strip() if isinstance(purpose, str) else None,
        }
        formal_case = raw_case.get("case")
        explicit_input = raw_case.get("input")
        explicit_answer = raw_case.get("jury_answer")
        has_formal = formal_case is not None
        has_explicit = explicit_input is not None or explicit_answer is not None
        if has_formal and has_explicit:
            add(
                "judge_qa_case_reference_conflict",
                f"{field} cannot combine case with input/jury_answer",
                field=field,
                **({"case_id": case_id} if case_id else {}),
            )
        elif has_formal:
            reference = _normalise_relative_path(formal_case)
            parts = PurePosixPath(reference).parts if reference else ()
            if (
                len(parts) != 2
                or parts[0] not in {"sample", "secret"}
                or parts[1] in {"", ".", ".."}
                or parts[1].casefold().endswith((".in", ".ans"))
            ):
                add(
                    "judge_qa_case_reference_invalid",
                    f"{field}.case must be sample/<name> or secret/<name> without an extension",
                    field=f"{field}.case",
                    actual=formal_case,
                    **({"case_id": case_id} if case_id else {}),
                )
            else:
                data = config.get("data") if isinstance(config.get("data"), dict) else {}
                directory_key = "sample_dir" if parts[0] == "sample" else "secret_dir"
                default = "data/sample" if parts[0] == "sample" else "data/secret"
                base = data.get(directory_key, default)
                if not isinstance(base, str) or not base.strip():
                    add(
                        "judge_qa_case_reference_invalid",
                        f"data.{directory_key} must be a non-empty path string",
                        field=f"data.{directory_key}",
                        **({"case_id": case_id} if case_id else {}),
                    )
                else:
                    base = base.strip().replace("\\", "/").rstrip("/")
                    input_path = resolve_file(
                        f"{field}.case.input",
                        f"{base}/{parts[1]}.in",
                        case_id=case_id,
                    )
                    answer_path = resolve_file(
                        f"{field}.case.jury_answer",
                        f"{base}/{parts[1]}.ans",
                        case_id=case_id,
                    )
                    parsed.update({
                        "reference": reference,
                        "input": input_path,
                        "jury_answer": answer_path,
                    })
        elif explicit_input is None or explicit_answer is None:
            add(
                "judge_qa_case_reference_incomplete",
                f"{field} must declare case or both input and jury_answer",
                field=field,
                **({"case_id": case_id} if case_id else {}),
            )
        else:
            parsed["input"] = resolve_file(
                f"{field}.input",
                explicit_input,
                required_prefix="judge-fixtures",
                case_id=case_id,
            )
            parsed["jury_answer"] = resolve_file(
                f"{field}.jury_answer",
                explicit_answer,
                required_prefix="judge-fixtures",
                case_id=case_id,
            )

        expected = raw_case.get("expected")
        allowed_expected = {"status"} if judge_type == "custom" else {
            "status", "timeout_kind", "termination_reason",
        }
        if not isinstance(expected, dict):
            add(
                "judge_qa_expected_invalid",
                f"{field}.expected must be a mapping",
                field=f"{field}.expected",
                **({"case_id": case_id} if case_id else {}),
            )
            expected = {}
        unknown_fields(expected, allowed_expected, f"{field}.expected", case_id=case_id)
        status = expected.get("status")
        allowed_statuses = _CHECKER_STATUSES if judge_type == "custom" else _INTERACTOR_STATUSES
        if not isinstance(status, str) or status not in allowed_statuses:
            add(
                "judge_qa_expected_status_invalid",
                f"{field}.expected.status must be one of: {', '.join(sorted(allowed_statuses))}",
                field=f"{field}.expected.status",
                actual=status,
                **({"case_id": case_id} if case_id else {}),
            )
        timeout_kind = expected.get("timeout_kind")
        if timeout_kind is not None and (
            not isinstance(timeout_kind, str) or timeout_kind not in {"idle", "total"}
        ):
            add(
                "judge_qa_timeout_kind_invalid",
                f"{field}.expected.timeout_kind must be idle or total",
                field=f"{field}.expected.timeout_kind",
                actual=timeout_kind,
                **({"case_id": case_id} if case_id else {}),
            )
        if timeout_kind is not None and status != "TLE":
            add(
                "judge_qa_timeout_kind_without_tle",
                f"{field}.expected.timeout_kind requires status: TLE",
                field=f"{field}.expected.timeout_kind",
                **({"case_id": case_id} if case_id else {}),
            )
        termination_reason = expected.get("termination_reason")
        if termination_reason is not None and (
            not isinstance(termination_reason, str)
            or not _TERMINATION_REASON_PATTERN.fullmatch(termination_reason)
        ):
            add(
                "judge_qa_termination_reason_invalid",
                f"{field}.expected.termination_reason must be a stable lowercase token",
                field=f"{field}.expected.termination_reason",
                actual=termination_reason,
                **({"case_id": case_id} if case_id else {}),
            )
        parsed["expected"] = {
            key: expected[key]
            for key in ("status", "timeout_kind", "termination_reason")
            if key in expected
        }
        if case_id:
            expected_by_id[_fold_id(case_id)] = status

        if judge_type == "custom":
            output = raw_case.get("contestant_output")
            if output is None:
                add(
                    "judge_qa_contestant_output_required",
                    f"{field}.contestant_output is required for Checker QA",
                    field=f"{field}.contestant_output",
                    **({"case_id": case_id} if case_id else {}),
                )
            else:
                parsed["contestant_output"] = resolve_file(
                    f"{field}.contestant_output",
                    output,
                    required_prefix="judge-fixtures",
                    case_id=case_id,
                )
        elif judge_type == "interactive":
            contestant = raw_case.get("contestant")
            if not isinstance(contestant, dict):
                add(
                    "judge_qa_contestant_invalid",
                    f"{field}.contestant must be a mapping",
                    field=f"{field}.contestant",
                    **({"case_id": case_id} if case_id else {}),
                )
            else:
                unknown_fields(
                    contestant,
                    {"source", "behavior"},
                    f"{field}.contestant",
                    case_id=case_id,
                )
                source = contestant.get("source")
                behavior = contestant.get("behavior")
                if (source is None) == (behavior is None):
                    add(
                        "judge_qa_contestant_mode_conflict",
                        f"{field}.contestant must declare exactly one of source or behavior",
                        field=f"{field}.contestant",
                        **({"case_id": case_id} if case_id else {}),
                    )
                elif source is not None:
                    parsed["contestant"] = {
                        "source": resolve_file(
                            f"{field}.contestant.source",
                            source,
                            required_prefix="code/judge-qa",
                            case_id=case_id,
                        ),
                    }
                elif (
                    not isinstance(behavior, str)
                    or behavior not in _INTERACTOR_BEHAVIORS
                ):
                    add(
                        "judge_qa_contestant_behavior_invalid",
                        f"{field}.contestant.behavior must be one of: "
                        f"{', '.join(sorted(_INTERACTOR_BEHAVIORS))}",
                        field=f"{field}.contestant.behavior",
                        actual=behavior,
                        **({"case_id": case_id} if case_id else {}),
                    )
                else:
                    parsed["contestant"] = {"behavior": behavior}
        parsed_cases.append(parsed)

    robustness = None
    raw_robustness = raw_qa.get("robustness")
    if raw_robustness is not None:
        if judge_type != "custom":
            add(
                "judge_qa_robustness_unsupported",
                "judge.qa.robustness is only valid for Checker QA",
                field="judge.qa.robustness",
            )
        if not isinstance(raw_robustness, dict):
            add(
                "judge_qa_robustness_invalid",
                "judge.qa.robustness must be a mapping",
                field="judge.qa.robustness",
            )
        else:
            unknown_fields(
                raw_robustness,
                {"baseline", "probes"},
                "judge.qa.robustness",
            )
            baseline = raw_robustness.get("baseline")
            probes = raw_robustness.get("probes")
            if not isinstance(baseline, str) or not _CASE_ID_PATTERN.fullmatch(baseline):
                add(
                    "judge_qa_robustness_baseline_invalid",
                    "judge.qa.robustness.baseline must be a valid case ID",
                    field="judge.qa.robustness.baseline",
                    actual=baseline,
                )
            elif _fold_id(baseline) not in seen_ids:
                add(
                    "judge_qa_robustness_baseline_missing",
                    f"robustness baseline does not reference a configured case: {baseline}",
                    field="judge.qa.robustness.baseline",
                    baseline=baseline,
                )
            elif expected_by_id.get(_fold_id(baseline)) != "AC":
                add(
                    "judge_qa_robustness_baseline_not_ac",
                    f"robustness baseline must expect AC: {baseline}",
                    field="judge.qa.robustness.baseline",
                    baseline=baseline,
                )
            if not isinstance(probes, list) or not probes:
                add(
                    "judge_qa_robustness_probes_invalid",
                    "judge.qa.robustness.probes must be a non-empty list",
                    field="judge.qa.robustness.probes",
                )
                probes = []
            elif len(probes) > MAX_JUDGE_QA_ROBUSTNESS_PROBES:
                add(
                    "judge_qa_robustness_probe_limit_exceeded",
                    "judge.qa.robustness.probes exceeds the fixed limit of "
                    f"{MAX_JUDGE_QA_ROBUSTNESS_PROBES}",
                    field="judge.qa.robustness.probes",
                    count=len(probes),
                    limit=MAX_JUDGE_QA_ROBUSTNESS_PROBES,
                )
                probes = probes[:MAX_JUDGE_QA_ROBUSTNESS_PROBES]
            invalid_probes = sorted({
                str(probe)
                for probe in probes
                if not isinstance(probe, str) or probe not in _ROBUSTNESS_PROBES
            })
            if invalid_probes:
                add(
                    "judge_qa_robustness_probe_invalid",
                    "unsupported robustness probes: " + ", ".join(invalid_probes),
                    field="judge.qa.robustness.probes",
                    probes=invalid_probes,
                )
            folded_probes = [
                probe.casefold() for probe in probes if isinstance(probe, str)
            ]
            seen_probes = set()
            duplicate_probes = set()
            for probe in folded_probes:
                if probe in seen_probes:
                    duplicate_probes.add(probe)
                else:
                    seen_probes.add(probe)
            duplicate_probes = sorted(duplicate_probes)
            if duplicate_probes:
                add(
                    "judge_qa_robustness_probe_duplicate",
                    "duplicate robustness probes: " + ", ".join(duplicate_probes),
                    field="judge.qa.robustness.probes",
                    probes=duplicate_probes,
                )
            robustness = {
                "baseline": baseline,
                "probes": list(probes),
            }

    total_bytes = sum(item["size"] for item in resolved_files.values())
    if len(resolved_files) > MAX_JUDGE_QA_FILES:
        add(
            "judge_qa_fixture_file_limit_exceeded",
            f"Judge QA references {len(resolved_files)} files; limit is {MAX_JUDGE_QA_FILES}",
            count=len(resolved_files),
            limit=MAX_JUDGE_QA_FILES,
        )
    if total_bytes > MAX_JUDGE_QA_TOTAL_BYTES:
        add(
            "judge_qa_fixture_total_bytes_exceeded",
            f"Judge QA fixture bytes exceed {MAX_JUDGE_QA_TOTAL_BYTES}",
            total_bytes=total_bytes,
            limit=MAX_JUDGE_QA_TOTAL_BYTES,
        )

    fixture_hash = None
    if not diagnostics:
        try:
            fixture_hash = _hash_fixture_files(resolved_files.values())
        except OSError as exc:
            add(
                "judge_qa_fixture_changed",
                f"fixture files changed or became unreadable while hashing: {exc}",
            )
    return {
        "configured": True,
        "applicable": judge_type in {"custom", "interactive"},
        "ok": not diagnostics,
        "schema_version": schema_version,
        "judge_type": judge_type,
        "fixture_hash": fixture_hash,
        "cases": parsed_cases,
        "robustness": robustness,
        "files": sorted(
            ({"path": item["path"], "size": item["size"]} for item in resolved_files.values()),
            key=lambda item: item["path"],
        ),
        "stats": {
            "cases": len(raw_cases),
            "files": len(resolved_files),
            "total_bytes": total_bytes,
        },
        "diagnostics": diagnostics,
    }
