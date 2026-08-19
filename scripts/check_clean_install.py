#!/usr/bin/env python3
"""Install exact npm packages in isolation and run the full sample delivery flow."""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

if __package__:
    from scripts.check_release import (
        ROOT,
        ReleaseCheckError,
        run_bounded,
        validate_metadata,
        validate_pack_inventories,
    )
    from scripts.check_published_release import (
        DEFAULT_REGISTRY,
        PublishedReleaseError,
        atomic_write_json,
        validate_npm_registry,
    )
else:
    from check_release import (
        ROOT,
        ReleaseCheckError,
        run_bounded,
        validate_metadata,
        validate_pack_inventories,
    )
    from check_published_release import (
        DEFAULT_REGISTRY,
        PublishedReleaseError,
        atomic_write_json,
        validate_npm_registry,
    )


WINDOWS_NODE_CHILD_LAUNCHER = (
    "const {spawnSync}=require('child_process');"
    "const child=spawnSync(process.argv[1],process.argv.slice(2),{stdio:'inherit'});"
    "if(child.error){console.error(child.error.message);process.exit(1);}"
    "process.exit(child.status===null?1:child.status);"
)


class CleanInstallError(RuntimeError):
    pass


# The generated scaffold defaults to a one-second limit.  That is appropriate
# for a new problem, but too close to Windows process-startup jitter for this
# release smoke test.  Keep the fixture's resource semantics intact while
# leaving enough margin to test the delivery flow deterministically.
E2E_TIME_LIMIT_SECONDS = 5


for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _run(command, *, cwd, env=None, timeout=1200):
    result = run_bounded(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        output_limit=16 * 1024 * 1024,
    )
    if result["reason"] != "completed" or result["returncode"]:
        detail = (result["stdout"] + "\n" + result["stderr"] + "\n" + str(result.get("message") or ""))[-12000:]
        raise CleanInstallError(
            f"command failed ({result.get('returncode')} / {result['reason']}): "
            f"{' '.join(map(str, command))}\n{detail}"
        )
    return result["stdout"]


def _run_json(command, *, cwd, env=None, timeout=1200):
    output = _run(command, cwd=cwd, env=env, timeout=timeout)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CleanInstallError(f"command did not return JSON: {output[-4000:]}") from exc
    if not isinstance(payload, dict) or not payload.get("ok", False):
        raise CleanInstallError(f"command returned a failing result: {payload!r}")
    return payload


def _bin_path(prefix, name):
    suffix = ".cmd" if os.name == "nt" else ""
    path = Path(prefix) / "node_modules" / ".bin" / f"{name}{suffix}"
    if not path.is_file():
        raise CleanInstallError(f"npm bin was not installed: {path}")
    return path


def _venv_python(venv):
    return Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _base_python():
    candidate = Path(getattr(sys, "_base_executable", "") or sys.executable)
    return candidate if candidate.is_file() else Path(sys.executable)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _delivery_identity(workspace, status):
    problem = status["problems"]["E2E"]
    manifest = dict(problem.get("manifest") or {})
    # A sealed revision binds measured Judge evidence, so a fresh no-cache
    # run intentionally receives a new revision identity even when all
    # reproducible delivery inputs and artifacts are unchanged.
    for volatile in ("batch_id", "built_at", "sealed_revision_id"):
        manifest.pop(volatile, None)
    data_root = Path(workspace) / "E2E/data"
    data = {
        path.relative_to(data_root).as_posix(): _sha256(path)
        for path in sorted(data_root.rglob("*"))
        if path.is_file()
    }
    return {
        "data": data,
        "manifest": manifest,
        "pdf": _sha256(Path(workspace) / "E2E/problem.pdf"),
        "package": _sha256(Path(workspace) / "E2E.zip"),
    }


