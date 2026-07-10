"""Isolated temporary workspaces for judging uploaded C++ submissions."""

from __future__ import annotations

import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml


MAX_SOURCE_BYTES = 1024 * 1024
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IGNORED_CODE_NAMES = {"__pycache__", ".probhub"}
_IGNORED_CODE_SUFFIXES = {".exe", ".o", ".obj", ".out", ".class", ".pyc", ".pdb"}


@dataclass(frozen=True)
class SubmissionWorkspace:
    task_id: str
    root: Path
    problem_dir: Path
    source_path: Path


def validate_cpp_upload(filename: str, source: bytes, max_bytes: int = MAX_SOURCE_BYTES) -> str:
    """Validate a browser upload and return normalized UTF-8 source text."""
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("invalid source filename")
    if Path(filename).suffix.lower() != ".cpp":
        raise ValueError("only .cpp source files are accepted")
    if not source:
        raise ValueError("source file is empty")
    if len(source) > int(max_bytes):
        raise ValueError(f"source file exceeds {int(max_bytes)} bytes")
    if b"\x00" in source:
        raise ValueError("source file contains NUL bytes")
    try:
        return source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("source file must be UTF-8 encoded") from exc



def cleanup_stale_submission_workspaces(
    workspace_root: str | Path,
    max_age_seconds: float = 24 * 60 * 60,
    now: float | None = None,
    protected_task_ids=(),
):
    """Remove only stale UUID-named task directories under the submission root."""
    workspace_root = Path(workspace_root).resolve()
    configured_root = workspace_root / ".probhub" / "submissions"
    if configured_root.is_symlink() or not configured_root.is_dir():
        return []
    submissions_root = configured_root.resolve()
    try:
        submissions_root.relative_to(workspace_root)
    except ValueError:
        return []

    protected = {str(task_id) for task_id in protected_task_ids}
    current = time.time() if now is None else float(now)
    removed = []
    for task_root in list(submissions_root.iterdir()):
        if (
            task_root.name in protected
            or not _TASK_ID_RE.fullmatch(task_root.name)
            or task_root.is_symlink()
            or not task_root.is_dir()
        ):
            continue
        resolved = task_root.resolve()
        try:
            resolved.relative_to(submissions_root)
        except ValueError:
            continue
        try:
            age = current - task_root.stat().st_mtime
        except OSError:
            continue
        if age < float(max_age_seconds):
            continue
        try:
            shutil.rmtree(resolved)
        except OSError:
            continue
        removed.append(task_root.name)
    try:
        submissions_root.rmdir()
    except OSError:
        pass
    return removed

def _resolve_inside(base: Path, value: str | Path, label: str) -> Path:
    base = base.resolve()
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the problem directory") from exc
    return candidate


def _copy_code_tree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ValueError("code directory must not be a symbolic link")
    if not source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"code tree contains a symbolic link: {path.relative_to(source)}")

    def ignore(_directory, names):
        ignored = []
        for name in names:
            path = Path(name)
            if name in _IGNORED_CODE_NAMES or path.suffix.lower() in _IGNORED_CODE_SUFFIXES:
                ignored.append(name)
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


def _load_problem_config(problem_dir: Path) -> dict:
    config_path = problem_dir / "probhub.yaml"
    if not config_path.is_file():
        return {}
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read probhub.yaml: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("probhub.yaml must contain a mapping")
    return config


def _submission_config(problem_dir: Path, config: dict) -> dict:
    prepared = dict(config)
    limits = dict(prepared.get("limits") or {})
    limits.setdefault("time", 1)
    limits.setdefault("memory", 256)
    limits.setdefault("output", 64)
    limits.setdefault("processes", 32)
    prepared["limits"] = limits

    data = dict(prepared.get("data") or {})
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        key = f"{suite}_dir"
        original = _resolve_inside(problem_dir, data.get(key, default), f"data.{key}")
        data[key] = str(original)
    prepared["data"] = data

    judge = dict(prepared.get("judge") or {})
    judge.setdefault("type", "standard")
    # Existing data has already passed the workspace sandbox. Submission judging
    # reads it without compiling a validator into the original problem tree.
    judge.pop("validator", None)
    for key in ("checker", "interactor"):
        entry = judge.get(key)
        value = entry.get("file") if isinstance(entry, dict) else entry
        if value:
            _resolve_inside(problem_dir, value, f"judge.{key}")
    prepared["judge"] = judge

    prepared["solutions"] = {
        "accepted": [{
            "file": "code/submission.cpp",
            "expected": {
                "status": ["AC"],
                "all": True,
                "forbid": ["WA", "TLE", "MLE", "OLE", "RE", "FAIL"],
            },
        }],
        "brute": [],
        "wrong": [],
    }
    prepared.pop("generators", None)
    prepared.pop("stress", None)
    return prepared


@contextmanager
def temporary_submission_workspace(
    workspace_root: str | Path,
    problem_dir: str | Path,
    task_id: str,
    filename: str,
    source: bytes,
):
    """Create and later remove an isolated mirror used only for one submission."""
    if not _TASK_ID_RE.fullmatch(str(task_id)):
        raise ValueError("invalid submission task id")

    workspace_root = Path(workspace_root).resolve()
    problem_dir = Path(problem_dir).resolve()
    try:
        problem_dir.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("problem directory is outside the workspace") from exc
    if not problem_dir.is_dir():
        raise ValueError("problem directory does not exist")

    source_text = validate_cpp_upload(filename, source)
    submissions_root = (workspace_root / ".probhub" / "submissions").resolve()
    task_root = (submissions_root / task_id).resolve()
    try:
        task_root.relative_to(submissions_root)
    except ValueError as exc:
        raise ValueError("submission workspace escapes its root") from exc
    if task_root.exists():
        raise ValueError("submission task already exists")

    prepared_problem = task_root / "problem"
    source_path = prepared_problem / "code" / "submission.cpp"
    submissions_root.mkdir(parents=True, exist_ok=True)
    try:
        config = _load_problem_config(problem_dir)
        prepared_problem.mkdir(parents=True)
        _copy_code_tree(problem_dir / "code", prepared_problem / "code")
        source_path.write_text(source_text, encoding="utf-8", newline="\n")
        prepared_config = _submission_config(problem_dir, config)
        (prepared_problem / "probhub.yaml").write_text(
            yaml.safe_dump(prepared_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
        yield SubmissionWorkspace(
            task_id=task_id,
            root=task_root,
            problem_dir=prepared_problem,
            source_path=source_path,
        )
    finally:
        if task_root.exists():
            shutil.rmtree(task_root)
        try:
            submissions_root.rmdir()
        except OSError:
            pass
