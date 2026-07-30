import importlib.util
import os
import uuid
from pathlib import Path

from . import __version__
from .errors import ProbHubError


def _webui_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "ui.py"
    if not script.is_file():
        raise ProbHubError(
            f"installed ProbHub WebUI is missing: {script}",
            code="webui_runtime_missing",
        )
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
