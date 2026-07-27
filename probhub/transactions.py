"""Workspace transaction validation, discovery, and recovery coordination."""

import json
import re
from pathlib import Path, PurePosixPath

from .errors import ProbHubError
from .workspace import load_workspace, problem_entries, resolve_problem_dir


TRANSACTION_JOURNAL_SCHEMA_VERSION = 1
TRANSACTION_PHASE_PREPARED = "prepared"
TRANSACTION_PHASE_COMMITTED = "committed"
_CHILD_NAME = re.compile(r"^(?:backup|payload)-(?:0|[1-9][0-9]*)$")


def validate_transaction_directory(transaction, parent, prefix):
    """Reject symlinked or escaped transaction directories before recovery."""
    transaction = Path(transaction)
    parent = Path(parent).resolve()
    if transaction.is_symlink() or not transaction.is_dir():
        raise ValueError(f"invalid transaction directory: {transaction}")
    resolved = transaction.resolve()
    resolved.relative_to(parent)
    if resolved.parent != parent or not resolved.name.startswith(prefix):
        raise ValueError(f"invalid transaction directory: {transaction}")
    return resolved


def _validate_journal_entries(transaction, entries, target_key):
    validated = []
    targets = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"journal entry {index} must be an object")
        target = entry.get(target_key)
        if not isinstance(target, str) or not target:
            raise ValueError(f"journal entry {index} has an invalid {target_key}")
        folded = target.casefold()
        if folded in targets:
            raise ValueError(f"duplicate journal target: {target}")
        targets.add(folded)
        existed = entry.get("existed")
        if not isinstance(existed, bool):
            raise ValueError(f"journal entry {index} has an invalid existed flag")
        backup = entry.get("backup")
        if existed and not backup:
            raise ValueError(f"journal entry {index} is missing its backup")
        if not existed and backup is not None:
            raise ValueError(
                f"journal entry {index} must not have a backup for a new target"
            )
        if backup:
            transaction_child(
                transaction,
                backup,
                expected_prefix="backup",
                index=index,
            )
        payload = entry.get("payload")
        if payload:
            transaction_child(
                transaction,
                payload,
                expected_prefix="payload",
                index=index,
            )
        validated.append(entry)
    return validated


def read_transaction_journal(transaction, *, target_key="target"):
    journal_path = Path(transaction) / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(journal, dict):
        raise ValueError("journal must be an object")
    if journal.get("schema_version") != TRANSACTION_JOURNAL_SCHEMA_VERSION:
        raise ValueError("unsupported journal schema")
    phase = journal.get("phase", TRANSACTION_PHASE_PREPARED)
    if phase not in {TRANSACTION_PHASE_PREPARED, TRANSACTION_PHASE_COMMITTED}:
        raise ValueError(f"invalid journal phase: {phase!r}")
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    return journal, phase, _validate_journal_entries(
        transaction,
        entries,
        target_key,
    )


def transaction_child(transaction, name, *, expected_prefix=None, index=None):
    """Resolve a journal child name without trusting on-disk path syntax."""
    if not isinstance(name, str) or not _CHILD_NAME.fullmatch(name):
        raise ValueError(f"invalid transaction child name: {name!r}")
    if expected_prefix is not None:
        expected = f"{expected_prefix}-{index}"
        if name != expected:
            raise ValueError(
                f"invalid transaction child name: expected {expected!r}, got {name!r}"
            )
    transaction = Path(transaction).resolve()
    child = transaction / name
    if child.is_symlink():
        raise ValueError(f"transaction child must not be a symlink: {child}")
    child.resolve(strict=False).relative_to(transaction)
    return child


def resolve_journal_target(root, relative):
    if not isinstance(relative, str):
        raise ValueError(f"invalid publish journal target: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise ValueError(f"invalid publish journal target: {relative}")
    root = Path(root).resolve()
    target = root
    for part in pure.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError(f"publish journal target must not use symlinks: {relative}")
    target.parent.resolve().relative_to(root)
    return target.absolute()


def _transaction_directories(root, workspace=None):
    root = Path(root).resolve()
    if workspace is None:
        _, workspace = load_workspace(root, allow_empty=True)

    found = []
    transaction_root = root / ".probhub"
    if transaction_root.is_dir():
        found.extend(transaction_root.glob("build-publish-*"))

    for entry in problem_entries(workspace):
        problem_dir = resolve_problem_dir(root, entry)
        if not problem_dir.is_dir():
            continue
        found.extend(problem_dir.glob(".probhub-gen-publish-*"))
        found.extend(problem_dir.glob(".probhub-fixate-*"))
    return tuple(sorted({path.absolute() for path in found}, key=str))


def pending_workspace_transactions(root, workspace=None):
    """Return workspace-relative pending transaction paths without mutating."""
    root = Path(root).resolve()
    pending = []
    for path in _transaction_directories(root, workspace):
        try:
            pending.append(path.relative_to(root).as_posix())
        except ValueError:
            pending.append(str(path))
    return pending


def ensure_no_pending_transactions(root, workspace=None):
    pending = pending_workspace_transactions(root, workspace)
    if pending:
        raise ProbHubError(
            "workspace has interrupted publish transactions that require a "
            "writer recovery pass: " + ", ".join(pending),
            code="recovery_required",
        )


def recover_workspace_transactions(root, workspace=None):
    """Recover every known transaction type while the caller holds build.lock."""
    root = Path(root).resolve()
    if workspace is None:
        _, workspace = load_workspace(root, allow_empty=True)

    # Lazy imports avoid module cycles: datagen imports stress helpers and the
    # build orchestrator imports this coordinator.
    from .building import _recover_build_publish_transactions
    from .datagen import _recover_generated_transactions
    from .stressing import _recover_fixate_transactions

    _recover_build_publish_transactions(root)
    for entry in problem_entries(workspace):
        problem_dir = resolve_problem_dir(root, entry)
        _recover_generated_transactions(problem_dir)
        _recover_fixate_transactions(problem_dir)
