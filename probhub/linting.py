import hashlib
import json
import math
import re
from pathlib import Path

from .builder_fingerprint import (
    BUILD_MANIFEST_SCHEMA_VERSION,
    TYPST_TEXT_TEMPLATE_SUFFIXES,
    builder_fingerprint_stale_fields,
    compute_builder_fingerprint,
    typst_template_paths,
)
from .calibration import evaluate_calibration, validate_calibration_config
from .datagen import recipe_coverage, resolve_data_dir
from .errors import ProbHubError
from .hashing import files_under, hash_file, hash_paths
from .judge_qa import (
    evaluate_judge_qa_evidence,
    inspect_judge_qa,
    judge_fixture_tree_paths,
)
from .metadata import build_meta, normalize_display_name
from .problem_paths import ProblemPathError, resolve_problem_regular_file
from .solutions import analyze_solution_verification
from .statement import parse_statement
from .statement_consistency import analyze_constraint_consistency, reconcile_constraints
from .typesetting import typst_boundary_protocol_supported
from .transactions import pending_workspace_transactions
from .workspace import load_problem, problem_entries

DEFAULT_FORBIDDEN = ("TODO", "FIXME", "114514", "待补充")
TYPST_LENGTH_PATTERN = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)(?:pt|mm|cm|in|em|fr|%)$")
STATEMENT_ASSET_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
CODE_HASH_IGNORED_SUFFIXES = {
    ".a",
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".exp",
    ".ilk",
    ".lib",
    ".o",
    ".obj",
    ".pdb",
    ".pyc",
    ".so",
}
CODE_HASH_IGNORED_DIRS = {
    ".cache",
    ".probhub",
    "__pycache__",
    "build",
    "dist",
}
STATEMENT_ASSET_IGNORED_DIRS = {
    ".preview",
    ".probhub",
    "__pycache__",
    "code",
    "data",
    "judge-fixtures",
    "output_validators",
}


def _problem_relative_path(problem_dir, value):
    if not isinstance(value, str) or not value.strip():
        return None
    problem_dir = Path(problem_dir).resolve()
    candidate = (problem_dir / value).resolve()
    try:
        candidate.relative_to(problem_dir)
    except ValueError:
        return None
    return candidate


def _problem_regular_file_path(problem_dir, value):
    try:
        return resolve_problem_regular_file(problem_dir, value), None
    except ProblemPathError as exc:
        return None, exc.reason


def _configured_file_error(field, value, reason, missing_label):
    if reason == "outside":
        return f"{field} must stay inside the problem directory: {value}"
    if reason == "link":
        return f"{field} must be a regular non-symlink file: {value}"
    if reason == "invalid":
        return f"{field} must be a non-empty relative path"
    return f"{missing_label} not found: {value}"


def problem_statement_asset_paths(problem_dir, *, check=None):
    problem_dir = Path(problem_dir).resolve()
    paths = []
    for path in problem_dir.rglob("*"):
        if check is not None:
            check()
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(problem_dir)
        if any(part in STATEMENT_ASSET_IGNORED_DIRS for part in relative.parts[:-1]):
            continue
        if path.suffix.lower() in STATEMENT_ASSET_SUFFIXES:
            paths.append(path)
    return paths


def compute_statement_assets_hash(problem_dir):
    problem_dir = Path(problem_dir).resolve()
    relative_paths = [
        path.relative_to(problem_dir)
        for path in problem_statement_asset_paths(problem_dir)
    ]
    return hash_paths(problem_dir, relative_paths)[0]


