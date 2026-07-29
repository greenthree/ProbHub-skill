import importlib.util
from importlib import metadata as importlib_metadata
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .process_control import run_managed_to_files


def _command_probe(command, args=("--version",)):
    path = shutil.which(command)
    if not path:
        return {"ok": False, "path": None, "lines": [], "diagnostic": None}
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-doctor-") as temp:
            stdout_path = Path(temp) / "stdout"
            stderr_path = Path(temp) / "stderr"
            result = run_managed_to_files(
                [path, *args],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=10,
                memory_limit_mb=512,
                output_limit_bytes=1024 * 1024,
                process_limit=8,
            )
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
        lines = (stdout or stderr).strip().splitlines()
        if result["reason"] != "completed":
            diagnostic = "timed out after 10s" if result["reason"] == "time_limit" else (result.get("message") or result["reason"])
            return {"ok": False, "path": path, "lines": lines, "diagnostic": diagnostic}
        return {
            "ok": result["returncode"] == 0,
            "path": path,
            "lines": lines,
            "diagnostic": None if result["returncode"] == 0 else (lines[0] if lines else "unknown"),
        }
    except OSError as exc:
        return {"ok": False, "path": path, "lines": [], "diagnostic": str(exc)}


def _command_version(command, args=("--version",)):
    probe = _command_probe(command, args)
    version = probe["lines"][0] if probe["lines"] else (probe["diagnostic"] or "unknown")
    return {"ok": probe["ok"], "path": probe["path"], "version": version}


def _version_tuple(text, *, prefix=""):
    match = re.search(rf"(?:^|\s){re.escape(prefix)}(\d+)\.(\d+)\.(\d+)(?:\s|$)", text or "")
    return tuple(map(int, match.groups())) if match else None


def run_doctor():
    node = _command_version("node")
    node_version = _version_tuple(node["version"], prefix="v")
    node.update({
        "requirement": ">=18",
        "version_ok": bool(node_version and node_version >= (18, 0, 0)),
    })
    node["ok"] = node["ok"] and node["version_ok"]

    typst = _command_version("typst")
    typst_version = _version_tuple(typst["version"], prefix="typst ")
    typst_version_ok = typst_version == (0, 14, 2)
    font_probe = _command_probe("typst", ("fonts",)) if typst["ok"] else {
        "ok": False,
        "lines": [],
        "diagnostic": typst.get("version"),
    }
    required_font = "Noto Sans CJK SC"
    font_ok = font_probe["ok"] and any(
        line.strip() == required_font for line in font_probe["lines"]
    )
    typst.update({
        "requirement": "0.14.2",
        "version_ok": typst_version_ok,
        "required_font": required_font,
        "font_ok": font_ok,
        "font_diagnostic": None if font_ok else (
            font_probe.get("diagnostic") or f"{required_font} was not found by `typst fonts`"
        ),
    })
    typst["ok"] = typst["ok"] and typst_version_ok and font_ok

    tools = {
        "python": {
            "ok": sys.version_info >= (3, 10),
            "path": sys.executable,
            "version": platform.python_version(),
            "requirement": ">=3.10",
        },
        "g++": _command_version("g++"),
        "typst": typst,
        "node": node,
        "npm": _command_version("npm"),
    }
    modules = {
        name: importlib.util.find_spec(name) is not None
        for name in ("flask", "yaml", "pypdf")
    }
    distributions = {"flask": "Flask", "yaml": "PyYAML", "pypdf": "pypdf"}
    module_versions = {}
    for name, distribution in distributions.items():
        if not modules[name]:
            module_versions[name] = None
            continue
        try:
            module_versions[name] = importlib_metadata.version(distribution)
        except importlib_metadata.PackageNotFoundError:
            module_versions[name] = "unknown"
    return {
        "ok": all(item["ok"] for item in tools.values()) and all(modules.values()),
        "tools": tools,
        "python_modules": modules,
        "python_module_versions": module_versions,
    }
