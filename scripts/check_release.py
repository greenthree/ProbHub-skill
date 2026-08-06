#!/usr/bin/env python3
"""Validate release metadata and npm tarball inventories."""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.process_control import run_managed_to_files


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


class ReleaseCheckError(RuntimeError):
    pass


def run_bounded(command, *, cwd, timeout=120, output_limit=8 * 1024 * 1024, env=None):
    with tempfile.TemporaryDirectory(prefix="probhub-release-command-") as temp:
        stdout_path = Path(temp) / "stdout"
        stderr_path = Path(temp) / "stderr"
        result = run_managed_to_files(
            [str(item) for item in command],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=timeout,
            memory_limit_mb=2048,
            output_limit_bytes=output_limit,
            process_limit=64,
            cwd=cwd,
            env=env,
        )
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    return {**result, "stdout": stdout, "stderr": stderr}


def _load_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8-sig"))


def _python_version():
    namespace = {}
    exec((ROOT / "probhub/__init__.py").read_text(encoding="utf-8"), namespace)
    return namespace["__version__"]


def validate_metadata(tag=None, *, require_head_tag=False, require_clean=False):
    main = _load_json("package.json")
    compat = _load_json("compat/probhub-skill/package.json")
    lock = _load_json("package-lock.json")
    version = main.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ReleaseCheckError(f"invalid package version: {version!r}")

    versions = {
        "package.json": version,
        "package-lock.json": lock.get("version"),
        "package-lock root": (lock.get("packages") or {}).get("", {}).get("version"),
        "compat package": compat.get("version"),
        "Python Core": _python_version(),
    }
    mismatched = {name: value for name, value in versions.items() if value != version}
    if mismatched:
        raise ReleaseCheckError(f"version mismatch for {version}: {mismatched}")
    if compat.get("dependencies") != {"probhub": version}:
        raise ReleaseCheckError("compat package must depend on the exact matching probhub version")
    if main.get("dependencies") not in (None, {}):
        raise ReleaseCheckError("the main npm bootstrap must remain dependency-free")
    lock_root = ((lock.get("packages") or {}).get("") or {})
    if lock_root.get("dependencies") not in (None, {}):
        raise ReleaseCheckError("package-lock root dependencies must remain empty")
    if main.get("engines") != {"node": ">=18"} or compat.get("engines") != {"node": ">=18"}:
        raise ReleaseCheckError("both npm packages must require Node >=18")
    expected_publish_gate = "python scripts/check_release.py --pack --require-head-tag --require-clean"
    if (main.get("scripts") or {}).get("prepublishOnly") != expected_publish_gate:
        raise ReleaseCheckError("the main npm package must enforce the release gate before publish")
    compat_publish_gate = "npm run check && python ../../scripts/check_release.py --pack --require-head-tag --require-clean"
    if (compat.get("scripts") or {}).get("prepublishOnly") != compat_publish_gate:
        raise ReleaseCheckError("the compatibility package must enforce the release gate before publish")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "## [Unreleased]" not in changelog:
        raise ReleaseCheckError("CHANGELOG.md is missing the [Unreleased] section")
    if not re.search(rf"^## \[{re.escape(version)}\](?:\s+-\s+\d{{4}}-\d{{2}}-\d{{2}})?\s*$", changelog, re.MULTILINE):
        raise ReleaseCheckError(f"CHANGELOG.md is missing a section for {version}")

    effective_tag = tag
    if effective_tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        effective_tag = os.environ.get("GITHUB_REF_NAME")
    if effective_tag is None and require_head_tag:
        effective_tag = f"v{version}"
    if effective_tag:
        expected = f"v{version}"
        if effective_tag != expected:
            raise ReleaseCheckError(f"release tag must be {expected}, found {effective_tag}")
        git = shutil.which("git")
        if not git:
            raise ReleaseCheckError("git is required to verify a release tag")
        head = run_bounded([git, "rev-parse", "HEAD"], cwd=ROOT, timeout=30)
        tagged = run_bounded([git, "rev-list", "-n", "1", effective_tag], cwd=ROOT, timeout=30)
        if (
            head["reason"] != "completed"
            or tagged["reason"] != "completed"
            or head["returncode"]
            or tagged["returncode"]
            or head["stdout"].strip() != tagged["stdout"].strip()
        ):
            raise ReleaseCheckError(f"tag {effective_tag} does not resolve to the current HEAD")
    if require_clean:
        git = shutil.which("git")
        if not git:
            raise ReleaseCheckError("git is required to verify a clean release worktree")
        status = run_bounded(
            [git, "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT,
            timeout=30,
        )
        if status["reason"] != "completed" or status["returncode"]:
            raise ReleaseCheckError("failed to inspect the release worktree")
        if status["stdout"].strip():
            raise ReleaseCheckError("release worktree must be clean before npm publish")
    return {"version": version, "tag": effective_tag, "versions": versions}


def _parse_npm_json(stdout, command):
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseCheckError(f"invalid npm JSON from {' '.join(command)}: {stdout[-2000:]}") from exc
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict) and len(payload) == 1:
        manifest = next(iter(payload.values()))
        if isinstance(manifest, dict):
            return manifest
    raise ReleaseCheckError(f"unexpected npm pack response: {payload!r}")