def problem_source_paths(problem_dir, config, *, check=None):
    problem_dir = Path(problem_dir).resolve()
    if check is not None:
        check()
    paths = [problem_dir / "probhub.yaml"]
    statement_config = config.get("statement") or {}
    statement_source = (
        statement_config.get("source", "problem.md")
        if isinstance(statement_config, dict)
        else "problem.md"
    )
    statement_path = _problem_relative_path(problem_dir, statement_source)
    if statement_path:
        paths.append(statement_path)
    judge = config.get("judge") or {}
    for key in ("validator", "checker", "interactor"):
        if judge.get(key):
            path = _problem_relative_path(problem_dir, judge[key])
            if path:
                paths.append(path)
    for generator in config.get("generators") or []:
        path = generator.get("file") if isinstance(generator, dict) else generator
        if path:
            candidate = _problem_relative_path(problem_dir, path)
            if candidate:
                paths.append(candidate)
    solutions = config.get("solutions") or {}
    for value in solutions.values():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            path = entry.get("file") if isinstance(entry, dict) else entry
            if path:
                candidate = _problem_relative_path(problem_dir, path)
                if candidate:
                    paths.append(candidate)
    stress = config.get("stress") or {}
    if isinstance(stress, dict):
        for key in ("generator", "accepted", "brute"):
            candidate = _problem_relative_path(problem_dir, stress.get(key))
            if candidate:
                paths.append(candidate)
    code_dir = problem_dir / "code"
    if code_dir.is_dir():
        for path in code_dir.rglob("*"):
            if check is not None:
                check()
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(
                    part.casefold() in CODE_HASH_IGNORED_DIRS
                    for part in path.relative_to(code_dir).parts[:-1]
                )
                and path.suffix.lower() not in CODE_HASH_IGNORED_SUFFIXES
            ):
                paths.append(path)
    paths.extend(judge_fixture_tree_paths(problem_dir, check=check))
    paths.extend(problem_statement_asset_paths(problem_dir, check=check))
    return paths


def compute_source_hash(problem_dir, config, *, check=None):
    problem_dir = Path(problem_dir).resolve()
    relative_paths = [
        path.relative_to(problem_dir)
        for path in problem_source_paths(problem_dir, config, check=check)
        if path.exists()
    ]
    return hash_paths(problem_dir, relative_paths, check=check)[0]


def compute_workspace_hash(root, workspace):
    root = Path(root).resolve()
    paths = [root / ".probhub/workspace.yaml"]
    try:
        paths.extend(typst_template_paths(root, workspace))
    except ProbHubError:
        # Read-only status still reports source/artifact drift when the builder
        # is unavailable; compute_builder_fingerprint supplies the fail-closed
        # diagnostic and formal writers remain blocked.
        pass
    return hash_paths(
        root,
        [path.relative_to(root) for path in paths if path.exists()],
        normalize_lf_suffixes=TYPST_TEXT_TEMPLATE_SUFFIXES,
    )[0]


def compute_collection_hash(root, workspace, loaded_problems=None):
    snapshot = {
        "workspace_hash": compute_workspace_hash(root, workspace),
        "problems": [],
    }
    loaded = loaded_problems
    if loaded is None:
        loaded = [load_problem(root, entry) for entry in problem_entries(workspace)]
    for problem_dir, config in loaded:
        snapshot["problems"].append({
            "id": config["id"],
            "metadata": build_meta(problem_dir, config),
            "statement_assets_hash": compute_statement_assets_hash(problem_dir),
        })
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_data_hash(problem_dir, config, *, check=None):
    data = config.get("data") or {}
    paths = []
    for key, default in (("sample_dir", "data/sample"), ("secret_dir", "data/secret")):
        paths.extend(files_under(
            problem_dir / data.get(key, default),
            {".in", ".ans"},
            check=check,
        ))
    return hash_paths(
        problem_dir,
        [path.relative_to(problem_dir) for path in paths],
        check=check,
    )[0]


