"""Conservative C++ mutation planning and isolated execution.

Mutation testing is evidence about the current data, not a proof that no
unknown wrong solution exists.  Mutants are always built and judged from a
temporary problem snapshot; the live workspace is only read and receives a
local evidence file after a complete successful run.
"""

from __future__ import annotations

import copy
import bisect
import hashlib
import json
import math
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath

from . import __version__
from .build_lock import workspace_build_lock, workspace_file_lock
from .errors import ProbHubError
from .io import atomic_write_json, read_yaml, write_yaml
from .judging import JUDGE_OUTPUT_LIMIT_BYTES, JUDGE_TIMEOUT_SECONDS, judge_problem
from .linting import compute_data_hash, compute_source_hash
from .mutation_config import (
    MAX_MUTATION_EXCLUSION_ID_BYTES,
    MAX_MUTATION_EXCLUSION_REASON_BYTES,
    MAX_MUTATION_EXCLUSIONS,
    MUTATION_OPERATORS,
    MUTATION_OPERATOR_VERSION,
    inspect_mutation_config,
    load_mutation_exclusions,
    normalize_mutation_exclusions,
)
from .mutation_syntax import (
    MUTATION_LOCATOR_VERSION,
    MUTATION_PARSER_TIMEOUT_SECONDS,
    locate_cpp_mutation_syntax,
    mutation_parser_identity,
)
from .problem_snapshot import copy_problem_consistently
from .process_control import (
    ProcessCancelled,
    cancellation_requested,
    run_managed_to_files,
)
from .solutions import (
    normalize_solution_entries,
    resolve_solution_source,
)
from .transactions import recover_workspace_transactions
from .workspace import load_workspace, problem_entries, resolve_problem_dir


MUTATION_EVIDENCE_SCHEMA_VERSION = 2
MUTATION_EVIDENCE_FILENAME = "mutation-evidence-v2.json"
MUTATION_LOCK_FILENAME = ".probhub/mutation.lock"
MAX_MUTANTS = 256
MAX_DIAGNOSTIC_BYTES = 2048
MAX_MUTATION_TEXT_BYTES = 512
MAX_HIT_CASES = 16
MAX_HIT_FIELD_BYTES = 256
MAX_MUTATION_EVIDENCE_BYTES = 4 * 1024 * 1024
MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION = 1
MUTATION_SCHEDULER = "serial-v1"
MUTATION_SNAPSHOT_MODEL = "immutable-problem-v1"
MUTATION_JUDGE_MEMORY_BASE_MB = 4096
MUTATION_JUDGE_MEMORY_HEADROOM_MB = 2048
MUTATION_JUDGE_PROCESS_BASE = 64
MUTATION_JUDGE_PROCESS_HEADROOM = 16
_MUTATION_EXECUTION_PROFILE_KEYS = frozenset({
    "schema_version",
    "scheduler",
    "snapshot",
    "mutant_timeout_seconds",
    "judge_output_limit_bytes",
    "judge_memory_limit_mb",
    "judge_process_limit",
})
_MUTATION_CLASSIFICATION_ORDER = (
    "killed",
    "survived",
    "compile-invalid",
    "infrastructure-failed",
)
_MUTATION_CLASSIFICATIONS = frozenset(_MUTATION_CLASSIFICATION_ORDER)
_MUTATION_EXCLUSION_STATUSES = frozenset({"matched", "out-of-scope", "unmatched"})


@dataclass(frozen=True)
class Mutation:
    id: str
    operator: str
    start: int
    end: int
    line: int
    column: int
    original: str
    replacement: str
    description: str

    def as_dict(self):
        return {
            "id": self.id,
            "operator": self.operator,
            "line": self.line,
            "column": self.column,
            "original": _bounded(self.original, MAX_MUTATION_TEXT_BYTES),
            "replacement": _bounded(self.replacement, MAX_MUTATION_TEXT_BYTES),
            "description": _bounded(self.description, MAX_MUTATION_TEXT_BYTES),
        }


_INTEGER_RE = re.compile(
    r"(?P<digits>0|[1-9][0-9]*)"
    r"(?P<suffix>(?:[uU](?:[lL]|ll|LL)?|(?:[lL]|ll|LL)[uU]?))?$"
)
_COMPARISON_REPLACEMENTS = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
}


def mutation_evidence_path(problem_dir):
    return Path(problem_dir) / ".probhub" / MUTATION_EVIDENCE_FILENAME


def _line_starts(source):
    starts = [0]
    starts.extend(index + 1 for index, character in enumerate(source) if character == "\n")
    return starts


def _line_column(line_starts, offset):
    line_index = bisect.bisect_right(line_starts, offset) - 1
    return line_index + 1, offset - line_starts[line_index] + 1


