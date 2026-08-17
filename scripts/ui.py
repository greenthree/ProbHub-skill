# -*- coding: utf-8 -*-
import os
import sys
import json
import hmac
import re
import secrets
import subprocess
import shutil
import tempfile
import webbrowser
import uuid
import time
import copy
from pathlib import Path
from threading import BoundedSemaphore, Lock, Timer
from urllib.parse import urlsplit

import yaml
from flask import Flask, g, jsonify, request, render_template_string, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.serving import ThreadedWSGIServer

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = next(
    (candidate for candidate in (SCRIPT_DIR, SCRIPT_DIR.parent) if (candidate / "probhub").is_dir()),
    SCRIPT_DIR.parent,
)
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
LOCAL_JUDGE_SCRIPT = next(
    (candidate for candidate in (SCRIPT_DIR / "local_judge.py", SCRIPT_DIR / "scripts" / "local_judge.py", PACKAGE_ROOT / "scripts" / "local_judge.py") if candidate.is_file()),
    SCRIPT_DIR / "local_judge.py",
)

from probhub.build_lock import workspace_build_lock
from probhub.building import build_workspace, create_build_plan, create_build_snapshot
from probhub.errors import ProbHubError
from probhub.pdf_processing import pdf_page_count as bounded_pdf_page_count
from probhub.process_control import (
    run_managed_to_files,
    spawn_managed,
    snapshot_process_tree,
    terminate_external_process_tree,
)
from probhub.submissions import (
    MAX_SOURCE_BYTES,
    SubmissionPreparationCancelled,
    SubmissionPreparationDeadlineExceeded,
    cleanup_stale_submission_workspaces,
    temporary_submission_workspace,
    validate_cpp_upload,
)
from probhub.typesetting import compile_collection
from probhub.webui_workspace import (
    load_contest_config,
    load_editor_data,
    save_contest_config as save_schema_contest_config,
    save_editor_data,
    schema_workspace_for_subtitle,
    statement_asset_path,
)
from probhub.workspace import load_workspace, problem_entries
from probhub.webui_tasks import BoundedPipeCapture, BoundedTaskExecutor, BoundedUtf8Log

app = Flask(__name__)
MAX_SUBMISSION_REQUEST_BYTES = MAX_SOURCE_BYTES + 64 * 1024
MAX_WEBUI_REQUEST_BYTES = 16 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_WEBUI_REQUEST_BYTES
WEBUI_CSRF_TOKEN = secrets.token_urlsafe(32)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

SANDBOX_JOBS = {}
SANDBOX_LOCK = Lock()
SANDBOX_PROCESSES = {}
SANDBOX_CANCEL_FILES = {}
SANDBOX_PROBLEM_LOCKS = {}
SANDBOX_PROBLEM_LOCKS_GUARD = Lock()
SUBMISSION_JOBS = {}
SUBMISSION_PROCESSES = {}
SUBMISSION_CANCEL_FILES = {}
SUBMISSION_LOCK = Lock()
WEBUI_TASK_RESULT_TTL = 60 * 60
SUBMISSION_FORCE_CANCEL_AFTER = 3.0
WEBUI_TASK_WORKERS = max(1, min(4, os.cpu_count() or 2))
WEBUI_TASK_QUEUE_SIZE = 16
WEBUI_TASK_QUEUE_DEADLINE = 5 * 60
WEBUI_TASK_EXECUTION_DEADLINE = 30 * 60
WEBUI_TASK_TOTAL_DEADLINE = 35 * 60
WEBUI_TASK_LOG_BYTES = 256 * 1024
WEBUI_TASK_EVENT_BYTES = 2 * 1024 * 1024
WEBUI_TASK_FINAL_EVENT_BYTES = 64 * 1024
WEBUI_TASK_PROCESS_OUTPUT_BYTES = 16 * 1024 * 1024
WEBUI_TASK_ERROR_BYTES = 64 * 1024
WEBUI_TASK_MAX_COMPLETED = 32
WEBUI_TASK_INFO_FILES_PER_ROLE = 64
WEBUI_HTTP_REQUEST_THREADS = 8
WEBUI_TASK_EXECUTOR = BoundedTaskExecutor(
    max_workers=WEBUI_TASK_WORKERS,
    max_queued=WEBUI_TASK_QUEUE_SIZE,
    name="probhub-webui",
)
WEBUI_REQUEST_SLOTS = BoundedSemaphore(WEBUI_TASK_EXECUTOR.capacity)
WEBUI_PREVIEW_TEMP = tempfile.TemporaryDirectory(prefix="probhub-webui-preview-")
WEBUI_PREVIEW_ROOT = Path(WEBUI_PREVIEW_TEMP.name)

try:
    cleanup_stale_submission_workspaces(Path.cwd())
except OSError:
    pass


def _url_origin(value, *, default_scheme=None):
    try:
        parsed = urlsplit(f"{default_scheme}://{value}") if default_scheme and "://" not in value else urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme.lower(), hostname, port
    except (TypeError, ValueError):
        return None


def _request_host_is_loopback():
    origin = _url_origin(request.host, default_scheme=request.scheme)
    return origin is not None and origin[1] in _LOOPBACK_HOSTS


def _matches_request_origin(value):
    candidate = _url_origin(value)
    expected = _url_origin(request.host, default_scheme=request.scheme)
    return candidate is not None and expected is not None and candidate == expected


def _security_error(code, message):
    return jsonify({"success": False, "error": message, "code": code}), 403


def _reserve_task_request():
    task_slot = WEBUI_REQUEST_SLOTS
    if not task_slot.acquire(blocking=False):
        return _queue_full_response()
    g.webui_task_slot = task_slot


def _transfer_task_request():
    task_slot = getattr(g, "webui_task_slot", None)
    g.webui_task_slot = None
    return task_slot


def _release_task_request_slot(task_slot):
    if task_slot is not None:
        task_slot.release()


def _run_with_task_slot(task_slot, callback):
    try:
        return callback()
    finally:
        task_slot.release()


def _discard_with_task_slot(task_slot, callback, reason):
    try:
        return callback(reason)
    finally:
        task_slot.release()


@app.teardown_request
def release_task_request(_error=None):
    task_slot = getattr(g, "webui_task_slot", None)
    if task_slot is not None:
        g.webui_task_slot = None
        task_slot.release()


@app.before_request
def protect_local_webui():
    if request.path == "/api/submission/run" and request.method == "POST":
        request.max_content_length = MAX_SUBMISSION_REQUEST_BYTES
    if not _request_host_is_loopback():
        return _security_error("invalid_host", "request Host must be localhost or a loopback address")

    if request.path.startswith("/api/"):
        fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site and fetch_site != "same-origin":
            return _security_error("cross_origin", "cross-origin browser requests are not allowed")
        for header in ("Origin", "Referer"):
            value = request.headers.get(header)
            if value and not _matches_request_origin(value):
                return _security_error("cross_origin", f"request {header} does not match this WebUI origin")

    if request.method in _WRITE_METHODS:
        token = request.headers.get("X-ProbHub-CSRF", "")
        if not hmac.compare_digest(token, WEBUI_CSRF_TOKEN):
            return _security_error("csrf_failed", "missing or invalid WebUI request token")
    if request.path in {"/api/sandbox/run", "/api/submission/run"} and request.method == "POST":
        return _reserve_task_request()


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    max_bytes = request.max_content_length or app.config["MAX_CONTENT_LENGTH"]
    return jsonify({
        "success": False,
        "error": "request body is too large",
        "code": "request_too_large",
        "max_bytes": max_bytes,
    }), 413


@app.after_request
def add_webui_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _schema_workspace(subtitle):
    workspace_path = Path.cwd() / ".probhub" / "workspace.yaml"
    if not workspace_path.is_file():
        raise ProbHubError(
            "Workspace Schema v1 is required; migrate this old workspace first",
            code="migration_required",
        )
    schema = schema_workspace_for_subtitle(Path.cwd(), subtitle)
    if schema is None:
        raise ProbHubError(
            f"unknown Schema v1 subtitle: {subtitle}",
            code="unknown_subtitle",
        )
    return schema


def _schema_typst_dir(subtitle):
    root, workspace = _schema_workspace(subtitle)
    relative = Path((workspace.get("typst") or {}).get("directory", "typst-statement/正式赛"))
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            "workspace Typst directory must stay inside the workspace",
            code="invalid_workspace_path",
        ) from exc
    return root, workspace, candidate


