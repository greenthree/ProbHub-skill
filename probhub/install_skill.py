"""Crash-recoverable installation of the packaged ProbHub Agent Skill.

The npm entry deliberately delegates filesystem publication to this stdlib-only
module.  Both supported Skill targets are committed as one transaction under an
OS file lock.  A prepared journal is rolled back after interruption; a committed
journal only has its private transaction directory cleaned up.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .build_lock import workspace_file_lock


LOCK_FILE = Path(".probhub-skill-install.lock")
LOCK_DIRECTORY = "probhub-skill-install-locks"
TRANSACTION_PREFIX = ".probhub-skill-install-"
CLEANUP_PREFIX = ".probhub-skill-cleanup-"
OWNER_FILE = "owner.json"
JOURNAL_SCHEMA_VERSION = 1
TARGETS = (
    ".claude/skills/probhub",
    ".agents/skills/probhub",
)
PACKAGE_ITEMS = ("SKILL.md", "references", "scripts", "probhub")
_TRANSACTION_NAME = re.compile(
    rf"^{re.escape(TRANSACTION_PREFIX)}[0-9a-f]{{32}}$"
)
_CLEANUP_NAME = re.compile(rf"^{re.escape(CLEANUP_PREFIX)}[0-9a-f]{{32}}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SkillInstallError(RuntimeError):
    """A safe, user-facing Skill installation failure."""


def _path_within(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _is_reparse_point(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except FileNotFoundError:
        return False


def _assert_immediate_child(path, base, pattern, label):
    path = Path(path)
    base = Path(base).resolve()
    if _is_reparse_point(path):
        raise SkillInstallError(f"{label} must not be a symlink or junction: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SkillInstallError(f"invalid {label}: {path}: {exc}") from exc
    if resolved.parent != base or not pattern.fullmatch(resolved.name):
        raise SkillInstallError(f"invalid {label}: {path}")
    return resolved


def _owner_payload(directory, base):
    directory = Path(directory)
    return {
        "schema_version": 1,
        "kind": "probhub-skill-install",
        "run_id": directory.name.rsplit("-", 1)[-1],
        "base": str(Path(base).resolve()),
    }


def _write_owner(directory, base):
    _atomic_json(Path(directory) / OWNER_FILE, _owner_payload(directory, base))


def _validate_owner(directory, base):
    marker = Path(directory) / OWNER_FILE
    if _is_reparse_point(marker) or not marker.is_file():
        raise SkillInstallError(f"invalid Skill install owner marker: {marker}")
    try:
        if marker.stat().st_size > 64 * 1024:
            raise SkillInstallError(f"Skill install owner marker is too large: {marker}")
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillInstallError(f"invalid Skill install owner marker: {marker}: {exc}") from exc
    if value != _owner_payload(directory, base):
        raise SkillInstallError(f"invalid Skill install owner marker: {marker}")


def _tree_hash(path, *, ignore_cache=False):
    path = Path(path)
    if _is_reparse_point(path) or not path.exists():
        raise SkillInstallError(f"unsafe or missing Skill tree path: {path}")
    digest = hashlib.sha256()

    def visit(current, relative):
        if _is_reparse_point(current):
            raise SkillInstallError(
                f"Skill trees must not contain symlinks or junctions: {current}"
            )
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise SkillInstallError(f"cannot inspect Skill tree path {current}: {exc}") from exc
        encoded = relative.as_posix().encode("utf-8")
        if stat.S_ISREG(metadata.st_mode):
            digest.update(b"F\0" + encoded + b"\0")
            try:
                with current.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise SkillInstallError(f"cannot read Skill tree file {current}: {exc}") from exc
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise SkillInstallError(f"Skill trees may contain only files and directories: {current}")
        digest.update(b"D\0" + encoded + b"\0")
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise SkillInstallError(f"cannot inspect Skill tree directory {current}: {exc}") from exc
        for child in children:
            if ignore_cache and (
                child.name == "__pycache__" or child.name.endswith((".pyc", ".pyo"))
            ):
                continue
            visit(Path(child.path), relative / child.name)

    visit(path, Path("."))
    return digest.hexdigest()


def _package_identity(root):
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for item in PACKAGE_ITEMS:
        source = root / item
        digest.update(item.encode("utf-8") + b"\0")
        digest.update(_tree_hash(source, ignore_cache=True).encode("ascii"))
    return digest.hexdigest()


def _copy_safe_path(source, destination):
    source = Path(source)
    destination = Path(destination)
    if _is_reparse_point(source):
        raise SkillInstallError(
            f"packaged Skill source must not contain symlinks or junctions: {source}"
        )
    try:
        metadata = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise SkillInstallError(f"cannot inspect packaged Skill source {source}: {exc}") from exc
    if stat.S_ISREG(metadata.st_mode):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise SkillInstallError(f"cannot copy packaged Skill source {source}: {exc}") from exc
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise SkillInstallError(
            f"packaged Skill source may contain only files and directories: {source}"
        )
    destination.mkdir()
    try:
        children = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as exc:
        raise SkillInstallError(f"cannot inspect packaged Skill source {source}: {exc}") from exc
    for child in children:
        if child.name == "__pycache__" or child.name.endswith(".pyc"):
            continue
        _copy_safe_path(Path(child.path), destination / child.name)


def _safe_target(base, relative, *, create_parents=False):
    if relative not in TARGETS:
        raise SkillInstallError(f"invalid Skill install target: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SkillInstallError(f"invalid Skill install target: {relative!r}")

    base = Path(base).resolve()
    current = base
    for part in pure.parts[:-1]:
        current = current / part
        if current.exists() or _is_reparse_point(current):
            if _is_reparse_point(current) or not current.is_dir():
                raise SkillInstallError(
                    f"Skill install target uses an unsafe parent: {current}"
                )
        elif create_parents:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            if _is_reparse_point(current) or not current.is_dir():
                raise SkillInstallError(
                    f"Skill install target uses an unsafe parent: {current}"
                )
        else:
            break

    target = base.joinpath(*pure.parts)
    if target.exists() or _is_reparse_point(target):
        if _is_reparse_point(target):
            raise SkillInstallError(
                f"Skill install target must not be a symlink or junction: {target}"
            )
    try:
        target.resolve(strict=False).relative_to(base)
    except (OSError, ValueError) as exc:
        raise SkillInstallError(f"Skill install target escapes its base: {target}") from exc
    return target


def _safe_transaction_child(transaction, name, expected):
    if name != expected:
        raise SkillInstallError(
            f"invalid Skill install transaction child: expected {expected!r}, got {name!r}"
        )
    child = Path(transaction) / name
    if _is_reparse_point(child):
        raise SkillInstallError(
            f"Skill install transaction child must not be a symlink or junction: {child}"
        )
    try:
        child.resolve(strict=False).relative_to(Path(transaction).resolve())
    except (OSError, ValueError) as exc:
        raise SkillInstallError(f"invalid Skill install transaction child: {child}") from exc
    return child


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_journal(transaction, base):
    _validate_owner(transaction, base)
    journal_path = Path(transaction) / "journal.json"
    if _is_reparse_point(journal_path) or not journal_path.is_file():
        raise SkillInstallError(f"invalid Skill install journal: {journal_path}")
    try:
        if journal_path.stat().st_size > 1024 * 1024:
            raise SkillInstallError(f"Skill install journal is too large: {journal_path}")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillInstallError(f"invalid Skill install journal: {journal_path}: {exc}") from exc
    if not isinstance(journal, dict):
        raise SkillInstallError("Skill install journal must be an object")
    if journal.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise SkillInstallError("unsupported Skill install journal schema")
    if journal.get("phase") not in {"prepared", "committed"}:
        raise SkillInstallError("invalid Skill install journal phase")
    entries = journal.get("entries")
    if not isinstance(entries, list) or len(entries) != len(TARGETS):
        raise SkillInstallError("Skill install journal must contain both targets")

    validated = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SkillInstallError(f"Skill install journal entry {index} must be an object")
        relative = entry.get("target")
        if relative not in TARGETS or relative in seen:
            raise SkillInstallError(f"invalid Skill install journal target: {relative!r}")
        seen.add(relative)
        if relative != TARGETS[index]:
            raise SkillInstallError("Skill install journal targets are out of order")
        if not isinstance(entry.get("existed"), bool):
            raise SkillInstallError(f"invalid existed flag for Skill target {relative}")
        old_hash = entry.get("old_hash")
        if entry["existed"]:
            if not isinstance(old_hash, str) or not _SHA256.fullmatch(old_hash):
                raise SkillInstallError(f"invalid original hash for Skill target {relative}")
        elif old_hash is not None:
            raise SkillInstallError(f"unexpected original hash for Skill target {relative}")
        payload_hash = entry.get("payload_hash")
        if not isinstance(payload_hash, str) or not _SHA256.fullmatch(payload_hash):
            raise SkillInstallError(f"invalid payload hash for Skill target {relative}")
        expected_backup = f"backup-{index}" if entry["existed"] else None
        if entry.get("backup") != expected_backup:
            raise SkillInstallError(f"invalid backup for Skill target {relative}")
        if entry.get("payload") != f"payload-{index}":
            raise SkillInstallError(f"invalid payload for Skill target {relative}")
        target = _safe_target(base, relative)
        backup = (
            _safe_transaction_child(transaction, entry["backup"], f"backup-{index}")
            if entry["backup"] is not None
            else None
        )
        payload = _safe_transaction_child(
            transaction, entry["payload"], f"payload-{index}"
        )
        validated.append((entry, target, backup, payload))
    if seen != set(TARGETS):
        raise SkillInstallError("Skill install journal does not name both targets")
    return journal, validated


def _remove_path(path):
    path = Path(path)
    if not path.exists() and not _is_reparse_point(path):
        return
    if _is_reparse_point(path):
        raise SkillInstallError(f"refusing to remove a symlink or junction: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _retire_transaction(transaction, base):
    transaction = Path(transaction)
    cleanup = Path(base) / f"{CLEANUP_PREFIX}{transaction.name[len(TRANSACTION_PREFIX):]}"
    try:
        os.replace(transaction, cleanup)
    except OSError:
        return False
    try:
        _validate_owner(cleanup, base)
        shutil.rmtree(cleanup)
    except OSError:
        return False
    return True


def _rollback_transaction(transaction, validated, base):
    failures = []
    for entry, target, backup, _payload in reversed(validated):
        try:
            if entry["existed"]:
                if backup is not None and backup.exists():
                    if _tree_hash(backup) != entry["old_hash"]:
                        raise SkillInstallError(
                            f"original Skill backup identity changed: {backup}"
                        )
                    if target.exists() or _is_reparse_point(target):
                        target_hash = _tree_hash(target)
                        if target_hash == entry["payload_hash"]:
                            _remove_path(target)
                        elif target_hash == entry["old_hash"]:
                            _remove_path(backup)
                            continue
                        else:
                            raise SkillInstallError(
                                f"refusing to replace an unowned Skill target during rollback: {target}"
                            )
                    _safe_target(base, entry["target"], create_parents=True)
                    os.replace(backup, target)
                elif not target.exists() or _tree_hash(target) != entry["old_hash"]:
                    raise SkillInstallError(
                        f"cannot prove that the original Skill target was restored: {target}"
                    )
            else:
                if target.exists() or _is_reparse_point(target):
                    if _tree_hash(target) != entry["payload_hash"]:
                        raise SkillInstallError(
                            f"refusing to remove an unowned Skill target during rollback: {target}"
                        )
                    _remove_path(target)
        except BaseException as exc:  # keep the journal even across interruption
            failures.append(f"{entry['target']}: {exc}")
    if failures:
        raise SkillInstallError("Skill install rollback failed: " + "; ".join(failures))


def _recover_locked(base):
    base = Path(base).resolve()
    for cleanup in sorted(base.glob(f"{CLEANUP_PREFIX}*"), key=lambda item: item.name):
        cleanup = _assert_immediate_child(
            cleanup, base, _CLEANUP_NAME, "Skill install cleanup directory"
        )
        _validate_owner(cleanup, base)
        try:
            shutil.rmtree(cleanup)
        except OSError:
            # A committed/rolled-back transaction may leave cleanup debris when
            # another process temporarily holds a file.  It is safe to retry.
            pass

    for transaction in sorted(
        base.glob(f"{TRANSACTION_PREFIX}*"), key=lambda item: item.name
    ):
        transaction = _assert_immediate_child(
            transaction, base, _TRANSACTION_NAME, "Skill install transaction"
        )
        journal, validated = _read_journal(transaction, base)
        if journal["phase"] == "prepared":
            _rollback_transaction(transaction, validated, base)
        _retire_transaction(transaction, base)


def _copy_package_payload(source_root, payload, version):
    source_root = Path(source_root).resolve()
    payload = Path(payload)
    payload.mkdir()
    source_before = _package_identity(source_root)
    for item in PACKAGE_ITEMS:
        source = source_root / item
        if _is_reparse_point(source) or not source.exists():
            raise SkillInstallError(f"packaged Skill source is missing or unsafe: {source}")
        destination = payload / item
        _copy_safe_path(source, destination)
    copied = _package_identity(payload)
    source_after = _package_identity(source_root)
    if source_before != copied or copied != source_after:
        raise SkillInstallError(
            "packaged Skill source changed while the immutable payload was being prepared"
        )
    _atomic_json(payload / ".probhub-version.json", {
        "version": version,
        "installedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


def _skill_lock_location(base):
    lock_root = Path(tempfile.gettempdir()).resolve() / LOCK_DIRECTORY
    if _is_reparse_point(lock_root):
        raise SkillInstallError(
            f"Skill install lock directory must not be a symlink or junction: {lock_root}"
        )
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillInstallError(f"cannot create Skill install lock directory: {exc}") from exc
    if _is_reparse_point(lock_root) or not lock_root.is_dir():
        raise SkillInstallError(
            f"Skill install lock directory is unsafe: {lock_root}"
        )
    identity = hashlib.sha256(os.fsencode(str(Path(base).resolve()))).hexdigest()
    return lock_root, Path(f"{identity}.lock")


def _validate_install_boundary(base, source_root):
    if _path_within(base, source_root):
        raise SkillInstallError(
            f"Skill install base must not be the packaged source or one of its descendants: {base}"
        )


def _package_version(source_root):
    package_json = Path(source_root) / "package.json"
    try:
        value = json.loads(package_json.read_text(encoding="utf-8"))
        version = value["version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SkillInstallError(f"cannot read packaged ProbHub version: {exc}") from exc
    if not isinstance(version, str) or not version:
        raise SkillInstallError("packaged ProbHub version is invalid")
    return version


def recover_skill_install(base_dir):
    base = Path(base_dir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    base = base.resolve()
    lock_root, lock_file = _skill_lock_location(base)
    with workspace_file_lock(
        lock_root,
        lock_file,
        busy_code="skill_install_busy",
        busy_message="another ProbHub Skill installation is already running",
        no_follow=True,
    ):
        _recover_locked(base)


def install_skill(base_dir, *, source_root=None, version=None, include_status=False):
    source_root = (
        Path(source_root).resolve()
        if source_root is not None
        else Path(__file__).resolve().parents[1]
    )
    base = Path(base_dir).expanduser().resolve()
    _validate_install_boundary(base, source_root)
    base.mkdir(parents=True, exist_ok=True)
    version = version or _package_version(source_root)
    lock_root, lock_file = _skill_lock_location(base)

    with workspace_file_lock(
        lock_root,
        lock_file,
        busy_code="skill_install_busy",
        busy_message="another ProbHub Skill installation is already running",
        no_follow=True,
    ):
        _recover_locked(base)
        suffix = uuid.uuid4().hex
        staging = base / f"{CLEANUP_PREFIX}{suffix}"
        transaction = base / f"{TRANSACTION_PREFIX}{suffix}"
        staging.mkdir()
        try:
            _write_owner(staging, base)
            entries = []
            first_payload = staging / "payload-0"
            _copy_package_payload(source_root, first_payload, version)
            second_payload = staging / "payload-1"
            _copy_safe_path(first_payload, second_payload)
            payload_hash = _tree_hash(first_payload)
            if _tree_hash(second_payload) != payload_hash:
                raise SkillInstallError("prepared Skill payloads are not identical")
            for index, relative in enumerate(TARGETS):
                target = _safe_target(base, relative, create_parents=True)
                payload = staging / f"payload-{index}"
                existed = target.exists()
                entries.append({
                    "target": relative,
                    "payload": payload.name,
                    "backup": f"backup-{index}" if existed else None,
                    "existed": existed,
                    "old_hash": _tree_hash(target) if existed else None,
                    "payload_hash": payload_hash,
                })
            journal = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "phase": "prepared",
                "entries": entries,
            }
            _atomic_json(staging / "journal.json", journal)
            _read_journal(staging, base)
            os.replace(staging, transaction)
            _, validated = _read_journal(transaction, base)

            try:
                for entry, target, backup, payload in validated:
                    _safe_target(base, entry["target"], create_parents=True)
                    if _tree_hash(payload) != entry["payload_hash"]:
                        raise SkillInstallError(
                            f"prepared Skill payload identity changed: {payload}"
                        )
                    if entry["existed"]:
                        if not target.exists() or _tree_hash(target) != entry["old_hash"]:
                            raise SkillInstallError(
                                f"Skill install target changed before publication: {target}"
                            )
                        os.replace(target, backup)
                    elif target.exists() or _is_reparse_point(target):
                        raise SkillInstallError(
                            f"Skill install target appeared before publication: {target}"
                        )
                    if target.exists() or _is_reparse_point(target):
                        raise SkillInstallError(
                            f"Skill install target changed during publication: {target}"
                        )
                    os.replace(payload, target)
                    if _tree_hash(target) != entry["payload_hash"]:
                        raise SkillInstallError(
                            f"published Skill payload identity changed: {target}"
                        )
                journal["phase"] = "committed"
                _atomic_json(transaction / "journal.json", journal)
            except BaseException as publish_error:
                try:
                    _rollback_transaction(transaction, validated, base)
                except BaseException as rollback_error:
                    raise SkillInstallError(
                        f"Skill install failed ({publish_error}); {rollback_error}"
                    ) from publish_error
                _retire_transaction(transaction, base)
                if isinstance(publish_error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise SkillInstallError(f"Skill install failed: {publish_error}") from publish_error

            # Publication is already committed.  Cleanup failure is deliberately
            # non-fatal and will be retried under the lock at the next launch.
            _retire_transaction(transaction, base)
        except BaseException:
            if staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError:
                    pass
            raise
    targets = tuple(_safe_target(base, relative) for relative in TARGETS)
    if include_status:
        return targets, tuple(bool(entry["existed"]) for entry in entries)
    return targets


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the packaged ProbHub Agent Skill")
    parser.add_argument("--base", required=True, help="home or local project directory")
    parser.add_argument("--version", help="npm package version written into the marker")
    arguments = parser.parse_args(argv)
    try:
        targets, replaced = install_skill(
            arguments.base,
            version=arguments.version,
            include_status=True,
        )
    except Exception as exc:
        print(f"ProbHub Skill installation failed: {exc}", file=sys.stderr)
        return 1
    for target, was_replaced in zip(targets, replaced):
        if was_replaced:
            print(f"已整体替换现有 ProbHub Skill 目录：{target}")
        else:
            print(f"installed ProbHub Skill: {target}")
    if any(replaced):
        print("注意：已替换目录中的本地手工修改不会保留。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