def _normalized_mutation_text(value):
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _mutation_candidates(source, operators, *, locations=None):
    locations = locations or locate_cpp_mutation_syntax(source)
    candidates = []
    selected = set(operators)
    if "comparison-boundary" in selected:
        for comparison in locations.comparisons:
            token = comparison.operator
            replacement = _COMPARISON_REPLACEMENTS.get(token.text)
            if replacement is not None:
                candidates.append((
                    token,
                    "comparison-boundary",
                    replacement,
                    f"replace {token.text} with {replacement}",
                ))

    if "integer-boundary" in selected:
        for comparison in locations.comparisons:
            if comparison.operator.text not in _COMPARISON_REPLACEMENTS:
                continue
            for token in (comparison.left, comparison.right):
                if token.kind != "number_literal":
                    continue
                literal = _INTEGER_RE.fullmatch(token.text)
                if literal is None:
                    continue
                for delta in (1, -1):
                    replacement = f"({token.text} {'+' if delta > 0 else '-'} 1)"
                    candidates.append((
                        token,
                        "integer-boundary",
                        replacement,
                        f"change integer boundary by {delta:+d}",
                    ))

    if "boolean-negation" in selected:
        for token in locations.unary_not:
            candidates.append((
                token,
                "boolean-negation",
                "",
                "remove boolean negation",
            ))
        for located in locations.conditions:
            token = located.condition
            if not token.text.startswith("(") or not token.text.endswith(")"):
                continue
            candidates.append((
                token,
                "boolean-negation",
                f"(!({token.text[1:-1]}))",
                f"negate {located.keyword} condition",
            ))
    return candidates


def _planned_mutations(source, operators, *, locations=None, control_check=None):
    candidates = _mutation_candidates(source, operators, locations=locations)
    candidates.sort(key=lambda item: (item[0].start, item[0].end, item[1], item[2]))
    line_starts = _line_starts(source)
    mutations = []
    for index, (token, operator, replacement, description) in enumerate(candidates):
        if control_check is not None and index % 128 == 0:
            control_check()
        line, column = _line_column(line_starts, token.start)
        identity_original = _normalized_mutation_text(token.text)
        identity_replacement = _normalized_mutation_text(replacement)
        identity = hashlib.sha256(
            f"{operator}\0{line}\0{column}\0{identity_original}\0{identity_replacement}".encode("utf-8")
        ).hexdigest()[:16]
        mutation_id = f"{MUTATION_OPERATOR_VERSION}:{operator}:{line}:{column}:{identity}"
        mutations.append(Mutation(
            id=mutation_id,
            operator=operator,
            start=token.start,
            end=token.end,
            line=line,
            column=column,
            original=token.text,
            replacement=replacement,
            description=description,
        ))
    return mutations


def _planning_control_check(cancel_check, deadline):
    if cancel_check is not None and cancel_check():
        raise ProbHubError("mutation parser was cancelled", code="mutation_parser_cancelled")
    if deadline is not None and time.monotonic() >= float(deadline):
        raise ProbHubError("mutation parser deadline exceeded", code="mutation_parser_timeout")


def plan_mutations(
    source,
    *,
    operators=None,
    max_mutants=MAX_MUTANTS,
    exclusions=None,
    parser_timeout=None,
    parser_cancel_check=None,
    parser_deadline=None,
):
    if not isinstance(source, str):
        raise ProbHubError("C++ source must be text", code="mutation_source_invalid")
    selected = list(MUTATION_OPERATORS if operators is None else operators)
    if not selected or any(item not in MUTATION_OPERATORS for item in selected):
        raise ProbHubError(
            "mutation operators must be chosen from: " + ", ".join(MUTATION_OPERATORS),
            code="mutation_operator_invalid",
        )
    if len(set(selected)) != len(selected):
        raise ProbHubError("mutation operators must be unique", code="mutation_operator_invalid")
    if max_mutants is None:
        max_mutants = MAX_MUTANTS
    if isinstance(max_mutants, bool) or not isinstance(max_mutants, int) or not 0 < max_mutants <= MAX_MUTANTS:
        raise ProbHubError(
            f"max_mutants must be an integer in 1..{MAX_MUTANTS}",
            code="mutation_limit_invalid",
        )
    exclusion_records = normalize_mutation_exclusions(exclusions)
    control_check = lambda: _planning_control_check(parser_cancel_check, parser_deadline)
    locations = locate_cpp_mutation_syntax(
        source,
        timeout=parser_timeout,
        cancel_check=parser_cancel_check,
        deadline=parser_deadline,
    )
    control_check()
    raw_mutations = _planned_mutations(
        source, selected, locations=locations, control_check=control_check
    )
    all_mutations = (
        raw_mutations
        if selected == list(MUTATION_OPERATORS)
        else _planned_mutations(
            source, MUTATION_OPERATORS, locations=locations, control_check=control_check
        )
    )
    exclusion_ids = {item["id"] for item in exclusion_records}
    raw_ids = {item.id for item in raw_mutations}
    all_ids = {item.id for item in all_mutations}
    applied_exclusions = [
        {
            **item,
            "status": (
                "matched"
                if item["id"] in raw_ids
                else "out-of-scope"
                if item["id"] in all_ids
                else "unmatched"
            ),
        }
        for item in exclusion_records
    ]
    effective_mutations = [
        item for item in raw_mutations
        if item.id not in exclusion_ids
    ]
    mutations = effective_mutations[:max_mutants]
    mutation_records = [item.as_dict() for item in mutations]
    plan_payload = {
        "operator_version": MUTATION_OPERATOR_VERSION,
        "locator_version": MUTATION_LOCATOR_VERSION,
        "operators": selected,
        "max_mutants": max_mutants,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "exclusions": applied_exclusions,
        "mutations": mutation_records,
    }
    plan_hash = hashlib.sha256(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": MUTATION_EVIDENCE_SCHEMA_VERSION,
        "operator_version": MUTATION_OPERATOR_VERSION,
        "locator_version": MUTATION_LOCATOR_VERSION,
        "operators": selected,
        "max_mutants": max_mutants,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "mutations": mutations,
        "exclusions": applied_exclusions,
        "plan_hash": plan_hash,
        "raw_planned": len(raw_mutations),
        "excluded": sum(item["status"] == "matched" for item in applied_exclusions),
        "planned": len(effective_mutations),
        "selected": len(mutations),
        "truncated": len(effective_mutations) > len(mutations),
    }


