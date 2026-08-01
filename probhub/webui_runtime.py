import importlib.util
import hashlib
import json
import os
import uuid
from pathlib import Path

from . import __version__
from .errors import ProbHubError


WEBUI_ASSET_MANIFEST = "asset-manifest.json"
WEBUI_SOURCE_ONLY_FILES = {
    WEBUI_ASSET_MANIFEST,
    "tailwind.config.cjs",
    "tailwind.input.css",
}


def _webui_asset_digest(candidate, expected):
    data = candidate.read_bytes()
    if expected.startswith("sha256-text-lf:"):
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        prefix = "sha256-text-lf:"
    elif expected.startswith("sha256:"):
        prefix = "sha256:"
    else:
        return None
    return prefix + hashlib.sha256(data).hexdigest()


def _require_webui_assets(script):
    asset_root = script.parent / "webui"
    manifest_path = asset_root / WEBUI_ASSET_MANIFEST
    if not manifest_path.is_file():
        raise ProbHubError(
            f"installed ProbHub WebUI assets are missing: {WEBUI_ASSET_MANIFEST}",
            code="webui_runtime_missing",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbHubError(
            f"installed ProbHub WebUI asset manifest is invalid: {exc}",
            code="webui_runtime_invalid",
        ) from exc
    files = manifest.get("files") if manifest.get("schema_version") == 1 else None
    if not isinstance(files, dict) or not files:
        raise ProbHubError(
            "installed ProbHub WebUI asset manifest is invalid",
            code="webui_runtime_invalid",
        )
    root = asset_root.resolve()
    actual_files = {
        path.relative_to(asset_root).as_posix()
        for path in asset_root.rglob("*")
        if path.is_file()
        and path.relative_to(asset_root).as_posix() not in WEBUI_SOURCE_ONLY_FILES
    }
    if set(files) != actual_files:
        raise ProbHubError(
            "installed ProbHub WebUI asset inventory does not match its manifest",
            code="webui_runtime_invalid",
        )
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ProbHubError(
                "installed ProbHub WebUI asset manifest is invalid",
                code="webui_runtime_invalid",
            )
        candidate = (asset_root / name).resolve()
        if (
            candidate == root
            or root not in candidate.parents
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            raise ProbHubError(
                f"installed ProbHub WebUI asset is missing or invalid: {name}",
                code="webui_runtime_missing",
            )
        actual = _webui_asset_digest(candidate, expected)
        if actual is None:
            raise ProbHubError(
                f"installed ProbHub WebUI asset manifest is invalid: {name}",
                code="webui_runtime_invalid",
            )
        if actual != expected:
            raise ProbHubError(
                f"installed ProbHub WebUI asset hash mismatch: {name}",
                code="webui_runtime_invalid",
            )


def _webui_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "ui.py"
    if not script.is_file():
        raise ProbHubError(
            f"installed ProbHub WebUI is missing: {script}",
            code="webui_runtime_missing",
        )
    _require_webui_assets(script)
    return script


def _load_webui_module():
    script = _webui_script()
    module_name = f"_probhub_webui_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise ProbHubError(
            f"installed ProbHub WebUI cannot be loaded: {script}",
            code="webui_runtime_invalid",
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        raise ProbHubError(
            f"installed ProbHub WebUI dependency is missing: {exc.name}",
            code="webui_dependency_missing",
        ) from exc
    return module, script


def _cleanup_webui_module(module):
    shutdown = getattr(module, "shutdown_webui_tasks", None)
    if callable(shutdown):
        shutdown()
    preview = getattr(module, "WEBUI_PREVIEW_TEMP", None)
    if preview is not None:
        preview.cleanup()


def check_webui(root):
    root = Path(root).resolve()
    previous = Path.cwd()
    module = None
    try:
        os.chdir(root)
        module, script = _load_webui_module()
        if not hasattr(module, "app") or not callable(getattr(module, "run_server", None)):
            raise ProbHubError(
                f"installed ProbHub WebUI has an incompatible entry contract: {script}",
                code="webui_runtime_incompatible",
            )
    finally:
        if module is not None:
            _cleanup_webui_module(module)
        os.chdir(previous)
    return {
        "ok": True,
        "version": __version__,
        "workspace": str(root),
        "runtime": str(script),
    }


def run_webui(root, *, port=33933, open_browser=True):
    root = Path(root).resolve()
    previous = Path.cwd()
    module = None
    try:
        os.chdir(root)
        module, script = _load_webui_module()
        runner = getattr(module, "run_server", None)
        if not callable(runner):
            raise ProbHubError(
                f"installed ProbHub WebUI has an incompatible entry contract: {script}",
                code="webui_runtime_incompatible",
            )
        runner(port=port, open_browser=open_browser)
    finally:
        if module is not None:
            _cleanup_webui_module(module)
        os.chdir(previous)
    return {
        "ok": True,
        "version": __version__,
        "workspace": str(root),
        "runtime": str(script),
    }
