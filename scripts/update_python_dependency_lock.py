"""Generate and verify the cross-platform Python runtime dependency lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.dependency_lock import (
    DependencyLockError,
    LockedArtifact,
    RequirementPin,
    load_dependency_lock,
    normalize_project_name,
    parse_dependency_lock,
    parse_source_requirements,
    render_dependency_lock,
    validate_locked_artifact,
    validate_target_coverage,
)


DEFAULT_SOURCE = ROOT / "requirements.txt"
DEFAULT_LOCK = ROOT / "requirements.lock"
PYPI_BASE_URL = "https://pypi.org/pypi"
METADATA_LIMIT_BYTES = 8 * 1024 * 1024
ARTIFACT_LIMIT_BYTES = 128 * 1024 * 1024
TOTAL_ARTIFACT_LIMIT_BYTES = 512 * 1024 * 1024
NETWORK_TIMEOUT_SECONDS = 60
DOWNLOAD_WORKERS = 8
USER_AGENT = "ProbHub-Python-lock/1"


@dataclass(frozen=True)
class _RemoteArtifact:
    artifact: LockedArtifact
    url: str


def _bounded_response_bytes(response, limit: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise DependencyLockError(
                    "dependency_lock_update_failed",
                    f"remote response exceeds the {limit}-byte limit",
                )
        except ValueError as exc:
            raise DependencyLockError(
                "dependency_lock_update_failed",
                "remote response has an invalid Content-Length",
            ) from exc
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"remote response exceeds the {limit}-byte limit",
        )
    return payload


def fetch_pypi_metadata(pin: RequirementPin) -> dict:
    name = urllib.parse.quote(pin.name, safe="")
    version = urllib.parse.quote(pin.version, safe="")
    request = urllib.request.Request(
        f"{PYPI_BASE_URL}/{name}/{version}/json",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in {"pypi.org", "www.pypi.org"}:
                raise DependencyLockError(
                    "dependency_lock_update_failed",
                    f"unexpected PyPI metadata redirect for {pin.name}: {response.geturl()}",
                )
            payload = _bounded_response_bytes(response, METADATA_LIMIT_BYTES)
    except DependencyLockError:
        raise
    except (OSError, TimeoutError) as exc:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"cannot fetch PyPI metadata for {pin.name}=={pin.version}: {exc}",
        ) from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI returned invalid metadata for {pin.name}=={pin.version}",
        ) from exc
    if not isinstance(parsed, dict):
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI returned an invalid metadata object for {pin.name}=={pin.version}",
        )
    return parsed


def _parse_remote_artifacts(pin: RequirementPin, metadata: dict) -> tuple[_RemoteArtifact, ...]:
    info = metadata.get("info") if isinstance(metadata, dict) else None
    urls = metadata.get("urls") if isinstance(metadata, dict) else None
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI metadata is missing release fields for {pin.name}=={pin.version}",
        )
    if normalize_project_name(str(info.get("name", ""))) != pin.normalized_name:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI project identity differs for {pin.name}=={pin.version}",
        )
    if str(info.get("version", "")) != pin.version:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI version identity differs for {pin.name}=={pin.version}",
        )
    remote_artifacts = []
    filenames = set()
    for entry in urls:
        if not isinstance(entry, dict):
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"PyPI returned an invalid release file for {pin.name}=={pin.version}",
            )
        if entry.get("yanked") is True:
            continue
        package_type = entry.get("packagetype")
        if package_type == "sdist":
            continue
        if package_type != "bdist_wheel":
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"unsupported PyPI distribution type for {pin.name}: {package_type!r}",
            )
        filename = entry.get("filename")
        url = entry.get("url")
        size = entry.get("size")
        digests = entry.get("digests")
        sha256 = digests.get("sha256") if isinstance(digests, dict) else None
        artifact = LockedArtifact(filename=filename, sha256=sha256, size=size)
        try:
            validate_locked_artifact(artifact)
        except DependencyLockError as exc:
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"invalid PyPI artifact for {pin.name}=={pin.version}: {exc}",
            ) from exc
        if not isinstance(url, str):
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"PyPI artifact URL is missing for {filename}",
            )
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "files.pythonhosted.org":
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"unexpected PyPI artifact host for {filename}",
            )
        if urllib.parse.unquote(Path(parsed_url.path).name) != filename:
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"PyPI artifact filename does not match its URL: {filename}",
            )
        filename_key = filename.casefold()
        if filename_key in filenames:
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"duplicate PyPI artifact filename for {pin.name}: {filename}",
            )
        filenames.add(filename_key)
        remote_artifacts.append(_RemoteArtifact(artifact=artifact, url=url))
    if not remote_artifacts:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI release has no non-yanked wheel for {pin.name}=={pin.version}",
        )
    return tuple(
        sorted(
            remote_artifacts,
            key=lambda item: (item.artifact.filename.casefold(), item.artifact.filename),
        )
    )


def verify_pypi_artifact(url: str, artifact: LockedArtifact) -> None:
    if artifact.size > ARTIFACT_LIMIT_BYTES:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"PyPI artifact exceeds the per-file limit: {artifact.filename}",
        )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != "files.pythonhosted.org":
                raise DependencyLockError(
                    "dependency_lock_update_failed",
                    f"unexpected artifact redirect for {artifact.filename}",
                )
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) != artifact.size:
                        raise DependencyLockError(
                            "dependency_lock_update_failed",
                            f"artifact size header changed for {artifact.filename}",
                        )
                except ValueError as exc:
                    raise DependencyLockError(
                        "dependency_lock_update_failed",
                        f"invalid artifact Content-Length for {artifact.filename}",
                    ) from exc
            digest = hashlib.sha256()
            received = 0
            while True:
                chunk = response.read(min(1024 * 1024, artifact.size + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > artifact.size or received > ARTIFACT_LIMIT_BYTES:
                    raise DependencyLockError(
                        "dependency_lock_update_failed",
                        f"artifact exceeded its locked size: {artifact.filename}",
                    )
                digest.update(chunk)
    except DependencyLockError:
        raise
    except (OSError, TimeoutError) as exc:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"cannot download PyPI artifact {artifact.filename}: {exc}",
        ) from exc
    if received != artifact.size:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"artifact size changed for {artifact.filename}: expected {artifact.size}, got {received}",
        )
    actual = digest.hexdigest()
    if actual != artifact.sha256:
        raise DependencyLockError(
            "dependency_lock_update_failed",
            f"artifact SHA-256 changed for {artifact.filename}: expected {artifact.sha256}, got {actual}",
        )


def collect_artifacts(
    pins,
    *,
    metadata_fetcher=fetch_pypi_metadata,
    artifact_verifier=verify_pypi_artifact,
    max_workers=DOWNLOAD_WORKERS,
):
    """Fetch all non-yanked wheels and independently hash their bytes."""

    remote_by_name = {}
    total_size = 0
    for pin in pins:
        remote = _parse_remote_artifacts(pin, metadata_fetcher(pin))
        remote_by_name[pin.normalized_name] = remote
        total_size += sum(item.artifact.size for item in remote)
        if total_size > TOTAL_ARTIFACT_LIMIT_BYTES:
            raise DependencyLockError(
                "dependency_lock_update_failed",
                f"runtime dependency artifacts exceed the {TOTAL_ARTIFACT_LIMIT_BYTES}-byte update limit",
            )
    work = [item for remote in remote_by_name.values() for item in remote]
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
        futures = [
            executor.submit(artifact_verifier, item.url, item.artifact)
            for item in work
        ]
        for future in futures:
            try:
                future.result()
            except DependencyLockError:
                raise
            except Exception as exc:
                raise DependencyLockError(
                    "dependency_lock_update_failed",
                    f"artifact verification failed: {exc}",
                ) from exc
    return {
        name: tuple(item.artifact for item in remote)
        for name, remote in remote_by_name.items()
    }


def _requirement_snapshot(requirement) -> dict:
    return {
        "requirement": requirement.pin.text,
        "artifacts": {
            artifact.filename: {
                "sha256": artifact.sha256,
                "size": artifact.size,
            }
            for artifact in requirement.artifacts
        },
    }


def describe_changes(old_lock, new_lock) -> list[dict]:
    old = {
        item.pin.normalized_name: _requirement_snapshot(item)
        for item in (old_lock.requirements if old_lock is not None else ())
    }
    new = {
        item.pin.normalized_name: _requirement_snapshot(item)
        for item in new_lock.requirements
    }
    changes = []
    for name in sorted(set(old) | set(new)):
        before = old.get(name)
        after = new.get(name)
        if before == after:
            continue
        before_artifacts = before["artifacts"] if before else {}
        after_artifacts = after["artifacts"] if after else {}
        changes.append({
            "name": name,
            "kind": "added" if before is None else "removed" if after is None else "updated",
            "old_requirement": before["requirement"] if before else None,
            "new_requirement": after["requirement"] if after else None,
            "artifacts_added": [
                {"filename": filename, **after_artifacts[filename]}
                for filename in sorted(set(after_artifacts) - set(before_artifacts))
            ],
            "artifacts_removed": [
                {"filename": filename, **before_artifacts[filename]}
                for filename in sorted(set(before_artifacts) - set(after_artifacts))
            ],
            "artifacts_changed": [
                {
                    "filename": filename,
                    "old": before_artifacts[filename],
                    "new": after_artifacts[filename],
                }
                for filename in sorted(set(before_artifacts) & set(after_artifacts))
                if before_artifacts[filename] != after_artifacts[filename]
            ],
        })
    return changes


def atomic_write(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DependencyLockError(
            "dependency_source_missing",
            f"cannot read dependency source {path}: {exc}",
        ) from exc


def check_lock(source_path=DEFAULT_SOURCE, lock_path=DEFAULT_LOCK) -> dict:
    lock = load_dependency_lock(lock_path, source_path=source_path)
    target_coverage = validate_target_coverage(lock)
    return {
        "ok": True,
        "status": "current",
        "source": str(Path(source_path)),
        "lock": str(Path(lock_path)),
        "source_sha256": lock.source_sha256,
        "lock_sha256": lock.lock_sha256,
        "requirements": len(lock.requirements),
        "artifacts": sum(len(item.artifacts) for item in lock.requirements),
        "targets": {name: len(packages) for name, packages in target_coverage.items()},
    }


def update_lock(*, source_path=DEFAULT_SOURCE, lock_path=DEFAULT_LOCK, apply=False) -> dict:
    source_path = Path(source_path)
    lock_path = Path(lock_path)
    source_text = _read_text(source_path)
    pins = parse_source_requirements(source_text)
    artifacts = collect_artifacts(pins)
    candidate_text = render_dependency_lock(source_text, artifacts)
    candidate = parse_dependency_lock(candidate_text, source_text=source_text)
    target_coverage = validate_target_coverage(candidate)
    old_lock = None
    old_error = None
    if lock_path.is_file():
        try:
            old_lock = load_dependency_lock(lock_path)
        except DependencyLockError as exc:
            old_error = {"code": exc.code, "message": str(exc)}
    changes = describe_changes(old_lock, candidate)
    current_text = None
    try:
        current_text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    changed = current_text != candidate_text
    applied = False
    if apply and changed:
        atomic_write(lock_path, candidate_text)
        applied = True
    return {
        "ok": True,
        "status": "updated" if applied else "changes" if changed else "current",
        "applied": applied,
        "source": str(source_path),
        "lock": str(lock_path),
        "source_sha256": candidate.source_sha256,
        "lock_sha256": candidate.lock_sha256,
        "requirements": len(candidate.requirements),
        "artifacts": sum(len(item.artifacts) for item in candidate.requirements),
        "changes": changes,
        "previous_lock_error": old_error,
        "targets": {name: len(packages) for name, packages in target_coverage.items()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="verify PyPI files and atomically update the lock")
    mode.add_argument("--dry-run", action="store_true", help="verify PyPI files and show the update without writing")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)
    try:
        if args.apply or args.dry_run:
            result = update_lock(
                source_path=args.source,
                lock_path=args.lock,
                apply=args.apply,
            )
        else:
            result = check_lock(args.source, args.lock)
    except DependencyLockError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
