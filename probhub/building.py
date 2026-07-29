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
from .calibration import (
    EVIDENCE_FILENAME,
    EVIDENCE_LOCK_FILENAME,
    read_calibration_evidence,
    write_calibration_evidence,
)
from .errors import ProbHubError
from .generations import checkpoint_revision, latest_checkpoint
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
from .typesetting import compile_collection, extract_problem_pdfs, is_temporary_typst_source
from .transactions import (
    TRANSACTION_PHASE_COMMITTED,
    TRANSACTION_PHASE_PREPARED,
    read_transaction_journal,
    recover_workspace_transactions,
    resolve_journal_target,
    transaction_child,
    validate_transaction_directory,
)
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


@dataclass(frozen=True)
class PackageProblem:
    entry: dict
    problem_dir: Path
    config: dict
    source_hash: str
    data_hash: str
    pdf_hash: str | None


@dataclass(frozen=True)
class PackagePlan:
    root: Path
    workspace: dict
    selected: tuple
    batch_id: str


@dataclass(frozen=True)
class PackageSnapshot:
    temporary_root: Path
    root: Path
    selected: tuple


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


def require_collection_sealed(plan):
    """Bind every problem in the formal collection to a sealed checkpoint."""
    checkpoints = {}
    rejected = []
    for item in plan.problems:
        problem_id = item.config["id"]
        try:
            checkpoint = latest_checkpoint(plan.root, problem_id)
        except ProbHubError as exc:
            rejected.append(f"{problem_id}: {exc.code or str(exc)}")
            continue
        if checkpoint is None:
            rejected.append(f"{problem_id}: no checkpoint")
            continue
        if checkpoint.get("state") != "sealed":
            rejected.append(f"{problem_id}: latest checkpoint is {checkpoint.get('state') or 'invalid'}")
            continue
        mismatches = []
        if checkpoint.get("source_hash") != item.source_hash:
            mismatches.append("source_hash")
        if checkpoint.get("data_hash") != item.data_hash:
            mismatches.append("data_hash")
        if mismatches:
            rejected.append(
                f"{problem_id}: sealed revision does not match live {', '.join(mismatches)}"
            )
            continue
        checkpoints[problem_id] = checkpoint

    if rejected:
        raise ProbHubError(
            "formal build requires current sealed revisions for every collection problem: "
            + "; ".join(rejected),
            code="sealed_revision_required",
        )
    return checkpoints


