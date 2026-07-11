import copy
import errno
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import __version__
from .build_lock import workspace_build_lock
from .errors import ProbHubError
from .hashing import hash_file
from .io import write_json
from .judging import judge_problem
from .linting import (
    BUILD_MANIFEST_SCHEMA_VERSION,
    compute_collection_hash,
    compute_data_hash,
    compute_source_hash,
    compute_workspace_hash,
    lint_workspace,
)
from .package_tools import build_verified_package, generate_domjudge_config, validate_output_validator_source
from .typesetting import compile_collection, extract_problem_pdfs
from .workspace import load_problem, load_workspace, problem_entries, select_entries


@dataclass(frozen=True)
class PlannedProblem:
    entry: dict
    problem_dir: Path
    config: dict
    source_hash: str
    data_hash: str


@dataclass(frozen=True)
class BuildPlan:
    root: Path
    workspace: dict
    typst_relative: Path
    problems: tuple
    selected: tuple
    workspace_hash: str
    collection_hash: str
    batch_id: str

    @property
    def loaded_problems(self):
        return tuple((item.problem_dir, item.config) for item in self.problems)


@dataclass(frozen=True)
class BuildSnapshot:
    temporary_root: Path
    root: Path
    workspace: dict
    problems: tuple
    selected: tuple

    @property
    def loaded_problems(self):
        return tuple((item.problem_dir, item.config) for item in self.problems)


def _raise_lint_errors(lint):
    messages = []
    for result in lint["problems"]:
        messages.extend(f"{result['id']}: {error}" for error in result["errors"])
    raise ProbHubError("lint failed: " + "; ".join(messages + lint.get("errors", [])))


def create_build_plan(root, workspace, entries):
    root = Path(root).resolve()
    workspace = copy.deepcopy(workspace)
    typst_value = (workspace.get("typst") or {}).get(
        "directory",
        "typst-statement/正式赛",
    )
    typst_dir = (root / typst_value).resolve()
    try:
        typst_relative = typst_dir.relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            f"Typst directory must stay inside the workspace: {typst_dir}"
        ) from exc
    all_entries = problem_entries(workspace)
    lint = lint_workspace(root, workspace, all_entries)
    if not lint["ok"]:
        _raise_lint_errors(lint)

    loaded = []
    for entry in all_entries:
        problem_dir, config = load_problem(root, entry)
        problem_dir = problem_dir.resolve()
        try:
            problem_dir.relative_to(root)
        except ValueError as exc:
            raise ProbHubError(
                f"problem directory must stay inside the workspace: {problem_dir}"
            ) from exc
        loaded.append(
            PlannedProblem(
                entry=copy.deepcopy(entry),
                problem_dir=problem_dir,
                config=copy.deepcopy(config),
                source_hash=compute_source_hash(problem_dir, config),
                data_hash=compute_data_hash(problem_dir, config),
            )
        )

    selected_ids = tuple(entry["id"] for entry in entries)
    selected_set = set(selected_ids)
    selected = tuple(item for item in loaded if item.config["id"] in selected_set)
    if len(selected) != len(selected_set):
        missing = sorted(selected_set - {item.config["id"] for item in selected})
        raise ProbHubError(f"unknown problem id(s): {', '.join(missing)}")

    loaded_pairs = tuple((item.problem_dir, item.config) for item in loaded)
    return BuildPlan(
        root=root,
        workspace=workspace,
        typst_relative=typst_relative,
        problems=tuple(loaded),
        selected=selected,
        workspace_hash=compute_workspace_hash(root, workspace),
        collection_hash=compute_collection_hash(
            root,
            workspace,
            loaded_problems=loaded_pairs,
        ),
        batch_id=uuid.uuid4().hex,
    )


def _snapshot_ignore(plan):
    excluded = {
        plan.root / ".probhub/build.lock",
        plan.root / ".probhub/build-tmp",
    }
    for item in plan.problems:
        excluded.update({
            item.problem_dir / "meta.json",
            item.problem_dir / "problem.yaml",
            item.problem_dir / "domjudge-problem.ini",
            item.problem_dir / "problem.pdf",
            item.problem_dir / "output_validators",
            item.problem_dir / ".probhub/build-manifest.json",
        })
        excluded.add(plan.root / f"{item.config['id']}.zip")

    typst_dir = plan.root / plan.typst_relative
    excluded.update({typst_dir / "main.pdf", typst_dir / "problems.json"})
    ignored_directories = {
        ".git",
        ".hg",
        ".mypy_cache",
        ".preview",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        "__pycache__",
        "node_modules",
    }
    ignored_probhub_entries = {
        "build-manifest.json",
        "build-tmp",
        "build.lock",
        "sandbox-cache-v1.json.tmp",
        "stress",
        "submissions",
    }

    def ignore(directory, names):
        directory = Path(directory)
        result = []
        for name in names:
            candidate = directory / name
            if candidate in excluded or name in ignored_directories:
                result.append(name)
                continue
            if directory.name == ".probhub" and name in ignored_probhub_entries:
                result.append(name)
        return result

    return ignore


