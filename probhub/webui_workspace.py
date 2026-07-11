import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import yaml

from .errors import ProbHubError
from .linting import lint_workspace
from .metadata import build_meta, natural_key
from .statement import render_statement
from .workspace import WORKSPACE_FILE, load_problem, load_workspace, problem_entries


def schema_workspace_for_subtitle(root, subtitle):
    root = Path(root).resolve()
    workspace_path = root / WORKSPACE_FILE
    if not workspace_path.is_file():
        return None
    loaded_root, workspace = load_workspace(root)
    typst_dir = Path((workspace.get("typst") or {}).get("directory", "typst-statement/正式赛"))
    if subtitle and typst_dir.name != subtitle:
        return None
    return loaded_root, workspace


def _hash_files(root, paths):
    digest = hashlib.sha256()
    for path in sorted({Path(path).resolve() for path in paths}, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes() if path.is_file() else b""
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _sample_paths(problem_dir, config):
    sample_dir = problem_dir / ((config.get("data") or {}).get("sample_dir", "data/sample"))
    paths = []
    if sample_dir.is_dir():
        for input_path in sample_dir.glob("*.in"):
            paths.extend((input_path, input_path.with_suffix(".ans")))
    return sample_dir, paths


def editor_revision(root, problem_dir, config):
    source = problem_dir / ((config.get("statement") or {}).get("source", "problem.md"))
    _, sample_paths = _sample_paths(problem_dir, config)
    return _hash_files(Path(root).resolve(), [problem_dir / "probhub.yaml", source, *sample_paths])


def workspace_revision(root, workspace=None):
    if workspace is None:
        _, workspace = load_workspace(root)
    payload = json.dumps(workspace.get("problems") or [], ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def full_workspace_revision(root):
    return _hash_files(Path(root).resolve(), [Path(root) / WORKSPACE_FILE])


def load_editor_data(root, workspace):
    root = Path(root).resolve()
    revision = workspace_revision(root, workspace)
    result = []
    for entry in problem_entries(workspace):
        problem_dir, config = load_problem(root, entry)
        meta = build_meta(problem_dir, config)
        sample_dir, _ = _sample_paths(problem_dir, config)
        samples = meta["problem"].get("samples", [])
        sample_inputs = sorted(sample_dir.glob("*.in"), key=natural_key) if sample_dir.is_dir() else []
        for sample, input_path in zip(samples, sample_inputs):
            sample["_name"] = input_path.stem
        meta["_id"] = config["id"]
        meta["_revision"] = editor_revision(root, problem_dir, config)
        meta["_workspace_revision"] = revision
        quote = (config.get("statement") or {}).get("quote")
        if isinstance(quote, dict):
            meta["statement"]["quote"] = {
                "text": str(quote.get("text", "")),
                "source": str(quote.get("source", "")),
            }
        result.append(meta)
    return result


def _yaml_bytes(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000).encode("utf-8")


def _text_bytes(value):
    return str(value).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _record_change(changes, path, content):
    path = Path(path)
    if content is None:
        if path.exists():
            changes[path] = None
        return
    if not path.is_file() or path.read_bytes() != content:
        changes[path] = content


def _commit_changes(changes, validate=None):
    changes = {Path(path): content for path, content in changes.items()}
    backups = {path: path.read_bytes() if path.is_file() else None for path in changes}
    staged = {}
    try:
        for path, content in changes.items():
            if content is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            os.close(handle)
            temporary = Path(temporary)
            temporary.write_bytes(content)
            staged[path] = temporary
        committed = []
        try:
            for path, content in changes.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(staged[path], path)
                committed.append(path)
            if validate is not None:
                validate()
        except Exception:
            # Restore every path already touched so one save cannot leave mixed source revisions.
            for path in reversed(committed):
                original = backups[path]
                if original is None:
                    path.unlink(missing_ok=True)
                    continue
                handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".restore", dir=path.parent)
                os.close(handle)
                temporary = Path(temporary)
                temporary.write_bytes(original)
                os.replace(temporary, path)
            raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def _positive_number(value, fallback):
    if isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number <= 0:
        return fallback
    return int(number) if number.is_integer() else number


def _sample_name(value):
    value = str(value or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
        return None
    return value


def _editable_payload(item):
    problem = (item or {}).get("problem") or {}
    statement = (item or {}).get("statement") or {}
    samples = []
    for sample in problem.get("samples") or []:
        if not isinstance(sample, dict):
            samples.append(sample)
            continue
        samples.append({
            "_name": sample.get("_name"),
            "input": sample.get("input", ""),
            "output": sample.get("output", ""),
        })
    quote = statement.get("quote")
    return {
        "problem": {
            "display_name": problem.get("display_name"),
            "difficulty": problem.get("difficulty"),
            "tags": problem.get("tags") or [],
            "time_limit": problem.get("time_limit"),
            "memory_limit": problem.get("memory_limit"),
            "samples": samples,
        },
        "statement": {
            "description": statement.get("description", ""),
            "input": statement.get("input", ""),
            "output": statement.get("output", ""),
            "notes": statement.get("notes", ""),
            "quote": quote if isinstance(quote, dict) else None,
        },
    }


def save_editor_data(root, workspace, submitted):
    root = Path(root).resolve()
    if not isinstance(submitted, list):
        raise ProbHubError("problems must be a list", code="invalid_editor_payload")

    entries = problem_entries(workspace)
    current_ids = [entry["id"] for entry in entries]
    submitted_ids = [str(item.get("_id", "")) for item in submitted if isinstance(item, dict)]
    if sorted(submitted_ids) != sorted(current_ids) or len(submitted_ids) != len(set(submitted_ids)):
        raise ProbHubError("problem ids do not match the workspace", code="invalid_editor_payload")

    expected_workspace = {item.get("_workspace_revision") for item in submitted}
    if expected_workspace != {workspace_revision(root, workspace)}:
        raise ProbHubError("workspace changed after the editor was loaded", code="source_conflict")

    entry_by_id = {entry["id"]: entry for entry in entries}
    item_by_id = {item["_id"]: item for item in submitted}
    current_items = {item["_id"]: item for item in load_editor_data(root, workspace)}
    loaded = {}
    for problem_id in current_ids:
        problem_dir, config = load_problem(root, entry_by_id[problem_id])
        item = item_by_id[problem_id]
        if item.get("_revision") != editor_revision(root, problem_dir, config):
            raise ProbHubError(f"{problem_id} changed after the editor was loaded", code="source_conflict")
        loaded[problem_id] = (problem_dir, config)

    new_workspace = copy.deepcopy(workspace)
    new_workspace["problems"] = [copy.deepcopy(entry_by_id[problem_id]) for problem_id in submitted_ids]
    changes = {}
    if submitted_ids != current_ids:
        _record_change(changes, root / WORKSPACE_FILE, _yaml_bytes(new_workspace))

    for problem_id in submitted_ids:
        problem_dir, current_config = loaded[problem_id]
        item = item_by_id[problem_id]
        if _editable_payload(item) == _editable_payload(current_items[problem_id]):
            continue
        problem = item.get("problem") or {}
        statement = item.get("statement") or {}
        config = copy.deepcopy(current_config)
        display_name = str(problem.get("display_name") or config.get("display_name") or config.get("name")).strip()
        if not display_name:
            raise ProbHubError(f"{problem_id} has an empty display name", code="invalid_editor_payload")
        config["display_name"] = display_name
        difficulty = problem.get("difficulty")
        if difficulty is None:
            config.pop("difficulty", None)
        elif isinstance(difficulty, int) and 0 <= difficulty <= 5:
            config["difficulty"] = difficulty
        else:
            raise ProbHubError(f"{problem_id} difficulty must be 0..5", code="invalid_editor_payload")
        tags = problem.get("tags") or []
        if not isinstance(tags, list):
            raise ProbHubError(f"{problem_id} tags must be a list", code="invalid_editor_payload")
        config["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
        limits = config.setdefault("limits", {})
        limits["time"] = _positive_number(problem.get("time_limit"), limits.get("time", 1))
        limits["memory"] = int(_positive_number(problem.get("memory_limit"), limits.get("memory", 256)))
        statement_config = config.setdefault("statement", {})
        quote = statement.get("quote")
        if isinstance(quote, dict) and (str(quote.get("text", "")).strip() or str(quote.get("source", "")).strip()):
            statement_config["quote"] = {
                "text": str(quote.get("text", "")).strip(),
                "source": str(quote.get("source", "")).strip(),
            }
        else:
            statement_config.pop("quote", None)

        config_path = problem_dir / "probhub.yaml"
        statement_path = problem_dir / statement_config.get("source", "problem.md")
        _record_change(changes, config_path, _yaml_bytes(config))
        _record_change(
            changes,
            statement_path,
            _text_bytes(render_statement(config.get("name") or display_name, statement)),
        )

        sample_dir, existing_sample_paths = _sample_paths(problem_dir, current_config)
        existing_names = {path.stem for path in existing_sample_paths if path.suffix == ".in"}
        used = set()
        next_number = 1
        for sample in problem.get("samples") or []:
            if not isinstance(sample, dict):
                raise ProbHubError(f"{problem_id} samples must be objects", code="invalid_editor_payload")
            name = _sample_name(sample.get("_name"))
            if name is None:
                while str(next_number) in existing_names or str(next_number) in used:
                    next_number += 1
                name = str(next_number)
                next_number += 1
            if name in used:
                raise ProbHubError(f"{problem_id} has duplicate sample name {name}", code="invalid_editor_payload")
            used.add(name)
            _record_change(
                changes,
                sample_dir / f"{name}.in",
                _text_bytes(str(sample.get("input", "")).rstrip("\r\n") + "\n"),
            )
            _record_change(
                changes,
                sample_dir / f"{name}.ans",
                _text_bytes(str(sample.get("output", "")).rstrip("\r\n") + "\n"),
            )
        for name in existing_names - used:
            _record_change(changes, sample_dir / f"{name}.in", None)
            _record_change(changes, sample_dir / f"{name}.ans", None)

    def validate_saved_sources():
        _, candidate_workspace = load_workspace(root)
        lint = lint_workspace(root, candidate_workspace)
        if lint["ok"]:
            return
        errors = list(lint.get("errors", []))
        for result in lint.get("problems", []):
            errors.extend(f"{result['id']}: {error}" for error in result.get("errors", []))
        raise ProbHubError("saved sources failed lint: " + "; ".join(errors), code="invalid_editor_payload")

    _commit_changes(changes, validate=validate_saved_sources)
    _, saved_workspace = load_workspace(root)
    return load_editor_data(root, saved_workspace)


def load_contest_config(root, workspace, defaults=None):
    config = dict(defaults or {})
    contest = workspace.get("contest") or {}
    for key in ("title", "subtitle", "author", "date"):
        if key in contest:
            config[key] = contest[key]
    cover = ((workspace.get("typst") or {}).get("cover") or {})
    for key in ("logo", "logo_width", "logo_space_above", "logo_space_below"):
        if key in cover:
            config[key] = cover[key]
    config["_revision"] = full_workspace_revision(root)
    return config


def save_contest_config(root, workspace, submitted):
    if not isinstance(submitted, dict):
        raise ProbHubError("contest config must be an object", code="invalid_editor_payload")
    if submitted.get("_revision") != full_workspace_revision(root):
        raise ProbHubError("workspace changed after the cover editor was loaded", code="source_conflict")
    updated = copy.deepcopy(workspace)
    contest = updated.setdefault("contest", {})
    for key in ("title", "subtitle", "author", "date"):
        contest[key] = str(submitted.get(key, "")).strip()
    typst = updated.setdefault("typst", {})
    cover = typst.setdefault("cover", {})
    for key, fallback in (
        ("logo", "usts.png"),
        ("logo_width", "9cm"),
        ("logo_space_above", "0em"),
        ("logo_space_below", "0em"),
    ):
        cover[key] = str(submitted.get(key, fallback)).strip() or fallback

    def validate_saved_workspace():
        _, candidate = load_workspace(root)
        lint = lint_workspace(root, candidate)
        if not lint["ok"]:
            raise ProbHubError("saved contest config failed workspace lint", code="invalid_editor_payload")

    _commit_changes({Path(root) / WORKSPACE_FILE: _yaml_bytes(updated)}, validate=validate_saved_workspace)
    _, saved = load_workspace(root)
    return load_contest_config(root, saved, defaults=submitted)