def apply_mutation(source, mutation):
    return source[:mutation.start] + mutation.replacement + source[mutation.end:]


def _mutation_execution_profile(config):
    limits = config.get("limits") if isinstance(config.get("limits"), dict) else {}
    problem_memory = limits.get("memory", 256)
    if not isinstance(problem_memory, int) or isinstance(problem_memory, bool) or problem_memory <= 0:
        problem_memory = 256
    problem_processes = limits.get("processes", 32)
    if not isinstance(problem_processes, int) or isinstance(problem_processes, bool) or problem_processes <= 0:
        problem_processes = 32
    return {
        "schema_version": MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION,
        "scheduler": MUTATION_SCHEDULER,
        "snapshot": MUTATION_SNAPSHOT_MODEL,
        "mutant_timeout_seconds": JUDGE_TIMEOUT_SECONDS,
        "judge_output_limit_bytes": JUDGE_OUTPUT_LIMIT_BYTES,
        "judge_memory_limit_mb": max(
            MUTATION_JUDGE_MEMORY_BASE_MB,
            2 * problem_memory + MUTATION_JUDGE_MEMORY_HEADROOM_MB,
        ),
        "judge_process_limit": max(
            MUTATION_JUDGE_PROCESS_BASE,
            2 * problem_processes + MUTATION_JUDGE_PROCESS_HEADROOM,
        ),
    }


def _valid_mutation_execution_profile(value):
    if not isinstance(value, dict) or set(value) != _MUTATION_EXECUTION_PROFILE_KEYS:
        return False
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION
        or value["scheduler"] != MUTATION_SCHEDULER
        or value["snapshot"] != MUTATION_SNAPSHOT_MODEL
    ):
        return False
    timeout = value["mutant_timeout_seconds"]
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        return False
    return all(
        isinstance(value[key], int)
        and not isinstance(value[key], bool)
        and value[key] > 0
        for key in (
            "judge_output_limit_bytes",
            "judge_memory_limit_mb",
            "judge_process_limit",
        )
    )


def _workspace_entry_for_problem(root, workspace, problem_dir):
    matches = [
        entry for entry in problem_entries(workspace)
        if resolve_problem_dir(root, entry) == problem_dir
    ]
    if len(matches) != 1:
        raise ProbHubError(
            f"mutation problem is not a unique workspace entry: {problem_dir}",
            code="mutation_problem_invalid",
        )
    return matches[0]


def _live_identity(problem_dir):
    config = read_yaml(Path(problem_dir) / "probhub.yaml")
    return (
        config,
        compute_source_hash(problem_dir, config),
        compute_data_hash(problem_dir, config),
    )


@lru_cache(maxsize=1)
def _compiler_fingerprint():
    parser_identity = mutation_parser_identity()
    with tempfile.TemporaryDirectory(prefix="probhub-mutation-compiler-") as temp:
        stdout = Path(temp) / "stdout"
        stderr = Path(temp) / "stderr"
        try:
            result = run_managed_to_files(
                ["g++", "--version"],
                stdout_path=stdout,
                stderr_path=stderr,
                timeout=10.0,
                memory_limit_mb=None,
                output_limit_bytes=64 * 1024,
                process_limit=8,
            )
        except OSError as exc:
            result = {"reason": "start_error", "message": str(exc), "returncode": None}
        output = stdout.read_text(encoding="utf-8", errors="replace") if stdout.is_file() else ""
    identity = next((line.strip() for line in output.splitlines() if line.strip()), None)
    fields = {
        "schema_version": 1,
        "probhub_version": __version__,
        "compiler": "g++",
        "compiler_identity": identity,
        "compiler_flags": ["-O2", "-std=c++17"],
        "parser": "tree-sitter-cpp",
        "locator_version": parser_identity["locator_version"],
        "parser_versions": {
            "tree_sitter": parser_identity["tree_sitter_version"],
            "tree_sitter_cpp": parser_identity["tree_sitter_cpp_version"],
        },
        "operator_version": MUTATION_OPERATOR_VERSION,
    }
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        **fields,
        "digest": digest,
        "available": bool(result.get("reason") == "completed" and result.get("returncode") == 0 and identity),
        **({"diagnostic": result.get("message") or result.get("reason")} if not identity else {}),
    }