def _configure_checker_qa(workspace):
    """Add a small Judge QA contract to the generated custom problem.

    The files are written into the temporary clean-install workspace rather
    than the package, so this exercises the installed Core without adding test
    fixtures to the published npm artifact.
    """
    problem = Path(workspace) / "E2E"
    config_path = problem / "probhub.yaml"
    config = config_path.read_text(encoding="utf-8")
    marker = "  checker: code/checker.cpp\n"
    if marker not in config:
        raise CleanInstallError("generated custom problem has no checker configuration")
    qa = (
        "  qa:\n"
        "    schema_version: 1\n"
        "    robustness:\n"
        "      baseline: accepts-sample\n"
        "      probes: [empty, truncated, extra-token]\n"
        "    cases:\n"
        "    - id: accepts-sample\n"
        "      purpose: valid-alternative\n"
        "      case: sample/1\n"
        "      contestant_output: judge-fixtures/checker/accepted.out\n"
        "      expected: {status: AC}\n"
    )
    config_path.write_text(config.replace(marker, marker + qa, 1), encoding="utf-8")
    output = problem / "judge-fixtures/checker/accepted.out"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"3\n")


def _set_e2e_time_limit(workspace, seconds=E2E_TIME_LIMIT_SECONDS):
    """Give the generated clean-install problem deterministic timing headroom."""
    config_path = Path(workspace) / "E2E/probhub.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CleanInstallError(f"failed to read generated E2E config: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("limits"), dict):
        raise CleanInstallError("generated E2E config has invalid limits")
    config["limits"]["time"] = seconds
    try:
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        raise CleanInstallError(f"failed to write generated E2E config: {exc}") from exc


