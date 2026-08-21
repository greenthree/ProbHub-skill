"""Bounded Python dependency installation used by the npm Skill entry."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

from .dependency_lock import (
    DependencyLockError,
    current_dependency_target,
    load_dependency_lock,
    validate_target_coverage,
    verify_wheelhouse,
)
from .process_control import run_managed_to_files


WINDOWS_NODE_CHILD_LAUNCHER = (
    "const {spawnSync}=require('child_process');"
    "const child=spawnSync(process.argv[1],process.argv.slice(2),{stdio:'inherit'});"
    "if(child.error){console.error(child.error.message);process.exit(1);}"
    "process.exit(child.status===null?1:child.status);"
)


for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass


def _inside_virtual_environment():
    return bool(
        getattr(sys, "real_prefix", None)
        or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    )


def _windows_supervised_command(command):
    if os.name != "nt":
        return command
    node = shutil.which("node")
    if not node:
        raise OSError("Node.js >= 18 is required to contain the Windows Python installer")
    # Windows Store Python uses an App Execution Alias that cannot itself be
    # created suspended. Start a bounded Node supervisor first; its Python and
    # pip descendants inherit the already-assigned Job Object.
    return [node, "-e", WINDOWS_NODE_CHILD_LAUNCHER, *command]


def _pip_command(action, requirements, *, user_install=False, wheelhouse=None):
    python = os.environ.get("PYTHON") or sys.executable
    command = [
        python,
        "-m",
        "pip",
        action,
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
    ]
    if action == "download":
        if wheelhouse is None:
            raise ValueError("wheelhouse is required for pip download")
        command.extend(["--dest", str(wheelhouse), "--no-deps"])
    elif action == "install":
        if wheelhouse is None:
            raise ValueError("wheelhouse is required for offline pip install")
        command.extend([
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--force-reinstall",
            "--no-deps",
        ])
        if user_install:
            command.append("--user")
    else:
        raise ValueError(f"unsupported pip action: {action}")
    command.extend(["-r", str(requirements)])
    return _windows_supervised_command(command)


def _pip_download_command(requirements, wheelhouse):
    return _pip_command("download", requirements, wheelhouse=wheelhouse)


def _pip_install_command(requirements, *, user_install, wheelhouse):
    return _pip_command(
        "install",
        requirements,
        user_install=user_install,
        wheelhouse=wheelhouse,
    )


def main():
    user_install = not _inside_virtual_environment()
    if user_install and os.environ.get("PROBHUB_ALLOW_SYSTEM_PYTHON") != "1":
        print(
            "ProbHub refused to modify the selected Python installation without "
            "explicit consent. Set PROBHUB_ALLOW_SYSTEM_PYTHON=1 when installing "
            "the pinned dependencies into this Python is intentional, or set "
            "PYTHON to another prepared supported CPython 3.10-3.12 x86_64 interpreter.",
            file=sys.stderr,
        )
        return 1
    package_root = Path(__file__).resolve().parents[1]
    source_requirements = package_root / "requirements.txt"
    requirements = package_root / "requirements.lock"
    try:
        lock = load_dependency_lock(requirements, source_path=source_requirements)
        validate_target_coverage(lock)
    except DependencyLockError as exc:
        print(
            f"ProbHub dependency lock is invalid ({exc.code}): {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        current_dependency_target()
    except DependencyLockError as exc:
        print(
            f"ProbHub dependency installation is unsupported ({exc.code}): {exc}",
            file=sys.stderr,
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="probhub-pip-") as temp:
        temp = Path(temp)
        wheelhouse = temp / "wheelhouse"
        wheelhouse.mkdir()
        env = os.environ.copy()
        for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.pop(variable, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if user_install:
            # Distro Python remains externally managed; pinned packages are
            # installed only into the selected interpreter's user site.
            env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
        def run_pip(command, label):
            stdout_path = temp / f"{label}-stdout"
            stderr_path = temp / f"{label}-stderr"
            result = run_managed_to_files(
                command,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=600,
                memory_limit_mb=2048,
                output_limit_bytes=16 * 1024 * 1024,
                process_limit=64,
                cwd=package_root,
                env=env,
            )
            stdout = stdout_path.read_text(encoding="utf-8", errors="backslashreplace") if stdout_path.is_file() else ""
            stderr = stderr_path.read_text(encoding="utf-8", errors="backslashreplace") if stderr_path.is_file() else ""
            if stdout:
                print(stdout, end="" if stdout.endswith("\n") else "\n")
            if stderr:
                print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
            return result, stderr

        try:
            result, stderr = run_pip(
                _pip_download_command(requirements, wheelhouse),
                "download",
            )
        except OSError as exc:
            print(f"ProbHub dependency download failed: {exc}", file=sys.stderr)
            return 1
        if result["reason"] != "completed" or result["returncode"] != 0:
            if user_install and "No module named pip" in stderr:
                print(
                    "The selected Python does not provide pip. On Ubuntu, install "
                    "it with: sudo apt install python3-pip",
                    file=sys.stderr,
                )
            print(
                f"ProbHub dependency download failed: "
                f"{result.get('message') or result['reason']}",
                file=sys.stderr,
            )
            return 1
        try:
            verify_wheelhouse(lock, wheelhouse)
        except DependencyLockError as exc:
            print(
                f"ProbHub dependency download failed ({exc.code}): {exc}",
                file=sys.stderr,
            )
            return 1
        try:
            result, stderr = run_pip(
                _pip_install_command(
                    requirements,
                    user_install=user_install,
                    wheelhouse=wheelhouse,
                ),
                "install",
            )
        except OSError as exc:
            print(f"ProbHub dependency installation failed: {exc}", file=sys.stderr)
            return 1
        if result["reason"] != "completed" or result["returncode"] != 0:
            if user_install and "No module named pip" in stderr:
                print(
                    "The selected Python does not provide pip. On Ubuntu, install "
                    "it with: sudo apt install python3-pip",
                    file=sys.stderr,
                )
            print(
                f"ProbHub dependency installation failed: "
                f"{result.get('message') or result['reason']}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