def _bounded(value, limit=MAX_DIAGNOSTIC_BYTES):
    text = str(value or "")
    encoded = text.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _bounded_optional(value, limit):
    return None if value is None else _bounded(value, limit)


def _classify_judge_result(result, mutant_name):
    final = result.get("final") or {}
    events = result.get("events") or []
    compile_events = [
        item for item in events
        if item.get("type") == "compile" and item.get("kind") == "std"
    ]
    if any(item.get("ok") is False for item in compile_events):
        return "compile-invalid", [], final.get("message") or final.get("code")
    if result.get("ok") and final.get("code") == "all_expectations_met":
        return "survived", [], final.get("message")
    hits = []
    infrastructure = False
    for item in events:
        if item.get("type") != "case" or item.get("program") != mutant_name:
            continue
        status = str(item.get("status") or "")
        if status == "FAIL" or item.get("failure_kind") in {"control_failure", "startup_failure"}:
            infrastructure = True
            continue
        if status != "AC":
            hits.append({
                "case": item.get("case"),
                "status": status,
                "termination_reason": item.get("termination_reason"),
                "failure_kind": item.get("failure_kind"),
            })
    code = final.get("code")
    if infrastructure or code in {
        "judge_timeout", "judge_output_limit", "judge_memory_limit",
        "judge_process_limit", "judge_failed", "validator_failed",
        "checker_compile_failed", "interactor_compile_failed", "checker_missing",
        "interactor_missing",
    }:
        return "infrastructure-failed", hits, code
    if hits or code == "expectation_not_met":
        return "killed", hits, code
    return "infrastructure-failed", hits, code or "unknown mutation result"


def _control_check(cancel_check, deadline):
    check = cancellation_requested if cancel_check is None else cancel_check
    if check():
        raise ProbHubError("mutation testing was cancelled", code="mutation_cancelled")
    if deadline is not None and time.monotonic() >= float(deadline):
        raise ProbHubError("mutation testing deadline exceeded", code="mutation_timeout")


