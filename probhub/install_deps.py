"""Bounded Python dependency installation used by the npm Skill entry."""

import os
import sys
import tempfile
from pathlib import Path

from .process_control import run_managed_to_files


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


def main():
    if not _inside_virtual_environment() and os.environ.get("PROBHUB_ALLOW_SYSTEM_PYTHON") != "1":
        print(
            "ProbHub Skill dependency installation requires an activated Python "
            "virtual environment. Activate one or set PYTHON to its interpreter. "
            "Set PROBHUB_ALLOW_SYSTEM_PYTHON=1 only when modifying this Python "
            "installation is intentional.",
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
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        result = run_managed_to_files(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
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
    if result["reason"] != "completed" or result["returncode"] != 0:
        print(
            f"ProbHub dependency installation failed: "
            f"{result.get('message') or result['reason']}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
