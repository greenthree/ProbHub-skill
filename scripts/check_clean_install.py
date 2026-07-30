#!/usr/bin/env python3
"""Install both npm tarballs in isolation and run the full sample delivery flow."""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from check_release import (
    ROOT,
    ReleaseCheckError,
    run_bounded,
    validate_metadata,
    validate_pack_inventories,
)


class CleanInstallError(RuntimeError):
    pass


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


def run_clean_install():
    metadata = validate_metadata()
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
        project = root / "含空格的项目"
        workspace = project / "赛事 工作区"
        for directory in (dist, prefix, home, compat_home, project):
            directory.mkdir(parents=True, exist_ok=True)

        inventories = validate_pack_inventories(dry_run=False, destination=dist)
        main_tgz = dist / inventories["main"]["filename"]
        compat_tgz = dist / inventories["compat"]["filename"]
        _run(
            [
                npm, "install", "--ignore-scripts", "--no-audit", "--no-fund",
                "--package-lock=false", "--prefix", prefix, main_tgz, compat_tgz,
            ],
            cwd=root,
            timeout=600,
        )

        installed_main = prefix / "node_modules/probhub"
        installed_compat = prefix / "node_modules/probhub-skill"
        if not installed_main.is_dir() or not installed_compat.is_dir():
            raise CleanInstallError("both npm packages were not installed from local tarballs")
        if not (installed_main / "bin/python.js").is_file():
            raise CleanInstallError("installed probhub package was not the locally packed release candidate")

        _run([sys.executable, "-m", "venv", venv], cwd=root, timeout=300)
        python = _venv_python(venv)
        missing_before = _run(
            [
                python,
                "-c",
                "import importlib.util, json; "
                "print(json.dumps({name: importlib.util.find_spec(name) is None "
                "for name in ('flask', 'yaml', 'pypdf')}))",
            ],
            cwd=root,
        )
        if not all(json.loads(missing_before).values()):
            raise CleanInstallError("fresh venv unexpectedly contains ProbHub runtime dependencies")

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        if not env.get("TYPST_PACKAGE_CACHE_PATH"):
            if os.name == "nt" and env.get("LOCALAPPDATA"):
                package_cache = Path(env["LOCALAPPDATA"]) / "typst/packages"
            else:
                cache_home = Path(env.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
                package_cache = cache_home / "typst/packages"
            env["TYPST_PACKAGE_CACHE_PATH"] = str(package_cache)
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
        probhub = _bin_path(prefix, "probhub")
        _bin_path(prefix, "probhub-skill")

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
        if not all(doctor.get("python_modules", {}).get(name) for name in ("flask", "yaml", "pypdf")):
            raise CleanInstallError(f"doctor omitted required Python modules: {doctor!r}")

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
        _run_json([*common, "new", "E2E", "--name", "干净安装闭环"], cwd=project, env=env)
        generated = _run_json([*common, "gen", "E2E", "--apply"], cwd=project, env=env)
        cases = generated.get("results", [])
        if not any(item.get("case") == "random01" and not item.get("manual", False) for item in cases):
            raise CleanInstallError(f"gen did not execute the generated recipe: {generated!r}")

        judged = _run_json([*common, "judge", "E2E", "--no-cache"], cwd=project, env=env)
        final = judged["problems"]["E2E"].get("final") or {}
        if final.get("code") != "all_expectations_met":
            raise CleanInstallError(f"judge final event mismatch: {final!r}")
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
            "version": metadata["version"],
            "packages": inventories,
            "workflow": ["doctor", "init", "ui-check", "new", "gen", "judge", "seal", "build", "status", "verify-package"],
            "rebuild_equivalent": True,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        result = run_clean_install()
    except (CleanInstallError, ReleaseCheckError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"clean install delivery flow passed for ProbHub {result['version']}")
    else:
        print(f"clean install delivery flow failed: {result['error']}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