def npm_pack_manifest(target=None, *, dry_run=True, destination=None):
    npm = shutil.which("npm")
    if not npm:
        raise ReleaseCheckError("npm was not found")
    command = [npm, "pack"]
    if target:
        command.append(str(target))
    if dry_run:
        command.append("--dry-run")
    command.append("--json")
    if destination is not None:
        command.extend(["--pack-destination", str(destination)])
    result = run_bounded(command, cwd=ROOT, timeout=120)
    if result["reason"] != "completed" or result["returncode"]:
        raise ReleaseCheckError(
            f"npm pack failed ({result.get('returncode')}): "
            f"{(result['stderr'] or result['stdout'] or result.get('message', ''))[-4000:]}"
        )
    return _parse_npm_json(result["stdout"], command)


def validate_pack_inventories(*, dry_run=True, destination=None):
    main = npm_pack_manifest(dry_run=dry_run, destination=destination)
    compat = npm_pack_manifest(
        ROOT / "compat/probhub-skill",
        dry_run=dry_run,
        destination=destination,
    )
    main_paths = {entry["path"] for entry in main.get("files", [])}
    compat_paths = {entry["path"] for entry in compat.get("files", [])}
    required_main = {
        "LICENSE", "README.md", "CHANGELOG.md", "SKILL.md", "package.json",
        "requirements.txt", "bin/init.js", "bin/probhub.js", "bin/python.js",
        "probhub/__init__.py", "probhub/cli.py", "probhub/install_deps.py",
        "probhub/install_skill.py", "probhub/process_control.py",
        "probhub/judge_qa.py", "probhub/judge_qa_evidence.py",
        "probhub/judge_qa_runtime.py",
        "probhub/webui_runtime.py",
        "probhub/assets/fonts/NotoSansCJKsc-Regular.otf",
        "probhub/assets/fonts/OFL.txt",
        "scripts/probhub.py", "scripts/local_judge.py", "scripts/ui.py",
        "scripts/webui/index.html", "scripts/webui/app.css", "scripts/webui/app.js",
        "scripts/webui/theme.js", "scripts/webui/mathjax-config.js",
        "scripts/webui/asset-manifest.json", "scripts/webui/vendor/fonts.css",
        "scripts/webui/vendor/tailwind.css", "scripts/webui/vendor/alpine.js",
        "scripts/webui/vendor/alpine-collapse.js", "scripts/webui/vendor/marked.js",
        "scripts/webui/vendor/mathjax-tex-svg.js", "scripts/webui/vendor/sortable.js",
        "scripts/webui/vendor/THIRD_PARTY_NOTICES.txt",
        "references/cli.md", "references/lib.typ", "references/main.typ",
        "references/problems.typ", "references/usts.png", "references/testlib.h",
        "references/installation.md",
        "references/aggregate-limit-derivation.md",
        "references/checker-interactor.md",
        "references/verification-modes.md",
    }
    missing = sorted(required_main - main_paths)
    if missing:
        raise ReleaseCheckError(f"main npm package is missing required files: {missing}")
    for prefix in ("scripts/webui/vendor/fonts/", "scripts/webui/vendor/licenses/"):
        if not any(path.startswith(prefix) for path in main_paths):
            raise ReleaseCheckError(f"main npm package is missing WebUI assets under {prefix}")
    forbidden = []
    for path in sorted(main_paths):
        lowered = path.lower()
        parts = lowered.split("/")
        if (
            "__pycache__" in parts
            or ".probhub" in parts
            or lowered == "scripts/audit_python_dependencies.py"
            or lowered.endswith((".pyc", ".pyo", ".exe", ".o", ".obj", ".zip"))
            or lowered.endswith("problem.pdf")
        ):
            forbidden.append(path)
    if forbidden:
        raise ReleaseCheckError(f"main npm package contains forbidden artifacts: {forbidden}")

    expected_compat = {"LICENSE", "README.md", "bin/init.js", "bin/probhub.js", "package.json"}
    if compat_paths != expected_compat:
        raise ReleaseCheckError(
            f"compat package inventory mismatch: expected {sorted(expected_compat)}, found {sorted(compat_paths)}"
        )
    return {
        "main": {"filename": main.get("filename"), "files": len(main_paths)},
        "compat": {"filename": compat.get("filename"), "files": len(compat_paths)},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", action="store_true", help="also inspect both npm package inventories")
    parser.add_argument("--tag", help="require this release tag and verify that it points at HEAD")
    parser.add_argument("--require-head-tag", action="store_true", help="require v<version> to point at HEAD")
    parser.add_argument("--require-clean", action="store_true", help="require a clean Git worktree")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        result = {
            "ok": True,
            "metadata": validate_metadata(
                args.tag,
                require_head_tag=args.require_head_tag,
                require_clean=args.require_clean,
            ),
        }
        if args.pack:
            result["packages"] = validate_pack_inventories()
    except (OSError, ReleaseCheckError) as exc:
        result = {"ok": False, "error": str(exc)}
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"release metadata ok: {result['metadata']['version']}")
        if "packages" in result:
            print(
                "npm package inventories ok: "
                f"probhub={result['packages']['main']['files']} files, "
                f"probhub-skill={result['packages']['compat']['files']} files"
            )
    else:
        print(f"release check failed: {result['error']}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