def lint_problem(root, workspace, entry):
    errors, warnings = [], []
    problem_dir, config = load_problem(root, entry)
    name = config.get("name") or config.get("display_name")
    if not name:
        errors.append("missing name/display_name")
    limits = config.get("limits") or {}
    time_limit = limits.get("time")
    memory = limits.get("memory")
    output_limit = limits.get("output", 64)
    process_limit = limits.get("processes", 32)
    if isinstance(time_limit, bool) or not isinstance(time_limit, int) or time_limit <= 0:
        errors.append("limits.time must be a positive integer")
    if not isinstance(memory, int) or memory < 256 or memory & (memory - 1):
        errors.append("limits.memory must be a power of two and at least 256")
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or output_limit <= 0:
        errors.append("limits.output must be a positive integer in MB")
    if isinstance(process_limit, bool) or not isinstance(process_limit, int) or process_limit <= 0:
        errors.append("limits.processes must be a positive integer")
    errors.extend(validate_calibration_config(config))
    parsed = None
    statement_value = config.get("statement")
    statement_config = {} if statement_value is None else statement_value
    if not isinstance(statement_config, dict):
        errors.append("statement must be a mapping")
        source_value = "problem.md"
    else:
        source_value = statement_config.get("source", "problem.md")
    source, source_reason = _problem_regular_file_path(problem_dir, source_value)
    statement_report = {
        "source": source_value,
        "structure": {
            "ok": False,
            "errors": [],
        },
    }
    if source_reason:
        message = _configured_file_error(
            "statement.source",
            source_value,
            source_reason,
            "statement",
        )
        errors.append(message)
        statement_report["structure"]["errors"].append({
            "code": "statement_source_invalid",
            "severity": "error",
            "message": message,
        })
    else:
        try:
            parsed = parse_statement(source, strict=False)
        except Exception as exc:
            errors.append(str(exc))
            statement_report["structure"]["errors"].append({
                "code": "statement_unreadable",
                "severity": "error",
                "message": str(exc),
            })
        else:
            statement_report["structure"] = parsed["structure"]
            errors.extend(issue["message"] for issue in parsed["structure"]["errors"])
        if parsed is not None:
            if parsed["title"] and name and parsed["title"] != name:
                warnings.append(f"statement title '{parsed['title']}' differs from name '{name}'")
            if parsed["unknown_headings"]:
                warnings.append("unknown statement headings: " + ", ".join(parsed["unknown_headings"]))
            forbidden = (workspace.get("lint") or {}).get("forbidden_patterns", list(DEFAULT_FORBIDDEN))
            for pattern in forbidden:
                if re.search(re.escape(str(pattern)), parsed["raw"], re.IGNORECASE):
                    errors.append(f"forbidden pattern in statement: {pattern}")
    judge = config.get("judge") or {}
    judge_type = str(judge.get("type", "standard")).strip().lower()
    if judge_type == "checker":
        judge_type = "custom"
    if judge_type not in {"standard", "custom", "interactive"}:
        errors.append(f"unsupported judge.type: {judge_type}")

    validator = judge.get("validator")
    validator_path = None
    if not validator:
        errors.append("judge.validator is required")
    else:
        validator_path, validator_reason = _problem_regular_file_path(problem_dir, validator)
        if validator_reason:
            errors.append(_configured_file_error(
                "judge.validator",
                validator,
                validator_reason,
                "validator",
            ))

    constraint_reconciliation = reconcile_constraints([], [])
    if parsed is not None and validator_path is not None:
        try:
            validator_text = validator_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            constraint_reconciliation["requires_review"] = True
            constraint_reconciliation["diagnostics"].append({
                "code": "constraint_analysis_unavailable",
                "severity": "info",
                "message": f"Validator source could not be read for constraint review: {exc}",
                "path": str(validator),
            })
        else:
            constraint_reconciliation = analyze_constraint_consistency(
                parsed,
                source_value,
                validator_text,
                validator,
            )
    else:
        constraint_reconciliation["requires_review"] = True
        constraint_reconciliation["diagnostics"].append({
            "code": "constraint_analysis_unavailable",
            "severity": "info",
            "message": "constraint review requires a readable statement and problem-local Validator",
        })
    warnings.extend(
        f"[{diagnostic['code']}] {diagnostic['message']}"
        for diagnostic in constraint_reconciliation["diagnostics"]
        if diagnostic.get("severity") == "warning"
    )

    checker = judge.get("checker")
    interactor = judge.get("interactor")
    if judge_type == "custom":
        if not checker:
            errors.append("judge.checker is required for custom judging")
        else:
            _, checker_reason = _problem_regular_file_path(problem_dir, checker)
            if checker_reason:
                errors.append(_configured_file_error(
                    "judge.checker",
                    checker,
                    checker_reason,
                    "checker",
                ))
        if interactor:
            errors.append("judge.interactor is only valid for interactive judging")
    elif judge_type == "interactive":
        if not interactor:
            errors.append("judge.interactor is required for interactive judging")
        else:
            _, interactor_reason = _problem_regular_file_path(problem_dir, interactor)
            if interactor_reason:
                errors.append(_configured_file_error(
                    "judge.interactor",
                    interactor,
                    interactor_reason,
                    "interactor",
                ))
        if checker:
            errors.append("judge.checker is not used for interactive judging")
        interactive = judge.get("interactive") or {}
        if not isinstance(interactive, dict):
            errors.append("judge.interactive must be a mapping")
        else:
            idle_limit = interactive.get("idle_limit")
            if idle_limit is not None and (not isinstance(idle_limit, (int, float)) or idle_limit <= 0):
                errors.append("judge.interactive.idle_limit must be positive")
            transcript_limit = interactive.get("transcript_limit")
            if transcript_limit is not None and (
                not isinstance(transcript_limit, int) or transcript_limit < 0
            ):
                errors.append("judge.interactive.transcript_limit must be a non-negative integer")
    else:
        if checker:
            errors.append("judge.checker requires judge.type: custom")
        if interactor:
            errors.append("judge.interactor requires judge.type: interactive")
    judge_qa = inspect_judge_qa(problem_dir, config)
    errors.extend(
        f"[{diagnostic['code']}] {diagnostic['message']}"
        for diagnostic in judge_qa["diagnostics"]
    )
    solutions = config.get("solutions") or {}
    configured_programs = set()
    if not isinstance(solutions, dict):
        errors.append("solutions must be a mapping")
        solutions = {}
    for solution_kind in ("accepted", "brute", "wrong"):
        entries = solutions.get(solution_kind) or []
        entries = entries if isinstance(entries, list) else [entries]
        for solution in entries:
            solution_file = solution.get("file") if isinstance(solution, dict) else solution
            if isinstance(solution_file, str) and solution_file.strip():
                configured_programs.add(solution_file.strip().replace("\\", "/"))

    stress = config.get("stress")
    if stress is not None:
        if not isinstance(stress, dict):
            errors.append("stress must be a mapping")
        else:
            if judge_type == "interactive":
                errors.append("stress is not supported for interactive judging")
            generator = stress.get("generator")
            if not generator:
                errors.append("stress.generator is required")
            else:
                generator_path = _problem_relative_path(problem_dir, generator)
                if generator_path is None:
                    errors.append(f"stress.generator must stay inside the problem directory: {generator}")
                elif not generator_path.is_file():
                    errors.append(f"stress generator not found: {generator}")
            for role, solution_kind in (("accepted", "accepted"), ("brute", "brute")):
                override = stress.get(role)
                if override:
                    override_path = _problem_relative_path(problem_dir, override)
                    if override_path is None:
                        errors.append(
                            f"stress.{role} must stay inside the problem directory: {override}"
                        )
                    elif not override_path.is_file():
                        errors.append(f"stress {role} not found: {override}")
                else:
                    entries = solutions.get(solution_kind) or []
                    entries = entries if isinstance(entries, list) else [entries]
                    if not any((item.get("file") if isinstance(item, dict) else item) for item in entries):
                        errors.append(f"stress requires a {solution_kind} solution")
            args = stress.get("args", ["{seed}"])
            args = [args] if isinstance(args, str) else args
            if not isinstance(args, list):
                errors.append("stress.args must be a string or list")
            else:
                for arg in args:
                    try:
                        str(arg).format(seed=1, round=1)
                    except (IndexError, KeyError, ValueError):
                        errors.append(f"invalid stress.args template: {arg}")
            rounds = stress.get("rounds", 1000)
            if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds <= 0:
                errors.append("stress.rounds must be a positive integer")
            for field in ("time_limit", "tool_timeout"):
                value = stress.get(field)
                if field in stress and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value <= 0
                ):
                    errors.append(f"stress.{field} must be a positive finite number")

    data = config.get("data") or {}
    groups = data.get("groups") or []
    if not isinstance(groups, list):
        errors.append("data.groups must be a list")
        groups = []
    group_names = []
    for group in groups:
        if not isinstance(group, dict):
            errors.append("data.groups entries must be mappings")
            continue
        group_name = str(group.get("name", "")).strip()
        if not group_name:
            errors.append("data group has no name")
            continue
        group_names.append(group_name)
        patterns = group.get("patterns") or group.get("cases") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            errors.append(f"data group {group_name} patterns must be a string or list")
            patterns = []
        if not patterns:
            warnings.append(f"data group {group_name} has no patterns; name-prefix matching will be used")
        targets = group.get("targets") or []
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list):
            errors.append(f"data group {group_name} targets must be a string or list")
            targets = []
        unknown_targets = sorted(
            str(target).replace("\\", "/") for target in targets
            if str(target).replace("\\", "/") not in configured_programs
        )
        if unknown_targets:
            errors.append(
                f"data group {group_name} has unknown targets: {', '.join(unknown_targets)}"
            )
    duplicate_groups = sorted({name for name in group_names if group_names.count(name) > 1})
    if duplicate_groups:
        errors.append("duplicate data group names: " + ", ".join(duplicate_groups))
    for kind, default in (("sample", "data/sample"), ("secret", "data/secret")):
        try:
            directory = resolve_data_dir(problem_dir, config, f"{kind}_dir", default)
        except ProbHubError as exc:
            errors.append(str(exc))
            continue
        inputs = set()
        answers = set()
        if directory.is_dir():
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                suffix = path.suffix.casefold()
                if suffix not in {".in", ".ans"}:
                    continue
                if path.suffix != suffix:
                    errors.append(
                        f"{kind} data extension must be lowercase {suffix}: {path.name}"
                    )
                (inputs if suffix == ".in" else answers).add(path.stem)
        if not inputs:
            errors.append(f"no {kind} inputs")
        if inputs - answers:
            errors.append(f"{kind} inputs without answers: {', '.join(sorted(inputs - answers))}")
        if answers - inputs:
            errors.append(f"{kind} answers without inputs: {', '.join(sorted(answers - inputs))}")
        folded = {}
        for stem in inputs | answers:
            folded.setdefault(stem.casefold(), []).append(stem)
        collisions = sorted(
            "/".join(sorted(group)) for group in folded.values() if len(group) > 1
        )
        if collisions:
            errors.append(
                f"{kind} case names collide case-insensitively "
                f"(they collapse on Windows checkouts): {', '.join(collisions)}"
            )

    try:
        recipes, uncovered = recipe_coverage(problem_dir, config)
    except ProbHubError as exc:
        errors.append(str(exc))
    else:
        if recipes:
            recipe_generators = {
                recipe.get("generator")
                for recipe in recipes
                if not recipe.get("manual") and recipe.get("generator")
            }
            for generator in sorted(recipe_generators):
                generator_path = _problem_relative_path(problem_dir, generator)
                if generator_path is None:
                    errors.append(
                        f"recipe generator must stay inside the problem directory: {generator}"
                    )
                elif not generator_path.is_file():
                    errors.append(f"recipe generator not found: {generator}")
            secret_directory = resolve_data_dir(problem_dir, config, "secret_dir", "data/secret")
            missing_manual = sorted(
                recipe["case"] for recipe in recipes
                if recipe.get("manual")
                and not (
                    (secret_directory / f"{recipe['case']}.in").is_file()
                    and (secret_directory / f"{recipe['case']}.ans").is_file()
                )
            )
            if missing_manual:
                warnings.append(
                    "manual recipe(s) have no data files yet: " + ", ".join(missing_manual)
                )
        if uncovered:
            if recipes:
                warnings.append(
                    f"{len(uncovered)} secret case(s) have no generation recipe "
                    f"(first: {uncovered[0]})"
                )
            else:
                warnings.append(
                    f"secret data has no generation recipes "
                    f"({len(uncovered)} case(s) not reproducible)"
                )
    solution_verification = analyze_solution_verification(problem_dir, config)
    errors.extend(solution_verification.get("errors") or [])
    warnings.extend(solution_verification.get("warnings") or [])
    source_hash = compute_source_hash(problem_dir, config)
    data_hash = compute_data_hash(problem_dir, config)
    judge_qa_evidence = evaluate_judge_qa_evidence(
        problem_dir,
        config,
        judge_qa,
        source_hash,
        data_hash,
    )
    judge_qa["evidence"] = judge_qa_evidence
    warnings.extend(judge_qa_evidence["warnings"])
    calibration = evaluate_calibration(
        problem_dir,
        config,
        source_hash,
        data_hash,
    )
    warnings.extend(calibration["warnings"])
    diagnostics = [
        *calibration["diagnostics"],
        *constraint_reconciliation["diagnostics"],
        *(solution_verification.get("diagnostics") or []),
        *judge_qa["diagnostics"],
        *judge_qa_evidence["diagnostics"],
    ]
    return {
        "id": entry["id"],
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "statement": statement_report,
        "constraint_reconciliation": constraint_reconciliation,
        "solution_verification": solution_verification,
        "calibration": calibration,
        "judge_qa": judge_qa,
        "source_hash": source_hash,
        "data_hash": data_hash,
    }


