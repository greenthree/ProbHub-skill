"""Isolated temporary workspaces for judging uploaded C++ submissions."""

from __future__ import annotations

import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml


MAX_SOURCE_BYTES = 1024 * 1024
MAX_FILENAME_BYTES = 255
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IGNORED_CODE_NAMES = {"__pycache__", ".probhub"}
_IGNORED_CODE_SUFFIXES = {".exe", ".o", ".obj", ".out", ".class", ".pyc", ".pdb"}


@dataclass
class SubmissionWorkspace:
    task_id: str
    root: Path
    problem_dir: Path
    source_path: Path
    cleanup_succeeded: bool | None = None
    cleanup_error: str | None = None


class SubmissionPreparationCancelled(RuntimeError):
    pass


class SubmissionPreparationDeadlineExceeded(TimeoutError):
    pass


def validate_cpp_upload(filename: str, source: bytes, max_bytes: int = MAX_SOURCE_BYTES) -> str:
    """Validate a browser upload and return normalized UTF-8 source text."""
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("invalid source filename")
    if Path(filename).suffix.lower() != ".cpp":
        raise ValueError("only .cpp source files are accepted")
    if len(filename.encode("utf-8", errors="replace")) > MAX_FILENAME_BYTES:
        raise ValueError(f"source filename exceeds {MAX_FILENAME_BYTES} bytes")
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


def _check_preparation(
    cancel_requested: Callable[[], bool] | None,
    deadline_monotonic: float | None,
) -> None:
    if cancel_requested is not None and cancel_requested():
        raise SubmissionPreparationCancelled("submission preparation cancelled")
    if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
        raise SubmissionPreparationDeadlineExceeded("submission preparation exceeded its deadline")


def _remove_submission_tree(path: Path) -> None:
    shutil.rmtree(path)


def _copy_code_tree(
    source: Path,
    destination: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    deadline_monotonic: float | None = None,
) -> None:
    if source.is_symlink():
        raise ValueError("code directory must not be a symbolic link")
    if not source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.mkdir(parents=True, exist_ok=False)
    for path in source.rglob("*"):
        _check_preparation(cancel_requested, deadline_monotonic)
        relative = path.relative_to(source)
        if path.is_symlink():
            raise ValueError(f"code tree contains a symbolic link: {relative}")
        if any(
            part in _IGNORED_CODE_NAMES or Path(part).suffix.lower() in _IGNORED_CODE_SUFFIXES
            for part in relative.parts
        ):
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise ValueError(f"code tree contains a non-file entry: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source_stream, target.open("xb") as target_stream:
            while True:
                _check_preparation(cancel_requested, deadline_monotonic)
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                target_stream.write(chunk)
        shutil.copystat(path, target)
    _check_preparation(cancel_requested, deadline_monotonic)


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
    *,
    cancel_requested: Callable[[], bool] | None = None,
    deadline_monotonic: float | None = None,
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
    prepared = SubmissionWorkspace(
        task_id=task_id,
        root=task_root,
        problem_dir=prepared_problem,
        source_path=source_path,
    )
    submissions_root.mkdir(parents=True, exist_ok=True)
    try:
        _check_preparation(cancel_requested, deadline_monotonic)
        config = _load_problem_config(problem_dir)
        _check_preparation(cancel_requested, deadline_monotonic)
        prepared_problem.mkdir(parents=True)
        _copy_code_tree(
            problem_dir / "code",
            prepared_problem / "code",
            cancel_requested=cancel_requested,
            deadline_monotonic=deadline_monotonic,
        )
        _check_preparation(cancel_requested, deadline_monotonic)
        source_path.write_text(source_text, encoding="utf-8", newline="\n")
        prepared_config = _submission_config(problem_dir, config)
        (prepared_problem / "probhub.yaml").write_text(
            yaml.safe_dump(prepared_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
        _check_preparation(cancel_requested, deadline_monotonic)
        yield prepared
    finally:
        # A delayed Windows handle release must not raise here and swallow
        # the in-flight judging result; stale directories are reclaimed by
        # cleanup_stale_submission_workspaces on the next startup.
        if task_root.exists():
            try:
                _remove_submission_tree(task_root)
            except OSError as exc:
                prepared.cleanup_error = str(exc)
        prepared.cleanup_succeeded = not task_root.exists()
        if not prepared.cleanup_succeeded and not prepared.cleanup_error:
            prepared.cleanup_error = "submission workspace still exists after cleanup"
        try:
            submissions_root.rmdir()
        except OSError:
            pass