def _snapshot_mismatches(plan, snapshot):
    changed = []
    workspace_hash = compute_workspace_hash(snapshot.root, snapshot.workspace)
    collection_hash = compute_collection_hash(
        snapshot.root,
        snapshot.workspace,
        loaded_problems=snapshot.loaded_problems,
    )
    if workspace_hash != plan.workspace_hash:
        changed.append("workspace_hash")
    if collection_hash != plan.collection_hash:
        changed.append("collection_hash")

    planned = {item.config["id"]: item for item in plan.selected}
    for item in snapshot.selected:
        expected = planned[item.config["id"]]
        if item.source_hash != expected.source_hash:
            changed.append(f"{item.config['id']}.source_hash")
        if item.data_hash != expected.data_hash:
            changed.append(f"{item.config['id']}.data_hash")
    return changed


@contextmanager
def create_build_snapshot(plan):
    temporary_root = None
    try:
        try:
            temporary_root = Path(tempfile.mkdtemp(
                prefix=f".{plan.root.name}-probhub-{plan.batch_id[:8]}-",
                dir=plan.root.parent,
            ))
            snapshot_root = temporary_root / "workspace"
            shutil.copytree(
                plan.root,
                snapshot_root,
                ignore=_snapshot_ignore(plan),
            )
            _, workspace = load_workspace(snapshot_root)
            planned_ids = {item.config["id"] for item in plan.selected}
            problems = []
            selected = []
            for entry in problem_entries(workspace):
                problem_dir, config = load_problem(snapshot_root, entry)
                item = PlannedProblem(
                    entry=copy.deepcopy(entry),
                    problem_dir=problem_dir.resolve(),
                    config=copy.deepcopy(config),
                    source_hash=compute_source_hash(problem_dir, config),
                    data_hash=compute_data_hash(problem_dir, config),
                )
                problems.append(item)
                if config["id"] in planned_ids:
                    selected.append(item)
            snapshot = BuildSnapshot(
                temporary_root=temporary_root,
                root=snapshot_root,
                workspace=workspace,
                problems=tuple(problems),
                selected=tuple(selected),
            )
            changed = _snapshot_mismatches(plan, snapshot)
        except ProbHubError:
            raise
        except Exception as exc:
            raise ProbHubError(
                f"failed to create build snapshot: {exc}",
                code="snapshot_failed",
            ) from exc
        if changed:
            raise ProbHubError(
                "build inputs changed while creating the snapshot: "
                + ", ".join(changed),
                code="inputs_changed",
            )
        yield snapshot
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


def assert_build_inputs_unchanged(plan):
    try:
        _, workspace = load_workspace(plan.root)
        workspace_hash = compute_workspace_hash(plan.root, workspace)
        collection_hash = compute_collection_hash(plan.root, workspace)
        entries = select_entries(
            workspace,
            [item.config["id"] for item in plan.selected],
        )
        current = {}
        for entry in entries:
            problem_dir, config = load_problem(plan.root, entry)
            current[config["id"]] = {
                "source_hash": compute_source_hash(problem_dir, config),
                "data_hash": compute_data_hash(problem_dir, config),
            }
    except Exception as exc:
        raise ProbHubError(
            f"build inputs changed while validating the live workspace: {exc}",
            code="inputs_changed",
        ) from exc

    changed = []
    if workspace_hash != plan.workspace_hash:
        changed.append("workspace_hash")
    if collection_hash != plan.collection_hash:
        changed.append("collection_hash")
    for item in plan.selected:
        problem_id = item.config["id"]
        values = current.get(problem_id, {})
        if values.get("source_hash") != item.source_hash:
            changed.append(f"{problem_id}.source_hash")
        if values.get("data_hash") != item.data_hash:
            changed.append(f"{problem_id}.data_hash")
    if changed:
        raise ProbHubError(
            "build inputs changed during the run: " + ", ".join(changed),
            code="inputs_changed",
        )


