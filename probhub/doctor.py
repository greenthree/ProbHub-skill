import importlib.util
from importlib import metadata as importlib_metadata
import os
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .builder_fingerprint import builder_toolchain_status
from .errors import ProbHubError
from .mutation_syntax import (
    TREE_SITTER_CPP_VERSION,
    TREE_SITTER_VERSION,
    locate_cpp_mutation_syntax,
)
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
                memory_limit_mb=None,
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


def _npm_javascript_entry(npm_path):
    raw = Path(npm_path)
    try:
        resolved = raw.resolve()
    except OSError:
        resolved = raw

    candidates = []
    for executable in (resolved, raw):
        if executable.suffix.lower() in {".js", ".cjs", ".mjs"}:
            candidates.append(executable)
        parent = executable.parent
        candidates.extend((
            parent / "node_modules/npm/bin/npm-cli.js",
            parent.parent / "lib/node_modules/npm/bin/npm-cli.js",
            parent.parent / "node_modules/npm/bin/npm-cli.js",
        ))

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _npm_version(node):
    npm_path = shutil.which("npm")
    if not npm_path:
        return {"ok": False, "path": None, "version": None}
    npm_cli = _npm_javascript_entry(npm_path) if node.get("ok") else None
    if npm_cli is not None:
        probe = _command_probe(node["path"], (str(npm_cli), "--version"))
        version = (
            probe["lines"][0]
            if probe["lines"]
            else (probe["diagnostic"] or "unknown")
        )
        return {"ok": probe["ok"], "path": npm_path, "version": version}
    if os.name != "nt":
        wrapper_probe = _command_probe(
            "sh",
            ("-c", 'exec "$1" --version', "probhub-npm", npm_path),
        )
        version = (
            wrapper_probe["lines"][0]
            if wrapper_probe["lines"]
            else (wrapper_probe["diagnostic"] or "unknown")
        )
        return {
            "ok": wrapper_probe["ok"],
            "path": npm_path,
            "version": version,
        }
    return _command_version("npm")


def _version_tuple(text, *, prefix=""):
    match = re.search(rf"(?:^|\s){re.escape(prefix)}(\d+)\.(\d+)\.(\d+)(?:\s|$)", text or "")
    return tuple(map(int, match.groups())) if match else None


def _mutation_parser_probe():
    try:
        locations = locate_cpp_mutation_syntax(
            "int f(int x) { return x < 3; }\n",
            timeout=10,
        )
        if len(locations.comparisons) != 1:
            return {"ok": False, "diagnostic": "parser smoke test returned no comparison"}
    except ProbHubError as exc:
        return {"ok": False, "diagnostic": str(exc)}
    return {"ok": True, "diagnostic": None}


def run_doctor():
    node = _command_version("node")
    node_version = _version_tuple(node["version"], prefix="v")
    node.update({
        "requirement": ">=18",
        "version_ok": bool(node_version and node_version >= (18, 0, 0)),
    })
    node["ok"] = node["ok"] and node["version_ok"]

    builder_toolchain = builder_toolchain_status()
    typst_probe = builder_toolchain["typst"]
    typst = {
        "ok": typst_probe["ok"],
        "path": typst_probe["path"],
        "version": (
            f"typst {typst_probe['version']}"
            if typst_probe["version"] is not None
            else (typst_probe["diagnostic"] or "unknown")
        ),
    }
    typst_version = _version_tuple(typst["version"], prefix="typst ")
    typst_version_ok = typst_version == (0, 14, 2)
    font = builder_toolchain["font"]
    font_ok = font["ok"]
    typst.update({
        "requirement": "0.14.2",
        "version_ok": typst_version_ok,
        "required_font": font["family"],
        "font_policy": font["policy"],
        "font_sha256": font["sha256"],
        "font_ok": font_ok,
        "font_diagnostic": font["diagnostic"],
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
        "npm": _npm_version(node),
    }
    modules = {
        name: importlib.util.find_spec(name) is not None
        for name in ("flask", "yaml", "pypdf", "tree_sitter", "tree_sitter_cpp")
    }
    modules["pypdf"] = modules["pypdf"] and bool(
        builder_toolchain["pypdf_version"]
    )
    distributions = {
        "flask": "Flask",
        "yaml": "PyYAML",
        "pypdf": "pypdf",
        "tree_sitter": "tree-sitter",
        "tree_sitter_cpp": "tree-sitter-cpp",
    }
    module_versions = {}
    for name, distribution in distributions.items():
        if not modules[name]:
            module_versions[name] = None
            continue
        try:
            module_versions[name] = (
                builder_toolchain["pypdf_version"]
                if name == "pypdf"
                else importlib_metadata.version(distribution)
            )
        except importlib_metadata.PackageNotFoundError:
            module_versions[name] = "unknown"
    modules["tree_sitter"] = (
        modules["tree_sitter"]
        and module_versions["tree_sitter"] == TREE_SITTER_VERSION
    )
    modules["tree_sitter_cpp"] = (
        modules["tree_sitter_cpp"]
        and module_versions["tree_sitter_cpp"] == TREE_SITTER_CPP_VERSION
    )
    parser_probe = {"ok": False, "diagnostic": "parser dependencies are unavailable"}
    if modules["tree_sitter"] and modules["tree_sitter_cpp"]:
        parser_probe = _mutation_parser_probe()
        if not parser_probe["ok"]:
            modules["tree_sitter"] = False
            modules["tree_sitter_cpp"] = False
    return {
        "ok": all(item["ok"] for item in tools.values()) and all(modules.values()),
        "tools": tools,
        "python_modules": modules,
        "python_module_versions": module_versions,
        "mutation_parser": parser_probe,
    }
