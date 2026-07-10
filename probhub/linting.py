import json
import re
from pathlib import Path

from .hashing import files_under, hash_file, hash_paths
from .statement import parse_statement
from .workspace import load_problem, problem_entries

DEFAULT_FORBIDDEN = ("TODO", "FIXME", "114514", "待补充")


def problem_source_paths(problem_dir, config):
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
    return paths


def compute_source_hash(problem_dir, config):
    return hash_paths(problem_dir, [path.relative_to(problem_dir) for path in problem_source_paths(problem_dir, config) if path.exists()])[0]


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
    if not isinstance(time_limit, int) or time_limit <= 0:
        errors.append("limits.time must be a positive integer")
    if not isinstance(memory, int) or memory < 256 or memory & (memory - 1):
        errors.append("limits.memory must be a power of two and at least 256")
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
    validator = judge.get("validator")
    if validator and not (problem_dir / validator).is_file():
        errors.append(f"validator not found: {validator}")
    data = config.get("data") or {}
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


def problem_status(problem_dir, config, root=None, workspace=None):
    manifest = load_manifest(problem_dir)
    current = {
        "source_hash": compute_source_hash(problem_dir, config),
        "data_hash": compute_data_hash(problem_dir, config),
        "pdf_hash": hash_file(problem_dir / "problem.pdf"),
        "package_hash": hash_file(problem_dir.parent / f"{config['id']}.zip"),
    }
    if root is not None and workspace is not None:
        current["workspace_hash"] = compute_workspace_hash(root, workspace)
    if not manifest:
        return {"state": "never-built", **current}
    stale = [key for key, value in current.items() if manifest.get(key) != value]
    return {"state": "stale" if stale else "current", "stale_fields": stale, **current, "manifest": manifest}