def _replace_staged_file(source, destination):
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise ProbHubError(
            f"staged artifact is missing: {source}",
            code="publish_failed",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise ProbHubError(
            f"failed to publish {destination}: {exc}",
            code="publish_failed",
        ) from exc


def _remove_generated_path(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _publish_output_validators(source, destination):
    try:
        _remove_generated_path(destination)
        if source.is_dir():
            shutil.copytree(source, destination)
    except OSError as exc:
        raise ProbHubError(
            f"failed to publish {destination}: {exc}",
            code="publish_failed",
        ) from exc


def _cached_binary_relative(cache_key):
    if not isinstance(cache_key, str) or "\\" in cache_key:
        return None
    path = PurePosixPath(cache_key)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        return None
    relative = Path(*path.parts).with_suffix(".exe")
    if relative.is_absolute() or relative.drive:
        return None
    return relative


def _publish_cache(source_problem, destination_problem):
    source = source_problem / ".probhub/sandbox-cache-v1.json"
    if not source.is_file():
        return
    destination = destination_problem / ".probhub/sandbox-cache-v1.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + f".{uuid.uuid4().hex}.tmp"
    )
    try:
        try:
            cache = json.loads(source.read_text(encoding="utf-8"))
            compile_entries = cache.get("compile")
            if not isinstance(compile_entries, dict):
                compile_entries = {}
            published_compile = {}
            source_root = source_problem.resolve()
            destination_root = destination_problem.resolve()
            for cache_key, entry in compile_entries.items():
                relative = _cached_binary_relative(cache_key)
                if relative is None or not isinstance(entry, dict):
                    continue
                staged_binary = source_problem / relative
                expected_digest = entry.get("binary_digest")
                if (
                    not staged_binary.is_file()
                    or not isinstance(expected_digest, str)
                    or hash_file(staged_binary) != expected_digest
                ):
                    continue
                live_binary = destination_problem / relative
                try:
                    staged_binary.resolve().relative_to(source_root)
                    live_binary.resolve().relative_to(destination_root)
                except ValueError:
                    continue
                live_binary.parent.mkdir(parents=True, exist_ok=True)
                binary_temporary = live_binary.with_name(
                    live_binary.name + f".{uuid.uuid4().hex}.tmp"
                )
                try:
                    shutil.copyfile(staged_binary, binary_temporary)
                    os.replace(binary_temporary, live_binary)
                finally:
                    if binary_temporary.exists():
                        binary_temporary.unlink()
                published_compile[cache_key] = entry

            cache["compile"] = published_compile
            temporary.write_text(
                json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        except (OSError, ValueError, TypeError):
            pass
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _publish_targets(plan):
    targets = [item.problem_dir / "meta.json" for item in plan.problems]
    typst_dir = plan.root / plan.typst_relative
    targets.extend((typst_dir / "problems.json", typst_dir / "main.pdf"))
    for item in plan.selected:
        targets.extend(
            item.problem_dir / name
            for name in (
                "problem.pdf",
                "problem.yaml",
                "domjudge-problem.ini",
                "output_validators",
                ".probhub/build-manifest.json",
            )
        )
        targets.append(plan.root / f"{item.config['id']}.zip")
    return targets


def _assert_publish_targets_available(plan, platform_name=None):
    if (platform_name or os.name) != "nt":
        return

    locked = []
    for target in _publish_targets(plan):
        candidates = target.rglob("*") if target.is_dir() else (target,)
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                descriptor = os.open(
                    candidate,
                    os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
            except OSError as exc:
                if (
                    isinstance(exc, PermissionError)
                    or exc.errno in {errno.EACCES, errno.EPERM}
                    or getattr(exc, "winerror", None) in {5, 32, 33}
                ):
                    locked.append(candidate)
                    continue
                raise
            else:
                os.close(descriptor)

    if locked:
        shown = ", ".join(str(path) for path in locked[:3])
        suffix = "" if len(locked) <= 3 else f" (+{len(locked) - 3} more)"
        raise ProbHubError(
            "generated artifact is in use and cannot be replaced: "
            f"{shown}{suffix}. Close the PDF/ZIP viewer and retry.",
            code="artifact_busy",
        )


def publish_build(plan, snapshot, manifests, pdfs, packages, publish_cache):
    _assert_publish_targets_available(plan)
    live_by_id = {item.config["id"]: item for item in plan.problems}
    staged_by_id = {item.config["id"]: item for item in snapshot.problems}

    for problem_id, live in live_by_id.items():
        staged = staged_by_id[problem_id]
        _replace_staged_file(
            staged.problem_dir / "meta.json",
            live.problem_dir / "meta.json",
        )

    live_typst = plan.root / plan.typst_relative
    staged_typst = snapshot.root / plan.typst_relative
    _replace_staged_file(
        staged_typst / "problems.json",
        live_typst / "problems.json",
    )
    _replace_staged_file(staged_typst / "main.pdf", live_typst / "main.pdf")

    live_pdfs = {}
    live_packages = {}
    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        for name in ("problem.pdf", "problem.yaml", "domjudge-problem.ini"):
            _replace_staged_file(
                staged.problem_dir / name,
                live.problem_dir / name,
            )
        _publish_output_validators(
            staged.problem_dir / "output_validators",
            live.problem_dir / "output_validators",
        )
        _replace_staged_file(
            snapshot.root / f"{problem_id}.zip",
            plan.root / f"{problem_id}.zip",
        )
        live_pdfs[problem_id] = {
            **pdfs[problem_id],
            "path": str(live.problem_dir / "problem.pdf"),
        }
        live_packages[problem_id] = {
            **packages[problem_id],
            "path": str(plan.root / f"{problem_id}.zip"),
        }

    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        _replace_staged_file(
            staged.problem_dir / ".probhub/build-manifest.json",
            live.problem_dir / ".probhub/build-manifest.json",
        )
        if publish_cache:
            _publish_cache(staged.problem_dir, live.problem_dir)

    return {
        "typst_dir": str(live_typst),
        "main_pdf": str(live_typst / "main.pdf"),
        "pdfs": live_pdfs,
        "packages": live_packages,
        "manifests": manifests,
    }


def write_manifest(
    problem_dir,
    config,
    package_path,
    source_hash,
    data_hash,
    workspace_hash,
    collection_hash,
    batch_id,
):
    manifest = {
        "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
        "problem_id": config["id"],
        "source_hash": source_hash,
        "data_hash": data_hash,
        "workspace_hash": workspace_hash,
        "collection_hash": collection_hash,
        "batch_id": batch_id,
        "pdf_hash": hash_file(problem_dir / "problem.pdf"),
        "package_hash": hash_file(package_path),
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "probhub_version": __version__,
    }
    write_json(problem_dir / ".probhub/build-manifest.json", manifest)
    return manifest


def package_problem(root, problem_dir, config, require_pdf=True):
    generate_domjudge_config(problem_dir, config)
    validate_output_validator_source(problem_dir, config)
    output = root / f"{config['id']}.zip"
    _, verification = build_verified_package(
        problem_dir,
        output,
        require_pdf=require_pdf,
    )
    if not verification["ok"]:
        raise ProbHubError(f"package verification failed for {config['id']}: {'; '.join(verification['errors'])}")
    return output, verification


def build_workspace(root, workspace, entries, run_judge=True, use_judge_cache=True):
    root = Path(root).resolve()
    with workspace_build_lock(root):
        _, live_workspace = load_workspace(root)
        selected_ids = [entry["id"] for entry in entries]
        live_entries = select_entries(live_workspace, selected_ids)
        plan = create_build_plan(root, live_workspace, live_entries)
        with create_build_snapshot(plan) as snapshot:
            judge_results = {}
            if run_judge:
                for item in snapshot.selected:
                    result = judge_problem(
                        snapshot.root,
                        item.problem_dir,
                        use_cache=use_judge_cache,
                    )
                    judge_results[item.config["id"]] = {
                        "ok": result["ok"],
                        "returncode": result["returncode"],
                        "final": result["final"],
                        "cache": result.get("cache", {}),
                    }
                    if not result["ok"]:
                        raise ProbHubError(
                            f"sandbox failed for {item.config['id']}: "
                            f"{result.get('final')}"
                        )

            _, staged_main_pdf, _ = compile_collection(
                snapshot.root,
                snapshot.workspace,
                snapshot.loaded_problems,
            )
            target_ids = {item.config["id"] for item in snapshot.selected}
            pdfs = extract_problem_pdfs(
                staged_main_pdf,
                snapshot.loaded_problems,
                only_ids=target_ids,
            )
            packages = {}
            for item in snapshot.selected:
                package_path, verification = package_problem(
                    snapshot.root,
                    item.problem_dir,
                    item.config,
                    require_pdf=True,
                )
                packages[item.config["id"]] = {
                    "path": str(package_path),
                    "verification": verification,
                }

            planned_by_id = {
                item.config["id"]: item for item in plan.selected
            }
            manifests = {}
            for item in snapshot.selected:
                problem_id = item.config["id"]
                planned = planned_by_id[problem_id]
                manifests[problem_id] = write_manifest(
                    item.problem_dir,
                    item.config,
                    Path(packages[problem_id]["path"]),
                    planned.source_hash,
                    planned.data_hash,
                    plan.workspace_hash,
                    plan.collection_hash,
                    plan.batch_id,
                )

            assert_build_inputs_unchanged(plan)
            published = publish_build(
                plan,
                snapshot,
                manifests,
                pdfs,
                packages,
                publish_cache=run_judge,
            )
            return {
                "ok": True,
                "batch_id": plan.batch_id,
                **published,
                "judge": judge_results,
            }
