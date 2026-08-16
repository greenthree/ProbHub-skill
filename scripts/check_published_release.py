#!/usr/bin/env python3
"""Verify one exact GitHub and npm release with bounded structured diagnostics."""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

if __package__:
    from scripts.check_release import (
        ROOT,
        SEMVER,
        ReleaseCheckError,
        run_bounded,
        validate_metadata,
        validate_pack_inventories,
    )
else:
    from check_release import (
        ROOT,
        SEMVER,
        ReleaseCheckError,
        run_bounded,
        validate_metadata,
        validate_pack_inventories,
    )


DEFAULT_REGISTRY = "https://registry.npmjs.org/"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHASUM = re.compile(r"^[0-9a-f]{40}$")


class PublishedReleaseError(RuntimeError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def _raise(code, message, *, details=None):
    raise PublishedReleaseError(code, message, details=details)


def _parse_object(stdout, *, code, source):
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        _raise(code, f"{source} returned invalid JSON", details={"tail": stdout[-2000:]})
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        _raise(code, f"{source} did not return a JSON object")
    return payload


def _run_json(command, *, code, source, timeout=120, env=None):
    result = run_bounded(command, cwd=ROOT, timeout=timeout, env=env)
    if result["reason"] != "completed" or result["returncode"]:
        detail = (result.get("stderr") or result.get("stdout") or result.get("message") or "")[-4000:]
        _raise(
            code,
            f"{source} failed",
            details={
                "reason": result.get("reason"),
                "returncode": result.get("returncode"),
                "tail": detail,
            },
        )
    return _parse_object(result["stdout"], code=f"{code}_invalid_json", source=source)


def _validate_version(version):
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        _raise("invalid_version", f"release version must be an exact semver: {version!r}")
    return version


def _validate_registry_url(registry_url):
    if registry_url != DEFAULT_REGISTRY:
        _raise(
            "registry_url_invalid",
            f"published release verification only accepts {DEFAULT_REGISTRY}",
        )
    return registry_url


def _npm_metadata(name, version, *, registry_url=DEFAULT_REGISTRY):
    npm = shutil.which("npm")
    if not npm:
        _raise("npm_missing", "npm was not found")
    return _run_json(
        [
            npm,
            "view",
            f"{name}@{version}",
            f"--registry={registry_url}",
            "--json",
        ],
        code="npm_view_failed",
        source=f"npm view {name}@{version}",
    )


def _dist_identity(metadata, *, name, version):
    package = f"{name}@{version}"
    dist = metadata.get("dist")
    if not isinstance(dist, dict):
        _raise("npm_dist_identity_invalid", f"{package} has no dist metadata")
    integrity = dist.get("integrity")
    shasum = dist.get("shasum")
    tarball = dist.get("tarball")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        _raise("npm_dist_identity_invalid", f"{package} has an invalid integrity field")
    if not isinstance(shasum, str) or not SHASUM.fullmatch(shasum):
        _raise("npm_dist_identity_invalid", f"{package} has an invalid shasum field")
    expected_tarball = f"{DEFAULT_REGISTRY}{name}/-/{name}-{version}.tgz"
    if tarball != expected_tarball:
        _raise("npm_dist_identity_invalid", f"{package} has an unexpected tarball URL")
    return {"integrity": integrity, "shasum": shasum, "tarball": tarball}


def validate_npm_registry(version, *, registry_url=DEFAULT_REGISTRY, require_latest=True):
    version = _validate_version(version)
    registry_url = _validate_registry_url(registry_url)
    packages = {}
    for name in ("probhub", "probhub-skill"):
        metadata = _npm_metadata(name, version, registry_url=registry_url)
        if metadata.get("name") != name or metadata.get("version") != version:
            _raise(
                "npm_package_identity_mismatch",
                f"registry identity mismatch for {name}@{version}",
                details={"name": metadata.get("name"), "version": metadata.get("version")},
            )
        package_id = metadata.get("_id")
        if package_id not in (None, f"{name}@{version}"):
            _raise("npm_package_identity_mismatch", f"unexpected npm _id for {name}@{version}")
        tags = metadata.get("dist-tags")
        latest = tags.get("latest") if isinstance(tags, dict) else None
        if require_latest and latest != version:
            _raise(
                "npm_latest_mismatch",
                f"{name} latest is {latest!r}, expected {version!r}",
            )
        dependencies = metadata.get("dependencies")
        if name == "probhub" and dependencies not in (None, {}):
            _raise("npm_main_dependencies_invalid", "probhub must remain dependency-free")
        if name == "probhub-skill" and dependencies != {"probhub": version}:
            _raise(
                "npm_compat_dependency_mismatch",
                f"probhub-skill@{version} must depend exactly on probhub@{version}",
                details={"dependencies": dependencies},
            )
        packages[name] = {
            "name": name,
            "version": version,
            "latest": latest,
            "dependencies": dependencies or {},
            "dist": _dist_identity(metadata, name=name, version=version),
        }
    try:
        inventories = validate_pack_inventories(
            main_target=f"probhub@{version}",
            compat_target=f"probhub-skill@{version}",
            registry_url=registry_url,
        )
    except ReleaseCheckError as exc:
        _raise("npm_inventory_invalid", str(exc))
    return {
        "registry": registry_url,
        "packages": packages,
        "inventories": inventories,
    }


def validate_github_release(version, repository):
    version = _validate_version(version)
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        _raise("github_repository_invalid", f"invalid GitHub repository: {repository!r}")
    gh = shutil.which("gh")
    if not gh:
        _raise("github_cli_missing", "GitHub CLI was not found")
    tag = f"v{version}"
    payload = _run_json(
        [
            gh,
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "name,tagName,isDraft,isPrerelease,publishedAt,targetCommitish,url",
        ],
        code="github_release_query_failed",
        source=f"GitHub Release {repository}@{tag}",
        timeout=60,
        env=os.environ.copy(),
    )
    if payload.get("tagName") != tag:
        _raise("github_release_tag_mismatch", f"GitHub Release tag is not {tag}")
    if payload.get("isDraft") is not False or payload.get("isPrerelease") is not False:
        _raise("github_release_state_mismatch", "GitHub Release must be published and stable")
    if not payload.get("publishedAt") or not payload.get("url"):
        _raise("github_release_state_mismatch", "GitHub Release is missing publication identity")
    return payload


def validate_published_release(version, repository, *, registry_url=DEFAULT_REGISTRY):
    version = _validate_version(version)
    try:
        local = validate_metadata(tag=f"v{version}")
    except ReleaseCheckError as exc:
        _raise("local_release_invalid", str(exc))
    if local.get("version") != version:
        _raise(
            "local_version_mismatch",
            f"checked out release is {local.get('version')!r}, expected {version!r}",
        )
    return {
        "version": version,
        "tag": f"v{version}",
        "local": local,
        "github": validate_github_release(version, repository),
        "npm": validate_npm_registry(version, registry_url=registry_url),
    }


def atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except (FileNotFoundError, OSError):
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="exact published semver")
    parser.add_argument("--repo", required=True, help="GitHub repository as owner/name")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--output", help="atomically write the structured result to this path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        release = validate_published_release(args.version, args.repo, registry_url=args.registry)
        result = {"ok": True, "code": "published_release_verified", "release": release}
    except PublishedReleaseError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
        if exc.details is not None:
            result["details"] = exc.details
    except OSError as exc:
        result = {"ok": False, "code": "io_error", "error": str(exc)}
    if args.output:
        atomic_write_json(args.output, result)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"published release verified: {args.version}")
    else:
        print(f"published release verification failed [{result['code']}]: {result['error']}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