def assert_collection_seals_unchanged(plan, checkpoints):
    """Revalidate the exact sealed revisions immediately before publication."""
    rejected = []
    for item in plan.problems:
        problem_id = item.config["id"]
        expected = checkpoints[problem_id]
        try:
            latest = latest_checkpoint(plan.root, problem_id)
            if (
                latest is None
                or latest.get("revision_id") != expected.get("revision_id")
            ):
                rejected.append(f"{problem_id}: latest sealed revision changed during build")
                continue
            checkpoint = checkpoint_revision(
                plan.root,
                problem_id,
                expected["revision_id"],
            )
        except (KeyError, ProbHubError) as exc:
            rejected.append(
                f"{problem_id}: {getattr(exc, 'code', None) or str(exc)}"
            )
            continue
        if (
            checkpoint.get("state") != "sealed"
            or checkpoint.get("source_hash") != item.source_hash
            or checkpoint.get("data_hash") != item.data_hash
        ):
            rejected.append(f"{problem_id}: sealed revision changed during build")
    if rejected:
        raise ProbHubError(
            "sealed build evidence changed during the run: " + "; ".join(rejected),
            code="sealed_revision_changed",
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
        "checkpoint-tmp",
        "checkpoints",
        "generation-tmp",
        "generation.lock",
        "generations",
        EVIDENCE_LOCK_FILENAME,
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
            if is_temporary_typst_source(candidate):
                result.append(name)
                continue
            if directory.name == ".probhub" and name in ignored_probhub_entries:
                result.append(name)
                continue
            if directory.name == ".probhub" and name.startswith("build-publish-"):
                result.append(name)
            if directory.name == ".probhub" and name.startswith(EVIDENCE_FILENAME + "."):
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

    planned = {item.config["id"]: item for item in plan.problems}
    for item in snapshot.problems:
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
        entries = problem_entries(workspace)
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
    for item in plan.problems:
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
    destination = Path(destination)
    try:
        if not source.is_dir():
            _remove_generated_path(destination)
            return
        # Stage the full copy next to the destination first so the old
        # validators are only removed once the replacement is complete.
        stage = destination.with_name(f"{destination.name}.stage-{uuid.uuid4().hex}")
        shutil.copytree(source, stage)
        try:
            _remove_generated_path(destination)
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
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


def _publish_calibration_evidence(source_problem, destination_problem):
    evidence = read_calibration_evidence(source_problem)
    if evidence is None:
        return {"published": False, "reason": "missing_staged_evidence"}
    try:
        published = write_calibration_evidence(destination_problem, evidence)
        return {
            "published": bool(published),
            "reason": None if published else "newer_live_evidence_preserved",
        }
    except (OSError, ValueError, TypeError, ProbHubError) as exc:
        # Calibration evidence is a local diagnostic artifact. A failure to
        # refresh it must not roll back an otherwise valid formal build, but it
        # must remain visible to the caller.
        return {
            "published": False,
            "reason": "publish_failed",
            "error": str(exc),
        }


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


def _assert_publish_paths_available(targets, platform_name=None):
    if (platform_name or os.name) != "nt":
        return

    locked = []
    for target in targets:
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


def _assert_publish_targets_available(plan, platform_name=None):
    _assert_publish_paths_available(
        _publish_targets(plan),
        platform_name=platform_name,
    )


def _replace_publish_path(source, target):
    os.replace(source, target)


def _publish_entry_specs(plan, snapshot):
    live_by_id = {item.config["id"]: item for item in plan.problems}
    staged_by_id = {item.config["id"]: item for item in snapshot.problems}
    specs = []
    for problem_id, live in live_by_id.items():
        staged = staged_by_id[problem_id]
        specs.append((staged.problem_dir / "meta.json", live.problem_dir / "meta.json"))

    live_typst = plan.root / plan.typst_relative
    staged_typst = snapshot.root / plan.typst_relative
    specs.extend((
        (staged_typst / "problems.json", live_typst / "problems.json"),
        (staged_typst / "main.pdf", live_typst / "main.pdf"),
    ))

    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        for name in (
            "problem.pdf",
            "problem.yaml",
            "domjudge-problem.ini",
            "output_validators",
        ):
            specs.append((staged.problem_dir / name, live.problem_dir / name))
        specs.append((snapshot.root / f"{problem_id}.zip", plan.root / f"{problem_id}.zip"))

    # Manifest is the commit marker and must be replaced last.
    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        specs.append((
            staged.problem_dir / ".probhub/build-manifest.json",
            live.problem_dir / ".probhub/build-manifest.json",
        ))
    return specs


def _package_publish_entry_specs(plan, snapshot):
    live_by_id = {item.config["id"]: item for item in plan.selected}
    specs = []
    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        for name in (
            "problem.yaml",
            "domjudge-problem.ini",
            "output_validators",
        ):
            specs.append((staged.problem_dir / name, live.problem_dir / name))
        specs.append((snapshot.root / f"{problem_id}.zip", plan.root / f"{problem_id}.zip"))
    return specs


def _journal_target(root, relative):
    return resolve_journal_target(root, relative)


def _rollback_publish_transaction(root, transaction, entries):
    errors = []
    for reverse_index, entry in enumerate(reversed(entries)):
        index = len(entries) - reverse_index - 1
        label = entry.get("target") if isinstance(entry, dict) else repr(entry)
        try:
            if not isinstance(entry, dict):
                raise ValueError("journal entry must be an object")
            target = _journal_target(root, entry["target"])
            backup_name = entry.get("backup")
            backup = (
                transaction_child(
                    transaction,
                    backup_name,
                    expected_prefix="backup",
                    index=index,
                )
                if backup_name
                else None
            )
            if backup is not None and backup.exists():
                _remove_generated_path(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace_publish_path(backup, target)
            elif entry.get("existed"):
                if not (target.exists() or target.is_symlink()):
                    raise FileNotFoundError(
                        f"missing recovery backup and target: {backup} -> {target}"
                    )
            elif not entry.get("existed"):
                _remove_generated_path(target)
        except BaseException as exc:
            errors.append(f"{label}: {exc}")
    return errors


def _recover_build_publish_transactions(root):
    root = Path(root).resolve()
    transaction_root = root / ".probhub"
    if not transaction_root.is_dir():
        return
    for transaction in sorted(transaction_root.glob("build-publish-*")):
        try:
            transaction = validate_transaction_directory(
                transaction,
                transaction_root,
                "build-publish-",
            )
            journal_path = transaction / "journal.json"
            if not journal_path.is_file():
                shutil.rmtree(transaction)
                continue
            _, phase, entries = read_transaction_journal(transaction)
            if phase == TRANSACTION_PHASE_COMMITTED:
                shutil.rmtree(transaction)
                continue
            errors = _rollback_publish_transaction(root, transaction, entries)
            if errors:
                raise OSError("; ".join(errors))
            shutil.rmtree(transaction)
        except BaseException as exc:
            raise ProbHubError(
                f"failed to recover interrupted build publish transaction {transaction}: {exc}",
                code="publish_recovery_failed",
            ) from exc


def _publish_transaction(root, batch_id, specs):
    root = Path(root).resolve()
    transaction = root / ".probhub" / f"build-publish-{batch_id}"
    transaction.mkdir(parents=True, exist_ok=False)
    cleanup_transaction = True
    cleanup_context = "staging"
    entries = []
    try:
        for index, (source, target) in enumerate(specs):
            source = Path(source)
            target = Path(target)
            relative = target.absolute().relative_to(root.absolute()).as_posix()
            staged = transaction / f"payload-{index}"
            source_kind = "missing"
            if source.is_dir():
                shutil.copytree(source, staged)
                source_kind = "directory"
            elif source.is_file():
                shutil.copyfile(source, staged)
                source_kind = "file"
            elif source.name != "output_validators":
                raise FileNotFoundError(f"staged build artifact is missing: {source}")
            existed = target.exists() or target.is_symlink()
            entries.append({
                "target": relative,
                "payload": staged.name if source_kind != "missing" else None,
                "kind": source_kind,
                "existed": existed,
                "backup": f"backup-{index}" if existed else None,
            })

        write_json(transaction / "journal.json", {
            "schema_version": 1,
            "phase": TRANSACTION_PHASE_PREPARED,
            "entries": entries,
        })
        for entry in entries:
            target = _journal_target(root, entry["target"])
            exists = target.exists() or target.is_symlink()
            if exists != entry["existed"]:
                raise ProbHubError(
                    f"publish target changed before commit: {entry['target']}",
                    code="publish_failed",
                )
        cleanup_transaction = False
        try:
            for index, entry in enumerate(entries):
                target = _journal_target(root, entry["target"])
                backup_name = entry.get("backup")
                backup = (
                    transaction_child(
                        transaction,
                        backup_name,
                        expected_prefix="backup",
                        index=index,
                    )
                    if backup_name
                    else None
                )
                if target.exists() or target.is_symlink():
                    if backup is None:
                        raise OSError(
                            f"new publish target appeared during commit: {target}"
                        )
                    _replace_publish_path(target, backup)
                payload_name = entry.get("payload")
                if payload_name:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    payload = transaction_child(
                        transaction,
                        payload_name,
                        expected_prefix="payload",
                        index=index,
                    )
                    _replace_publish_path(payload, target)
        except BaseException as exc:
            errors = _rollback_publish_transaction(root, transaction, entries)
            if errors:
                raise ProbHubError(
                    "artifact publish failed and rollback was incomplete; recovery files remain "
                    f"in {transaction}: {'; '.join(errors)}",
                    code="publish_rollback_failed",
                ) from exc
            cleanup_transaction = True
            cleanup_context = "rollback"
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ProbHubError(
                f"failed to publish artifact transaction: {exc}",
                code="publish_failed",
            ) from exc
        write_json(transaction / "journal.json", {
            "schema_version": 1,
            "phase": TRANSACTION_PHASE_COMMITTED,
            "entries": entries,
        })
        cleanup_transaction = True
        cleanup_context = "committed"
    except ProbHubError:
        raise
    except Exception as exc:
        raise ProbHubError(
            f"failed to stage artifact publish transaction: {exc}",
            code="publish_failed",
        ) from exc
    finally:
        if cleanup_transaction:
            try:
                shutil.rmtree(transaction)
            except OSError as exc:
                detail = {
                    "committed": "build artifacts were committed",
                    "rollback": "build publication was rolled back",
                    "staging": "build transaction staging failed",
                }[cleanup_context]
                raise ProbHubError(
                    f"{detail} but transaction cleanup failed; "
                    f"the next writer will retry cleanup: {transaction}: {exc}",
                    code="publish_cleanup_failed",
                ) from exc


def _publish_build_transaction(plan, snapshot):
    _publish_transaction(
        plan.root,
        plan.batch_id,
        _publish_entry_specs(plan, snapshot),
    )


def publish_build(plan, snapshot, manifests, pdfs, packages, publish_cache):
    _assert_publish_targets_available(plan)
    live_by_id = {item.config["id"]: item for item in plan.problems}
    _publish_build_transaction(plan, snapshot)

    live_typst = plan.root / plan.typst_relative
    live_pdfs = {}
    live_packages = {}
    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        live_pdfs[problem_id] = {
            **pdfs[problem_id],
            "path": str(live.problem_dir / "problem.pdf"),
        }
        live_packages[problem_id] = {
            **packages[problem_id],
            "path": str(plan.root / f"{problem_id}.zip"),
        }

    calibration_warnings = []
    for staged in snapshot.selected:
        problem_id = staged.config["id"]
        live = live_by_id[problem_id]
        if publish_cache:
            _publish_cache(staged.problem_dir, live.problem_dir)
            evidence_publish = _publish_calibration_evidence(
                staged.problem_dir, live.problem_dir
            )
            if evidence_publish.get("reason") == "publish_failed":
                calibration_warnings.append({
                    "code": "calibration_publish_failed",
                    "severity": "warning",
                    "problem": problem_id,
                    "message": evidence_publish.get("error") or "failed to publish calibration evidence",
                })

    return {
        "typst_dir": str(live_typst),
        "main_pdf": str(live_typst / "main.pdf"),
        "pdfs": live_pdfs,
        "packages": live_packages,
        "manifests": manifests,
        "warnings": calibration_warnings,
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
    sealed_revision_id,
):
    manifest = {
        "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
        "problem_id": config["id"],
        "source_hash": source_hash,
        "data_hash": data_hash,
        "workspace_hash": workspace_hash,
        "collection_hash": collection_hash,
        "batch_id": batch_id,
        "sealed_revision_id": sealed_revision_id,
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
        expected_config=config,
        source_problem_dir=problem_dir,
        run_validator=True,
    )
    if not verification["ok"]:
        raise ProbHubError(f"package verification failed for {config['id']}: {'; '.join(verification['errors'])}")
    return output, verification


def _package_pdf_hash(problem_dir):
    path = Path(problem_dir) / "problem.pdf"
    if path.is_symlink():
        raise ProbHubError(
            f"package PDF must not be a symlink: {path}",
            code="unsafe_package_source",
        )
    if not path.exists():
        return None
    if not path.is_file():
        raise ProbHubError(
            f"package PDF must be a regular file: {path}",
            code="unsafe_package_source",
        )
    return hash_file(path)


def create_package_plan(root, workspace, entries):
    root = Path(root).resolve()
    workspace = copy.deepcopy(workspace)
    lint = lint_workspace(root, workspace, entries)
    if not lint["ok"]:
        _raise_lint_errors(lint)
    selected = []
    for entry in entries:
        problem_dir, config = load_problem(root, entry)
        problem_dir = problem_dir.resolve()
        canonical_entry = copy.deepcopy(entry)
        canonical_entry["directory"] = problem_dir.relative_to(root).as_posix()
        selected.append(
            PackageProblem(
                entry=canonical_entry,
                problem_dir=problem_dir,
                config=copy.deepcopy(config),
                source_hash=compute_source_hash(problem_dir, config),
                data_hash=compute_data_hash(problem_dir, config),
                pdf_hash=_package_pdf_hash(problem_dir),
            )
        )
    return PackagePlan(
        root=root,
        workspace=workspace,
        selected=tuple(selected),
        batch_id=uuid.uuid4().hex,
    )


def _package_snapshot_ignore(source_root):
    source_root = Path(source_root)
    ignored_directories = {
        ".git",
        ".hg",
        ".mypy_cache",
        ".preview",
        ".probhub",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        "__pycache__",
        "node_modules",
    }
    generated_at_root = {
        "domjudge-problem.ini",
        "meta.json",
        "output_validators",
        "problem.yaml",
    }

    def ignore(directory, names):
        directory = Path(directory)
        result = [name for name in names if name in ignored_directories]
        if directory == source_root:
            result.extend(name for name in names if name in generated_at_root)
        return result

    return ignore


@contextmanager
def create_package_snapshot(plan):
    temporary_root = None
    try:
        try:
            temporary_root = Path(tempfile.mkdtemp(
                prefix=f".{plan.root.name}-probhub-package-{plan.batch_id[:8]}-",
                dir=plan.root.parent,
            ))
            snapshot_root = temporary_root / "workspace"
            snapshot_root.mkdir()
            selected = []
            for item in plan.selected:
                relative = Path(item.entry.get("directory", item.entry["id"]))
                target = snapshot_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(
                    item.problem_dir,
                    target,
                    ignore=_package_snapshot_ignore(item.problem_dir),
                    symlinks=True,
                )
                problem_dir, config = load_problem(snapshot_root, item.entry)
                snapshot_item = PackageProblem(
                    entry=copy.deepcopy(item.entry),
                    problem_dir=problem_dir.resolve(),
                    config=copy.deepcopy(config),
                    source_hash=compute_source_hash(problem_dir, config),
                    data_hash=compute_data_hash(problem_dir, config),
                    pdf_hash=_package_pdf_hash(problem_dir),
                )
                changed = []
                if snapshot_item.source_hash != item.source_hash:
                    changed.append("source_hash")
                if snapshot_item.data_hash != item.data_hash:
                    changed.append("data_hash")
                if snapshot_item.pdf_hash != item.pdf_hash:
                    changed.append("problem.pdf")
                if changed:
                    raise ProbHubError(
                        f"package inputs changed while creating the snapshot: "
                        f"{item.config['id']}: {', '.join(changed)}",
                        code="inputs_changed",
                    )
                selected.append(snapshot_item)
            yield PackageSnapshot(
                temporary_root=temporary_root,
                root=snapshot_root,
                selected=tuple(selected),
            )
        except ProbHubError:
            raise
        except Exception as exc:
            raise ProbHubError(
                f"failed to create package snapshot: {exc}",
                code="snapshot_failed",
            ) from exc
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


def assert_package_inputs_unchanged(plan):
    try:
        _, workspace = load_workspace(plan.root)
        current_entries = {
            entry["id"]: entry
            for entry in select_entries(
                workspace,
                [item.config["id"] for item in plan.selected],
            )
        }
        changed = []
        for item in plan.selected:
            problem_id = item.config["id"]
            entry = current_entries[problem_id]
            problem_dir, config = load_problem(plan.root, entry)
            if problem_dir.resolve() != item.problem_dir:
                changed.append(f"{problem_id}.workspace_entry")
                continue
            if compute_source_hash(problem_dir, config) != item.source_hash:
                changed.append(f"{problem_id}.source_hash")
            if compute_data_hash(problem_dir, config) != item.data_hash:
                changed.append(f"{problem_id}.data_hash")
            if _package_pdf_hash(problem_dir) != item.pdf_hash:
                changed.append(f"{problem_id}.problem.pdf")
    except ProbHubError as exc:
        if exc.code == "inputs_changed":
            raise
        raise ProbHubError(
            f"package inputs changed while validating the live workspace: {exc}",
            code="inputs_changed",
        ) from exc
    except Exception as exc:
        raise ProbHubError(
            f"package inputs changed while validating the live workspace: {exc}",
            code="inputs_changed",
        ) from exc
    if changed:
        raise ProbHubError(
            "package inputs changed during the run: " + ", ".join(changed),
            code="inputs_changed",
        )


def package_workspace(root, workspace, entries, require_pdf=True):
    root = Path(root).resolve()
    with workspace_build_lock(root):
        _, live_workspace = load_workspace(root)
        recover_workspace_transactions(root, live_workspace)
        selected_ids = [entry["id"] for entry in entries]
        live_entries = select_entries(live_workspace, selected_ids)
        plan = create_package_plan(root, live_workspace, live_entries)
        with create_package_snapshot(plan) as snapshot:
            packages = {}
            for item in snapshot.selected:
                package_path, verification = package_problem(
                    snapshot.root,
                    item.problem_dir,
                    item.config,
                    require_pdf=require_pdf,
                )
                packages[item.config["id"]] = {
                    "path": str(plan.root / f"{item.config['id']}.zip"),
                    "verification": verification,
                }

            assert_package_inputs_unchanged(plan)
            specs = _package_publish_entry_specs(plan, snapshot)
            _assert_publish_paths_available(target for _, target in specs)
            _publish_transaction(plan.root, plan.batch_id, specs)
            return {
                "ok": True,
                "batch_id": plan.batch_id,
                "packages": packages,
            }


def build_workspace(root, workspace, entries, run_judge=True, use_judge_cache=True):
    root = Path(root).resolve()
    with workspace_build_lock(root):
        _, live_workspace = load_workspace(root)
        recover_workspace_transactions(root, live_workspace)
        selected_ids = [entry["id"] for entry in entries]
        live_entries = select_entries(live_workspace, selected_ids)
        plan = create_build_plan(root, live_workspace, live_entries)
        sealed_checkpoints = require_collection_sealed(plan)
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
                        "summaries": result.get("summaries", []),
                        "calibration": result.get("calibration"),
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
                    sealed_checkpoints[problem_id]["revision_id"],
                )

            assert_build_inputs_unchanged(plan)
            assert_collection_seals_unchanged(plan, sealed_checkpoints)
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
