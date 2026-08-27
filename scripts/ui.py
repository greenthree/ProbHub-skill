# -*- coding: utf-8 -*-
import os
import sys
import json
import hmac
import re
import secrets
import subprocess
import tempfile
import webbrowser
import time
from pathlib import Path
from threading import BoundedSemaphore, Timer
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, render_template_string, send_file
from werkzeug.exceptions import RequestEntityTooLarge

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
)
from probhub.submissions import (
    MAX_SOURCE_BYTES,
    validate_cpp_upload,
)
from probhub.typesetting import compile_collection
from probhub.webui_preview import (
    count_preview_pages,
    preview_pages_dir,
    preview_pdf_path,
    publish_preview_pdf,
    render_preview_page,
    validate_typst_directory,
)
from probhub.webui_workspace import (
    load_contest_config,
    load_editor_data,
    save_contest_config as save_schema_contest_config,
    save_editor_data,
    sandbox_problem_info as core_sandbox_problem_info,
    schema_workspace_for_subtitle,
    statement_asset_path,
)
from probhub.webui_health import HEALTH_SCHEMA_VERSION, build_health_summary
from probhub.webui_coverage import COVERAGE_SCHEMA_VERSION, build_coverage_summary
from probhub.workspace import load_workspace, problem_entries
from probhub.webui_tasks import (
    BoundedTaskExecutor,
    JudgeSupervisorLimits,
    supervise_judge_process as core_supervise_judge_process,
    WebUiTaskService,
)
from probhub.webui_server import BoundedThreadedWSGIServer

app = Flask(__name__)
MAX_SUBMISSION_REQUEST_BYTES = MAX_SOURCE_BYTES + 64 * 1024
MAX_WEBUI_REQUEST_BYTES = 16 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_WEBUI_REQUEST_BYTES
WEBUI_CSRF_TOKEN = secrets.token_urlsafe(32)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

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
WEBUI_HTTP_REQUEST_IDLE_TIMEOUT = 30.0
WEBUI_HTTP_OVERLOAD_RESPONSE_TIMEOUT = 0.25
WEBUI_HTTP_OVERLOAD_RETRY_AFTER = 1
WEBUI_TASK_EXECUTOR = BoundedTaskExecutor(
    max_workers=WEBUI_TASK_WORKERS,
    max_queued=WEBUI_TASK_QUEUE_SIZE,
    name="probhub-webui",
)
WEBUI_REQUEST_SLOTS = BoundedSemaphore(WEBUI_TASK_EXECUTOR.capacity)
WEBUI_PREVIEW_TEMP = tempfile.TemporaryDirectory(prefix="probhub-webui-preview-")
WEBUI_PREVIEW_ROOT = Path(WEBUI_PREVIEW_TEMP.name)

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
    response.headers.pop("Access-Control-Allow-Origin", None)
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
    candidate, _ = validate_typst_directory(root, workspace)
    return root, workspace, candidate


def _preview_pdf_path(subtitle, *, root=None, workspace=None, create=False):
    """Resolve a preview PDF through a validated Schema v1 workspace identity."""
    if root is None or workspace is None:
        root, workspace = _schema_workspace(subtitle)
    return preview_pdf_path(
        WEBUI_PREVIEW_ROOT,
        root,
        workspace,
        subtitle,
        create=create,
    )


def _preview_pages_dir(subtitle, *, root=None, workspace=None, create=False):
    if root is None or workspace is None:
        root, workspace = _schema_workspace(subtitle)
    return preview_pages_dir(
        WEBUI_PREVIEW_ROOT,
        root,
        workspace,
        subtitle,
        create=create,
    )


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


def _probhub_error_payload(error):
    payload = {"success": False, "error": str(error), "code": error.code}
    payload.update({
        key: value
        for key, value in (getattr(error, "details", {}) or {}).items()
        if key not in {"success", "error", "code"}
    })
    return payload


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
        if not (Path.cwd() / ".probhub" / "workspace.yaml").is_file():
            raise ProbHubError(
                "Workspace Schema v1 is required; migrate this old workspace first",
                code="migration_required",
            )
        _, workspace = load_workspace(Path.cwd(), allow_empty=True)
        typst_dir, _ = validate_typst_directory(Path.cwd(), workspace)
        return jsonify([typst_dir.name])
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), 409

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


