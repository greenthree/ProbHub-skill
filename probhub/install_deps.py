"""Bounded Python dependency installation used by the npm Skill entry."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

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


def _pip_install_command(requirements, *, user_install):
    python = os.environ.get("PYTHON") or sys.executable
    command = [
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    ]
    if user_install:
        command.append("--user")
    command.extend([
        "--only-binary",
        "tree-sitter,tree-sitter-cpp",
    ])
    command.extend(["-r", str(requirements)])
    if os.name != "nt":
        return command
    node = shutil.which("node")
    if not node:
        raise OSError("Node.js >= 18 is required to contain the Windows Python installer")
    # Windows Store Python uses an App Execution Alias that cannot itself be
    # created suspended. Start a bounded Node supervisor first; its Python and
    # pip descendants inherit the already-assigned Job Object.
    return [node, "-e", WINDOWS_NODE_CHILD_LAUNCHER, *command]


def main():
    user_install = not _inside_virtual_environment()
    if user_install and os.environ.get("PROBHUB_ALLOW_SYSTEM_PYTHON") != "1":
        print(
            "ProbHub refused to modify the selected Python installation without "
            "explicit consent. Set PROBHUB_ALLOW_SYSTEM_PYTHON=1 when installing "
            "the pinned dependencies into this Python is intentional, or set "
            "PYTHON to another prepared Python 3.10+ interpreter.",
            file=sys.stderr,
        )
        return 1
    package_root = Path(__file__).resolve().parents[1]
    requirements = package_root / "requirements.txt"
    if not requirements.is_file():
        print(f"ProbHub requirements file is missing: {requirements}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="probhub-pip-") as temp:
        stdout_path = Path(temp) / "stdout"
        stderr_path = Path(temp) / "stderr"
        env = os.environ.copy()
        for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.pop(variable, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if user_install:
            # Distro Python remains externally managed; pinned packages are
            # installed only into the selected interpreter's user site.
            env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
        try:
            command = _pip_install_command(requirements, user_install=user_install)
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
        except OSError as exc:
            print(f"ProbHub dependency installation failed: {exc}", file=sys.stderr)
            return 1
        stdout = stdout_path.read_text(encoding="utf-8", errors="backslashreplace") if stdout_path.is_file() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="backslashreplace") if stderr_path.is_file() else ""
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
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
