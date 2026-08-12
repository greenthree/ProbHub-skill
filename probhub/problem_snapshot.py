import shutil
from pathlib import Path

from .errors import ProbHubError
from .io import read_yaml
from .linting import compute_data_hash, compute_source_hash
from .workspace import load_problem


def problem_path(root, entry):
    root = Path(root).resolve()
    problem_dir = (root / entry.get("directory", entry["id"])).resolve()
    try:
        problem_dir.relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            f"problem directory must stay inside the workspace: {problem_dir}"
        ) from exc
    return problem_dir


def problem_copy_ignore(source_root):
    source_root = Path(source_root).resolve()
    ignored_directories = {
        ".git", ".hg", ".mypy_cache", ".preview", ".pytest_cache",
        ".ruff_cache", ".svn", "__pycache__", "node_modules",
        "output_validators",
    }
    ignored_root_files = {
        "domjudge-problem.ini", "meta.json", "problem.pdf", "problem.yaml",
    }

    def ignore(directory, names):
        directory = Path(directory).resolve()
        result = []
        for name in names:
            candidate = directory / name
            if candidate.is_symlink():
                result.append(name)
            elif name in ignored_directories or name == ".probhub":
                result.append(name)
            elif directory == source_root and name in ignored_root_files:
                result.append(name)
            elif candidate.is_file() and candidate.suffix.lower() in {
                ".exe", ".o", ".obj", ".pyc",
            }:
                result.append(name)
        return result

    return ignore


def copy_problem_consistently(
    root,
    entry,
    destination,
    *,
    expected_source_hash=None,
    expected_data_hash=None,
    operation="snapshot",
):
    expected_problem_dir = problem_path(root, entry)
    problem_dir, config = load_problem(root, entry)
    problem_dir = problem_dir.resolve()
    if problem_dir != expected_problem_dir:
        raise ProbHubError(f"unexpected problem directory: {problem_dir}")
    source_hash = compute_source_hash(problem_dir, config)
    data_hash = compute_data_hash(problem_dir, config)
    if (
        expected_source_hash is not None and source_hash != expected_source_hash
    ) or (
        expected_data_hash is not None and data_hash != expected_data_hash
    ):
        raise ProbHubError(
            f"verified inputs changed before creating {operation} for {entry['id']}",
            code="inputs_changed",
        )
    shutil.copytree(
        problem_dir,
        destination,
        ignore=problem_copy_ignore(problem_dir),
    )

    copied_config = read_yaml(destination / "probhub.yaml")
    copied_source_hash = compute_source_hash(destination, copied_config)
    copied_data_hash = compute_data_hash(destination, copied_config)
    try:
        _, current_config = load_problem(root, entry)
        current_source_hash = compute_source_hash(problem_dir, current_config)
        current_data_hash = compute_data_hash(problem_dir, current_config)
    except Exception as exc:
        raise ProbHubError(
            f"problem changed while creating {operation} for {entry['id']}: {exc}",
            code="inputs_changed",
        ) from exc

    if (
        copied_source_hash != source_hash
        or copied_data_hash != data_hash
        or current_source_hash != source_hash
        or current_data_hash != data_hash
        or (expected_source_hash is not None and copied_source_hash != expected_source_hash)
        or (expected_data_hash is not None and copied_data_hash != expected_data_hash)
    ):
        raise ProbHubError(
            f"problem changed while creating {operation} for {entry['id']}",
            code="inputs_changed",
        )
    return copied_config, source_hash, data_hash
