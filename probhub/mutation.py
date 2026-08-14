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
import multiprocessing
import re
import shutil
import subprocess
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
    snapshot_process_tree,
    terminate_external_process_tree,
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
MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION = 2
MUTATION_SCHEDULER = "serial-v1"
MUTATION_PARALLEL_SCHEDULER = "spawn-bounded-v1"
MUTATION_SNAPSHOT_MODEL = "immutable-problem-v1"
MUTATION_JUDGE_MEMORY_BASE_MB = 4096
MUTATION_JUDGE_MEMORY_HEADROOM_MB = 2048
MUTATION_JUDGE_PROCESS_BASE = 64
MUTATION_JUDGE_PROCESS_HEADROOM = 16
MUTATION_MAX_JOBS = 2
MUTATION_WORKER_TOKEN_BUDGET = 2
MUTATION_WHOLE_MEMORY_BUDGET_MB = 8192
MUTATION_WHOLE_PROCESS_BUDGET = 160
MUTATION_CANCEL_GRACE_SECONDS = 2.0
_MUTATION_EXECUTION_PROFILE_V1_KEYS = frozenset({
    "schema_version",
    "scheduler",
    "snapshot",
    "mutant_timeout_seconds",
    "judge_output_limit_bytes",
    "judge_memory_limit_mb",
    "judge_process_limit",
})
_MUTATION_EXECUTION_PROFILE_V2_KEYS = _MUTATION_EXECUTION_PROFILE_V1_KEYS | frozenset({
    "requested_jobs",
    "effective_jobs",
    "worker_token_budget",
    "whole_memory_budget_mb",
    "whole_process_budget",
    "configured_concurrent_memory_ceiling_mb",
    "configured_concurrent_process_ceiling",
    "resource_budget_scope",
    "cancel_grace_seconds",
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


@dataclass(frozen=True)
class _MutationWorkerTask:
    index: int
    mutation: Mutation
    baseline: str
    execution_root: str
    worker_root: str
    source: str
    source_relative: str
    config: dict
    use_cache: bool
    execution_profile: dict
    deadline: float | None


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


def _mutation_execution_profile(config, *, requested_jobs=1, selected=1):
    limits = config.get("limits") if isinstance(config.get("limits"), dict) else {}
    problem_memory = limits.get("memory", 256)
    if not isinstance(problem_memory, int) or isinstance(problem_memory, bool) or problem_memory <= 0:
        problem_memory = 256
    problem_processes = limits.get("processes", 32)
    if not isinstance(problem_processes, int) or isinstance(problem_processes, bool) or problem_processes <= 0:
        problem_processes = 32
    judge_memory_limit = max(
        MUTATION_JUDGE_MEMORY_BASE_MB,
        2 * problem_memory + MUTATION_JUDGE_MEMORY_HEADROOM_MB,
    )
    judge_process_limit = max(
        MUTATION_JUDGE_PROCESS_BASE,
        2 * problem_processes + MUTATION_JUDGE_PROCESS_HEADROOM,
    )
    whole_memory_budget = max(MUTATION_WHOLE_MEMORY_BUDGET_MB, judge_memory_limit)
    whole_process_budget = max(MUTATION_WHOLE_PROCESS_BUDGET, judge_process_limit)
    budget_jobs = min(
        MUTATION_WORKER_TOKEN_BUDGET,
        whole_memory_budget // judge_memory_limit,
        whole_process_budget // judge_process_limit,
    )
    effective_jobs = min(requested_jobs, max(selected, 0), budget_jobs)
    return {
        "schema_version": MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION,
        "scheduler": (
            MUTATION_PARALLEL_SCHEDULER
            if effective_jobs > 1 else MUTATION_SCHEDULER
        ),
        "snapshot": MUTATION_SNAPSHOT_MODEL,
        "mutant_timeout_seconds": JUDGE_TIMEOUT_SECONDS,
        "judge_output_limit_bytes": JUDGE_OUTPUT_LIMIT_BYTES,
        "judge_memory_limit_mb": judge_memory_limit,
        "judge_process_limit": judge_process_limit,
        "requested_jobs": requested_jobs,
        "effective_jobs": effective_jobs,
        "worker_token_budget": MUTATION_WORKER_TOKEN_BUDGET,
        "whole_memory_budget_mb": whole_memory_budget,
        "whole_process_budget": whole_process_budget,
        "configured_concurrent_memory_ceiling_mb": effective_jobs * judge_memory_limit,
        "configured_concurrent_process_ceiling": effective_jobs * judge_process_limit,
        "resource_budget_scope": "scheduler-configured-not-host-reservation",
        "cancel_grace_seconds": MUTATION_CANCEL_GRACE_SECONDS,
    }


def _valid_mutation_execution_profile(value):
    if not isinstance(value, dict):
        return False
    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        return False
    if schema_version == 1:
        if set(value) != _MUTATION_EXECUTION_PROFILE_V1_KEYS:
            return False
        schedulers = {MUTATION_SCHEDULER}
    elif schema_version == MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION:
        if set(value) != _MUTATION_EXECUTION_PROFILE_V2_KEYS:
            return False
        schedulers = {MUTATION_SCHEDULER, MUTATION_PARALLEL_SCHEDULER}
    else:
        return False
    if (
        value["scheduler"] not in schedulers
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
    if not all(
        isinstance(value[key], int)
        and not isinstance(value[key], bool)
        and value[key] > 0
        for key in (
            "judge_output_limit_bytes",
            "judge_memory_limit_mb",
            "judge_process_limit",
        )
    ):
        return False
    if schema_version == 1:
        return True
    positive_integer_keys = (
        "requested_jobs",
        "worker_token_budget",
        "whole_memory_budget_mb",
        "whole_process_budget",
    )
    nonnegative_integer_keys = (
        "effective_jobs",
        "configured_concurrent_memory_ceiling_mb",
        "configured_concurrent_process_ceiling",
    )
    if not all(
        isinstance(value[key], int)
        and not isinstance(value[key], bool)
        and value[key] > 0
        for key in positive_integer_keys
    ) or not all(
        isinstance(value[key], int)
        and not isinstance(value[key], bool)
        and value[key] >= 0
        for key in nonnegative_integer_keys
    ):
        return False
    grace = value["cancel_grace_seconds"]
    return bool(
        value["requested_jobs"] <= MUTATION_MAX_JOBS
        and value["worker_token_budget"] <= MUTATION_WORKER_TOKEN_BUDGET
        and value["effective_jobs"] <= value["requested_jobs"]
        and value["effective_jobs"] <= value["worker_token_budget"]
        and value["resource_budget_scope"] == "scheduler-configured-not-host-reservation"
        and isinstance(grace, (int, float))
        and not isinstance(grace, bool)
        and math.isfinite(grace)
        and grace >= 0
        and (
            value["scheduler"] == MUTATION_PARALLEL_SCHEDULER
        ) == (value["effective_jobs"] > 1)
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
    compile_events = [
        item for item in events
        if item.get("type") == "compile" and item.get("kind") == "std"
    ]
    if any(item.get("ok") is False for item in compile_events):
        return "compile-invalid", [], final.get("message") or code
    if result.get("ok") and code == "all_expectations_met":
        return "survived", [], final.get("message")
    if hits or code == "expectation_not_met":
        return "killed", hits, code
    return "infrastructure-failed", hits, code or "unknown mutation result"


def _control_check(cancel_check, deadline):
    check = cancellation_requested if cancel_check is None else cancel_check
    if check():
        raise ProbHubError("mutation testing was cancelled", code="mutation_cancelled")
    if deadline is not None and time.monotonic() >= float(deadline):
        raise ProbHubError("mutation testing deadline exceeded", code="mutation_timeout")


def _mutation_result_record(mutation, classification, hits=(), diagnostic=None):
    bounded_hits = [{
        "case": _bounded_optional(item.get("case"), MAX_HIT_FIELD_BYTES),
        "status": _bounded(item.get("status"), MAX_HIT_FIELD_BYTES),
        "termination_reason": _bounded_optional(
            item.get("termination_reason"), MAX_HIT_FIELD_BYTES
        ),
        "failure_kind": _bounded_optional(
            item.get("failure_kind"), MAX_HIT_FIELD_BYTES
        ),
    } for item in list(hits)[:MAX_HIT_CASES]]
    return {
        **mutation.as_dict(),
        "classification": classification,
        "hit_cases": bounded_hits,
        "hit_cases_total": len(hits),
        "hit_cases_truncated": len(hits) > len(bounded_hits),
        "diagnostic": {"code": _bounded(diagnostic or "unknown")},
    }


def _cancelled_mutation_record(task, code, message):
    return _mutation_result_record(
        task.mutation,
        "cancelled",
        diagnostic=code or message or "mutation_cancelled",
    )


def _valid_mutation_run_record(task, value):
    expected = task.mutation.as_dict()
    if not isinstance(value, dict) or any(
        value.get(key) != expected_value
        for key, expected_value in expected.items()
    ):
        return False
    classification = value.get("classification")
    hit_cases = value.get("hit_cases")
    hit_cases_total = value.get("hit_cases_total")
    diagnostic = value.get("diagnostic")
    return bool(
        classification in {*_MUTATION_CLASSIFICATIONS, "cancelled"}
        and isinstance(hit_cases, list)
        and isinstance(hit_cases_total, int)
        and not isinstance(hit_cases_total, bool)
        and hit_cases_total >= len(hit_cases)
        and isinstance(value.get("hit_cases_truncated"), bool)
        and isinstance(diagnostic, dict)
        and isinstance(diagnostic.get("code"), str)
    )


def _execute_mutation_task(task, cancel_check):
    worker = Path(task.worker_root)
    phase = "prepare"
    try:
        if worker.exists():
            shutil.rmtree(worker)
        shutil.copytree(task.baseline, worker)
        source_relative = Path(*PurePosixPath(task.source_relative).parts)
        (worker / source_relative).write_text(
            apply_mutation(task.source, task.mutation),
            encoding="utf-8",
            newline="",
        )
        config_copy = copy.deepcopy(task.config)
        config_copy["solutions"] = {
            "accepted": [{
                "file": task.source_relative,
                "expected": {"status": "AC", "all": True},
            }],
            "brute": [],
            "wrong": [],
        }
        write_yaml(worker / "probhub.yaml", config_copy)
        remaining = (
            None if task.deadline is None
            else float(task.deadline) - time.monotonic()
        )
        judge_timeout = task.execution_profile["mutant_timeout_seconds"]
        if remaining is not None:
            if remaining <= 0:
                raise ProbHubError(
                    "mutation testing deadline exceeded",
                    code="mutation_timeout",
                )
            judge_timeout = max(0.001, min(judge_timeout, remaining))

        def judge_cancel_check():
            return bool(
                cancel_check()
                or (
                    task.deadline is not None
                    and time.monotonic() >= float(task.deadline)
                )
            )

        phase = "judge"
        judged = judge_problem(
            task.execution_root,
            worker,
            use_cache=task.use_cache,
            timeout=judge_timeout,
            output_limit_bytes=task.execution_profile["judge_output_limit_bytes"],
            memory_limit_mb=task.execution_profile["judge_memory_limit_mb"],
            process_limit=task.execution_profile["judge_process_limit"],
            cancel_check=judge_cancel_check,
        )
        if cancel_check():
            raise ProcessCancelled("mutation worker was cancelled")
        if task.deadline is not None and time.monotonic() >= float(task.deadline):
            raise ProbHubError(
                "mutation testing deadline exceeded",
                code="mutation_timeout",
            )
        if (
            remaining is not None
            and remaining < task.execution_profile["mutant_timeout_seconds"]
            and (judged.get("final") or {}).get("code") == "judge_timeout"
        ):
            raise ProbHubError(
                "mutation testing deadline exceeded",
                code="mutation_timeout",
            )
        classification, hits, diagnostic = _classify_judge_result(
            judged, task.source_relative
        )
        return _mutation_result_record(
            task.mutation,
            classification,
            hits,
            diagnostic,
        )
    except ProcessCancelled:
        raise
    except ProbHubError as exc:
        if exc.code in {"mutation_cancelled", "mutation_timeout"}:
            raise
        return _mutation_result_record(
            task.mutation,
            "infrastructure-failed",
            diagnostic=exc.code or (
                "mutation_worker_prepare_failed"
                if phase == "prepare" else "mutation_judge_failed"
            ),
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return _mutation_result_record(
            task.mutation,
            "infrastructure-failed",
            diagnostic=(
                "mutation_worker_prepare_failed"
                if phase == "prepare" else "mutation_judge_failed"
            ),
        )


def _cleanup_mutation_worker(path, *, timeout=2.0):
    path = Path(path)
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while True:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        else:
            return True


def _mutation_worker_entry(task, cancel_event, connection):
    try:
        result = _execute_mutation_task(task, cancel_event.is_set)
    except ProcessCancelled as exc:
        result = _cancelled_mutation_record(
            task, "mutation_cancelled", str(exc)
        )
    except ProbHubError as exc:
        if exc.code in {"mutation_cancelled", "mutation_timeout"}:
            result = _cancelled_mutation_record(task, exc.code, str(exc))
        else:
            result = _mutation_result_record(
                task.mutation,
                "infrastructure-failed",
                diagnostic=exc.code or "mutation_worker_failed",
            )
    except BaseException as exc:
        result = _mutation_result_record(
            task.mutation,
            "infrastructure-failed",
            diagnostic=f"mutation_worker_{type(exc).__name__}",
        )
    if not _cleanup_mutation_worker(task.worker_root):
        result = _mutation_result_record(
            task.mutation,
            "infrastructure-failed",
            diagnostic="mutation_worker_cleanup_failed",
        )
    if result["classification"] == "infrastructure-failed":
        cancel_event.set()
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


class _MultiprocessingProcessAdapter:
    def __init__(self, process):
        self._process = process
        self.pid = process.pid

    def wait(self, timeout=None):
        self._process.join(timeout=timeout)
        if self._process.is_alive():
            raise subprocess.TimeoutExpired("mutation worker", timeout)
        return self._process.exitcode

    def kill(self):
        self._process.kill()


def _force_stop_mutation_worker(process, known_pids):
    if process.pid and process.is_alive():
        known_pids.update(snapshot_process_tree(process.pid))
    if process.is_alive():
        terminate_external_process_tree(
            _MultiprocessingProcessAdapter(process), known_pids
        )
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)
    stopped = not process.is_alive()
    if stopped:
        try:
            process.close()
        except (OSError, ValueError):
            pass
    return stopped


def _run_serial_mutations(tasks, *, cancel_check, deadline):
    results = []
    stop_code = None
    check = cancellation_requested if cancel_check is None else cancel_check
    for task in tasks:
        _control_check(cancel_check, deadline)
        try:
            result = _execute_mutation_task(task, check)
        except ProcessCancelled as exc:
            _control_check(cancel_check, deadline)
            raise ProbHubError(
                "mutation testing was cancelled",
                code="mutation_cancelled",
            ) from exc
        finally:
            cleaned = _cleanup_mutation_worker(task.worker_root)
        if not cleaned:
            result = _mutation_result_record(
                task.mutation,
                "infrastructure-failed",
                diagnostic="mutation_worker_cleanup_failed",
            )
        results.append(result)
        if result["classification"] == "infrastructure-failed":
            stop_code = "infrastructure-failed"
            break
    for task in tasks[len(results):]:
        results.append(_cancelled_mutation_record(
            task,
            stop_code or "mutation_not_started",
            "mutation was not started after scheduler cancellation",
        ))
    return {"results": results, "stop_code": stop_code}


def _run_parallel_mutations(
    tasks,
    *,
    jobs,
    cancel_check,
    deadline,
    cancel_grace=MUTATION_CANCEL_GRACE_SECONDS,
    worker_target=_mutation_worker_entry,
):
    context = multiprocessing.get_context("spawn")
    cancel_event = context.Event()
    active = {}
    results = {}
    next_task = 0
    stop_code = None
    cancellation_started = None
    check = cancellation_requested if cancel_check is None else cancel_check

    def begin_stop(code):
        nonlocal stop_code, cancellation_started
        if stop_code is None:
            stop_code = code
            cancellation_started = time.monotonic()
            cancel_event.set()

    def launch(task):
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=worker_target,
            args=(task, cancel_event, child),
            name=f"probhub-mutation-{task.index}",
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            try:
                process.close()
            except (OSError, ValueError):
                pass
            raise
        child.close()
        active[task.index] = (process, parent, task, {process.pid})

    try:
        while active or (next_task < len(tasks) and stop_code is None):
            if stop_code is None:
                if check():
                    begin_stop("mutation_cancelled")
                elif deadline is not None and time.monotonic() >= float(deadline):
                    begin_stop("mutation_timeout")
                elif cancel_event.is_set():
                    begin_stop("infrastructure-failed")

            made_progress = False
            for index in sorted(tuple(active)):
                process, connection, task, known_pids = active[index]
                if process.pid and process.is_alive():
                    known_pids.update(snapshot_process_tree(process.pid))
                if connection.poll():
                    try:
                        result = connection.recv()
                    except (EOFError, OSError):
                        result = _mutation_result_record(
                            task.mutation,
                            "infrastructure-failed",
                            diagnostic="mutation_worker_result_failed",
                        )
                    if not _valid_mutation_run_record(task, result):
                        result = _mutation_result_record(
                            task.mutation,
                            "infrastructure-failed",
                            diagnostic="mutation_worker_result_invalid",
                        )
                    connection.close()
                    process.join(timeout=1.0)
                    if process.is_alive():
                        _force_stop_mutation_worker(process, known_pids)
                        result = _mutation_result_record(
                            task.mutation,
                            "infrastructure-failed",
                            diagnostic="mutation_worker_did_not_exit",
                        )
                    else:
                        try:
                            process.close()
                        except (OSError, ValueError):
                            pass
                    if not _cleanup_mutation_worker(task.worker_root):
                        result = _mutation_result_record(
                            task.mutation,
                            "infrastructure-failed",
                            diagnostic="mutation_worker_cleanup_failed",
                        )
                    results[index] = result
                    del active[index]
                    made_progress = True
                    if result.get("classification") == "infrastructure-failed":
                        begin_stop("infrastructure-failed")
                    elif result.get("classification") == "cancelled" and stop_code is None:
                        diagnostic = (result.get("diagnostic") or {}).get("code")
                        begin_stop(
                            "mutation_timeout"
                            if diagnostic == "mutation_timeout"
                            else "infrastructure-failed"
                        )
                elif not process.is_alive():
                    connection.close()
                    process.join(timeout=0.5)
                    try:
                        process.close()
                    except (OSError, ValueError):
                        pass
                    _cleanup_mutation_worker(task.worker_root)
                    results[index] = _mutation_result_record(
                        task.mutation,
                        "infrastructure-failed",
                        diagnostic="mutation_worker_exited",
                    )
                    del active[index]
                    made_progress = True
                    begin_stop("infrastructure-failed")

            now = time.monotonic()
            if (
                stop_code is not None
                and active
                and cancellation_started is not None
                and now - cancellation_started >= cancel_grace
            ):
                for index in sorted(tuple(active)):
                    process, connection, task, known_pids = active.pop(index)
                    connection.close()
                    _force_stop_mutation_worker(process, known_pids)
                    _cleanup_mutation_worker(task.worker_root)
                    results[index] = _cancelled_mutation_record(
                        task,
                        stop_code,
                        "worker terminated after scheduler cancellation",
                    )
                    made_progress = True

            while (
                stop_code is None
                and not cancel_event.is_set()
                and len(active) < jobs
                and next_task < len(tasks)
            ):
                task = tasks[next_task]
                try:
                    launch(task)
                except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
                    results[task.index] = _mutation_result_record(
                        task.mutation,
                        "infrastructure-failed",
                        diagnostic="mutation_worker_start_failed",
                    )
                    next_task += 1
                    begin_stop("infrastructure-failed")
                    made_progress = True
                    break
                next_task += 1
                made_progress = True

            if not made_progress and (active or next_task < len(tasks)):
                time.sleep(0.01)
    finally:
        cancel_event.set()
        for process, connection, task, known_pids in active.values():
            connection.close()
            _force_stop_mutation_worker(process, known_pids)
            _cleanup_mutation_worker(task.worker_root)

    for task in tasks[next_task:]:
        results[task.index] = _cancelled_mutation_record(
            task,
            stop_code or "mutation_not_started",
            "mutation was not started after scheduler cancellation",
        )
    ordered = [results[task.index] for task in tasks]
    if stop_code == "mutation_cancelled":
        raise ProbHubError(
            "mutation testing was cancelled",
            code="mutation_cancelled",
        )
    if stop_code == "mutation_timeout":
        raise ProbHubError(
            "mutation testing deadline exceeded",
            code="mutation_timeout",
        )
    return {"results": ordered, "stop_code": stop_code}


def mutation_test_problem(
    root,
    problem_dir,
    *,
    use_cache=True,
    operators=None,
    max_mutants=MAX_MUTANTS,
    jobs=1,
    cancel_check=None,
    deadline=None,
):
    root = Path(root).resolve()
    problem_dir = Path(problem_dir).resolve()
    if (
        not isinstance(jobs, int)
        or isinstance(jobs, bool)
        or jobs not in {1, MUTATION_MAX_JOBS}
    ):
        raise ProbHubError(
            f"mutation jobs must be 1 or {MUTATION_MAX_JOBS}",
            code="mutation_jobs_invalid",
        )
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
        execution_profile = _mutation_execution_profile(
            config,
            requested_jobs=jobs,
            selected=plan["selected"],
        )
        # The strict source fence above already validated the configured path.
        # Reuse that logical path instead of deriving it from resolved physical
        # paths, which may mix Windows long and 8.3 aliases for the same tree.
        source_relative = Path(*PurePosixPath(
            str(accepted[0]["file"]).replace("\\", "/")
        ).parts)
        mutant_name = source_relative.as_posix()
        execution_root.mkdir()
        effective_jobs = execution_profile["effective_jobs"]
        tasks = tuple(
            _MutationWorkerTask(
                index=index,
                mutation=mutation,
                baseline=str(baseline),
                execution_root=str(execution_root),
                worker_root=str(
                    execution_root / (
                        "problem"
                        if effective_jobs <= 1
                        else f"problem-{index:04d}"
                    )
                ),
                source=source,
                source_relative=mutant_name,
                config=config,
                use_cache=use_cache,
                execution_profile=execution_profile,
                deadline=deadline,
            )
            for index, mutation in enumerate(plan["mutations"])
        )
        scheduled = (
            _run_parallel_mutations(
                tasks,
                jobs=effective_jobs,
                cancel_check=cancel_check,
                deadline=deadline,
            )
            if effective_jobs > 1
            else _run_serial_mutations(
                tasks,
                cancel_check=cancel_check,
                deadline=deadline,
            )
        )
        run_results = scheduled["results"]
        results = [
            item for item in run_results
            if item["classification"] != "cancelled"
        ]

        _control_check(cancel_check, deadline)
        counts = {name: sum(
            item["classification"] == name for item in results
        ) for name in _MUTATION_CLASSIFICATION_ORDER}
        status = (
            "infrastructure-failed"
            if scheduled["stop_code"] or counts["infrastructure-failed"]
            else "passed"
        )
        run_counts = {
            name: sum(item["classification"] == name for item in run_results)
            for name in (*_MUTATION_CLASSIFICATION_ORDER, "cancelled")
        }
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

    result = {
        "ok": status == "passed",
        "status": status,
        "evidence": evidence,
        "execution": {
            "requested_jobs": execution_profile["requested_jobs"],
            "effective_jobs": execution_profile["effective_jobs"],
            "stop_code": scheduled["stop_code"],
            "summary": run_counts,
        },
    }
    if status != "passed":
        result["execution"]["mutations"] = run_results
    return result


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