def _installed_package_metadata(package_root):
    package_path = Path(package_root) / "package.json"
    if not package_path.is_file():
        raise CleanInstallError(f"installed npm package has no package.json: {package_root}")
    try:
        payload = json.loads(package_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanInstallError(f"installed npm package metadata is invalid: {package_root}") from exc
    if not isinstance(payload, dict):
        raise CleanInstallError(f"installed npm package metadata is not an object: {package_root}")
    return payload


def _package_install_plan(*, registry_version, registry_url, dist):
    if registry_version is None:
        metadata = validate_metadata()
        source = "local-tarballs"
        inventories = validate_pack_inventories(dry_run=False, destination=dist)
        install_targets = [
            Path(dist) / inventories["main"]["filename"],
            Path(dist) / inventories["compat"]["filename"],
        ]
    else:
        published = validate_npm_registry(registry_version, registry_url=registry_url)
        metadata = {"version": registry_version}
        source = "npm-registry"
        inventories = published["inventories"]
        install_targets = [
            f"probhub@{metadata['version']}",
            f"probhub-skill@{metadata['version']}",
        ]
    return {
        "metadata": metadata,
        "source": source,
        "inventories": inventories,
        "install_targets": install_targets,
    }


def run_clean_install(*, registry_version=None, registry_url=DEFAULT_REGISTRY):
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise CleanInstallError("node and npm are required")

    with tempfile.TemporaryDirectory(prefix="ProbHub 发布闭环 ") as temp:
        root = Path(temp)
        dist = root / "npm packages"
        prefix = root / "npm prefix"
        venv = root / "python venv"
        home = root / "isolated home"
        compat_home = root / "compat isolated home"
        documented_project = root / "documented PowerShell project"
        documented_workspace = documented_project / "赛事 工作区"
        documented_user_base = root / "documented Python user base"
        project = root / "含空格的项目"
        workspace = project / "赛事 工作区"
        for directory in (
            dist,
            prefix,
            home,
            compat_home,
            documented_project,
            documented_user_base,
            project,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        plan = _package_install_plan(
            registry_version=registry_version,
            registry_url=registry_url,
            dist=dist,
        )
        metadata = plan["metadata"]
        source = plan["source"]
        inventories = plan["inventories"]
        install_targets = plan["install_targets"]
        _run(
            [
                npm, "install", "--ignore-scripts", "--no-audit", "--no-fund",
                "--package-lock=false", "--prefix", prefix,
                f"--registry={registry_url}", *install_targets,
            ],
            cwd=root,
            timeout=600,
        )

        installed_main = prefix / "node_modules/probhub"
        installed_compat = prefix / "node_modules/probhub-skill"
        if not installed_main.is_dir() or not installed_compat.is_dir():
            raise CleanInstallError(f"both npm packages were not installed from {source}")
        if not (installed_main / "bin/python.js").is_file():
            raise CleanInstallError("installed probhub package is missing the Python bootstrap")
        main_package = _installed_package_metadata(installed_main)
        compat_package = _installed_package_metadata(installed_compat)
        if main_package.get("name") != "probhub" or main_package.get("version") != metadata["version"]:
            raise CleanInstallError(f"installed main package identity mismatch: {main_package!r}")
        if (
            compat_package.get("name") != "probhub-skill"
            or compat_package.get("version") != metadata["version"]
            or compat_package.get("dependencies") != {"probhub": metadata["version"]}
        ):
            raise CleanInstallError(f"installed compatibility package identity mismatch: {compat_package!r}")

        base_env = os.environ.copy()
        base_env.pop("PYTHONPATH", None)
        if not base_env.get("TYPST_PACKAGE_CACHE_PATH"):
            if os.name == "nt" and base_env.get("LOCALAPPDATA"):
                package_cache = Path(base_env["LOCALAPPDATA"]) / "typst/packages"
            else:
                cache_home = Path(base_env.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
                package_cache = cache_home / "typst/packages"
            base_env["TYPST_PACKAGE_CACHE_PATH"] = str(package_cache)

        probhub = _bin_path(prefix, "probhub")
        probhub_skill = _bin_path(prefix, "probhub-skill")
        base_python = _base_python()
        documented_env = base_env.copy()
        documented_env.update({
            "PYTHON": str(base_python),
            "PYTHONUSERBASE": str(documented_user_base),
            "PROBHUB_ALLOW_SYSTEM_PYTHON": "1",
        })
        if os.name == "nt":
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if not powershell:
                raise CleanInstallError("PowerShell is required for the documented Windows install smoke")
            quoted_entry = str(probhub_skill).replace("'", "''")
            _run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"$env:PROBHUB_ALLOW_SYSTEM_PYTHON = '1'; & '{quoted_entry}' --local",
                ],
                cwd=documented_project,
                env=documented_env,
            )
        else:
            _run([probhub_skill, "--local"], cwd=documented_project, env=documented_env)
        for agent_dir in (
            documented_project / ".claude/skills/probhub",
            documented_project / ".agents/skills/probhub",
        ):
            marker = agent_dir / ".probhub-version.json"
            if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8"))["version"] != metadata["version"]:
                raise CleanInstallError(f"documented Skill installation is incomplete: {agent_dir}")
        _run_json([probhub, "--json", "doctor"], cwd=documented_project, env=documented_env)
        _run_json(
            [
                probhub,
                "--json",
                "init",
                documented_workspace,
                "--title",
                "Documented Install",
                "--subtitle",
                "正式赛",
                "--author",
                "ProbHub CI",
            ],
            cwd=documented_project,
            env=documented_env,
        )
        _run_json(
            [probhub, "--workspace", documented_workspace, "--json", "ui", "--check"],
            cwd=documented_project,
            env=documented_env,
        )

        venv_command = [base_python, "-m", "venv", venv]
        if os.name == "nt":
            venv_command = [node, "-e", WINDOWS_NODE_CHILD_LAUNCHER, *venv_command]
        _run(venv_command, cwd=root, timeout=300)
        python = _venv_python(venv)
        missing_before = _run(
            [
                python,
                "-c",
                "import importlib.util, json; "
                "print(json.dumps({name: importlib.util.find_spec(name) is None "
                "for name in ('flask', 'yaml', 'pypdf', 'tree_sitter', 'tree_sitter_cpp')}))",
            ],
            cwd=root,
        )
        if not all(json.loads(missing_before).values()):
            raise CleanInstallError("fresh venv unexpectedly contains ProbHub runtime dependencies")

        env = base_env.copy()
        env["PYTHON"] = str(python)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        shadow_sentinel = root / "shadow-module-executed"
        shadow_payload = (
            "import os\nfrom pathlib import Path\n"
            "Path(os.environ['PROBHUB_SHADOW_SENTINEL']).write_text('executed')\n"
            "raise SystemExit(97)\n"
        )
        for shadow_name in ("probhub.py", "runpy.py", "sitecustomize.py"):
            (project / shadow_name).write_text(shadow_payload, encoding="utf-8")
        env["PROBHUB_SHADOW_SENTINEL"] = str(shadow_sentinel)
        version = _run([probhub, "--version"], cwd=project, env=env).strip()
        if version != metadata["version"]:
            raise CleanInstallError(f"installed CLI version mismatch: {version}")
        for package_bin in (installed_main / "bin/probhub.js", installed_compat / "bin/probhub.js"):
            forwarded = _run([node, package_bin, "--version"], cwd=project, env=env).strip()
            if forwarded != metadata["version"]:
                raise CleanInstallError(f"package forwarding bin version mismatch: {forwarded}")
        if shadow_sentinel.exists():
            raise CleanInstallError("workspace Python modules shadowed the packaged Core")

        _run([node, installed_main / "bin/init.js"], cwd=project, env=env)
        for agent_dir in (home / ".claude/skills/probhub", home / ".agents/skills/probhub"):
            version_file = agent_dir / ".probhub-version.json"
            if not version_file.is_file() or json.loads(version_file.read_text(encoding="utf-8"))["version"] != metadata["version"]:
                raise CleanInstallError(f"Skill injection is incomplete: {agent_dir}")
            if not (agent_dir / "probhub/cli.py").is_file() or not (agent_dir / "references/main.typ").is_file():
                raise CleanInstallError(f"Skill payload is incomplete: {agent_dir}")

        compat_env = env.copy()
        compat_env["HOME"] = str(compat_home)
        compat_env["USERPROFILE"] = str(compat_home)
        _run(
            [node, installed_compat / "bin/init.js", "--skip-python-deps"],
            cwd=project,
            env=compat_env,
        )
        for agent_dir in (
            compat_home / ".claude/skills/probhub",
            compat_home / ".agents/skills/probhub",
        ):
            marker = agent_dir / ".probhub-version.json"
            if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8"))["version"] != metadata["version"]:
                raise CleanInstallError(
                    f"compatibility package Skill forwarding is incomplete: {agent_dir}"
                )
        if shadow_sentinel.exists():
            raise CleanInstallError("workspace Python modules shadowed a Skill installer")

        doctor = _run_json([probhub, "--json", "doctor"], cwd=project, env=env)
        required_tools = {"python", "g++", "typst", "node", "npm"}
        if required_tools - set(doctor.get("tools", {})):
            raise CleanInstallError(f"doctor omitted required tools: {doctor!r}")
        if not all(
            doctor.get("python_modules", {}).get(name)
            for name in ("flask", "yaml", "pypdf", "tree_sitter", "tree_sitter_cpp")
        ):
            raise CleanInstallError(f"doctor omitted required Python modules: {doctor!r}")
        if not doctor.get("mutation_parser", {}).get("ok"):
            raise CleanInstallError(f"doctor mutation parser smoke failed: {doctor!r}")

        initialized = _run_json(
            [probhub, "--json", "init", workspace, "--title", "Clean Install", "--subtitle", "正式赛", "--author", "ProbHub CI"],
            cwd=project,
            env=env,
        )
        if Path(initialized["workspace"]).resolve() != (workspace / ".probhub/workspace.yaml").resolve():
            raise CleanInstallError(f"init returned the wrong workspace: {initialized!r}")
        for relative in ("typst-statement/lib.typ", "typst-statement/usts.png", "typst-statement/正式赛/main.typ", "typst-statement/正式赛/problems.typ"):
            if not (workspace / relative).is_file():
                raise CleanInstallError(f"init omitted Typst template: {relative}")

        webui = _run_json(
            [probhub, "--workspace", workspace, "--json", "ui", "--check"],
            cwd=project,
            env=env,
        )
        if webui.get("version") != metadata["version"]:
            raise CleanInstallError(f"installed WebUI version mismatch: {webui!r}")

        common = [probhub, "--workspace", workspace, "--json"]
        _run_json(
            [*common, "new", "E2E", "--name", "干净安装闭环", "--judge", "custom"],
            cwd=project,
            env=env,
        )
        _set_e2e_time_limit(workspace)
        _configure_checker_qa(workspace)
        generated = _run_json([*common, "gen", "E2E", "--apply"], cwd=project, env=env)
        cases = generated.get("results", [])
        if not any(item.get("case") == "random01" and not item.get("manual", False) for item in cases):
            raise CleanInstallError(f"gen did not execute the generated recipe: {generated!r}")

        judged = _run_json([*common, "judge", "E2E", "--no-cache"], cwd=project, env=env)
        final = judged["problems"]["E2E"].get("final") or {}
        if final.get("code") != "all_expectations_met":
            raise CleanInstallError(f"judge final event mismatch: {final!r}")
        judge_qa = _run_json(
            [*common, "judge-qa", "E2E", "--no-cache"], cwd=project, env=env
        )
        qa_result = judge_qa.get("problems", {}).get("E2E", {})
        if qa_result.get("status") != "passed":
            raise CleanInstallError(f"Judge QA did not pass: {judge_qa!r}")
        sealed = _run_json([*common, "seal", "E2E"], cwd=project, env=env)
        if sealed.get("checkpoint", {}).get("state") != "sealed":
            raise CleanInstallError(f"seal did not publish a sealed checkpoint: {sealed!r}")
        _run_json([*common, "build", "E2E"], cwd=project, env=env)
        status = _run_json([*common, "status", "E2E"], cwd=project, env=env)
        if status["problems"]["E2E"].get("state") != "current":
            raise CleanInstallError(f"built problem is not current: {status!r}")
        verified = _run_json(
            [*common, "verify-package", workspace / "E2E.zip", "--require-pdf", "--problem", "E2E"],
            cwd=project,
            env=env,
        )
        if verified.get("verification_scope") != "deep":
            raise CleanInstallError(f"package was not deeply verified: {verified!r}")
        first_identity = _delivery_identity(workspace, status)

        regenerated = _run_json([*common, "gen", "E2E", "--apply"], cwd=project, env=env)
        if any(
            not item.get("manual", False) and item.get("state") != "unchanged"
            for item in regenerated.get("results", [])
        ):
            raise CleanInstallError(f"recipe rebuild was not byte-equivalent: {regenerated!r}")
        _run_json([*common, "seal", "E2E", "--no-cache"], cwd=project, env=env)
        _run_json([*common, "build", "E2E", "--no-cache"], cwd=project, env=env)
        rebuilt_status = _run_json([*common, "status", "E2E"], cwd=project, env=env)
        if rebuilt_status["problems"]["E2E"].get("state") != "current":
            raise CleanInstallError(f"rebuilt problem is not current: {rebuilt_status!r}")
        rebuilt_verified = _run_json(
            [*common, "verify-package", workspace / "E2E.zip", "--require-pdf", "--problem", "E2E"],
            cwd=project,
            env=env,
        )
        if rebuilt_verified.get("verification_scope") != "deep":
            raise CleanInstallError(f"rebuilt package was not deeply verified: {rebuilt_verified!r}")
        second_identity = _delivery_identity(workspace, rebuilt_status)
        if second_identity != first_identity:
            raise CleanInstallError(
                "rebuilding the same sources and recipes changed delivery identity: "
                f"first={first_identity!r}, second={second_identity!r}"
            )
        return {
            "ok": True,
            "code": "clean_install_passed",
            "version": metadata["version"],
            "source": source,
            "registry": registry_url if source == "npm-registry" else None,
            "packages": inventories,
            "documented_install": ["skill-install", "doctor", "ui-check"],
            "workflow": [
                "doctor", "init", "ui-check", "new", "gen", "judge",
                "judge-qa", "seal", "build", "status", "verify-package",
            ],
            "rebuild_equivalent": True,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry-version",
        help="install both exact package versions from the official npm registry",
    )
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--output", help="atomically write the structured result to this path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        result = run_clean_install(
            registry_version=args.registry_version,
            registry_url=args.registry,
        )
    except PublishedReleaseError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
        if exc.details is not None:
            result["details"] = exc.details
    except (CleanInstallError, ReleaseCheckError, OSError) as exc:
        result = {"ok": False, "code": "clean_install_failed", "error": str(exc)}
    if args.output:
        atomic_write_json(args.output, result)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"clean install delivery flow passed for ProbHub {result['version']}")
    else:
        print(f"clean install delivery flow failed: {result['error']}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