@app.route('/api/health', methods=['GET'])
def get_health():
    """Return a read-only, bounded projection of Core health state."""
    subtitle = request.args.get('subtitle')
    if not subtitle:
        return jsonify({
            "success": True,
            "schema_version": HEALTH_SCHEMA_VERSION,
            "workspace": {},
            "summary": {},
            "problems": [],
            "generation": {"state": "none", "ok": False, "missing": []},
            "diagnostics": [],
        })
    try:
        if not (Path.cwd() / ".probhub" / "workspace.yaml").is_file():
            raise ProbHubError(
                "Workspace Schema v1 is required; migrate this old workspace first",
                code="migration_required",
            )
        root, workspace = load_workspace(Path.cwd(), allow_empty=True)
        typst_dir = Path((workspace.get("typst") or {}).get("directory", "typst-statement/正式赛"))
        if subtitle and typst_dir.name != subtitle:
            raise ProbHubError(f"unknown Schema v1 subtitle: {subtitle}", code="unknown_subtitle")
        selected = request.args.getlist("problem") or None
        return jsonify(build_health_summary(root, workspace, selected))
    except ProbHubError as exc:
        status = 409 if exc.code in {"migration_required", "unknown_subtitle", "recovery_required"} else 400
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), status
    except Exception as exc:
        # Fail closed: never turn a malformed Core result into a partial
        # success that the dashboard might mistake for a delivery decision.
        return jsonify({
            "success": False,
            "error": "health summary unavailable",
            "code": "health_failed",
        }), 500


@app.route('/api/coverage', methods=['GET'])
def get_coverage():
    """Return the bounded, read-only data-group and delivery projection."""
    collection = request.args.get('collection')
    if not collection and 'subtitle' in request.args:
        return jsonify({
            "success": False,
            "error": "use collection to select an existing typesetting collection",
            "code": "invalid_collection_selector",
        }), 400
    if not collection:
        return jsonify({
            "success": True,
            "schema_version": COVERAGE_SCHEMA_VERSION,
            "source_revision": None,
            "workspace": {},
            "problems": [],
            "delivery": {"state": "see-health", "requires": []},
            "diagnostics": [],
        })
    try:
        if not (Path.cwd() / ".probhub" / "workspace.yaml").is_file():
            raise ProbHubError(
                "Workspace Schema v1 is required; migrate this old workspace first",
                code="migration_required",
            )
        root, workspace = load_workspace(Path.cwd(), allow_empty=True)
        typst_dir = Path((workspace.get("typst") or {}).get("directory", "typst-statement/正式赛"))
        if typst_dir.name != collection:
            raise ProbHubError(f"unknown Schema v1 collection: {collection}", code="unknown_collection")
        selected = request.args.getlist("problem") or None
        return jsonify(build_coverage_summary(root, workspace, selected_ids=selected))
    except ProbHubError as exc:
        status = 409 if exc.code in {"migration_required", "unknown_collection", "recovery_required"} else 400
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), status
    except Exception:
        # A malformed or partial projection must never look like a delivery
        # decision to the client.
        return jsonify({
            "success": False,
            "error": "coverage summary unavailable",
            "code": "coverage_failed",
        }), 500


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
        return jsonify(_probhub_error_payload(e)), status
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
                publish_preview_pdf(
                    WEBUI_PREVIEW_ROOT,
                    root,
                    snapshot.workspace,
                    subtitle,
                    staged_pdf,
                )
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
        return jsonify(_probhub_error_payload(e)), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _sandbox_problem_info(subtitle, index):
    root, workspace = _schema_workspace(subtitle)
    # The Core owns Schema v1 parsing, limits, data accounting and path
    # validation. This wrapper only adapts the installed WebUI launcher state.
    return core_sandbox_problem_info(
        root,
        workspace,
        index,
        script_exists=LOCAL_JUDGE_SCRIPT.is_file(),
        files_per_role=WEBUI_TASK_INFO_FILES_PER_ROLE,
    )


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
    return core_supervise_judge_process(
        job_id,
        command,
        env,
        cancel_file,
        result,
        jobs=jobs,
        lock=lock,
        processes=processes,
        cancel_files=cancel_files,
        execution_deadline=execution_deadline,
        limits=JudgeSupervisorLimits(
            log_bytes=WEBUI_TASK_LOG_BYTES,
            event_bytes=WEBUI_TASK_EVENT_BYTES,
            final_event_bytes=WEBUI_TASK_FINAL_EVENT_BYTES,
            process_output_bytes=WEBUI_TASK_PROCESS_OUTPUT_BYTES,
            force_cancel_after=SUBMISSION_FORCE_CANCEL_AFTER,
        ),
    )


