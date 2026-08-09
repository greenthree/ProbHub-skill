"""Conservative C++ mutation planning and isolated execution.

Mutation testing is evidence about the current data, not a proof that no
unknown wrong solution exists.  Mutants are always built and judged from a
temporary problem snapshot; the live workspace is only read and receives a
local evidence file after a complete successful run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .build_lock import workspace_build_lock, workspace_file_lock
from .errors import ProbHubError
from .io import atomic_write_json, read_yaml, write_yaml
from .judging import JUDGE_TIMEOUT_SECONDS, judge_problem
from .linting import compute_data_hash, compute_source_hash
from .process_control import run_managed_to_files
from .solutions import (
    _mask_cpp_non_code,
    normalize_solution_entries,
    resolve_solution_source,
)


MUTATION_EVIDENCE_SCHEMA_VERSION = 1
MUTATION_OPERATOR_VERSION = "cpp-token-v1"
MUTATION_EVIDENCE_FILENAME = "mutation-evidence-v1.json"
MUTATION_LOCK_FILENAME = ".probhub/mutation.lock"
MAX_MUTANTS = 256
MAX_DIAGNOSTIC_BYTES = 2048
MAX_MUTATION_TEXT_BYTES = 512
MAX_HIT_CASES = 16
MAX_HIT_FIELD_BYTES = 256
MAX_MUTATION_EVIDENCE_BYTES = 4 * 1024 * 1024
_MUTATION_CLASSIFICATIONS = frozenset({
    "killed",
    "survived",
    "compile-invalid",
    "infrastructure-failed",
})
MUTATION_OPERATORS = (
    "comparison-boundary",
    "boolean-negation",
    "integer-boundary",
)


@dataclass(frozen=True)
class CppToken:
    start: int
    end: int
    text: str
    kind: str


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


_TOKEN_RE = re.compile(
    r"(?:"
    r"0[xX][0-9A-Fa-f]+[uUlL]*|"
    r"0[bB][01]+[uUlL]*|"
    r"0[0-7]*[uUlL]*|"
    r"[1-9][0-9]*[uUlL]*|"
    r"[A-Za-z_]\w*|"
    r"==|!=|<=|>=|&&|\|\||\+\+|--|<<|>>|\+=|-=|\*=|/=|%=|->|::|"
    r"[^\s]"
    r")"
)
_INTEGER_RE = re.compile(r"(?:0|[1-9][0-9]*)[uUlL]*$")
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


def _is_directive(source, token):
    offset = token.start() if callable(getattr(token, "start", None)) else token.start
    line_start = source.rfind("\n", 0, offset) + 1
    return source[line_start:offset].lstrip().startswith("#")


def tokenize_cpp(source):
    """Tokenize code while masking comments and literals byte-for-byte.

    The mask comes from the existing source verifier and preserves positions,
    including newlines.  This deliberately is not a C++ parser: unsupported
    constructs simply produce no mutation instead of guessing.
    """
    masked = _mask_cpp_non_code(source)
    tokens = []
    for match in _TOKEN_RE.finditer(masked):
        text = source[match.start():match.end()]
        if not text or text.isspace() or _is_directive(source, match):
            continue
        kind = "identifier"
        if _INTEGER_RE.fullmatch(text):
            kind = "integer"
        elif text in _COMPARISON_REPLACEMENTS or text in {"!", "(", ")", ";"}:
            kind = "operator" if text in _COMPARISON_REPLACEMENTS or text == "!" else "punct"
        tokens.append(CppToken(match.start(), match.end(), text, kind))
    return tokens


def _line_column(source, offset):
    line = source.count("\n", 0, offset) + 1
    previous = source.rfind("\n", 0, offset)
    return line, offset - previous


def _matching_parenthesis(tokens, opening_index):
    depth = 0
    for index in range(opening_index, len(tokens)):
        text = tokens[index].text
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
            if depth == 0:
                return index
            if depth < 0:
                return None
    return None


def _condition_is_mutable(tokens, opening_index, closing_index):
    inner_depth = 0
    for token in tokens[opening_index + 1:closing_index]:
        if token.text in {"(", "[", "{"}:
            inner_depth += 1
        elif token.text in {")", "]", "}"
        }:
            inner_depth = max(0, inner_depth - 1)
        elif inner_depth == 0 and token.text in {";", "="}:
            # Declarations in if-init statements and malformed fragments are
            # intentionally excluded from the first syntax-level slice.
            return False
    return opening_index + 1 < closing_index


def _mutation_candidates(source, operators):
    tokens = tokenize_cpp(source)
    candidates = []
    selected = set(operators)
    if "comparison-boundary" in selected:
        for index, token in enumerate(tokens):
            replacement = _COMPARISON_REPLACEMENTS.get(token.text)
            previous = tokens[index - 1].text if index else None
            if replacement is not None and previous not in {"operator", "template"}:
                candidates.append((
                    token,
                    "comparison-boundary",
                    replacement,
                    f"replace {token.text} with {replacement}",
                ))

    if "integer-boundary" in selected:
        for index, token in enumerate(tokens):
            if token.kind != "integer":
                continue
            adjacent_identifier = (
                index + 1 < len(tokens)
                and tokens[index + 1].start == token.end
                and tokens[index + 1].kind == "identifier"
            )
            if adjacent_identifier:
                continue
            previous = tokens[index - 1].text if index else None
            following = tokens[index + 1].text if index + 1 < len(tokens) else None
            if previous not in _COMPARISON_REPLACEMENTS and following not in _COMPARISON_REPLACEMENTS:
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
        for token in tokens:
            if token.text == "!":
                candidates.append((
                    token,
                    "boolean-negation",
                    "",
                    "remove boolean negation",
                ))
        for index, token in enumerate(tokens[:-1]):
            if token.text not in {"if", "while"} or tokens[index + 1].text != "(":
                continue
            closing_index = _matching_parenthesis(tokens, index + 1)
            if closing_index is None or not _condition_is_mutable(tokens, index + 1, closing_index):
                continue
            opening = tokens[index + 1]
            closing = tokens[closing_index]
            candidates.append((
                CppToken(opening.start, closing.end, source[opening.start:closing.end], "condition"),
                "boolean-negation",
                f"(!({source[opening.end:closing.start]}))",
                f"negate {token.text} condition",
            ))
    return candidates


def plan_mutations(source, *, operators=None, max_mutants=MAX_MUTANTS):
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
    candidates = _mutation_candidates(source, selected)
    candidates.sort(key=lambda item: (item[0].start, item[0].end, item[1], item[2]))
    mutations = []
    for token, operator, replacement, description in candidates[:max_mutants]:
        line, column = _line_column(source, token.start)
        identity = hashlib.sha256(
            f"{operator}\0{line}\0{column}\0{token.text}\0{replacement}".encode("utf-8")
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
    mutation_records = [item.as_dict() for item in mutations]
    plan_payload = {
        "operator_version": MUTATION_OPERATOR_VERSION,
        "operators": selected,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "mutations": mutation_records,
    }
    plan_hash = hashlib.sha256(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": MUTATION_EVIDENCE_SCHEMA_VERSION,
        "operator_version": MUTATION_OPERATOR_VERSION,
        "operators": selected,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "mutations": mutations,
        "plan_hash": plan_hash,
        "planned": len(candidates),
        "truncated": len(candidates) > len(mutations),
    }


def apply_mutation(source, mutation):
    return source[:mutation.start] + mutation.replacement + source[mutation.end:]


def _copy_snapshot(source, target):
    source = Path(source).resolve()
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    for current, directories, files in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if name not in {".probhub", "__pycache__", "output_validators"}
            and not (current_path / name).is_symlink()
        ]
        relative = current_path.relative_to(source)
        destination = target / relative
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            source_file = current_path / name
            if source_file.is_symlink():
                continue
            target_file = destination / name
            target_file.write_bytes(source_file.read_bytes())


def _compiler_fingerprint():
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
        "parser": "mask-cpp-non-code+token-regex",
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
        "judge_timeout", "judge_output_limit", "judge_failed", "validator_failed",
        "checker_compile_failed", "interactor_compile_failed", "checker_missing",
        "interactor_missing",
    }:
        return "infrastructure-failed", hits, code
    if hits or code == "expectation_not_met":
        return "killed", hits, code
    return "infrastructure-failed", hits, code or "unknown mutation result"


def _control_check(cancel_check, deadline):
    if cancel_check is not None and cancel_check():
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
    config = read_yaml(problem_dir / "probhub.yaml")
    judge_type = str((config.get("judge") or {}).get("type", "standard")).strip().lower()
    if judge_type not in {"standard", ""}:
        raise ProbHubError(
            "mutation testing currently supports judge.type standard only",
            code="mutation_standard_required",
        )
    accepted = normalize_solution_entries(config)["std"]
    if not accepted or not accepted[0].get("file"):
        raise ProbHubError("mutation testing requires a configured accepted solution", code="mutation_source_missing")
    source_path, source_error, source_message = resolve_solution_source(
        problem_dir, accepted[0]["file"]
    )
    if source_path is None:
        raise ProbHubError(
            f"accepted source is invalid: {source_message or source_error}",
            code="mutation_source_invalid",
        )
    if source_path.suffix.lower() != ".cpp":
        raise ProbHubError("mutation testing currently supports C++ accepted solutions only", code="mutation_cpp_required")
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProbHubError(f"cannot read accepted source: {source_path}: {exc}", code="mutation_source_invalid") from exc
    source_hash = compute_source_hash(problem_dir, config)
    data_hash = compute_data_hash(problem_dir, config)
    plan = plan_mutations(source, operators=operators, max_mutants=max_mutants)
    builder_fingerprint = _compiler_fingerprint()
    if not builder_fingerprint.get("available"):
        raise ProbHubError(
            "mutation testing requires an available g++ C++17 compiler",
            code="mutation_compiler_unavailable",
        )
    results = []
    source_relative = source_path.relative_to(problem_dir)
    mutant_name = source_relative.as_posix()
    evidence_path = mutation_evidence_path(problem_dir)
    with workspace_build_lock(root), workspace_file_lock(
        problem_dir,
        MUTATION_LOCK_FILENAME,
        busy_code="mutation_busy",
        busy_message="another mutation testing run is already running",
        no_follow=True,
    ):
        for mutation in plan["mutations"]:
            _control_check(cancel_check, deadline)
            current_config = read_yaml(problem_dir / "probhub.yaml")
            if (
                compute_source_hash(problem_dir, current_config) != source_hash
                or compute_data_hash(problem_dir, current_config) != data_hash
            ):
                raise ProbHubError("mutation inputs changed during execution", code="inputs_changed")
            with tempfile.TemporaryDirectory(prefix="probhub-mutation-") as temp:
                snapshot = Path(temp) / problem_dir.name
                _copy_snapshot(problem_dir, snapshot)
                source_in_snapshot = snapshot / source_relative
                source_in_snapshot.write_text(
                    apply_mutation(source, mutation), encoding="utf-8", newline=""
                )
                config_copy = copy.deepcopy(config)
                config_copy["solutions"] = {
                    "accepted": [{"file": mutant_name, "expected": {"status": "AC", "all": True}}],
                    "brute": [],
                    "wrong": [],
                }
                write_yaml(snapshot / "probhub.yaml", config_copy)
                _control_check(cancel_check, deadline)
                remaining = None if deadline is None else float(deadline) - time.monotonic()
                judge_timeout = JUDGE_TIMEOUT_SECONDS
                if remaining is not None:
                    if remaining <= 0:
                        raise ProbHubError(
                            "mutation testing deadline exceeded",
                            code="mutation_timeout",
                        )
                    judge_timeout = max(0.001, min(JUDGE_TIMEOUT_SECONDS, remaining))
                try:
                    judged = judge_problem(
                        root,
                        snapshot,
                        use_cache=use_cache,
                        timeout=judge_timeout,
                    )
                    _control_check(cancel_check, deadline)
                    if (
                        remaining is not None
                        and remaining < JUDGE_TIMEOUT_SECONDS
                        and (judged.get("final") or {}).get("code") == "judge_timeout"
                    ):
                        raise ProbHubError(
                            "mutation testing deadline exceeded",
                            code="mutation_timeout",
                        )
                    classification, hits, diagnostic = _classify_judge_result(judged, mutant_name)
                except ProbHubError as exc:
                    if exc.code in {"mutation_cancelled", "mutation_timeout"}:
                        raise
                    classification, hits, diagnostic = "infrastructure-failed", [], str(exc)
                except (OSError, ValueError, TypeError) as exc:
                    classification, hits, diagnostic = "infrastructure-failed", [], str(exc)
                bounded_hits = [
                    {
                        "case": _bounded_optional(item.get("case"), MAX_HIT_FIELD_BYTES),
                        "status": _bounded(item.get("status"), MAX_HIT_FIELD_BYTES),
                        "termination_reason": _bounded_optional(
                            item.get("termination_reason"), MAX_HIT_FIELD_BYTES
                        ),
                        "failure_kind": _bounded_optional(
                            item.get("failure_kind"), MAX_HIT_FIELD_BYTES
                        ),
                    }
                    for item in hits[:MAX_HIT_CASES]
                ]
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
        current_config = read_yaml(problem_dir / "probhub.yaml")
        if (
            compute_source_hash(problem_dir, current_config) != source_hash
            or compute_data_hash(problem_dir, current_config) != data_hash
        ):
            raise ProbHubError("mutation inputs changed during execution", code="inputs_changed")
        counts = {name: sum(item["classification"] == name for item in results) for name in (
            "killed", "survived", "compile-invalid", "infrastructure-failed"
        )}
        status = "infrastructure-failed" if counts["infrastructure-failed"] else "passed"
        evidence = {
            "schema_version": MUTATION_EVIDENCE_SCHEMA_VERSION,
            "status": status,
            "problem_id": config.get("id"),
            "source_path": accepted[0]["file"],
            "source_hash": source_hash,
            "data_hash": data_hash,
            "builder_fingerprint": builder_fingerprint,
            "operator_version": MUTATION_OPERATOR_VERSION,
            "operators": plan["operators"],
            "max_mutants": max_mutants,
            "plan_hash": plan["plan_hash"],
            "planned": plan["planned"],
            "executed": len(results),
            "truncated": plan["truncated"],
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
            atomic_write_json(evidence_path, evidence)
    return {
        "ok": status == "passed",
        "status": status,
        "evidence": evidence,
    }


def mutation_evidence_profile(problem_dir, config):
    path = mutation_evidence_path(problem_dir)
    if not path.is_file():
        return {"state": "missing", "summary": {}, "mutants": 0}
    try:
        if path.stat().st_size > MAX_MUTATION_EVIDENCE_BYTES:
            return {"state": "invalid", "summary": {}, "mutants": 0}
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"state": "invalid", "summary": {}, "mutants": 0}
    if not isinstance(evidence, dict):
        return {"state": "invalid", "summary": {}, "mutants": 0}
    summary = evidence.get("summary")
    mutations = evidence.get("mutations")
    expected_counts = tuple(sorted(_MUTATION_CLASSIFICATIONS))
    if (
        evidence.get("schema_version") != MUTATION_EVIDENCE_SCHEMA_VERSION
        or not isinstance(summary, dict)
        or set(summary) != set(expected_counts)
        or not isinstance(mutations, list)
        or not isinstance(evidence.get("plan_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence["plan_hash"])
        or not isinstance(evidence.get("builder_fingerprint"), dict)
    ):
        return {"state": "invalid", "summary": {}, "mutants": 0}
    if any(not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool) or summary[key] < 0 for key in expected_counts):
        return {"state": "invalid", "summary": {}, "mutants": len(mutations)}
    executed = evidence.get("executed")
    planned = evidence.get("planned")
    max_mutants = evidence.get("max_mutants")
    if (
        not isinstance(executed, int) or isinstance(executed, bool) or executed < 0
        or not isinstance(planned, int) or isinstance(planned, bool) or planned < executed
        or not isinstance(max_mutants, int) or isinstance(max_mutants, bool)
        or not 0 < max_mutants <= MAX_MUTANTS
        or not isinstance(evidence.get("truncated"), bool)
        or sum(summary.values()) != executed
        or len(mutations) != executed
    ):
        return {"state": "invalid", "summary": {}, "mutants": len(mutations)}
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not isinstance(item.get("operator"), str)
        or item.get("operator") not in MUTATION_OPERATORS
        or not isinstance(item.get("line"), int) or item["line"] < 1
        or not isinstance(item.get("column"), int) or item["column"] < 1
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
        return {"state": "invalid", "summary": {}, "mutants": len(mutations)}
    actual_counts = {name: sum(item["classification"] == name for item in mutations) for name in expected_counts}
    if actual_counts != summary:
        return {"state": "invalid", "summary": {}, "mutants": len(mutations)}
    fingerprint = evidence["builder_fingerprint"]
    fingerprint_required = {
        "schema_version", "probhub_version", "compiler", "compiler_identity",
        "compiler_flags", "parser", "operator_version", "digest", "available",
    }
    if (
        set(fingerprint) != fingerprint_required
        or not isinstance(fingerprint["schema_version"], int)
        or not isinstance(fingerprint["probhub_version"], str)
        or fingerprint["compiler"] != "g++"
        or not isinstance(fingerprint["compiler_identity"], str)
        or not isinstance(fingerprint["compiler_flags"], list)
        or not all(isinstance(flag, str) for flag in fingerprint["compiler_flags"])
        or not isinstance(fingerprint["parser"], str)
        or fingerprint["operator_version"] != MUTATION_OPERATOR_VERSION
        or not isinstance(fingerprint["available"], bool)
        or not isinstance(fingerprint["digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint["digest"])
    ):
        return {"state": "invalid", "summary": {}, "mutants": len(mutations)}
    # `_compiler_fingerprint` computes the digest over its descriptive fields
    # before adding the runtime `available` bit.
    fingerprint_payload = {
        key: fingerprint[key]
        for key in fingerprint_required
        if key not in {"digest", "available"}
    }
    expected_digest = hashlib.sha256(json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if expected_digest != fingerprint["digest"]:
        return {"state": "invalid", "summary": {}, "mutants": len(mutations)}
    try:
        current_source = compute_source_hash(problem_dir, config)
        current_data = compute_data_hash(problem_dir, config)
        accepted = normalize_solution_entries(config)["std"]
        if not accepted or not accepted[0].get("file"):
            return {"state": "invalid", "summary": summary, "mutants": len(mutations)}
        source_path, source_error, source_message = resolve_solution_source(problem_dir, accepted[0]["file"])
        if source_path is None:
            return {"state": "invalid", "summary": summary, "mutants": len(mutations)}
        source = source_path.read_text(encoding="utf-8")
        plan = plan_mutations(source, operators=evidence.get("operators"), max_mutants=max_mutants)
    except (OSError, ProbHubError, ValueError, TypeError):
        return {"state": "invalid", "summary": summary, "mutants": len(mutations)}
    if (
        evidence.get("problem_id") != config.get("id")
        or evidence.get("source_path") != accepted[0]["file"]
        or evidence.get("source_hash") != current_source
        or evidence.get("data_hash") != current_data
        or evidence.get("operator_version") != MUTATION_OPERATOR_VERSION
        or evidence.get("operators") != plan["operators"]
        or evidence.get("plan_hash") != plan["plan_hash"]
        or evidence.get("planned") != plan["planned"]
        or evidence.get("truncated") != plan["truncated"]
        or evidence.get("status") != "passed"
    ):
        state = "stale"
    else:
        state = "current" if fingerprint["available"] else "stale"
    return {
        "state": state,
        "status": evidence.get("status"),
        "summary": dict(summary),
        "mutants": len(mutations),
        "operator_version": evidence.get("operator_version"),
    }
