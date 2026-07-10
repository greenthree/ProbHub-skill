from pathlib import Path

from .errors import ProbHubError
from .io import read_yaml

WORKSPACE_FILE = Path(".probhub/workspace.yaml")
PROBLEM_FILE = "probhub.yaml"


def find_workspace(start=None):
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / WORKSPACE_FILE).is_file():
            return candidate
    raise ProbHubError("not inside a ProbHub workspace (.probhub/workspace.yaml not found)")


def load_workspace(root=None, allow_empty=False):
    root = find_workspace(root) if root is None else Path(root).resolve()
    path = root / WORKSPACE_FILE
    if not path.is_file():
        raise ProbHubError(f"workspace config not found: {path}")
    data = read_yaml(path)
    if data.get("schema_version") != 1:
        raise ProbHubError(f"unsupported workspace schema_version: {data.get('schema_version')}")
    entries = data.get("problems") or []
    if not isinstance(entries, list):
        raise ProbHubError("workspace problems must be a list")
    if not entries and not allow_empty:
        raise ProbHubError("workspace has no problems")
    return root, data


def problem_entries(workspace):
    result = []
    for item in workspace.get("problems", []):
        item = {"id": item} if isinstance(item, str) else dict(item)
        problem_id = str(item.get("id", "")).strip()
        if not problem_id:
            raise ProbHubError("workspace problem entry has no id")
        item.setdefault("directory", problem_id)
        result.append(item)
    return result


def load_problem(root, entry):
    problem_dir = root / entry.get("directory", entry["id"])
    config_path = problem_dir / PROBLEM_FILE
    if not config_path.is_file():
        raise ProbHubError(f"problem config not found: {config_path}")
    config = read_yaml(config_path)
    if config.get("schema_version") != 1:
        raise ProbHubError(f"{config_path}: unsupported schema_version")
    if config.get("id") != entry["id"]:
        raise ProbHubError(f"{config_path}: id does not match workspace entry {entry['id']}")
    return problem_dir, config


def select_entries(workspace, problem_ids=None):
    entries = problem_entries(workspace)
    if not problem_ids:
        return entries
    wanted = set(problem_ids)
    selected = [entry for entry in entries if entry["id"] in wanted]
    missing = sorted(wanted - {entry["id"] for entry in selected})
    if missing:
        raise ProbHubError(f"unknown problem id(s): {', '.join(missing)}")
    return selected