WEBUI_TASK_SERVICE = WebUiTaskService(
    workspace_root=Path.cwd(),
    local_judge_script=LOCAL_JUDGE_SCRIPT,
    executor=WEBUI_TASK_EXECUTOR,
    result_ttl=WEBUI_TASK_RESULT_TTL,
    max_completed=WEBUI_TASK_MAX_COMPLETED,
    queue_deadline=WEBUI_TASK_QUEUE_DEADLINE,
    execution_deadline=WEBUI_TASK_EXECUTION_DEADLINE,
    total_deadline=WEBUI_TASK_TOTAL_DEADLINE,
    force_cancel_after=SUBMISSION_FORCE_CANCEL_AFTER,
    log_bytes=WEBUI_TASK_LOG_BYTES,
    event_bytes=WEBUI_TASK_EVENT_BYTES,
    final_event_bytes=WEBUI_TASK_FINAL_EVENT_BYTES,
    process_output_bytes=WEBUI_TASK_PROCESS_OUTPUT_BYTES,
    error_bytes=WEBUI_TASK_ERROR_BYTES,
)
WEBUI_TASK_SERVICE.supervisor_hook = (
    lambda *args, **kwargs: _supervise_judge_process(*args, **kwargs)
)
SANDBOX_JOBS = WEBUI_TASK_SERVICE.sandbox_jobs
SANDBOX_LOCK = WEBUI_TASK_SERVICE.sandbox_lock
SANDBOX_PROCESSES = WEBUI_TASK_SERVICE.sandbox_processes
SANDBOX_CANCEL_FILES = WEBUI_TASK_SERVICE.sandbox_cancel_files
SUBMISSION_JOBS = WEBUI_TASK_SERVICE.submission_jobs
SUBMISSION_LOCK = WEBUI_TASK_SERVICE.submission_lock
SUBMISSION_PROCESSES = WEBUI_TASK_SERVICE.submission_processes
SUBMISSION_CANCEL_FILES = WEBUI_TASK_SERVICE.submission_cancel_files


def _task_service():
    """Refresh adapter-controlled dependencies before dispatching a task."""

    WEBUI_TASK_SERVICE.executor = WEBUI_TASK_EXECUTOR
    WEBUI_TASK_SERVICE.workspace_root = Path.cwd()
    WEBUI_TASK_SERVICE.local_judge_script = LOCAL_JUDGE_SCRIPT
    WEBUI_TASK_SERVICE.result_ttl = float(WEBUI_TASK_RESULT_TTL)
    WEBUI_TASK_SERVICE.max_completed = int(WEBUI_TASK_MAX_COMPLETED)
    WEBUI_TASK_SERVICE.queue_deadline = float(WEBUI_TASK_QUEUE_DEADLINE)
    WEBUI_TASK_SERVICE.execution_deadline = float(WEBUI_TASK_EXECUTION_DEADLINE)
    WEBUI_TASK_SERVICE.total_deadline = float(WEBUI_TASK_TOTAL_DEADLINE)
    WEBUI_TASK_SERVICE.force_cancel_after = float(SUBMISSION_FORCE_CANCEL_AFTER)
    WEBUI_TASK_SERVICE.error_bytes = int(WEBUI_TASK_ERROR_BYTES)
    WEBUI_TASK_SERVICE.limits = JudgeSupervisorLimits(
        log_bytes=int(WEBUI_TASK_LOG_BYTES),
        event_bytes=int(WEBUI_TASK_EVENT_BYTES),
        final_event_bytes=int(WEBUI_TASK_FINAL_EVENT_BYTES),
        process_output_bytes=int(WEBUI_TASK_PROCESS_OUTPUT_BYTES),
        force_cancel_after=float(SUBMISSION_FORCE_CANCEL_AFTER),
    )
    return WEBUI_TASK_SERVICE


