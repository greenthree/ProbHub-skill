"""Recipe-driven secret data generation for `probhub gen`.

Secret test cases stop being opaque bytes: each case declares a recipe in
`data.recipes` — either `manual: true` (bytes are the source of truth) or a
generator invocation (`generator` + exact `args`). `gen` compiles the
generator, runs the recipe, validates the input, produces the answer with
the first accepted solution, and reports new/changed/unchanged per case.
Nothing is written unless every case succeeds and `--apply` is set.

Outputs are normalised to LF so recipes reproduce byte-identical data on
Windows and Linux (matching DOMjudge expectations).
"""

import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path

from .errors import ProbHubError
from .io import atomic_write_json
from .stressing import _prepare_program, _run

CASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TOOL_TIMEOUT_SECONDS = 60.0
TOOL_MEMORY_LIMIT_MB = 2048
GEN_MANIFEST_SCHEMA_VERSION = 1
GEN_MANIFEST_PATH = Path(".probhub/gen-manifest.json")


def _normalize_newlines(payload):
    return payload.replace(b"\r\n", b"\n")


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def problem_recipes(config):
    """Parse and validate data.recipes; raises ProbHubError on bad structure."""
    data = config.get("data") or {}
    raw = data.get("recipes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProbHubError("data.recipes must be a list", code="invalid_recipes")
    recipes = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProbHubError(
                f"data.recipes entries must be mappings, got: {item!r}",
                code="invalid_recipes",
            )
        case = item.get("case")
        if not isinstance(case, str) or not CASE_NAME_PATTERN.match(case):
            raise ProbHubError(
                f"invalid recipe case name: {case!r}",
                code="invalid_recipes",
            )
        if case in seen:
            raise ProbHubError(
                f"duplicate recipe case: {case}",
                code="invalid_recipes",
            )
        seen.add(case)
        if item.get("manual"):
            if item.get("generator") or item.get("args"):
                raise ProbHubError(
                    f"manual recipe must not define generator/args: {case}",
                    code="invalid_recipes",
                )
            recipes.append({"case": case, "manual": True})
            continue
        args = item.get("args")
        if args is None:
            args = []
        if isinstance(args, (str, int, float)):
            args = [args]
        if not isinstance(args, list):
            raise ProbHubError(
                f"recipe args must be a list for case {case}",
                code="invalid_recipes",
            )
        generator = item.get("generator")
        if generator is not None and not isinstance(generator, str):
            raise ProbHubError(
                f"recipe generator must be a path string for case {case}",
                code="invalid_recipes",
            )
        recipes.append({
            "case": case,
            "manual": False,
            "generator": generator,
            "args": [str(value) for value in args],
        })
    return recipes


def recipe_coverage(problem_dir, config):
    """Return (recipes, uncovered_secret_cases) for lint-level reporting."""
    recipes = problem_recipes(config)
    covered = {recipe["case"] for recipe in recipes}
    secret_dir = problem_dir / ((config.get("data") or {}).get("secret_dir", "data/secret"))
    uncovered = []
    if secret_dir.is_dir():
        for path in sorted(secret_dir.glob("*.in")):
            if path.stem not in covered:
                uncovered.append(path.stem)
    return recipes, uncovered


def _resolve_inside(problem_dir, relative, label):
    if not relative:
        raise ProbHubError(f"{label} is not configured", code="gen_config")
    candidate = (problem_dir / relative).resolve()
    try:
        candidate.relative_to(Path(problem_dir).resolve())
    except ValueError as exc:
        raise ProbHubError(
            f"{label} must stay inside the problem directory: {relative}",
            code="gen_config",
        ) from exc
    if not candidate.is_file():
        raise ProbHubError(f"{label} not found: {relative}", code="gen_config")
    return candidate


def _first_accepted(config):
    entries = (config.get("solutions") or {}).get("accepted") or []
    if not isinstance(entries, list):
        entries = [entries]
    for entry in entries:
        value = entry.get("file") if isinstance(entry, dict) else entry
        if value:
            return value
    raise ProbHubError(
        "gen requires at least one accepted solution to produce answers",
        code="gen_config",
    )


def _atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_problem_data(problem_dir, config, *, apply_changes=False, only=None):
    problem_dir = Path(problem_dir).resolve()
    data_config = config.get("data") or {}
    secret_dir = problem_dir / data_config.get("secret_dir", "data/secret")
    recipes = problem_recipes(config)
    if not recipes:
        raise ProbHubError(
            "no data.recipes configured; declare recipes before running gen",
            code="no_recipes",
        )
    only_set = set(only or [])
    known = {recipe["case"] for recipe in recipes}
    unknown = sorted(only_set - known)
    if unknown:
        raise ProbHubError(
            "unknown recipe case(s): " + ", ".join(unknown),
            code="gen_config",
        )

    limits = config.get("limits") or {}
    time_limit = limits.get("time") if isinstance(limits.get("time"), int) else 1
    output_limit = limits.get("output", 64)
    if not isinstance(output_limit, int) or output_limit <= 0:
        output_limit = 64
    answer_timeout = max(30.0, 10.0 * float(time_limit))

    validator_rel = (config.get("judge") or {}).get("validator")
    validator_source = _resolve_inside(problem_dir, validator_rel, "judge.validator")
    accepted_rel = _first_accepted(config)
    accepted_source = _resolve_inside(problem_dir, accepted_rel, "accepted solution")
    default_generator = next(iter(config.get("generators") or []), None)

    selected = [
        recipe for recipe in recipes
        if not only_set or recipe["case"] in only_set
    ]

    results = []
    failures = []
    warnings = []
    with tempfile.TemporaryDirectory(prefix="probhub-gen-") as temp:
        build_dir = Path(temp) / "build"
        build_dir.mkdir()
        validator_cmd, _ = _prepare_program(validator_source, build_dir, "validator")
        accepted_cmd, _ = _prepare_program(accepted_source, build_dir, "accepted")
        generator_commands = {}

        def generator_command(relative):
            resolved = _resolve_inside(problem_dir, relative, "generator")
            key = str(resolved)
            if key not in generator_commands:
                role = f"generator{len(generator_commands)}"
                generator_commands[key] = _prepare_program(resolved, build_dir, role)[0]
            return generator_commands[key]

        for recipe in selected:
            case = recipe["case"]
            input_path = secret_dir / f"{case}.in"
            answer_path = secret_dir / f"{case}.ans"
            if recipe["manual"]:
                state = "manual"
                if not input_path.is_file() or not answer_path.is_file():
                    state = "manual-missing"
                    warnings.append(f"manual recipe has no data files yet: {case}")
                results.append({"case": case, "state": state, "manual": True})
                continue

            generator_rel = recipe["generator"] or default_generator
            if not generator_rel:
                failures.append({
                    "case": case,
                    "stage": "config",
                    "message": "recipe has no generator and the problem declares none",
                })
                continue
            try:
                command = generator_command(generator_rel)
            except ProbHubError as exc:
                failures.append({"case": case, "stage": "config", "message": str(exc)})
                continue

            generated = _run(
                command + recipe["args"],
                b"",
                TOOL_TIMEOUT_SECONDS,
                problem_dir,
                memory_limit=TOOL_MEMORY_LIMIT_MB,
                output_limit=max(output_limit * 2, 64),
            )
            if generated["status"] != "AC":
                failures.append({
                    "case": case,
                    "stage": "generator",
                    "status": generated["status"],
                    "message": generated["message"]
                    or generated["stderr"].decode("utf-8", errors="replace")[:400],
                })
                continue
            input_bytes = _normalize_newlines(generated["stdout"])

            validated = _run(
                validator_cmd,
                input_bytes,
                TOOL_TIMEOUT_SECONDS,
                problem_dir,
                memory_limit=TOOL_MEMORY_LIMIT_MB,
                output_limit=8,
            )
            if validated["status"] != "AC":
                failures.append({
                    "case": case,
                    "stage": "validator",
                    "status": validated["status"],
                    "message": validated["message"]
                    or validated["stderr"].decode("utf-8", errors="replace")[:400],
                })
                continue

            memory_limit = limits.get("memory")
            if not isinstance(memory_limit, int) or isinstance(memory_limit, bool):
                memory_limit = 256
            answered = _run(
                accepted_cmd,
                input_bytes,
                answer_timeout,
                problem_dir,
                memory_limit=max(TOOL_MEMORY_LIMIT_MB, 2 * memory_limit),
                output_limit=output_limit,
            )
            if answered["status"] != "AC":
                failures.append({
                    "case": case,
                    "stage": "accepted",
                    "status": answered["status"],
                    "message": answered["message"]
                    or answered["stderr"].decode("utf-8", errors="replace")[:400],
                })
                continue
            answer_bytes = _normalize_newlines(answered["stdout"])

            def file_state(path, payload):
                if not path.is_file():
                    return "new", None
                current = path.read_bytes()
                if _normalize_newlines(current) == payload:
                    return "unchanged", _sha256(current)
                return "changed", _sha256(current)

            input_state, old_input_hash = file_state(input_path, input_bytes)
            answer_state, old_answer_hash = file_state(answer_path, answer_bytes)
            if input_state == "unchanged" and answer_state == "unchanged":
                state = "unchanged"
            elif "changed" in (input_state, answer_state):
                state = "changed"
            else:
                state = "new"
            results.append({
                "case": case,
                "state": state,
                "manual": False,
                "generator": generator_rel,
                "args": recipe["args"],
                "input": {
                    "state": input_state,
                    "bytes": len(input_bytes),
                    "sha256": _sha256(input_bytes),
                    "previous_sha256": old_input_hash,
                },
                "answer": {
                    "state": answer_state,
                    "bytes": len(answer_bytes),
                    "sha256": _sha256(answer_bytes),
                    "previous_sha256": old_answer_hash,
                },
                "_input_bytes": input_bytes,
                "_answer_bytes": answer_bytes,
            })

        _, uncovered = recipe_coverage(problem_dir, config)
        if not only_set:
            for stem in uncovered:
                warnings.append(f"secret case has no recipe: {stem}")
        total_cases = len(recipes) + len(uncovered)
        if total_cases < 20:
            warnings.append(
                f"secret test count {total_cases} is below the strength discipline "
                "(references/mistake-taxonomy.md: >=20 multi-case / >=30 single-case files)"
            )

        pending = [item for item in results if item.get("state") in {"new", "changed"}]
        applied = False
        if failures:
            for item in results:
                item.pop("_input_bytes", None)
                item.pop("_answer_bytes", None)
            return {
                "ok": False,
                "code": "gen_failed",
                "applied": False,
                "results": results,
                "failures": failures,
                "warnings": warnings,
            }

        if apply_changes and pending:
            for item in pending:
                _atomic_write_bytes(secret_dir / f"{item['case']}.in", item["_input_bytes"])
                _atomic_write_bytes(secret_dir / f"{item['case']}.ans", item["_answer_bytes"])
            applied = True
        if apply_changes:
            manifest_cases = {}
            for item in results:
                if item.get("manual"):
                    continue
                manifest_cases[item["case"]] = {
                    "generator": item["generator"],
                    "args": item["args"],
                    "input_sha256": item["input"]["sha256"],
                    "answer_sha256": item["answer"]["sha256"],
                }
            atomic_write_json(problem_dir / GEN_MANIFEST_PATH, {
                "schema_version": GEN_MANIFEST_SCHEMA_VERSION,
                "generator_sources": sorted(
                    str(Path(path).relative_to(problem_dir)) for path in generator_commands
                ),
                "cases": manifest_cases,
            })

    for item in results:
        item.pop("_input_bytes", None)
        item.pop("_answer_bytes", None)
    summary = {
        "recipes": len(recipes),
        "selected": len(selected),
        "new": sum(1 for item in results if item.get("state") == "new"),
        "changed": sum(1 for item in results if item.get("state") == "changed"),
        "unchanged": sum(1 for item in results if item.get("state") == "unchanged"),
        "manual": sum(1 for item in results if item.get("manual")),
        "uncovered": len(uncovered),
    }
    return {
        "ok": True,
        "applied": applied,
        "pending": [item["case"] for item in pending],
        "results": results,
        "failures": [],
        "warnings": warnings,
        "summary": summary,
    }
