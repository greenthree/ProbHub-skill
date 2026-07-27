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
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from .errors import ProbHubError
from .io import atomic_write_json, normalize_newlines as _normalize_newlines
from .process_control import DEFAULT_PROCESS_LIMIT
from .stressing import _prepare_program, _run

CASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TOOL_TIMEOUT_SECONDS = 60.0
TOOL_MEMORY_LIMIT_MB = 2048
GEN_MANIFEST_SCHEMA_VERSION = 1
GEN_MANIFEST_PATH = Path(".probhub/gen-manifest.json")


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
        # Case-insensitive: case names become file names, and Windows file
        # systems would collapse gen01/GEN01 into one file.
        folded = case.casefold()
        if folded in seen:
            raise ProbHubError(
                f"duplicate recipe case (case-insensitive): {case}",
                code="invalid_recipes",
            )
        seen.add(folded)
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


def resolve_data_dir(problem_dir, config, key, default):
    """Resolve data.sample_dir/secret_dir with type and containment checks."""
    relative = (config.get("data") or {}).get(key, default)
    if not isinstance(relative, str) or not relative.strip():
        raise ProbHubError(
            f"data.{key} must be a non-empty path string",
            code="gen_config",
        )
    problem_dir = Path(problem_dir).resolve()
    candidate = (problem_dir / relative).resolve()
    try:
        candidate.relative_to(problem_dir)
    except ValueError as exc:
        raise ProbHubError(
            f"data.{key} must stay inside the problem directory: {relative}",
            code="gen_config",
        ) from exc
    return candidate


def recipe_coverage(problem_dir, config):
    """Return (recipes, uncovered_secret_cases) for lint-level reporting."""
    recipes = problem_recipes(config)
    covered = {recipe["case"] for recipe in recipes}
    secret_dir = resolve_data_dir(problem_dir, config, "secret_dir", "data/secret")
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


def _merged_manifest_cases(problem_dir, recipes, results, warnings):
    """Merge this run's generator-recipe results into the existing manifest.

    A partial `--case` run must not drop provenance recorded for the other
    cases; entries survive only while their case still has a generator recipe.
    """
    declared = {recipe["case"] for recipe in recipes if not recipe.get("manual")}
    merged = {}
    manifest_path = problem_dir / GEN_MANIFEST_PATH
    if manifest_path.is_file():
        existing = None
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            warnings.append(
                f"existing gen manifest is unreadable and will be rebuilt "
                f"from this run only: {exc}"
            )
        if existing is not None:
            if (
                not isinstance(existing, dict)
                or existing.get("schema_version") != GEN_MANIFEST_SCHEMA_VERSION
                or not isinstance(existing.get("cases"), dict)
            ):
                warnings.append(
                    "existing gen manifest has an unexpected schema and will be "
                    "rebuilt from this run only"
                )
            else:
                for case, entry in existing["cases"].items():
                    if case in declared and isinstance(entry, dict):
                        merged[case] = entry
    for item in results:
        if item.get("manual"):
            continue
        merged[item["case"]] = {
            "generator": item["generator"],
            "args": item["args"],
            "input_sha256": item["input"]["sha256"],
            "answer_sha256": item["answer"]["sha256"],
        }
    return merged