def _preview_pdf_path(subtitle):
    if not subtitle or '..' in subtitle or '/' in subtitle or '\\' in subtitle:
        raise ValueError("Invalid subtitle")
    return WEBUI_PREVIEW_ROOT / subtitle / "main.pdf"


def problem_limits(problem_entry):
    problem = (problem_entry or {}).get("problem", {})
    try:
        time_limit = float(problem.get("time_limit", 1))
    except (TypeError, ValueError):
        time_limit = 1.0
    try:
        memory_limit = int(float(problem.get("memory_limit", 256)))
    except (TypeError, ValueError):
        memory_limit = 256
    time_limit = max(time_limit, 0.1)
    memory_limit = max(memory_limit, 1)
    if float(time_limit).is_integer():
        time_limit = int(time_limit)
    return time_limit, memory_limit


def read_probhub_config_from_dir(prob_dir):
    config_path = os.path.join(prob_dir, "probhub.yaml")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError):
        return None
    return config if isinstance(config, dict) else None


def _config_entry_file(entry):
    return entry.get("file") if isinstance(entry, dict) else entry


def _config_entries(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _normalized_config_path(entry):
    path = _config_entry_file(entry)
    return str(path).replace("\\", "/") if path else None


# ==========================================
# 前端 UI 模板 (TailwindCSS + Alpine.js + SortableJS + Marked + MathJax)
# ==========================================
WEBUI_ASSET_DIR = SCRIPT_DIR / "webui"
WEBUI_TEMPLATE_PATH = WEBUI_ASSET_DIR / "index.html"
WEBUI_ASSET_MANIFEST = json.loads((WEBUI_ASSET_DIR / "asset-manifest.json").read_text(encoding="utf-8"))
WEBUI_ENTRY_ASSETS = frozenset({
    "app.css", "app.js", "mathjax-config.js", "theme.js",
    "vendor/fonts.css", "vendor/tailwind.css", "vendor/marked.js",
    "vendor/mathjax-tex-svg.js", "vendor/alpine-collapse.js", "vendor/alpine.js",
    "vendor/sortable.js",
})
WEBUI_MISSING_ENTRY_ASSETS = WEBUI_ENTRY_ASSETS - frozenset(WEBUI_ASSET_MANIFEST["files"])
if WEBUI_MISSING_ENTRY_ASSETS:
    raise RuntimeError(
        "WebUI asset manifest is missing entry assets: "
        + ", ".join(sorted(WEBUI_MISSING_ENTRY_ASSETS))
    )
WEBUI_PUBLIC_ASSETS = frozenset(
    name for name in WEBUI_ASSET_MANIFEST["files"]
    if name != "index.html"
    and name != "vendor/THIRD_PARTY_NOTICES.txt"
    and not name.startswith("vendor/licenses/")
)
HTML_TEMPLATE = WEBUI_TEMPLATE_PATH.read_text(encoding="utf-8")

# ==========================================
# 后端 API 路由
# ==========================================

@app.route('/')
def index():
    return render_template_string(
        HTML_TEMPLATE,
        csrf_token=WEBUI_CSRF_TOKEN,
    )


@app.route('/webui/assets/<path:filename>', methods=['GET'])
def webui_asset(filename):
    if filename not in WEBUI_PUBLIC_ASSETS:
        return "Not found", 404
    return send_file(WEBUI_ASSET_DIR / filename, conditional=True)


@app.route('/api/subtitles', methods=['GET'])
def get_subtitles():
    """Return the Schema v1 Typst collection configured by the workspace."""
    try:
        _, workspace = load_workspace(Path.cwd(), allow_empty=True)
        typst_dir = Path((workspace.get("typst") or {}).get("directory", "typst-statement/正式赛"))
        return jsonify([typst_dir.name])
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": "migration_required"}), 409

@app.route('/api/data', methods=['GET'])
def get_data():
    subtitle = request.args.get('subtitle')
    if not subtitle:
        return jsonify([])
    try:
        root, workspace = _schema_workspace(subtitle)
        return jsonify(load_editor_data(root, workspace))
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), 409
    except Exception as e:
        print(f"[-] Data Load Error: {e}")
        return jsonify({"success": False, "error": str(e), "code": "data_load_failed"}), 500


@app.route('/api/problem-assets/<subtitle>/<problem_id>/<path:asset_path>', methods=['GET'])
def get_problem_asset(subtitle, problem_id, asset_path):
    try:
        root, workspace = _schema_workspace(subtitle)
        return send_file(statement_asset_path(root, workspace, problem_id, asset_path))
    except (ProbHubError, OSError, ValueError):
        return "Not found", 404

@app.route('/api/data', methods=['POST'])
def save_data():
    payload = request.json
    subtitle = payload.get('subtitle')
    new_data = payload.get('problems', [])
    
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
        
    try:
        root, _ = _schema_workspace(subtitle)
        with workspace_build_lock(root):
            _, live_workspace = load_workspace(root)
            problems = save_editor_data(root, live_workspace, new_data)
        return jsonify({"success": True, "problems": problems})
    except ProbHubError as e:
        status = 409 if e.code in {"source_conflict", "build_busy", "migration_required", "unknown_subtitle"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        print(f"[-] Save Data Error: {e}")
        return jsonify({"success": False, "error": str(e)})

def analyze_compile_error(output):
    text = (output or "").strip()
    lower = text.lower()
    if not text:
        return {
            "message": "Typst 编译失败，但没有返回详细错误",
            "suggestion": "查看启动 ui.py 的终端，确认 typst 是否正常安装并可执行。",
        }
    if any(marker in lower for marker in (
        "being used by another process",
        "os error 32",
        "access is denied",
        "permission denied",
        "cannot access the file",
        "failed to create file",
        "failed to write",
    )):
        return {
            "message": "PDF 输出文件可能正被占用，Typst 无法覆盖写入",
            "suggestion": "先关闭正在打开 main.pdf 的 PDF 查看器或浏览器预览页，再重新编译。",
        }
    if "not found" in lower and "typst" in lower:
        return {
            "message": "未找到 typst 命令",
            "suggestion": "确认 Typst 已安装，并且 typst 命令已加入 PATH。",
        }
    if any(marker in lower for marker in ("failed to load", "file not found", "no such file")):
        return {
            "message": "Typst 编译时找不到某个引用文件",
            "suggestion": "检查图片、PDF、Typst include/import 路径是否存在，尤其是题面中的相对路径资源。",
        }
    if "error:" in lower or "expected" in lower or "unknown" in lower:
        first_error = next((line.strip() for line in text.splitlines() if line.strip()), "Typst 语法错误")
        return {
            "message": first_error[:160],
            "suggestion": "根据 Typst 报错中的文件名和行列号检查刚修改的题面、封面设置或 quote 内容。",
        }
    return {
        "message": text.splitlines()[0][:160],
        "suggestion": "查看启动 ui.py 的终端输出，定位 Typst 返回的完整错误信息。",
    }

@app.route('/api/compile', methods=['POST'])
def compile_pdf():
    """仅编译 typst，不分发 PDF。"""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        root, _ = _schema_workspace(subtitle)
        with workspace_build_lock(root):
            _, live_workspace = load_workspace(root)
            entries = problem_entries(live_workspace)
            plan = create_build_plan(root, live_workspace, entries)
            with create_build_snapshot(plan) as snapshot:
                _, staged_pdf, _ = compile_collection(
                    snapshot.root,
                    snapshot.workspace,
                    snapshot.loaded_problems,
                )
                preview_pdf = _preview_pdf_path(subtitle)
                preview_pdf.parent.mkdir(parents=True, exist_ok=True)
                temporary = preview_pdf.with_suffix(".pdf.tmp")
                shutil.copyfile(staged_pdf, temporary)
                os.replace(temporary, preview_pdf)
        return jsonify({"success": True, "preview": True})
    except ProbHubError as e:
        detail = analyze_compile_error(str(e))
        status = 409 if e.code in {"artifact_busy", "build_busy", "inputs_changed", "migration_required", "unknown_subtitle"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code, **detail}), status
    except FileNotFoundError as e:
        detail = analyze_compile_error("typst not found")
        return jsonify({"success": False, "error": str(e), **detail})
    except subprocess.CalledProcessError as e:
        output = "\n".join(part for part in (e.stderr, e.stdout, str(e)) if part)
        detail = analyze_compile_error(output)
        print(f"[-] Typst compile failed:\n{output}")
        return jsonify({"success": False, "error": str(e), "output": output[-4000:], **detail})