def lint_workspace(root, workspace, selected=None):
    entries = selected or problem_entries(workspace)
    pending = pending_workspace_transactions(root, workspace)
    if pending:
        message = (
            "workspace recovery required before lint: " + ", ".join(pending)
        )
        return {
            "ok": False,
            "code": "recovery_required",
            "errors": [message],
            "problems": [
                {
                    "id": entry["id"],
                    "ok": False,
                    "errors": [message],
                    "warnings": [],
                }
                for entry in entries
            ],
        }
    results = [lint_problem(root, workspace, entry) for entry in entries]
    ids = [entry["id"] for entry in problem_entries(workspace)]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    errors = [f"duplicate problem id: {item}" for item in duplicate_ids]
    if not typst_boundary_protocol_supported(root, workspace):
        names = {}
        for entry in problem_entries(workspace):
            _, config = load_problem(root, entry)
            display_name = config.get("display_name") or config.get("name")
            names.setdefault(normalize_display_name(display_name), []).append(
                (entry["id"], display_name)
            )
        collisions = [items for items in names.values() if len(items) > 1]
        for items in collisions:
            details = ", ".join(
                f"{problem_id}={display_name!r}"
                for problem_id, display_name in items
            )
            errors.append(
                "legacy Typst template requires unique normalized problem display names: "
                + details
            )
    cover = ((workspace.get("typst") or {}).get("cover") or {})
    if not isinstance(cover, dict):
        errors.append("typst.cover must be a mapping")
    else:
        logo = cover.get("logo")
        if logo is not None:
            logo_path = Path(str(logo))
            if logo_path.is_absolute() or ".." in logo_path.parts:
                errors.append("typst.cover.logo must stay inside the Typst directory")
        for field in ("logo_width", "logo_space_above", "logo_space_below"):
            value = cover.get(field)
            if value is not None and not TYPST_LENGTH_PATTERN.fullmatch(str(value).strip()):
                errors.append(f"typst.cover.{field} must be a Typst length")
    return {"ok": not errors and all(item["ok"] for item in results), "errors": errors, "problems": results}


