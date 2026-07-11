import hashlib
import json
import math
import re
from pathlib import Path

from .hashing import files_under, hash_file, hash_paths
from .metadata import build_meta
from .statement import parse_statement
from .workspace import load_problem, problem_entries

DEFAULT_FORBIDDEN = ("TODO", "FIXME", "114514", "待补充")
BUILD_MANIFEST_SCHEMA_VERSION = 2
STATEMENT_ASSET_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
STATEMENT_ASSET_IGNORED_DIRS = {
    ".preview",
    ".probhub",
    "__pycache__",
    "code",
    "data",
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


def problem_statement_asset_paths(problem_dir):
    problem_dir = Path(problem_dir).resolve()
    paths = []
    for path in problem_dir.rglob("*"):
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


def problem_source_paths(problem_dir, config):
    problem_dir = Path(problem_dir).resolve()
    paths = [problem_dir / "probhub.yaml", problem_dir / ((config.get("statement") or {}).get("source", "problem.md"))]
    judge = config.get("judge") or {}
    for key in ("validator", "checker", "interactor"):
        if judge.get(key):
            paths.append(problem_dir / judge[key])
    for generator in config.get("generators") or []:
        path = generator.get("file") if isinstance(generator, dict) else generator
        if path:
            paths.append(problem_dir / path)
    solutions = config.get("solutions") or {}
    for value in solutions.values():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            path = entry.get("file") if isinstance(entry, dict) else entry
            if path:
                paths.append(problem_dir / path)
    stress = config.get("stress") or {}
    if isinstance(stress, dict):
        for key in ("generator", "accepted", "brute"):
            candidate = _problem_relative_path(problem_dir, stress.get(key))
            if candidate:
                paths.append(candidate)
    paths.extend(problem_statement_asset_paths(problem_dir))
    return paths


def compute_source_hash(problem_dir, config):
    problem_dir = Path(problem_dir).resolve()
    relative_paths = [
        path.relative_to(problem_dir)
        for path in problem_source_paths(problem_dir, config)
        if path.exists()
    ]
    return hash_paths(problem_dir, relative_paths)[0]


def compute_workspace_hash(root, workspace):
    paths = [root / ".probhub/workspace.yaml"]
    typst_dir = root / ((workspace.get("typst") or {}).get("directory", "typst-statement/正式赛"))
    typst_root = typst_dir.parent
    if typst_root.exists():
        for path in typst_root.rglob("*"):
            if not path.is_file() or ".preview" in path.parts:
                continue
            if path.suffix.lower() in {".typ", ".png", ".jpg", ".jpeg", ".svg"}:
                paths.append(path)
    return hash_paths(root, [path.relative_to(root) for path in paths if path.exists()])[0]


def compute_collection_hash(root, workspace):
    snapshot = {
        "workspace_hash": compute_workspace_hash(root, workspace),
        "problems": [],
    }
    for entry in problem_entries(workspace):
        problem_dir, config = load_problem(root, entry)
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


def compute_data_hash(problem_dir, config):
    data = config.get("data") or {}
    paths = []
    for key, default in (("sample_dir", "data/sample"), ("secret_dir", "data/secret")):
        paths.extend(files_under(problem_dir / data.get(key, default), {".in", ".ans"}))
    return hash_paths(problem_dir, [path.relative_to(problem_dir) for path in paths])[0]


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
    if not isinstance(time_limit, int) or time_limit <= 0:
        errors.append("limits.time must be a positive integer")
    if not isinstance(memory, int) or memory < 256 or memory & (memory - 1):
        errors.append("limits.memory must be a power of two and at least 256")
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or output_limit <= 0:
        errors.append("limits.output must be a positive integer in MB")
    if isinstance(process_limit, bool) or not isinstance(process_limit, int) or process_limit <= 0:
        errors.append("limits.processes must be a positive integer")
    source = problem_dir / ((config.get("statement") or {}).get("source", "problem.md"))
    try:
        parsed = parse_statement(source)
        if parsed["title"] and name and parsed["title"] != name:
            warnings.append(f"statement title '{parsed['title']}' differs from name '{name}'")
        if parsed["unknown_headings"]:
            warnings.append("unknown statement headings: " + ", ".join(parsed["unknown_headings"]))
        forbidden = (workspace.get("lint") or {}).get("forbidden_patterns", list(DEFAULT_FORBIDDEN))
        for pattern in forbidden:
            if re.search(re.escape(str(pattern)), parsed["raw"], re.IGNORECASE):
                errors.append(f"forbidden pattern in statement: {pattern}")
    except Exception as exc:
        errors.append(str(exc))
    judge = config.get("judge") or {}
    judge_type = str(judge.get("type", "standard")).strip().lower()
    if judge_type == "checker":
        judge_type = "custom"
    if judge_type not in {"standard", "custom", "interactive"}:
        errors.append(f"unsupported judge.type: {judge_type}")

    validator = judge.get("validator")
    if not validator:
        errors.append("judge.validator is required")
    elif not (problem_dir / validator).is_file():
        errors.append(f"validator not found: {validator}")

    checker = judge.get("checker")
    interactor = judge.get("interactor")
    if judge_type == "custom":
        if not checker:
            errors.append("judge.checker is required for custom judging")
        elif not (problem_dir / checker).is_file():
            errors.append(f"checker not found: {checker}")
        if interactor:
            errors.append("judge.interactor is only valid for interactive judging")
    elif judge_type == "interactive":
        if not interactor:
            errors.append("judge.interactor is required for interactive judging")
        elif not (problem_dir / interactor).is_file():
            errors.append(f"interactor not found: {interactor}")
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
    solutions = config.get("solutions") or {}
    configured_programs = set()
    referenced_groups = set()
    allowed_statuses = {"AC", "WA", "TLE", "MLE", "OLE", "RE", "FAIL"}
    for solution_kind in ("accepted", "brute", "wrong"):
        entries = solutions.get(solution_kind) or []
        entries = entries if isinstance(entries, list) else [entries]
        for solution in entries:
            solution_file = solution.get("file") if isinstance(solution, dict) else solution
            if solution_file:
                solution_file = str(solution_file).replace("\\", "/")
                configured_programs.add(solution_file)
                if not (problem_dir / solution_file).is_file():
                    errors.append(f"solution not found: {solution_file}")
            if not isinstance(solution, dict) or not solution.get("expected"):
                continue
            expected = solution.get("expected")
            if not isinstance(expected, dict):
                errors.append(f"solutions.{solution_kind}.expected must be a mapping")
                continue
            if "status" not in expected:
                errors.append(f"expected.status is required for {solution_file}")
            for field in ("status", "forbid"):
                values = expected.get(field)
                values = [values] if isinstance(values, str) else values or []
                if not isinstance(values, list):
                    errors.append(f"expected.{field} must be a status or list for {solution_file}")
                    continue
                if field == "status" and not values:
                    errors.append(f"expected.status must not be empty for {solution_file}")
                invalid = sorted({str(status).upper() for status in values} - allowed_statuses)
                if invalid:
                    errors.append(
                        f"invalid expected {field} for {solution_file}: {', '.join(invalid)}"
                    )
            groups = expected.get("groups")
            groups = [groups] if isinstance(groups, str) else groups or []
            if not isinstance(groups, list):
                errors.append(f"expected.groups must be a name or list for {solution_file}")
                groups = []
            referenced_groups.update(str(group) for group in groups)
            if "all" in expected and not isinstance(expected.get("all"), bool):
                errors.append(f"expected.all must be boolean for {solution_file}")

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
                    except (KeyError, ValueError):
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
    unknown_groups = sorted(referenced_groups - set(group_names))
    if unknown_groups:
        errors.append("unknown expected groups: " + ", ".join(unknown_groups))

    for kind, default in (("sample", "data/sample"), ("secret", "data/secret")):
        directory = problem_dir / data.get(f"{kind}_dir", default)
        inputs = {path.stem for path in directory.glob("*.in")} if directory.is_dir() else set()
        answers = {path.stem for path in directory.glob("*.ans")} if directory.is_dir() else set()
        if not inputs:
            errors.append(f"no {kind} inputs")
        if inputs - answers:
            errors.append(f"{kind} inputs without answers: {', '.join(sorted(inputs - answers))}")
        if answers - inputs:
            errors.append(f"{kind} answers without inputs: {', '.join(sorted(answers - inputs))}")
    return {"id": entry["id"], "ok": not errors, "errors": errors, "warnings": warnings, "source_hash": compute_source_hash(problem_dir, config), "data_hash": compute_data_hash(problem_dir, config)}


def lint_workspace(root, workspace, selected=None):
    entries = selected or problem_entries(workspace)
    results = [lint_problem(root, workspace, entry) for entry in entries]
    ids = [entry["id"] for entry in problem_entries(workspace)]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    errors = [f"duplicate problem id: {item}" for item in duplicate_ids]
    return {"ok": not errors and all(item["ok"] for item in results), "errors": errors, "problems": results}


def load_manifest(problem_dir):
    path = problem_dir / ".probhub/build-manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def problem_status(
    problem_dir,
    config,
    root=None,
    workspace=None,
    workspace_hash=None,
    collection_hash=None,
):
    manifest = load_manifest(problem_dir)
    current = {
        "source_hash": compute_source_hash(problem_dir, config),
        "data_hash": compute_data_hash(problem_dir, config),
        "pdf_hash": hash_file(problem_dir / "problem.pdf"),
        "package_hash": hash_file(problem_dir.parent / f"{config['id']}.zip"),
    }
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
    if not manifest:
        return {"state": "never-built", **current}
    stale = []
    if manifest.get("schema_version") != BUILD_MANIFEST_SCHEMA_VERSION:
        stale.append("manifest_schema")
    stale.extend(key for key, value in current.items() if manifest.get(key) != value)
    return {"state": "stale" if stale else "current", "stale_fields": stale, **current, "manifest": manifest}
