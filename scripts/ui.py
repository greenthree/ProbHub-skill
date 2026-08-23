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
import uuid
import time
from pathlib import Path
from threading import BoundedSemaphore, Lock, Timer
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
    snapshot_process_tree_identities,
    terminate_external_process_tree,
)
from probhub.submissions import (
    MAX_SOURCE_BYTES,
    cleanup_stale_submission_workspaces,
    temporary_submission_workspace,
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
from probhub.webui_judge_protocol import (
    apply_sandbox_event as core_apply_sandbox_event,
    consume_jsonl_chunk as core_consume_jsonl_chunk,
    empty_sandbox_result as core_empty_sandbox_result,
    sandbox_log_line as core_sandbox_log_line,
    submission_verdict as core_submission_verdict,
)
from probhub.workspace import load_workspace, problem_entries
from probhub.webui_tasks import (
    ACTIVE_TASK_STATUSES as CORE_ACTIVE_TASK_STATUSES,
    BoundedTaskExecutor,
    BoundedUtf8Log,
    JudgeSupervisorLimits,
    acquire_task_lock as core_acquire_task_lock,
    bounded_error_text as core_bounded_error_text,
    claim_task_execution as core_claim_task_execution,
    discard_queued_job as core_discard_queued_job,
    prune_job_registry as core_prune_job_registry,
    public_job_payload as core_public_job_payload,
    publish_task_progress as core_publish_task_progress,
    run_sandbox_job as core_run_sandbox_job,
    run_submission_job as core_run_submission_job,
    supervise_judge_process as core_supervise_judge_process,
    task_cancel_requested as core_task_cancel_requested,
    task_executor_error as core_task_executor_error,
    task_failure_result as core_task_failure_result,
    touch_cancel_file as core_touch_cancel_file,
)
from probhub.webui_server import BoundedThreadedWSGIServer

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
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
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


def _empty_sandbox_result(info):
    return core_empty_sandbox_result(info)


def _apply_sandbox_event(result, event):
    return core_apply_sandbox_event(result, event)


def _sandbox_log_line(event):
    return core_sandbox_log_line(event)


ACTIVE_TASK_STATUSES = set(CORE_ACTIVE_TASK_STATUSES)


def _prune_job_registry(jobs):
    return core_prune_job_registry(
        jobs,
        result_ttl=WEBUI_TASK_RESULT_TTL,
        max_completed=WEBUI_TASK_MAX_COMPLETED,
        now=time.time(),
    )


def _prune_sandbox_jobs():
    _prune_job_registry(SANDBOX_JOBS)


def _prune_submission_jobs():
    _prune_job_registry(SUBMISSION_JOBS)


def _task_failure_result(result, code, message, *, status="failed"):
    return core_task_failure_result(result, code, message, status=status)


def _bounded_error_text(value):
    return core_bounded_error_text(value, WEBUI_TASK_ERROR_BYTES)


def _public_job_payload(job):
    return core_public_job_payload(job)


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
    return core_discard_queued_job(
        jobs,
        lock,
        job_id,
        reason,
        submission=submission,
        result_ttl=WEBUI_TASK_RESULT_TTL,
        max_completed=WEBUI_TASK_MAX_COMPLETED,
        now=time.time(),
    )


def _task_executor_error(jobs, lock, job_id, exc, *, submission=False):
    return core_task_executor_error(
        jobs,
        lock,
        job_id,
        exc,
        submission=submission,
        workspace_root=Path.cwd(),
        error_limit_bytes=WEBUI_TASK_ERROR_BYTES,
        result_ttl=WEBUI_TASK_RESULT_TTL,
        max_completed=WEBUI_TASK_MAX_COMPLETED,
        now=time.time(),
    )


def _task_cancel_requested(jobs, lock, job_id):
    return core_task_cancel_requested(jobs, lock, job_id)


def _claim_task_execution(jobs, lock, job_id):
    return core_claim_task_execution(
        jobs,
        lock,
        job_id,
        execution_deadline=WEBUI_TASK_EXECUTION_DEADLINE,
        wall_time=time.time,
        monotonic_time=time.monotonic,
    )


def _sandbox_problem_lock(problem_dir):
    key = str(Path(problem_dir).resolve())
    with SANDBOX_PROBLEM_LOCKS_GUARD:
        return SANDBOX_PROBLEM_LOCKS.setdefault(key, Lock())


def _acquire_problem_lock(lock, jobs, jobs_lock, job_id, execution_deadline):
    return core_acquire_task_lock(
        lock,
        jobs,
        jobs_lock,
        job_id,
        execution_deadline,
        monotonic_time=time.monotonic,
    )


def _publish_task_progress(jobs, lock, job_id, result, log):
    return core_publish_task_progress(jobs, lock, job_id, result, log)


def _consume_jsonl_chunk(result, log, state, chunk, *, final=False):
    return core_consume_jsonl_chunk(
        result,
        log,
        state,
        chunk,
        final=final,
        event_limit_bytes=WEBUI_TASK_EVENT_BYTES,
        final_event_limit_bytes=WEBUI_TASK_FINAL_EVENT_BYTES,
    )


def _touch_cancel_file(cancel_file):
    return core_touch_cancel_file(cancel_file)


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


def _sandbox_process_spec(cancel_file, info):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
    return (
        [sys.executable, str(LOCAL_JUDGE_SCRIPT), info["dir"], "--jsonl", "--cancellable"],
        env,
    )


def _run_sandbox_job(job_id, info):
    return core_run_sandbox_job(
        job_id,
        info,
        jobs=SANDBOX_JOBS,
        lock=SANDBOX_LOCK,
        processes=SANDBOX_PROCESSES,
        cancel_files=SANDBOX_CANCEL_FILES,
        result_factory=_empty_sandbox_result,
        problem_lock_factory=_sandbox_problem_lock,
        acquire_problem_lock=_acquire_problem_lock,
        process_spec_factory=_sandbox_process_spec,
        supervise=_supervise_judge_process,
        execution_deadline=WEBUI_TASK_EXECUTION_DEADLINE,
        result_ttl=WEBUI_TASK_RESULT_TTL,
        max_completed=WEBUI_TASK_MAX_COMPLETED,
        error_limit_bytes=WEBUI_TASK_ERROR_BYTES,
        now_fn=time.time,
        monotonic_fn=time.monotonic,
    )


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
    return core_submission_verdict(result)


def _submission_process_spec(cancel_file, prepared):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
    return (
        [
            sys.executable,
            str(LOCAL_JUDGE_SCRIPT),
            str(prepared.problem_dir),
            "--jsonl",
            "--no-cache",
            "--cancellable",
        ],
        env,
    )


def _run_submission_job(job_id, info, filename, source):
    return core_run_submission_job(
        job_id,
        info,
        filename,
        source,
        workspace_root=Path.cwd(),
        jobs=SUBMISSION_JOBS,
        lock=SUBMISSION_LOCK,
        processes=SUBMISSION_PROCESSES,
        cancel_files=SUBMISSION_CANCEL_FILES,
        result_factory=_empty_sandbox_result,
        workspace_factory=temporary_submission_workspace,
        process_spec_factory=_submission_process_spec,
        supervise=_supervise_judge_process,
        verdict_factory=_submission_verdict,
        execution_deadline=WEBUI_TASK_EXECUTION_DEADLINE,
        result_ttl=WEBUI_TASK_RESULT_TTL,
        max_completed=WEBUI_TASK_MAX_COMPLETED,
        error_limit_bytes=WEBUI_TASK_ERROR_BYTES,
        now_fn=time.time,
        monotonic_fn=time.monotonic,
    )


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
            terminate_external_process_tree(
                proc, snapshot_process_tree_identities(proc.pid)
            )
    WEBUI_TASK_EXECUTOR.shutdown(wait=True, cancel_pending=True)
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