@app.route('/api/distribute', methods=['POST'])
def distribute_pdfs():
    """Use the Schema v1 Core to build and publish the configured collection."""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        root, workspace = _schema_workspace(subtitle)
        result = build_workspace(root, workspace, problem_entries(workspace))
        distributed = [
            {
                "name": problem_id,
                "status": "ok",
                "dir": str(Path(details["path"]).parent),
                "zip": "verified",
            }
            for problem_id, details in result["pdfs"].items()
        ]
        return jsonify({
            "success": True,
            "distributed": distributed,
            "batch_id": result["batch_id"],
        })
    except ProbHubError as e:
        status = 409 if e.code in {"artifact_busy", "build_busy", "inputs_changed", "migration_required", "unknown_subtitle"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _load_problem_by_index(subtitle, index):
    root, workspace = _schema_workspace(subtitle)
    entries = problem_entries(workspace)
    if index < 0 or index >= len(entries):
        raise ValueError("Problem index out of range")
    data = load_editor_data(root, workspace)
    return data[index]


def _sandbox_problem_info(subtitle, index):
    problem = _load_problem_by_index(subtitle, index)
    display_name = problem.get("problem", {}).get("display_name", "")
    root, workspace = _schema_workspace(subtitle)
    entries = problem_entries(workspace)
    prob_dir = str(root / entries[index].get("directory", entries[index]["id"]))
    info = {
        "name": display_name,
        "matched": bool(prob_dir),
        "dir": prob_dir,
        "runnable": False,
        "limits": {"time": problem_limits(problem)[0], "memory": problem_limits(problem)[1]},
        "data_count": 0,
        "sample_count": 0,
        "secret_count": 0,
        "files": {"validator": False, "std": [], "brute": [], "wrong": [], "other": []},
        "file_counts": {"std": 0, "brute": 0, "wrong": 0, "other": 0},
        "files_truncated": False,
        # The installed Core owns local_judge.py; the workspace may only keep
        # thin launchers and must not need a copied runtime script.
        "script_exists": LOCAL_JUDGE_SCRIPT.is_file(),
    }
    if not prob_dir:
        info["reason"] = "no matching directory"
        return info

    for suite in ("sample", "secret"):
        data_dir = os.path.join(prob_dir, "data", suite)
        if os.path.isdir(data_dir):
            with os.scandir(data_dir) as entries:
                info[f"{suite}_count"] = sum(
                    1 for entry in entries if entry.is_file() and entry.name.endswith(".in")
                )
    info["data_count"] = info["sample_count"] + info["secret_count"]

    def add_file(role, source):
        info["file_counts"][role] += 1
        if len(info["files"][role]) < WEBUI_TASK_INFO_FILES_PER_ROLE:
            info["files"][role].append(source)
        else:
            info["files_truncated"] = True

    config = read_probhub_config_from_dir(prob_dir)
    if config is None:
        info["reason"] = "migration_required"
        return info
    if config.get("schema_version") != 1:
        info["reason"] = "unsupported_schema"
        return info

    judge = config.get("judge") or {}
    validator = _normalized_config_path(judge.get("validator"))
    if validator and os.path.isfile(os.path.join(prob_dir, validator)):
        info["files"]["validator"] = validator

    solutions = config.get("solutions") or {}
    configured = set()
    for config_key, display_key in (("accepted", "std"), ("brute", "brute"), ("wrong", "wrong")):
        for entry in _config_entries(solutions.get(config_key)):
            source = _normalized_config_path(entry)
            if source and os.path.isfile(os.path.join(prob_dir, source)):
                configured.add(source)
                add_file(display_key, source)

    if info["files"]["validator"]:
        configured.add(info["files"]["validator"])
    for entry in _config_entries(config.get("generators")):
        source = _normalized_config_path(entry)
        if source and source not in configured and os.path.isfile(os.path.join(prob_dir, source)):
            add_file("other", source)
    info["runnable"] = info["script_exists"] and info["data_count"] > 0
    return info


def _empty_sandbox_result(info):
    return {
        "problem": info,
        "limits": info.get("limits", {"time": 1, "memory": 256}),
        "compiles": [],
        "validator": [],
        "cases": [],
        "groups": [],
        "expectations": {},
        "summaries": {},
        "cache": {},
        "transcripts": [],
        "final": None,
    }


def _apply_sandbox_event(result, event):
    typ = event.get("type")
    if typ == "limits":
        result["limits"] = {
            "time": event.get("time_limit", 1),
            "memory": event.get("memory_limit", 256),
            "output": event.get("output_limit", 64),
            "processes": event.get("process_limit", 32),
            "judge_type": event.get("judge_type", "standard"),
            "idle_limit": event.get("idle_limit"),
            "transcript_limit": event.get("transcript_limit"),
            "platform": event.get("platform"),
            "machine": event.get("machine"),
            "target_guarantee": event.get("target_guarantee", False),
            "measurement_note": event.get("measurement_note"),
        }
    elif typ == "groups":
        result["groups"] = event.get("groups") or []
    elif typ == "compile":
        result["compiles"].append({
            "kind": event.get("kind"),
            "file": event.get("file"),
            "ok": event.get("ok"),
            "stderr": event.get("stderr", ""),
            "cached": bool(event.get("cached")),
        })
    elif typ == "validator":
        result["validator"].append({
            "case": event.get("case"),
            "ok": bool(event.get("ok")),
            "cached": bool(event.get("cached")),
        })
    elif typ == "case":
        result["cases"].append({
            "kind": event.get("kind"),
            "program": event.get("program"),
            "case": event.get("case"),
            "groups": event.get("groups") or [],
            "status": event.get("status"),
            "time": float(event.get("time") or 0),
            "memory": event.get("memory"),
            "time_limit": event.get("time_limit"),
            "memory_limit": event.get("memory_limit"),
            "memory_enforced": event.get("memory_enforced"),
            "judge_type": event.get("judge_type", "standard"),
            "message": event.get("message", ""),
            "timeout_kind": event.get("timeout_kind"),
            "transcript_truncated": bool(event.get("transcript_truncated")),
            "output_bytes": event.get("output_bytes"),
            "termination_reason": event.get("termination_reason"),
            "cached": bool(event.get("cached")),
        })
    elif typ == "transcript":
        result["transcripts"].append({
            "kind": event.get("kind"),
            "program": event.get("program"),
            "case": event.get("case"),
            "entries": event.get("entries") or [],
            "truncated": bool(event.get("truncated")),
            "cached": bool(event.get("cached")),
        })
    elif typ == "summary":
        program = event.get("program") or f"{event.get('kind', 'unknown')}-summary"
        result["summaries"][program] = {
            "kind": event.get("kind"),
            "program": program,
            "stats": event.get("stats", {}),
            "expectation": event.get("expectation") or {},
            "calibration": event.get("calibration") or {},
        }
    elif typ == "expectation":
        program = event.get("program") or f"{event.get('kind', 'unknown')}-expectation"
        result["expectations"][program] = {
            key: value for key, value in event.items()
            if key not in {"protocol", "protocol_version", "type"}
        }
    elif typ == "cache":
        result["cache"] = {
            key: value for key, value in event.items()
            if key not in {"protocol", "protocol_version", "type"}
        }
    elif typ == "final":
        result["final"] = {
            "ok": bool(event.get("ok")),
            "status": event.get("status", ""),
            "code": event.get("code", ""),
            "message": event.get("message", ""),
        }


def _sandbox_log_line(event):
    typ = event.get("type")
    if typ == "limits":
        return (
            f"[limits:{event.get('judge_type', 'standard')}] "
            f"{event.get('time_limit', 1):g}s / {event.get('memory_limit', 256)}MB"
        )
    if typ == "groups":
        names = [group.get("name") for group in (event.get("groups") or [])]
        return f"[groups] {', '.join(name for name in names if name) or 'none'}"
    if typ == "compile":
        ok = event.get("ok")
        status = "SKIP" if ok is None else ("OK" if ok else "FAIL")
        return f"[compile:{status}] {event.get('kind')} · {event.get('file')}"
    if typ == "validator":
        return f"[validator:{'OK' if event.get('ok') else 'FAIL'}] {event.get('case')}"
    if typ == "case":
        detail = f" · {event.get('message')}" if event.get("message") else ""
        return (
            f"[{event.get('kind')}:{event.get('judge_type', 'standard')}] "
            f"{event.get('program')} / {event.get('case')} -> {event.get('status')} "
            f"({float(event.get('time') or 0):.3f}s){detail}"
        )
    if typ == "transcript":
        suffix = " truncated" if event.get("truncated") else ""
        return (
            f"[transcript{suffix}] {event.get('program')} / {event.get('case')}: "
            f"{len(event.get('entries') or [])} chunks"
        )
    if typ == "summary":
        return f"[summary] {event.get('program')}: {event.get('stats')}"
    if typ == "expectation":
        state = "PASS" if event.get("ok") else "FAIL"
        first = event.get("first_forbidden") or event.get("first_expected_match") or event.get("first_non_ac")
        suffix = f" · first={first.get('case')}:{first.get('status')}" if first else ""
        return (
            f"[expectation:{state}] {event.get('program')} "
            f"status={event.get('expected_statuses') or []} "
            f"groups={event.get('groups') or ['all']}{suffix}"
        )
    if typ == "final":
        return f"[final:{'OK' if event.get('ok') else 'FAIL'}] {event.get('message')}"
    return json.dumps(event, ensure_ascii=False)


ACTIVE_TASK_STATUSES = {"queued", "running", "cancelling"}


def _prune_job_registry(jobs):
    now = time.time()
    expired = [
        job_id for job_id, job in jobs.items()
        if job.get("status") not in ACTIVE_TASK_STATUSES
        and now - float(job.get("finished_at") or job.get("created_at") or now) >= WEBUI_TASK_RESULT_TTL
    ]
    for job_id in expired:
        jobs.pop(job_id, None)
    finished = sorted(
        (
            (job_id, job)
            for job_id, job in jobs.items()
            if job.get("status") not in ACTIVE_TASK_STATUSES
        ),
        key=lambda item: float(item[1].get("finished_at") or item[1].get("created_at") or 0),
    )
    for job_id, _ in finished[: max(0, len(finished) - WEBUI_TASK_MAX_COMPLETED)]:
        jobs.pop(job_id, None)


def _prune_sandbox_jobs():
    _prune_job_registry(SANDBOX_JOBS)


def _prune_submission_jobs():
    _prune_job_registry(SUBMISSION_JOBS)


def _task_failure_result(result, code, message, *, status="failed"):
    if result is None:
        result = {}
    result["final"] = {
        "ok": False,
        "status": status,
        "code": code,
        "message": message,
    }
    return result


def _bounded_error_text(value):
    log = BoundedUtf8Log(WEBUI_TASK_ERROR_BYTES)
    log.append(str(value))
    return log.text


def _public_job_payload(job):
    payload = copy.deepcopy(job)
    for key in list(payload):
        if key.startswith("_"):
            payload.pop(key, None)
    return payload


def _queue_full_response():
    response = jsonify({
        "success": False,
        "code": "queue_full",
        "error": "WebUI task queue is full; retry later",
        "retryable": True,
        "retry_after": 2,
        "capacity": WEBUI_TASK_EXECUTOR.capacity,
    })
    response.status_code = 429
    response.headers["Retry-After"] = "2"
    return response


def _discard_queued_job(jobs, lock, job_id, reason, *, submission=False):
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") not in {"queued", "cancelling"}:
            return
        cancelled = reason in {"cancelled", "shutdown"} or bool(job.get("cancel_requested"))
        code = "cancelled" if cancelled else "queue_timeout"
        message = "task cancelled before execution" if cancelled else "task exceeded its queue deadline"
        result = _task_failure_result(job.get("result"), code, message, status=code)
        if submission:
            result.setdefault("submission", {}).update(
                task_id=job_id,
                filename=job.get("filename", ""),
                verdict="CANCELLED" if cancelled else "FAIL",
                workspace_cleaned=True,
            )
        job.update(
            status="cancelled" if cancelled else "failed",
            verdict=("CANCELLED" if cancelled else "FAIL") if submission else job.get("verdict"),
            result=result,
            logs=message,
            finished_at=time.time(),
            failure_code=code,
        )
        _prune_job_registry(jobs)


def _task_executor_error(jobs, lock, job_id, exc, *, submission=False):
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") not in ACTIVE_TASK_STATUSES:
            return
        message = _bounded_error_text(f"WebUI task worker failed: {exc}")
        failure_code = "task_worker_failed"
        result = job.get("result") or {}
        if submission:
            workspace_cleaned = not (Path.cwd() / ".probhub" / "submissions" / job_id).exists()
            submission_result = result.setdefault("submission", {})
            submission_result.update(
                task_id=job_id,
                filename=job.get("filename", ""),
                verdict="FAIL",
                workspace_cleaned=workspace_cleaned,
            )
            if not workspace_cleaned:
                failure_code = "submission_cleanup_failed"
                submission_result.update(
                    cleanup_error="submission workspace still exists after worker failure",
                    original_failure_code="task_worker_failed",
                )
        result = _task_failure_result(
            result,
            failure_code,
            "submission workspace cleanup failed" if failure_code == "submission_cleanup_failed" else message,
        )
        job.update(
            status="failed",
            verdict="FAIL" if submission else job.get("verdict"),
            result=result,
            logs=message,
            finished_at=time.time(),
            failure_code=failure_code,
        )
        _prune_job_registry(jobs)


def _task_cancel_requested(jobs, lock, job_id):
    with lock:
        return bool(jobs.get(job_id, {}).get("cancel_requested"))


def _claim_task_execution(jobs, lock, job_id):
    with lock:
        job = jobs.get(job_id)
        if not job:
            return None, "missing"
        if job.get("cancel_requested"):
            return None, "cancelled"
        now = time.time()
        now_monotonic = time.monotonic()
        deadline_monotonic = job.get("_deadline_monotonic")
        if deadline_monotonic is None:
            remaining_wall = max(0.0, float(job.get("deadline_at") or now) - now)
            deadline_monotonic = now_monotonic + remaining_wall
            job["_deadline_monotonic"] = deadline_monotonic
        remaining_total = float(deadline_monotonic) - now_monotonic
        if remaining_total <= 0:
            return None, "deadline"
        execution_seconds = min(WEBUI_TASK_EXECUTION_DEADLINE, remaining_total)
        job.update(
            status="running",
            started_at=now,
            execution_deadline_at=now + execution_seconds,
        )
        return now_monotonic + execution_seconds, None


def _sandbox_problem_lock(problem_dir):
    key = str(Path(problem_dir).resolve())
    with SANDBOX_PROBLEM_LOCKS_GUARD:
        return SANDBOX_PROBLEM_LOCKS.setdefault(key, Lock())


def _acquire_problem_lock(lock, jobs, jobs_lock, job_id, execution_deadline):
    while time.monotonic() < execution_deadline:
        if _task_cancel_requested(jobs, jobs_lock, job_id):
            return "cancelled"
        if lock.acquire(timeout=min(0.05, max(0.001, execution_deadline - time.monotonic()))):
            return "acquired"
    return "deadline"


def _publish_task_progress(jobs, lock, job_id, result, log):
    with lock:
        job = jobs.get(job_id)
        if job is not None:
            job["logs"] = log.text
            job["logs_truncated"] = log.truncated
            # The worker keeps mutating its local result between polls. Publish
            # an immutable snapshot so Flask never deep-copies a changing dict.
            job["result"] = copy.deepcopy(result)


def _consume_jsonl_chunk(result, log, state, chunk, *, final=False):
    state["buffer"] += chunk
    lines = state["buffer"].split(b"\n")
    state["buffer"] = b"" if final else lines.pop()
    if final and lines and not lines[-1]:
        lines.pop()
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        text = raw_line.decode("utf-8", errors="replace")
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            log.append(text)
            continue
        event_size = len(raw_line) + 1
        if event.get("type") == "final":
            if state.get("protocol_error"):
                continue
            if state.get("final_seen"):
                state["protocol_error"] = "duplicate_final_event"
                _task_failure_result(
                    result,
                    "judge_protocol_error",
                    "judge emitted more than one final event",
                )
                log.append("[final:FAIL] judge emitted more than one final event")
                continue
            state["final_seen"] = True
            if event_size > WEBUI_TASK_FINAL_EVENT_BYTES:
                state["protocol_error"] = "task_event_limit"
                result["details_truncated"] = True
                _task_failure_result(
                    result,
                    "task_event_limit",
                    "judge final event exceeded its limit",
                )
                log.append("[final:FAIL] judge final event exceeded its limit")
                continue
            _apply_sandbox_event(result, event)
            log.append(_sandbox_log_line(event))
            continue
        if state["event_bytes"] + event_size <= WEBUI_TASK_EVENT_BYTES:
            _apply_sandbox_event(result, event)
            state["event_bytes"] += event_size
            log.append(_sandbox_log_line(event))
        else:
            result["details_truncated"] = True
            if not state.get("detail_limit_reported"):
                state["detail_limit_reported"] = True
                log.append("[details truncated: event budget exceeded]")


def _touch_cancel_file(cancel_file):
    try:
        Path(cancel_file).touch(exist_ok=True)
    except OSError:
        pass


def _supervise_judge_process(
    job_id,
    command,
    env,
    cancel_file,
    result,
    *,
    jobs,
    lock,
    processes,
    cancel_files,
    execution_deadline,
):
    log = BoundedUtf8Log(WEBUI_TASK_LOG_BYTES)
    parse_state = {
        "buffer": b"",
        "event_bytes": 0,
        "final_seen": False,
        "protocol_error": None,
    }
    return_code = None
    stop_reason = None
    stop_started = None
    known_pids = set()
    managed = None
    capture = None
    if _task_cancel_requested(jobs, lock, job_id):
        _task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
        _publish_task_progress(jobs, lock, job_id, result, log)
        return {"returncode": None, "stop_reason": "cancelled", "logs": log.text}
    if time.monotonic() >= execution_deadline:
        _task_failure_result(result, "task_deadline_exceeded", "task exceeded its total deadline")
        _publish_task_progress(jobs, lock, job_id, result, log)
        return {"returncode": None, "stop_reason": "deadline", "logs": log.text}

    try:
        managed = spawn_managed(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=Path.cwd(),
            env=env,
            memory_limit_mb=None,
            process_limit=64,
        )
        proc = managed.proc
        capture = BoundedPipeCapture(
            proc.stdout,
            WEBUI_TASK_PROCESS_OUTPUT_BYTES,
            name=f"probhub-webui-pipe-{job_id[:8]}",
        ).start()
        with lock:
            processes[job_id] = proc
            cancel_files[job_id] = Path(cancel_file)
        while True:
            published = False
            for chunk in capture.drain_chunks():
                _consume_jsonl_chunk(result, log, parse_state, chunk)
                published = True
            if published:
                _publish_task_progress(jobs, lock, job_id, result, log)

            now_mono = time.monotonic()
            if capture.overflow and stop_reason is None:
                stop_reason = "output_limit"
                stop_started = now_mono
                known_pids = snapshot_process_tree(proc.pid)
            elif _task_cancel_requested(jobs, lock, job_id) and stop_reason is None:
                stop_reason = "cancelled"
                stop_started = now_mono
                known_pids = snapshot_process_tree(proc.pid)
                _touch_cancel_file(cancel_file)
            elif now_mono >= execution_deadline and stop_reason is None:
                stop_reason = "deadline"
                stop_started = now_mono
                known_pids = snapshot_process_tree(proc.pid)
                _touch_cancel_file(cancel_file)

            if stop_reason == "output_limit" and proc.poll() is None:
                terminate_external_process_tree(proc, known_pids)
            elif (
                stop_reason is not None
                and proc.poll() is None
                and now_mono - float(stop_started or now_mono) >= SUBMISSION_FORCE_CANCEL_AFTER
            ):
                terminate_external_process_tree(proc, known_pids)

            return_code = proc.poll()
            if return_code is not None:
                break
            time.sleep(0.05)
    finally:
        if managed is not None:
            if managed.proc.poll() is None:
                terminate_external_process_tree(managed.proc, known_pids)
            managed.terminate()
            managed.close()
        if capture is not None:
            if not capture.join(timeout=2.0):
                try:
                    capture.stream.close()
                except (OSError, ValueError):
                    pass
                capture.join(timeout=1.0)
            for chunk in capture.drain_chunks():
                _consume_jsonl_chunk(result, log, parse_state, chunk)
            _consume_jsonl_chunk(result, log, parse_state, b"", final=True)
            # A short-lived process can exit before the supervisor observes
            # the reader's overflow flag. Re-check after the pipe is fully
            # drained so fast output floods cannot bypass the task budget.
            if capture.overflow and stop_reason is None:
                stop_reason = "output_limit"
            try:
                capture.stream.close()
            except (OSError, ValueError):
                pass
        with lock:
            processes.pop(job_id, None)
            cancel_files.pop(job_id, None)

    if stop_reason is None and _task_cancel_requested(jobs, lock, job_id):
        stop_reason = "cancelled"
    if stop_reason == "cancelled":
        _task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
    elif stop_reason == "deadline":
        _task_failure_result(result, "task_deadline_exceeded", "task exceeded its execution deadline")
    elif stop_reason == "output_limit":
        _task_failure_result(result, "task_output_limit", "task protocol output exceeded its limit")
    elif capture is not None and capture.error is not None:
        _task_failure_result(result, "judge_protocol_error", "failed to read judge protocol output")
        stop_reason = "protocol_error"
    elif parse_state.get("protocol_error"):
        stop_reason = "protocol_error"
    elif result.get("final") is None:
        _task_failure_result(result, "missing_final_event", "judge exited without a final event")
        stop_reason = "protocol_error"
    elif result["final"].get("code") == "task_event_limit":
        stop_reason = "protocol_error"
    elif bool(result["final"].get("ok")) != (return_code == 0):
        _task_failure_result(result, "judge_protocol_error", "judge final event disagreed with its exit code")
        stop_reason = "protocol_error"
    _publish_task_progress(jobs, lock, job_id, result, log)
    return {"returncode": return_code, "stop_reason": stop_reason, "logs": log.text}


def _run_sandbox_job(job_id, info):
    result = _empty_sandbox_result(info)
    problem_lock = None
    problem_lock_acquired = False
    try:
        execution_deadline, start_failure = _claim_task_execution(
            SANDBOX_JOBS, SANDBOX_LOCK, job_id
        )
        if start_failure == "cancelled":
            _discard_queued_job(SANDBOX_JOBS, SANDBOX_LOCK, job_id, "cancelled")
            return
        if start_failure is not None:
            code = "task_deadline_exceeded" if start_failure == "deadline" else "task_worker_failed"
            message = "task exceeded its total deadline" if start_failure == "deadline" else "task is unavailable"
            result = _task_failure_result(result, code, message)
            with SANDBOX_LOCK:
                if job_id in SANDBOX_JOBS:
                    SANDBOX_JOBS[job_id].update(
                        status="failed",
                        result=result,
                        logs=message,
                        finished_at=time.time(),
                        failure_code=code,
                    )
                    _prune_sandbox_jobs()
            return
        problem_lock = _sandbox_problem_lock(info["dir"])
        lock_status = _acquire_problem_lock(
            problem_lock,
            SANDBOX_JOBS,
            SANDBOX_LOCK,
            job_id,
            execution_deadline,
        )
        if lock_status != "acquired":
            code = "cancelled" if lock_status == "cancelled" else "task_deadline_exceeded"
            message = "task cancelled" if lock_status == "cancelled" else "task exceeded its total deadline"
            result = _task_failure_result(result, code, message, status=code)
            with SANDBOX_LOCK:
                if job_id in SANDBOX_JOBS:
                    SANDBOX_JOBS[job_id].update(
                        status="cancelled" if lock_status == "cancelled" else "failed",
                        result=result,
                        logs=message,
                        finished_at=time.time(),
                        failure_code=code,
                    )
                    _prune_sandbox_jobs()
            return
        problem_lock_acquired = True
        with tempfile.TemporaryDirectory(prefix="probhub-webui-sandbox-control-") as temp:
            cancel_file = Path(temp) / "cancel.requested"
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
            run = _supervise_judge_process(
                job_id,
                [sys.executable, str(LOCAL_JUDGE_SCRIPT), info["dir"], "--jsonl", "--cancellable"],
                env,
                cancel_file,
                result,
                jobs=SANDBOX_JOBS,
                lock=SANDBOX_LOCK,
                processes=SANDBOX_PROCESSES,
                cancel_files=SANDBOX_CANCEL_FILES,
                execution_deadline=execution_deadline,
            )
        final = result.get("final") or {}
        with SANDBOX_LOCK:
            job = SANDBOX_JOBS.get(job_id)
            if job is not None:
                cancelled = (
                    bool(job.get("cancel_requested"))
                    or run["stop_reason"] == "cancelled"
                    or final.get("status") == "cancelled"
                )
                if cancelled:
                    _task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
                    final = result["final"]
                final_ok = bool(final.get("ok")) and run["returncode"] == 0
                job.update(
                    status="cancelled" if cancelled else ("success" if final_ok else "failed"),
                    returncode=run["returncode"],
                    result=result,
                    logs=run["logs"],
                    finished_at=time.time(),
                    failure_code=None if final_ok else final.get("code"),
                )
                _prune_sandbox_jobs()
    except Exception as exc:
        cancelled = _task_cancel_requested(SANDBOX_JOBS, SANDBOX_LOCK, job_id)
        code = "cancelled" if cancelled else "task_worker_failed"
        message = "task cancelled" if cancelled else _bounded_error_text(exc)
        result = _task_failure_result(result, code, message, status=code)
        with SANDBOX_LOCK:
            if job_id in SANDBOX_JOBS:
                SANDBOX_JOBS[job_id].update(
                    status="cancelled" if cancelled else "failed",
                    result=result,
                    logs=message,
                    finished_at=time.time(),
                    failure_code=code,
                )
                _prune_sandbox_jobs()
    finally:
        if problem_lock_acquired:
            problem_lock.release()


@app.route('/api/sandbox/problem')
def sandbox_problem():
    try:
        subtitle = request.args.get("subtitle")
        index = int(request.args.get("index", "-1"))
        if not subtitle:
            return jsonify({"success": False, "error": "Missing subtitle"})
        return jsonify({"success": True, "info": _sandbox_problem_info(subtitle, index)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sandbox/run', methods=['POST'])
def sandbox_run():
    try:
        payload = request.json or {}
        subtitle = payload.get("subtitle")
        index = int(payload.get("index", -1))
        if not subtitle:
            return jsonify({"success": False, "error": "Missing subtitle"})
        info = _sandbox_problem_info(subtitle, index)
        if not info.get("runnable"):
            return jsonify({"success": False, "error": info.get("reason") or "problem is not runnable"}), 400
        job_id = uuid.uuid4().hex
        now = time.time()
        now_monotonic = time.monotonic()
        with SANDBOX_LOCK:
            _prune_sandbox_jobs()
            SANDBOX_JOBS[job_id] = {
                "status": "queued",
                "logs": "",
                "logs_truncated": False,
                "result": _empty_sandbox_result(info),
                "created_at": now,
                "queue_deadline_at": now + WEBUI_TASK_QUEUE_DEADLINE,
                "deadline_at": now + WEBUI_TASK_TOTAL_DEADLINE,
                "_deadline_monotonic": now_monotonic + WEBUI_TASK_TOTAL_DEADLINE,
                "cancel_requested": False,
            }
        task_slot = _transfer_task_request()
        try:
            accepted = WEBUI_TASK_EXECUTOR.submit(
                job_id,
                lambda: _run_with_task_slot(task_slot, lambda: _run_sandbox_job(job_id, info)),
                queue_timeout=WEBUI_TASK_QUEUE_DEADLINE,
                on_discard=lambda reason: _discard_with_task_slot(
                    task_slot,
                    lambda discard_reason: _discard_queued_job(
                        SANDBOX_JOBS,
                        SANDBOX_LOCK,
                        job_id,
                        discard_reason,
                    ),
                    reason,
                ),
                on_error=lambda exc: _task_executor_error(
                    SANDBOX_JOBS, SANDBOX_LOCK, job_id, exc
                ),
            )
        except BaseException:
            _release_task_request_slot(task_slot)
            with SANDBOX_LOCK:
                SANDBOX_JOBS.pop(job_id, None)
            raise
        if not accepted:
            _release_task_request_slot(task_slot)
            with SANDBOX_LOCK:
                SANDBOX_JOBS.pop(job_id, None)
            return _queue_full_response()
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except RequestEntityTooLarge:
        raise
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sandbox/job/<job_id>')
def sandbox_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    WEBUI_TASK_EXECUTOR.expire_pending()
    with SANDBOX_LOCK:
        _prune_sandbox_jobs()
        job = SANDBOX_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        payload = _public_job_payload(job)
    return jsonify({"success": True, **payload})


@app.route('/api/sandbox/job/<job_id>/cancel', methods=['POST'])
def sandbox_cancel(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    with SANDBOX_LOCK:
        job = SANDBOX_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        if job.get("status") not in ACTIVE_TASK_STATUSES:
            return jsonify({"success": True, "status": job.get("status")})
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        cancel_file = SANDBOX_CANCEL_FILES.get(job_id)
    if cancel_file is not None:
        _touch_cancel_file(cancel_file)
    WEBUI_TASK_EXECUTOR.cancel_pending(job_id)
    with SANDBOX_LOCK:
        status = SANDBOX_JOBS.get(job_id, {}).get("status", "cancelling")
    return jsonify({"success": True, "status": status})


def _submission_verdict(result):
    submission_compiles = [item for item in (result.get("compiles") or []) if item.get("kind") == "std"]
    if submission_compiles and not submission_compiles[-1].get("ok"):
        return "CE"
    statuses = [item.get("status") for item in (result.get("cases") or []) if item.get("status")]
    if statuses and all(status == "AC" for status in statuses):
        return "AC"
    for status in ("FAIL", "RE", "MLE", "OLE", "TLE", "WA"):
        if status in statuses:
            return status
    return "FAIL" if result.get("final") else "PENDING"


def _run_submission_job(job_id, info, filename, source):
    result = _empty_sandbox_result(info)
    result["submission"] = {
        "task_id": job_id,
        "filename": filename,
        "workspace_cleaned": False,
    }
    prepared = None
    task_root = Path.cwd() / ".probhub" / "submissions" / job_id
    try:
        execution_deadline, start_failure = _claim_task_execution(
            SUBMISSION_JOBS, SUBMISSION_LOCK, job_id
        )
        if start_failure == "cancelled":
            _discard_queued_job(SUBMISSION_JOBS, SUBMISSION_LOCK, job_id, "cancelled", submission=True)
            return
        if start_failure is not None:
            code = "task_deadline_exceeded" if start_failure == "deadline" else "task_worker_failed"
            message = "task exceeded its total deadline" if start_failure == "deadline" else "task is unavailable"
            result["submission"].update(workspace_cleaned=not task_root.exists(), verdict="FAIL")
            _task_failure_result(result, code, message)
            with SUBMISSION_LOCK:
                if job_id in SUBMISSION_JOBS:
                    SUBMISSION_JOBS[job_id].update(
                        status="failed",
                        verdict="FAIL",
                        result=result,
                        logs=message,
                        finished_at=time.time(),
                        failure_code=code,
                    )
                    _prune_submission_jobs()
            return
        with temporary_submission_workspace(
            Path.cwd(),
            info["dir"],
            job_id,
            filename,
            source,
            cancel_requested=lambda: _task_cancel_requested(
                SUBMISSION_JOBS, SUBMISSION_LOCK, job_id
            ),
            deadline_monotonic=execution_deadline,
        ) as prepared:
            cancel_file = prepared.root / "cancel.requested"
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
            run = _supervise_judge_process(
                job_id,
                [
                    sys.executable,
                    str(LOCAL_JUDGE_SCRIPT),
                    str(prepared.problem_dir),
                    "--jsonl",
                    "--no-cache",
                    "--cancellable",
                ],
                env,
                cancel_file,
                result,
                jobs=SUBMISSION_JOBS,
                lock=SUBMISSION_LOCK,
                processes=SUBMISSION_PROCESSES,
                cancel_files=SUBMISSION_CANCEL_FILES,
                execution_deadline=execution_deadline,
            )
        workspace_cleaned = bool(prepared.cleanup_succeeded)
        result["submission"]["workspace_cleaned"] = workspace_cleaned
        if prepared.cleanup_error:
            result["submission"]["cleanup_error"] = _bounded_error_text(prepared.cleanup_error)
        with SUBMISSION_LOCK:
            job = SUBMISSION_JOBS.get(job_id)
            if job is not None:
                cancelled = (
                    bool(job.get("cancel_requested"))
                    or run["stop_reason"] == "cancelled"
                    or (result.get("final") or {}).get("status") == "cancelled"
                )
                deadline_failed = (
                    run["stop_reason"] == "deadline"
                    or time.monotonic() >= execution_deadline
                )
                infrastructure_failed = run["stop_reason"] in {
                    "deadline", "output_limit", "missing", "protocol_error"
                }
                if not workspace_cleaned:
                    verdict = "FAIL"
                    original_failure_code = None
                    if cancelled:
                        original_failure_code = "cancelled"
                    elif deadline_failed:
                        original_failure_code = "task_deadline_exceeded"
                    elif (result.get("final") or {}).get("code"):
                        original_failure_code = result["final"]["code"]
                    if original_failure_code:
                        result["submission"]["original_failure_code"] = original_failure_code
                    _task_failure_result(
                        result,
                        "submission_cleanup_failed",
                        "submission workspace cleanup failed",
                    )
                    infrastructure_failed = True
                    cancelled = False
                elif cancelled:
                    verdict = "CANCELLED"
                    _task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
                elif deadline_failed:
                    verdict = "FAIL"
                    _task_failure_result(result, "task_deadline_exceeded", "task exceeded its total deadline")
                    infrastructure_failed = True
                else:
                    verdict = _submission_verdict(result)
                result["submission"]["verdict"] = verdict
                status = "cancelled" if cancelled else ("failed" if infrastructure_failed else "completed")
                job.update(
                    status=status,
                    verdict=verdict,
                    returncode=run["returncode"],
                    logs=run["logs"],
                    result=result,
                    finished_at=time.time(),
                    failure_code=(result.get("final") or {}).get("code") or None,
                )
                _prune_submission_jobs()
    except SubmissionPreparationCancelled as exc:
        workspace_cleaned = not task_root.exists()
        verdict = "CANCELLED" if workspace_cleaned else "FAIL"
        code = "cancelled" if workspace_cleaned else "submission_cleanup_failed"
        result["submission"].update(workspace_cleaned=workspace_cleaned, verdict=verdict)
        if not workspace_cleaned:
            result["submission"].update(
                cleanup_error="submission workspace still exists after cancellation",
                original_failure_code="cancelled",
            )
        _task_failure_result(
            result,
            code,
            "task cancelled" if workspace_cleaned else "submission workspace cleanup failed",
            status="cancelled" if workspace_cleaned else "failed",
        )
        with SUBMISSION_LOCK:
            if job_id in SUBMISSION_JOBS:
                SUBMISSION_JOBS[job_id].update(
                    status="cancelled" if workspace_cleaned else "failed",
                    verdict=verdict,
                    logs=_bounded_error_text(exc),
                    result=result,
                    finished_at=time.time(),
                    failure_code=code,
                )
                _prune_submission_jobs()
    except SubmissionPreparationDeadlineExceeded as exc:
        workspace_cleaned = not task_root.exists()
        result["submission"].update(workspace_cleaned=workspace_cleaned, verdict="FAIL")
        code = "task_deadline_exceeded" if workspace_cleaned else "submission_cleanup_failed"
        if not workspace_cleaned:
            result["submission"].update(
                cleanup_error="submission workspace still exists after deadline",
                original_failure_code="task_deadline_exceeded",
            )
        _task_failure_result(
            result,
            code,
            "task exceeded its total deadline" if workspace_cleaned else "submission workspace cleanup failed",
        )
        with SUBMISSION_LOCK:
            if job_id in SUBMISSION_JOBS:
                SUBMISSION_JOBS[job_id].update(
                    status="failed",
                    verdict="FAIL",
                    logs=_bounded_error_text(exc),
                    result=result,
                    finished_at=time.time(),
                    failure_code=code,
                )
                _prune_submission_jobs()
    except Exception as exc:
        cancelled = _task_cancel_requested(SUBMISSION_JOBS, SUBMISSION_LOCK, job_id)
        verdict = "CANCELLED" if cancelled else "FAIL"
        code = "cancelled" if cancelled else "task_worker_failed"
        message = "task cancelled" if cancelled else _bounded_error_text(exc)
        workspace_cleaned = not task_root.exists()
        if not workspace_cleaned:
            result["submission"]["original_failure_code"] = code
            code = "submission_cleanup_failed"
            verdict = "FAIL"
            message = "submission workspace cleanup failed"
            cancelled = False
        result["submission"].update(workspace_cleaned=workspace_cleaned, verdict=verdict)
        if not workspace_cleaned:
            result["submission"]["cleanup_error"] = "submission workspace still exists after failure"
        _task_failure_result(result, code, message, status=code)
        with SUBMISSION_LOCK:
            if job_id in SUBMISSION_JOBS:
                SUBMISSION_JOBS[job_id].update(
                    status="cancelled" if cancelled else "failed",
                    verdict=verdict,
                    logs=message,
                    result=result,
                    finished_at=time.time(),
                    failure_code=code,
                )
                _prune_submission_jobs()


@app.route('/api/submission/run', methods=['POST'])
def submission_run():
    try:
        subtitle = request.form.get("subtitle", "")
        index = int(request.form.get("index", "-1"))
        upload = request.files.get("source")
        if not subtitle:
            return jsonify({"success": False, "error": "Missing subtitle"}), 400
        if upload is None:
            return jsonify({"success": False, "error": "Missing source file"}), 400
        filename = upload.filename or ""
        source = upload.stream.read(MAX_SOURCE_BYTES + 1)
        validate_cpp_upload(filename, source)
        # Resolve the selected problem before accepting the job. This prevents
        # retaining uploads for invalid or non-runnable selections.
        info = _sandbox_problem_info(subtitle, index)
        if not info.get("runnable"):
            return jsonify({"success": False, "error": info.get("reason") or "problem is not runnable"}), 400

        job_id = uuid.uuid4().hex
        now = time.time()
        now_monotonic = time.monotonic()
        with SUBMISSION_LOCK:
            _prune_submission_jobs()
            protected = [
                current_id for current_id, job in SUBMISSION_JOBS.items()
                if job.get("status") in {"queued", "running", "cancelling"}
            ]
            cleanup_stale_submission_workspaces(Path.cwd(), protected_task_ids=protected)
            SUBMISSION_JOBS[job_id] = {
                "status": "queued",
                "verdict": "PENDING",
                "logs": "",
                "result": None,
                "filename": filename,
                "created_at": now,
                "queue_deadline_at": now + WEBUI_TASK_QUEUE_DEADLINE,
                "deadline_at": now + WEBUI_TASK_TOTAL_DEADLINE,
                "_deadline_monotonic": now_monotonic + WEBUI_TASK_TOTAL_DEADLINE,
                "cancel_requested": False,
                "logs_truncated": False,
            }
        task_slot = _transfer_task_request()
        try:
            accepted = WEBUI_TASK_EXECUTOR.submit(
                job_id,
                lambda: _run_with_task_slot(
                    task_slot,
                    lambda: _run_submission_job(job_id, info, filename, source),
                ),
                queue_timeout=WEBUI_TASK_QUEUE_DEADLINE,
                on_discard=lambda reason: _discard_with_task_slot(
                    task_slot,
                    lambda discard_reason: _discard_queued_job(
                        SUBMISSION_JOBS,
                        SUBMISSION_LOCK,
                        job_id,
                        discard_reason,
                        submission=True,
                    ),
                    reason,
                ),
                on_error=lambda exc: _task_executor_error(
                    SUBMISSION_JOBS, SUBMISSION_LOCK, job_id, exc, submission=True
                ),
            )
        except BaseException:
            _release_task_request_slot(task_slot)
            with SUBMISSION_LOCK:
                SUBMISSION_JOBS.pop(job_id, None)
            raise
        if not accepted:
            _release_task_request_slot(task_slot)
            with SUBMISSION_LOCK:
                SUBMISSION_JOBS.pop(job_id, None)
            return _queue_full_response()
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except RequestEntityTooLarge:
        raise
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/submission/job/<job_id>')
def submission_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    WEBUI_TASK_EXECUTOR.expire_pending()
    with SUBMISSION_LOCK:
        _prune_submission_jobs()
        job = SUBMISSION_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        payload = _public_job_payload(job)
    return jsonify({"success": True, **payload})


@app.route('/api/submission/job/<job_id>/cancel', methods=['POST'])
def submission_cancel(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    with SUBMISSION_LOCK:
        job = SUBMISSION_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return jsonify({"success": True, "status": job.get("status"), "verdict": job.get("verdict")})
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        cancel_file = SUBMISSION_CANCEL_FILES.get(job_id)
    if cancel_file is not None:
        _touch_cancel_file(cancel_file)
    WEBUI_TASK_EXECUTOR.cancel_pending(job_id)
    with SUBMISSION_LOCK:
        current = SUBMISSION_JOBS.get(job_id, {})
        status = current.get("status", "cancelling")
        verdict = current.get("verdict")
    return jsonify({"success": True, "status": status, "verdict": verdict})


@app.route('/api/config/<subtitle>', methods=['GET'])
def get_contest_config(subtitle):
    try:
        root, workspace = _schema_workspace(subtitle)
        return jsonify({"success": True, "config": load_contest_config(root, workspace, {})})
    except ProbHubError as exc:
        status = 409 if exc.code in {"migration_required", "unknown_subtitle"} else 400
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/config/<subtitle>', methods=['POST'])
def save_contest_config(subtitle):
    try:
        config = request.json
        root, _ = _schema_workspace(subtitle)
        with workspace_build_lock(root):
            _, live_workspace = load_workspace(root)
            saved = save_schema_contest_config(root, live_workspace, config)
        return jsonify({"success": True, "config": saved})
    except RequestEntityTooLarge:
        raise
    except ProbHubError as e:
        status = 409 if e.code in {"source_conflict", "build_busy", "migration_required", "unknown_subtitle"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/pdf-pages/<subtitle>')
def pdf_page_count(subtitle):
    """Return the number of pages in main.pdf."""
    try:
        _, _, typst_dir = _schema_typst_dir(subtitle)
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), 409
    preview_path = _preview_pdf_path(subtitle)
    pdf_path = str(preview_path if preview_path.is_file() else typst_dir / "main.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({"pages": 0})
    try:
        return jsonify({"pages": bounded_pdf_page_count(pdf_path)})
    except Exception:
        return jsonify({"pages": 0})


@app.route('/api/pdf-page/<subtitle>/<int:page>')
def serve_pdf_page(subtitle, page):
    """Render a single PDF page through Poppler into the process temp cache."""
    from flask import send_file

    try:
        _, _, typst_dir = _schema_typst_dir(subtitle)
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), 409
    preview_path = _preview_pdf_path(subtitle)
    pdf_path = str(preview_path if preview_path.is_file() else typst_dir / "main.pdf")
    if not os.path.exists(pdf_path):
        return "PDF not compiled", 404

    # Validate page number
    try:
        total = bounded_pdf_page_count(pdf_path)
        if page < 0 or page >= total:
            return "Page out of range", 404
    except Exception:
        return "Invalid PDF", 500

    # Page rendering is a read-only WebUI concern; never cache into the workspace.
    preview_dir = str(WEBUI_PREVIEW_ROOT / subtitle / ".pages")
    os.makedirs(preview_dir, exist_ok=True)
    png_path = os.path.join(preview_dir, f"page-{page + 1}.png")

    # Regenerate if PNG is missing or older than PDF
    if not os.path.exists(png_path) or os.path.getmtime(png_path) < os.path.getmtime(pdf_path):
        prefix = str(Path(png_path).with_suffix(""))
        stdout_path = Path(preview_dir) / f"page-{page + 1}.stdout"
        stderr_path = Path(preview_dir) / f"page-{page + 1}.stderr"
        try:
            rendered = run_managed_to_files(
                [
                    "pdftoppm",
                    "-f", str(page + 1),
                    "-l", str(page + 1),
                    "-singlefile",
                    "-png",
                    "-r", "144",
                    pdf_path,
                    prefix,
                ],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=30,
                memory_limit_mb=512,
                output_limit_bytes=4 * 1024 * 1024,
                process_limit=8,
                cwd=Path.cwd(),
            )
        except OSError as exc:
            return f"Render failed: {exc}", 500
        if rendered["reason"] != "completed" or rendered["returncode"] != 0:
            detail = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
            return f"Render failed: {detail[-1000:]}", 500

    if not os.path.exists(png_path):
        return "Render failed", 500

    return send_file(os.path.abspath(png_path), mimetype='image/png')


def open_browser(url="http://127.0.0.1:33933"):
    webbrowser.open_new(url)


def shutdown_webui_tasks():
    process_items = []
    cancel_paths = []
    for jobs, processes, cancel_files, lock in (
        (SANDBOX_JOBS, SANDBOX_PROCESSES, SANDBOX_CANCEL_FILES, SANDBOX_LOCK),
        (SUBMISSION_JOBS, SUBMISSION_PROCESSES, SUBMISSION_CANCEL_FILES, SUBMISSION_LOCK),
    ):
        with lock:
            for job in jobs.values():
                if job.get("status") in ACTIVE_TASK_STATUSES:
                    job["cancel_requested"] = True
                    job["status"] = "cancelling"
            process_items.extend(processes.values())
            cancel_paths.extend(cancel_files.values())
    for cancel_path in cancel_paths:
        _touch_cancel_file(cancel_path)
    WEBUI_TASK_EXECUTOR.shutdown(wait=False, cancel_pending=True)
    for proc in process_items:
        if proc.poll() is None:
            terminate_external_process_tree(proc, snapshot_process_tree(proc.pid))
    WEBUI_TASK_EXECUTOR.shutdown(wait=True, cancel_pending=True)
    WEBUI_PREVIEW_TEMP.cleanup()


class BoundedThreadedWSGIServer(ThreadedWSGIServer):
    """Werkzeug server with a hard cap on live request-handler threads."""

    daemon_threads = True

    def __init__(self, host, port, application, *, max_request_threads):
        if isinstance(max_request_threads, bool) or int(max_request_threads) <= 0:
            raise ValueError("max_request_threads must be a positive integer")
        self.max_request_threads = int(max_request_threads)
        self._request_slots = BoundedSemaphore(self.max_request_threads)
        self._request_stats_lock = Lock()
        self._active_request_threads = 0
        self._peak_request_threads = 0
        super().__init__(host, port, application)

    def process_request(self, request_socket, client_address):
        self._request_slots.acquire()
        try:
            super().process_request(request_socket, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request_socket, client_address):
        with self._request_stats_lock:
            self._active_request_threads += 1
            self._peak_request_threads = max(
                self._peak_request_threads,
                self._active_request_threads,
            )
        try:
            super().process_request_thread(request_socket, client_address)
        finally:
            with self._request_stats_lock:
                self._active_request_threads -= 1
            self._request_slots.release()

    def request_thread_stats(self):
        with self._request_stats_lock:
            return {
                "active": self._active_request_threads,
                "peak": self._peak_request_threads,
                "limit": self.max_request_threads,
            }


def _create_webui_server(port, *, max_request_threads=WEBUI_HTTP_REQUEST_THREADS):
    return BoundedThreadedWSGIServer(
        "127.0.0.1",
        port,
        app,
        max_request_threads=max_request_threads,
    )


def run_server(*, port=33933, open_browser=True):
    url = f"http://127.0.0.1:{port}"
    print("\n" + "="*50)
    print("🚀 ProbHub 动态排版控制台启动...")
    print(f"📂 工作区: {Path.cwd()}")
    print(f"🌐 地址: {url}")
    print("="*50)
    if open_browser:
        browser_opener = globals()["open_browser"]
        Timer(1, lambda: browser_opener(url)).start()
    try:
        server = _create_webui_server(port)
        server.serve_forever()
    finally:
        if "server" in locals():
            server.server_close()
        shutdown_webui_tasks()

if __name__ == '__main__':
    run_server()