def generate_problem_data(problem_dir, config, *, apply_changes=False, only=None):
    problem_dir = Path(problem_dir).resolve()
    judge_type = str(((config.get("judge") or {}).get("type", "standard"))).strip().lower()
    if judge_type == "interactive":
        raise ProbHubError(
            "probhub gen does not support interactive problems: answers are defined by "
            "the interactor protocol, not by piping inputs into an accepted solution",
            code="gen_unsupported",
        )
    secret_dir = resolve_data_dir(problem_dir, config, "secret_dir", "data/secret")
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
    raw_time = limits.get("time")
    time_limit = raw_time if isinstance(raw_time, int) and not isinstance(raw_time, bool) else 1
    output_limit = limits.get("output", 64)
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or output_limit <= 0:
        output_limit = 64
    process_limit = limits.get("processes", DEFAULT_PROCESS_LIMIT)
    if isinstance(process_limit, bool) or not isinstance(process_limit, int) or process_limit <= 0:
        process_limit = DEFAULT_PROCESS_LIMIT
    # Answers may take longer than the contest TL, but a typo like time: 2000
    # (milliseconds written where seconds belong) must not hang gen for hours.
    answer_timeout = min(600.0, max(30.0, 10.0 * float(time_limit)))
    tool_timeout = (config.get("data") or {}).get("gen_tool_timeout", TOOL_TIMEOUT_SECONDS)
    if (
        isinstance(tool_timeout, bool)
        or not isinstance(tool_timeout, (int, float))
        or not (0 < float(tool_timeout) <= 3600)
    ):
        raise ProbHubError(
            "data.gen_tool_timeout must be a positive number of seconds (at most 3600)",
            code="gen_config",
        )
    tool_timeout = float(tool_timeout)

    first_generator = next(iter(config.get("generators") or []), None)
    if isinstance(first_generator, dict):
        first_generator = first_generator.get("file")
    default_generator = str(first_generator) if first_generator else None

    selected = [
        recipe for recipe in recipes
        if not only_set or recipe["case"] in only_set
    ]

    results = []
    failures = []
    warnings = []
    needs_programs = any(not recipe["manual"] for recipe in selected)
    validator_source = accepted_source = None
    if needs_programs:
        # Only generator recipes need the toolchain; an all-manual run must not
        # demand a resolvable validator or accepted solution.
        validator_rel = (config.get("judge") or {}).get("validator")
        validator_source = _resolve_inside(problem_dir, validator_rel, "judge.validator")
        accepted_rel = _first_accepted(config)
        accepted_source = _resolve_inside(problem_dir, accepted_rel, "accepted solution")
    with tempfile.TemporaryDirectory(prefix="probhub-gen-") as temp:
        build_dir = Path(temp) / "build"
        build_dir.mkdir()
        validator_cmd = accepted_cmd = None
        if needs_programs:
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
                stage = "compile" if getattr(exc, "code", None) == "compile_failed" else "config"
                failures.append({"case": case, "stage": stage, "message": str(exc)})
                continue

            generated = _run(
                command + recipe["args"],
                b"",
                tool_timeout,
                problem_dir,
                memory_limit=TOOL_MEMORY_LIMIT_MB,
                output_limit=max(output_limit * 2, 64),
                process_limit=process_limit,
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
                tool_timeout,
                problem_dir,
                memory_limit=TOOL_MEMORY_LIMIT_MB,
                output_limit=8,
                process_limit=process_limit,
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
                process_limit=process_limit,
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
                # Exact bytes: "unchanged" is the byte-consistency guarantee, so a
                # CRLF file on disk counts as changed even if it normalises equal.
                if current == payload:
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
        manifest_written = False
        if failures:
            for item in results:
                item.pop("_input_bytes", None)
                item.pop("_answer_bytes", None)
            return {
                "ok": False,
                "code": "gen_failed",
                "applied": False,
                "manifest_written": False,
                "results": results,
                "failures": failures,
                "warnings": warnings,
            }

        if apply_changes and pending:
            staged = []
            try:
                try:
                    secret_dir.mkdir(parents=True, exist_ok=True)
                    for item in pending:
                        for suffix, payload in (
                            (".in", item["_input_bytes"]),
                            (".ans", item["_answer_bytes"]),
                        ):
                            final = secret_dir / f"{item['case']}{suffix}"
                            temporary = final.with_name(final.name + f".{uuid.uuid4().hex}.tmp")
                            temporary.write_bytes(payload)
                            staged.append((temporary, final))
                    # Every payload is staged before the first replace, so a
                    # failure cannot leave a half-updated .in/.ans pair behind.
                    for temporary, final in staged:
                        os.replace(temporary, final)
                except OSError as exc:
                    raise ProbHubError(
                        f"failed to write generated data: {exc}",
                        code="gen_write_failed",
                    ) from exc
            finally:
                for temporary, _ in staged:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            applied = True
        if apply_changes:
            manifest_cases = _merged_manifest_cases(problem_dir, recipes, results, warnings)
            atomic_write_json(problem_dir / GEN_MANIFEST_PATH, {
                "schema_version": GEN_MANIFEST_SCHEMA_VERSION,
                "generator_sources": sorted({
                    str(entry.get("generator")).replace("\\", "/")
                    for entry in manifest_cases.values()
                    if entry.get("generator")
                }),
                "cases": manifest_cases,
            })
            manifest_written = True

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
        "manifest_written": manifest_written,
        "pending": [item["case"] for item in pending],
        "results": results,
        "failures": [],
        "warnings": warnings,
        "summary": summary,
    }