def _read_manifest(problem_dir):
    path = problem_dir / ".probhub/build-manifest.json"
    if not path.is_file():
        return None, None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable build manifest: {exc}"
    if not isinstance(manifest, dict):
        return None, "invalid build manifest: expected a JSON object"
    return manifest, None


def load_manifest(problem_dir):
    return _read_manifest(problem_dir)[0]


def problem_status(
    problem_dir,
    config,
    root=None,
    workspace=None,
    workspace_hash=None,
    collection_hash=None,
    builder_fingerprint=None,
    builder_fingerprint_error=None,
):
    if root is not None and workspace is not None:
        pending = pending_workspace_transactions(root, workspace)
        if pending:
            return {
                "state": "recovery-required",
                "code": "recovery_required",
                "pending_transactions": pending,
            }
    manifest, manifest_error = _read_manifest(problem_dir)
    current = {
        "source_hash": compute_source_hash(problem_dir, config),
        "data_hash": compute_data_hash(problem_dir, config),
        "pdf_hash": hash_file(problem_dir / "problem.pdf"),
        "package_hash": hash_file(problem_dir.parent / f"{config['id']}.zip"),
    }
    judge_qa = inspect_judge_qa(problem_dir, config)
    judge_qa_evidence = evaluate_judge_qa_evidence(
        problem_dir,
        config,
        judge_qa,
        current["source_hash"],
        current["data_hash"],
    )
    judge_qa["evidence"] = judge_qa_evidence
    if root is not None and workspace is not None:
        current["workspace_hash"] = (
            compute_workspace_hash(root, workspace)
            if workspace_hash is None
            else workspace_hash
        )
        current["collection_hash"] = (
            compute_collection_hash(root, workspace)
            if collection_hash is None
            else collection_hash
        )
        if builder_fingerprint is None and builder_fingerprint_error is None:
            try:
                builder_fingerprint = compute_builder_fingerprint(root, workspace)
            except ProbHubError as exc:
                builder_fingerprint_error = {
                    "code": exc.code or "builder_fingerprint_failed",
                    "error": str(exc),
                }
    if not manifest:
        calibration = evaluate_calibration(
            problem_dir,
            config,
            current["source_hash"],
            current["data_hash"],
        )
        return {
            "state": "never-built",
            **({"manifest_error": manifest_error} if manifest_error else {}),
            **current,
            **(
                {"builder_fingerprint": builder_fingerprint}
                if builder_fingerprint is not None
                else {}
            ),
            **(
                {"builder_fingerprint_error": builder_fingerprint_error}
                if builder_fingerprint_error is not None
                else {}
            ),
            "warnings": [
                *calibration["warnings"],
                *judge_qa_evidence["warnings"],
            ],
            "diagnostics": [
                *calibration["diagnostics"],
                *judge_qa_evidence["diagnostics"],
            ],
            "calibration": calibration,
            "judge_qa": judge_qa,
        }
    stale = []
    if manifest.get("schema_version") != BUILD_MANIFEST_SCHEMA_VERSION:
        stale.append("manifest_schema")
    elif not isinstance(manifest.get("batch_id"), str) or not manifest["batch_id"]:
        stale.append("batch_id")
    elif (
        not isinstance(manifest.get("sealed_revision_id"), str)
        or not manifest["sealed_revision_id"]
    ):
        stale.append("sealed_revision_id")
    elif not isinstance(manifest.get("builder_fingerprint"), dict):
        stale.append("builder_fingerprint")
    elif builder_fingerprint_error is not None:
        stale.append("builder_fingerprint.unavailable")
    elif builder_fingerprint is not None:
        stale.extend(
            builder_fingerprint_stale_fields(
                manifest["builder_fingerprint"],
                builder_fingerprint,
            )
        )
    stale.extend(key for key, value in current.items() if manifest.get(key) != value)
    calibration = evaluate_calibration(
        problem_dir,
        config,
        current["source_hash"],
        current["data_hash"],
    )
    return {
        "state": "stale" if stale else "current",
        "stale_fields": stale,
        **current,
        **(
            {"builder_fingerprint": builder_fingerprint}
            if builder_fingerprint is not None
            else {}
        ),
        **(
            {"builder_fingerprint_error": builder_fingerprint_error}
            if builder_fingerprint_error is not None
            else {}
        ),
        "manifest": manifest,
        "warnings": [
            *calibration["warnings"],
            *judge_qa_evidence["warnings"],
        ],
        "diagnostics": [
            *calibration["diagnostics"],
            *judge_qa_evidence["diagnostics"],
        ],
        "calibration": calibration,
        "judge_qa": judge_qa,
    }