def _run_sandbox_job(job_id, info):
    return _task_service().run_sandbox_job(job_id, info)


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
        task_slot = _transfer_task_request()
        accepted, job_id = _task_service().enqueue_sandbox(
            info,
            task_slot=task_slot,
            worker=_run_sandbox_job,
        )
        if not accepted:
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
    payload = _task_service().get("sandbox", job_id)
    if payload is None:
        return jsonify({"success": False, "error": "job not found"}), 404
    return jsonify({"success": True, **payload})


@app.route('/api/sandbox/job/<job_id>/cancel', methods=['POST'])
def sandbox_cancel(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    state = _task_service().cancel("sandbox", job_id)
    if state is None:
        return jsonify({"success": False, "error": "job not found"}), 404
    return jsonify({"success": True, **state})


def _run_submission_job(job_id, info, filename, source):
    return _task_service().run_submission_job(job_id, info, filename, source)


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

        task_slot = _transfer_task_request()
        accepted, job_id = _task_service().enqueue_submission(
            info,
            filename,
            source,
            task_slot=task_slot,
            worker=_run_submission_job,
        )
        if not accepted:
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
    payload = _task_service().get("submission", job_id)
    if payload is None:
        return jsonify({"success": False, "error": "job not found"}), 404
    return jsonify({"success": True, **payload})


@app.route('/api/submission/job/<job_id>/cancel', methods=['POST'])
def submission_cancel(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    state = _task_service().cancel("submission", job_id)
    if state is None:
        return jsonify({"success": False, "error": "job not found"}), 404
    return jsonify({"success": True, **state})


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
        return jsonify(_probhub_error_payload(e)), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/pdf-pages/<subtitle>')
def pdf_page_count(subtitle):
    """Return the number of pages in main.pdf."""
    try:
        root, workspace, _ = _schema_typst_dir(subtitle)
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), 409
    try:
        pages = count_preview_pages(
            WEBUI_PREVIEW_ROOT,
            root,
            workspace,
            subtitle,
            page_count_fn=bounded_pdf_page_count,
        )
        return jsonify({"pages": pages})
    except Exception:
        return jsonify({"pages": 0})


@app.route('/api/pdf-page/<subtitle>/<int:page>')
def serve_pdf_page(subtitle, page):
    """Render a single PDF page through Poppler into the process temp cache."""
    from flask import send_file

    try:
        root, workspace, _ = _schema_typst_dir(subtitle)
        rendered = render_preview_page(
            WEBUI_PREVIEW_ROOT,
            root,
            workspace,
            subtitle,
            page,
            page_count_fn=bounded_pdf_page_count,
            run_managed_fn=run_managed_to_files,
        )
    except ProbHubError as exc:
        return jsonify({"success": False, "error": str(exc), "code": exc.code}), 409
    status = rendered["status"]
    if status == "not_compiled":
        return "PDF not compiled", 404
    if status == "page_out_of_range":
        return "Page out of range", 404
    if status == "invalid_pdf":
        return "Invalid PDF", 500
    if status == "render_failed":
        return f"Render failed: {rendered.get('error', '')}", 500
    return send_file(os.path.abspath(rendered["path"]), mimetype='image/png')


def open_browser(url="http://127.0.0.1:33933"):
    webbrowser.open_new(url)


def shutdown_webui_tasks():
    _task_service().shutdown()
    WEBUI_PREVIEW_TEMP.cleanup()


def _create_webui_server(
    port,
    *,
    max_request_threads=WEBUI_HTTP_REQUEST_THREADS,
    request_idle_timeout=WEBUI_HTTP_REQUEST_IDLE_TIMEOUT,
    overload_response_timeout=WEBUI_HTTP_OVERLOAD_RESPONSE_TIMEOUT,
    overload_retry_after=WEBUI_HTTP_OVERLOAD_RETRY_AFTER,
):
    return BoundedThreadedWSGIServer(
        "127.0.0.1",
        port,
        app,
        max_request_threads=max_request_threads,
        request_idle_timeout=request_idle_timeout,
        overload_response_timeout=overload_response_timeout,
        overload_retry_after=overload_retry_after,
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
