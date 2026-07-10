import importlib.util
import platform
import shutil
import subprocess
import sys


def _command_version(command, args=("--version",)):
    path = shutil.which(command)
    if not path:
        return {"ok": False, "path": None, "version": None}
    try:
        proc = subprocess.run([path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        text = (proc.stdout or proc.stderr).strip().splitlines()
        return {"ok": proc.returncode == 0, "path": path, "version": text[0] if text else "unknown"}
    except OSError as exc:
        return {"ok": False, "path": path, "version": str(exc)}


def run_doctor():
    tools = {
        "python": {"ok": True, "path": sys.executable, "version": platform.python_version()},
        "g++": _command_version("g++"),
        "typst": _command_version("typst"),
        "node": _command_version("node"),
    }
    modules = {name: importlib.util.find_spec(name) is not None for name in ("yaml", "pypdf")}
    return {"ok": all(item["ok"] for item in tools.values()) and all(modules.values()), "tools": tools, "python_modules": modules}