def mutation_test_problem(
    root,
    problem_dir,
    *,
    use_cache=True,
    operators=None,
    max_mutants=MAX_MUTANTS,
    cancel_check=None,
    deadline=None,
):
    root = Path(root).resolve()
    problem_dir = Path(problem_dir).resolve()
    evidence_path = mutation_evidence_path(problem_dir)
    results = []
    status = "infrastructure-failed"
    evidence = None

    with workspace_file_lock(
        problem_dir,
        MUTATION_LOCK_FILENAME,
        busy_code="mutation_busy",
        busy_message="another mutation testing run is already running",
        no_follow=True,
    ), tempfile.TemporaryDirectory(prefix="probhub-mutation-") as temp:
        _control_check(cancel_check, deadline)
        temporary_root = Path(temp)
        baseline = temporary_root / "baseline"
        execution_root = temporary_root / "execution"
        worker = execution_root / "problem"

        with workspace_build_lock(root):
            _, workspace = load_workspace(root)
            recover_workspace_transactions(root, workspace)
            _, workspace = load_workspace(root)
            entry = _workspace_entry_for_problem(root, workspace, problem_dir)
            config, source_hash, data_hash = copy_problem_consistently(
                root,
                entry,
                baseline,
                operation="mutation snapshot",
            )

        judge_type = str((config.get("judge") or {}).get("type", "standard")).strip().lower()
        if judge_type not in {"standard", ""}:
            raise ProbHubError(
                "mutation testing currently supports judge.type standard only",
                code="mutation_standard_required",
            )
        accepted = normalize_solution_entries(config)["std"]
        if not accepted or not accepted[0].get("file"):
            raise ProbHubError(
                "mutation testing requires a configured accepted solution",
                code="mutation_source_missing",
            )
        source_path, source_error, source_message = resolve_solution_source(
            baseline, accepted[0]["file"]
        )
        if source_path is None:
            raise ProbHubError(
                f"accepted source is invalid: {source_message or source_error}",
                code="mutation_source_invalid",
            )
        if source_path.suffix.lower() != ".cpp":
            raise ProbHubError(
                "mutation testing currently supports C++ accepted solutions only",
                code="mutation_cpp_required",
            )
        try:
            source = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProbHubError(
                f"cannot read accepted source: {source_path}: {exc}",
                code="mutation_source_invalid",
            ) from exc

        exclusions = load_mutation_exclusions(config)
        _control_check(cancel_check, deadline)
        parser_remaining = None if deadline is None else float(deadline) - time.monotonic()
        parser_timeout = MUTATION_PARSER_TIMEOUT_SECONDS
        parser_uses_deadline = False
        if parser_remaining is not None:
            if parser_remaining <= 0:
                raise ProbHubError(
                    "mutation testing deadline exceeded",
                    code="mutation_timeout",
                )
            parser_uses_deadline = parser_remaining < MUTATION_PARSER_TIMEOUT_SECONDS
            parser_timeout = max(
                0.001,
                min(MUTATION_PARSER_TIMEOUT_SECONDS, parser_remaining),
            )
        try:
            plan = plan_mutations(
                source,
                operators=operators,
                max_mutants=max_mutants,
                exclusions=exclusions,
                parser_timeout=parser_timeout,
                parser_cancel_check=cancel_check,
                parser_deadline=deadline,
            )
        except ProbHubError as exc:
            if exc.code == "mutation_parser_cancelled":
                raise ProbHubError(
                    "mutation testing was cancelled",
                    code="mutation_cancelled",
                ) from exc
            if exc.code == "mutation_parser_timeout" and parser_uses_deadline:
                raise ProbHubError(
                    "mutation testing deadline exceeded",
                    code="mutation_timeout",
                ) from exc
            raise

        builder_fingerprint = copy.deepcopy(_compiler_fingerprint())
        if not builder_fingerprint.get("available"):
            raise ProbHubError(
                "mutation testing requires an available g++ C++17 compiler",
                code="mutation_compiler_unavailable",
            )
        execution_profile = _mutation_execution_profile(config)
        # The strict source fence above already validated the configured path.
        # Reuse that logical path instead of deriving it from resolved physical
        # paths, which may mix Windows long and 8.3 aliases for the same tree.
        source_relative = Path(*PurePosixPath(
            str(accepted[0]["file"]).replace("\\", "/")
        ).parts)
        mutant_name = source_relative.as_posix()
        execution_root.mkdir()

        for mutation in plan["mutations"]:
            _control_check(cancel_check, deadline)
            phase = "prepare"
            try:
                if worker.exists():
                    shutil.rmtree(worker)
                shutil.copytree(baseline, worker)
                source_in_worker = worker / source_relative
                source_in_worker.write_text(
                    apply_mutation(source, mutation),
                    encoding="utf-8",
                    newline="",
                )
                config_copy = copy.deepcopy(config)
                config_copy["solutions"] = {
                    "accepted": [{
                        "file": mutant_name,
                        "expected": {"status": "AC", "all": True},
                    }],
                    "brute": [],
                    "wrong": [],
                }
                write_yaml(worker / "probhub.yaml", config_copy)
                remaining = (
                    None if deadline is None
                    else float(deadline) - time.monotonic()
                )
                judge_timeout = execution_profile["mutant_timeout_seconds"]
                if remaining is not None:
                    if remaining <= 0:
                        raise ProbHubError(
                            "mutation testing deadline exceeded",
                            code="mutation_timeout",
                        )
                    judge_timeout = max(0.001, min(judge_timeout, remaining))

                def judge_cancel_check():
                    return bool(
                        (
                            cancellation_requested()
                            if cancel_check is None
                            else cancel_check()
                        )
                        or (
                            deadline is not None
                            and time.monotonic() >= float(deadline)
                        )
                    )

                phase = "judge"
                judged = judge_problem(
                    execution_root,
                    worker,
                    use_cache=use_cache,
                    timeout=judge_timeout,
                    output_limit_bytes=execution_profile["judge_output_limit_bytes"],
                    memory_limit_mb=execution_profile["judge_memory_limit_mb"],
                    process_limit=execution_profile["judge_process_limit"],
                    cancel_check=judge_cancel_check,
                )
                _control_check(cancel_check, deadline)
                if (
                    remaining is not None
                    and remaining < execution_profile["mutant_timeout_seconds"]
                    and (judged.get("final") or {}).get("code") == "judge_timeout"
                ):
                    raise ProbHubError(
                        "mutation testing deadline exceeded",
                        code="mutation_timeout",
                    )
                classification, hits, diagnostic = _classify_judge_result(
                    judged, mutant_name
                )
            except ProcessCancelled as exc:
                _control_check(cancel_check, deadline)
                raise ProbHubError(
                    "mutation testing was cancelled",
                    code="mutation_cancelled",
                ) from exc
            except ProbHubError as exc:
                if exc.code in {"mutation_cancelled", "mutation_timeout"}:
                    raise
                classification, hits, diagnostic = (
                    "infrastructure-failed",
                    [],
                    exc.code or (
                        "mutation_worker_prepare_failed"
                        if phase == "prepare"
                        else "mutation_judge_failed"
                    ),
                )
            except (OSError, UnicodeError, ValueError, TypeError):
                classification, hits, diagnostic = (
                    "infrastructure-failed",
                    [],
                    (
                        "mutation_worker_prepare_failed"
                        if phase == "prepare"
                        else "mutation_judge_failed"
                    ),
                )

            bounded_hits = [{
                "case": _bounded_optional(item.get("case"), MAX_HIT_FIELD_BYTES),
                "status": _bounded(item.get("status"), MAX_HIT_FIELD_BYTES),
                "termination_reason": _bounded_optional(
                    item.get("termination_reason"), MAX_HIT_FIELD_BYTES
                ),
                "failure_kind": _bounded_optional(
                    item.get("failure_kind"), MAX_HIT_FIELD_BYTES
                ),
            } for item in hits[:MAX_HIT_CASES]]
            results.append({
                **mutation.as_dict(),
                "classification": classification,
                "hit_cases": bounded_hits,
                "hit_cases_total": len(hits),
                "hit_cases_truncated": len(hits) > len(bounded_hits),
                "diagnostic": {"code": _bounded(diagnostic or "unknown")},
            })
            if classification == "infrastructure-failed":
                break

        _control_check(cancel_check, deadline)
        counts = {name: sum(
            item["classification"] == name for item in results
        ) for name in _MUTATION_CLASSIFICATION_ORDER}
        status = "infrastructure-failed" if counts["infrastructure-failed"] else "passed"
        evidence = {
            "schema_version": MUTATION_EVIDENCE_SCHEMA_VERSION,
            "status": status,
            "problem_id": config.get("id"),
            "source_path": accepted[0]["file"],
            "source_hash": source_hash,
            "data_hash": data_hash,
            "builder_fingerprint": builder_fingerprint,
            "execution_profile": execution_profile,
            "operator_version": MUTATION_OPERATOR_VERSION,
            "locator_version": MUTATION_LOCATOR_VERSION,
            "operators": plan["operators"],
            "max_mutants": plan["max_mutants"],
            "plan_hash": plan["plan_hash"],
            "raw_planned": plan["raw_planned"],
            "excluded": plan["excluded"],
            "planned": plan["planned"],
            "selected": plan["selected"],
            "executed": len(results),
            "truncated": plan["truncated"],
            "exclusions": plan["exclusions"],
            "summary": counts,
            "mutations": results,
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }
        if status == "passed":
            evidence_size = len(
                (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
            if evidence_size > MAX_MUTATION_EVIDENCE_BYTES:
                raise ProbHubError(
                    f"mutation evidence exceeds {MAX_MUTATION_EVIDENCE_BYTES} bytes",
                    code="mutation_evidence_limit",
                )
            with workspace_build_lock(root):
                try:
                    _, live_workspace = load_workspace(root)
                    recover_workspace_transactions(root, live_workspace)
                    _, current_source_hash, current_data_hash = _live_identity(problem_dir)
                except Exception as exc:
                    raise ProbHubError(
                        "mutation inputs changed during execution",
                        code="inputs_changed",
                    ) from exc
                if current_source_hash != source_hash or current_data_hash != data_hash:
                    raise ProbHubError(
                        "mutation inputs changed during execution",
                        code="inputs_changed",
                    )
                try:
                    atomic_write_json(evidence_path, evidence)
                except OSError as exc:
                    raise ProbHubError(
                        f"failed to publish mutation evidence: {exc}",
                        code="mutation_evidence_publish_failed",
                    ) from exc

    return {
        "ok": status == "passed",
        "status": status,
        "evidence": evidence,
    }


def mutation_evidence_profile(problem_dir, config):
    config_report = inspect_mutation_config(config)
    configured_exclusions = list(config_report["exclusions"])

    def profile(state, *, summary=None, mutants=0, planning=None, exclusions=None, diagnostics=None, status=None):
        return {
            "state": state,
            "status": status,
            "summary": dict(summary or {}),
            "mutants": mutants,
            "operator_version": MUTATION_OPERATOR_VERSION,
            "locator_version": MUTATION_LOCATOR_VERSION,
            "planning": dict(planning or {
                "raw": 0,
                "excluded": 0,
                "effective": 0,
                "selected": 0,
                "configured_exclusions": len(configured_exclusions),
                "out_of_scope_exclusions": 0,
                "unmatched_exclusions": 0,
            }),
            "exclusions": list(exclusions or []),
            "diagnostics": list(diagnostics or []),
        }

    if not config_report["valid"]:
        # lint owns Schema diagnostics; returning them again would duplicate
        # the same issue in workspace reports.
        return profile("invalid")
    path = mutation_evidence_path(problem_dir)
    if not path.is_file():
        return profile("missing")
    try:
        if path.stat().st_size > MAX_MUTATION_EVIDENCE_BYTES:
            return profile("invalid")
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return profile("invalid")
    if not isinstance(evidence, dict):
        return profile("invalid")
    summary = evidence.get("summary")
    mutations = evidence.get("mutations")
    exclusion_records = evidence.get("exclusions")
    execution_profile = evidence.get("execution_profile")
    expected_counts = tuple(sorted(_MUTATION_CLASSIFICATIONS))
    if (
        isinstance(evidence.get("schema_version"), bool)
        or not isinstance(evidence.get("schema_version"), int)
        or evidence.get("schema_version") != MUTATION_EVIDENCE_SCHEMA_VERSION
        or not isinstance(summary, dict)
        or set(summary) != set(expected_counts)
        or not isinstance(mutations, list)
        or not isinstance(evidence.get("plan_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence["plan_hash"])
        or not isinstance(evidence.get("builder_fingerprint"), dict)
        or (
            "execution_profile" in evidence
            and not _valid_mutation_execution_profile(execution_profile)
        )
        or (
            "locator_version" in evidence
            and (
                not isinstance(evidence.get("locator_version"), str)
                or len(evidence["locator_version"].encode("utf-8", errors="replace")) > 128
            )
        )
        or not isinstance(exclusion_records, list)
        or len(exclusion_records) > MAX_MUTATION_EXCLUSIONS
    ):
        return profile("invalid")
    if any(not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool) or summary[key] < 0 for key in expected_counts):
        return profile("invalid", mutants=len(mutations))
    if any(
        not isinstance(item, dict)
        or set(item) != {"id", "reason", "status"}
        or not isinstance(item.get("id"), str)
        or len(item["id"].encode("utf-8", errors="replace")) > MAX_MUTATION_EXCLUSION_ID_BYTES
        or not isinstance(item.get("reason"), str)
        or len(item["reason"].encode("utf-8", errors="replace")) > MAX_MUTATION_EXCLUSION_REASON_BYTES
        or item.get("status") not in _MUTATION_EXCLUSION_STATUSES
        for item in exclusion_records
    ):
        return profile("invalid", mutants=len(mutations))
    try:
        normalized_evidence_exclusions = normalize_mutation_exclusions([
            {"id": item["id"], "reason": item["reason"]}
            for item in exclusion_records
        ])
    except ProbHubError:
        return profile("invalid", mutants=len(mutations))
    if normalized_evidence_exclusions != [
        {"id": item["id"], "reason": item["reason"]}
        for item in exclusion_records
    ]:
        return profile("invalid", mutants=len(mutations))
    executed = evidence.get("executed")
    raw_planned = evidence.get("raw_planned")
    excluded = evidence.get("excluded")
    planned = evidence.get("planned")
    selected = evidence.get("selected")
    max_mutants = evidence.get("max_mutants")
    if (
        not isinstance(executed, int) or isinstance(executed, bool) or executed < 0
        or not isinstance(raw_planned, int) or isinstance(raw_planned, bool) or raw_planned < 0
        or not isinstance(excluded, int) or isinstance(excluded, bool) or excluded < 0
        or not isinstance(planned, int) or isinstance(planned, bool) or planned < 0
        or not isinstance(selected, int) or isinstance(selected, bool) or selected < 0
        or not isinstance(max_mutants, int) or isinstance(max_mutants, bool)
        or not 0 < max_mutants <= MAX_MUTANTS
        or not isinstance(evidence.get("truncated"), bool)
        or raw_planned != planned + excluded
        or excluded != sum(item["status"] == "matched" for item in exclusion_records)
        or selected != min(planned, max_mutants)
        or executed != selected
        or evidence["truncated"] != (planned > selected)
        or sum(summary.values()) != executed
        or len(mutations) != executed
    ):
        return profile("invalid", mutants=len(mutations))
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not isinstance(item.get("operator"), str)
        or item.get("operator") not in MUTATION_OPERATORS
        or not isinstance(item.get("line"), int) or isinstance(item.get("line"), bool) or item["line"] < 1
        or not isinstance(item.get("column"), int) or isinstance(item.get("column"), bool) or item["column"] < 1
        or not isinstance(item.get("original"), str)
        or len(item["original"].encode("utf-8", errors="replace")) > MAX_MUTATION_TEXT_BYTES
        or not isinstance(item.get("replacement"), str)
        or len(item["replacement"].encode("utf-8", errors="replace")) > MAX_MUTATION_TEXT_BYTES
        or not isinstance(item.get("description"), str)
        or len(item["description"].encode("utf-8", errors="replace")) > MAX_MUTATION_TEXT_BYTES
        or item.get("classification") not in _MUTATION_CLASSIFICATIONS
        or not isinstance(item.get("hit_cases"), list)
        or len(item["hit_cases"]) > MAX_HIT_CASES
        or not isinstance(item.get("hit_cases_total"), int)
        or isinstance(item.get("hit_cases_total"), bool)
        or item["hit_cases_total"] < len(item["hit_cases"])
        or not isinstance(item.get("hit_cases_truncated"), bool)
        or item["hit_cases_truncated"] != (item["hit_cases_total"] > len(item["hit_cases"]))
        or any(
            not isinstance(hit, dict)
            or set(hit) != {"case", "status", "termination_reason", "failure_kind"}
            or hit["case"] is not None and not isinstance(hit["case"], str)
            or not isinstance(hit["status"], str)
            or hit["termination_reason"] is not None and not isinstance(hit["termination_reason"], str)
            or hit["failure_kind"] is not None and not isinstance(hit["failure_kind"], str)
            or any(
                len(value.encode("utf-8", errors="replace")) > MAX_HIT_FIELD_BYTES
                for value in hit.values()
                if isinstance(value, str)
            )
            for hit in item["hit_cases"]
        )
        or not isinstance(item.get("diagnostic"), dict)
        or not isinstance(item["diagnostic"].get("code"), str)
        or len(item["diagnostic"]["code"].encode("utf-8", errors="replace")) > MAX_DIAGNOSTIC_BYTES
        for item in mutations
    ):
        return profile("invalid", mutants=len(mutations))
    actual_counts = {name: sum(item["classification"] == name for item in mutations) for name in expected_counts}
    if actual_counts != summary:
        return profile("invalid", mutants=len(mutations))
    fingerprint = evidence["builder_fingerprint"]
    legacy_fingerprint_required = {
        "schema_version", "probhub_version", "compiler", "compiler_identity",
        "compiler_flags", "parser", "operator_version", "digest", "available",
    }
    fingerprint_required = legacy_fingerprint_required | {
        "locator_version", "parser_versions",
    }
    if (
        frozenset(fingerprint) not in {
            frozenset(legacy_fingerprint_required),
            frozenset(fingerprint_required),
        }
        or not isinstance(fingerprint["schema_version"], int)
        or isinstance(fingerprint["schema_version"], bool)
        or fingerprint["schema_version"] != 1
        or not isinstance(fingerprint["probhub_version"], str)
        or fingerprint["compiler"] != "g++"
        or not isinstance(fingerprint["compiler_identity"], str)
        or not isinstance(fingerprint["compiler_flags"], list)
        or not all(isinstance(flag, str) for flag in fingerprint["compiler_flags"])
        or not isinstance(fingerprint["parser"], str)
        or fingerprint["operator_version"] != MUTATION_OPERATOR_VERSION
        or fingerprint["available"] is not True
        or not isinstance(fingerprint["digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint["digest"])
    ):
        return profile("invalid", mutants=len(mutations))
    if set(fingerprint) == fingerprint_required and (
        not isinstance(fingerprint["locator_version"], str)
        or len(fingerprint["locator_version"].encode("utf-8", errors="replace")) > 128
        or not isinstance(fingerprint["parser_versions"], dict)
        or set(fingerprint["parser_versions"]) != {"tree_sitter", "tree_sitter_cpp"}
        or not all(isinstance(value, str) for value in fingerprint["parser_versions"].values())
    ):
        return profile("invalid", mutants=len(mutations))
    # `_compiler_fingerprint` computes the digest over its descriptive fields
    # before adding the runtime `available` bit.
    fingerprint_payload = {
        key: fingerprint[key]
        for key in set(fingerprint)
        if key not in {"digest", "available"}
    }
    expected_digest = hashlib.sha256(json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if expected_digest != fingerprint["digest"]:
        return profile("invalid", mutants=len(mutations))
    try:
        current_fingerprint = _compiler_fingerprint()
    except (OSError, ProbHubError):
        current_fingerprint = {}
    builder_matches = bool(
        current_fingerprint.get("available") is True
        and current_fingerprint.get("digest") == fingerprint["digest"]
    )
    try:
        current_source = compute_source_hash(problem_dir, config)
        current_data = compute_data_hash(problem_dir, config)
        accepted = normalize_solution_entries(config)["std"]
        if not accepted or not accepted[0].get("file"):
            return profile("invalid", summary=summary, mutants=len(mutations))
        source_path, source_error, source_message = resolve_solution_source(problem_dir, accepted[0]["file"])
        if source_path is None:
            return profile("invalid", summary=summary, mutants=len(mutations))
        source = source_path.read_text(encoding="utf-8")
        plan = plan_mutations(
            source,
            operators=evidence.get("operators"),
            max_mutants=max_mutants,
            exclusions=configured_exclusions,
        )
    except (OSError, ProbHubError, ValueError, TypeError):
        return profile("invalid", summary=summary, mutants=len(mutations))
    expected_mutations = [item.as_dict() for item in plan["mutations"]]
    recorded_mutations = [
        {key: item.get(key) for key in expected}
        for item, expected in zip(mutations, expected_mutations)
    ]
    if (
        evidence.get("problem_id") != config.get("id")
        or evidence.get("source_path") != accepted[0]["file"]
        or evidence.get("source_hash") != current_source
        or evidence.get("data_hash") != current_data
        or evidence.get("operator_version") != MUTATION_OPERATOR_VERSION
        or evidence.get("locator_version") != MUTATION_LOCATOR_VERSION
        or evidence.get("operators") != plan["operators"]
        or evidence.get("plan_hash") != plan["plan_hash"]
        or evidence.get("raw_planned") != plan["raw_planned"]
        or evidence.get("excluded") != plan["excluded"]
        or evidence.get("planned") != plan["planned"]
        or evidence.get("selected") != plan["selected"]
        or evidence.get("truncated") != plan["truncated"]
        or exclusion_records != plan["exclusions"]
        or len(recorded_mutations) != len(expected_mutations)
        or recorded_mutations != expected_mutations
        or evidence.get("status") != "passed"
    ):
        state = "stale"
    else:
        state = "current" if builder_matches else "stale"
    current_exclusions = plan["exclusions"]
    out_of_scope = [item for item in current_exclusions if item["status"] == "out-of-scope"]
    unmatched = [item for item in current_exclusions if item["status"] == "unmatched"]
    diagnostics = []
    if unmatched:
        diagnostics.append({
            "code": "mutation_exclusion_unmatched",
            "severity": "warning",
            "message": f"{len(unmatched)} mutation exclusion(s) do not match the current plan",
            "ids": [item["id"] for item in unmatched],
        })
    current_evidence = state == "current"
    return profile(
        state,
        status=evidence.get("status") if current_evidence else None,
        summary=summary if current_evidence else {},
        mutants=len(mutations) if current_evidence else 0,
        planning={
            "raw": plan["raw_planned"],
            "excluded": plan["excluded"],
            "effective": plan["planned"],
            "selected": plan["selected"],
            "configured_exclusions": len(current_exclusions),
            "out_of_scope_exclusions": len(out_of_scope),
            "unmatched_exclusions": len(unmatched),
        },
        exclusions=current_exclusions,
        diagnostics=diagnostics,
    )
