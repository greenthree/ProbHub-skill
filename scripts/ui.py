# -*- coding: utf-8 -*-
import os
import sys
import json
import hmac
import re
import secrets
import signal
import subprocess
import shutil
import tempfile
import webbrowser
import uuid
import time
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread, Timer
from urllib.parse import urlsplit

import yaml
from flask import Flask, jsonify, request, render_template_string, send_file
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
from probhub.process_control import (
    run_managed_to_files,
    snapshot_process_tree,
    terminate_external_process_tree,
)
from probhub.submissions import (
    MAX_SOURCE_BYTES,
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

app = Flask(__name__)
MAX_SUBMISSION_REQUEST_BYTES = MAX_SOURCE_BYTES + 64 * 1024
MAX_WEBUI_REQUEST_BYTES = 16 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_WEBUI_REQUEST_BYTES
WEBUI_CSRF_TOKEN = secrets.token_urlsafe(32)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

BASE_DIR = "typst-statement"
SANDBOX_JOBS = {}
SANDBOX_LOCK = Lock()
SUBMISSION_JOBS = {}
SUBMISSION_PROCESSES = {}
SUBMISSION_CANCEL_FILES = {}
SUBMISSION_LOCK = Lock()
SUBMISSION_RESULT_TTL = 60 * 60
SUBMISSION_FORCE_CANCEL_AFTER = 3.0
MAX_CONCURRENT_SUBMISSIONS = max(1, min(4, os.cpu_count() or 2))
SUBMISSION_SLOTS = BoundedSemaphore(MAX_CONCURRENT_SUBMISSIONS)
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


def secure_path(subtitle, filename):
    """安全路径拼接，防止路径穿越攻击"""
    if not subtitle or '..' in subtitle or '/' in subtitle or '\\' in subtitle:
        raise ValueError("Invalid subtitle")
    return os.path.join(BASE_DIR, subtitle, filename)


def _schema_workspace(subtitle):
    return schema_workspace_for_subtitle(Path.cwd(), subtitle)


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


def read_problem_limits_from_dir(prob_dir):
    time_limit = 1.0
    memory_limit = 256
    has_meta_time_limit = False
    has_meta_memory_limit = False

    config = read_probhub_config_from_dir(prob_dir)
    if config is not None:
        limits = config.get("limits") or {}
        try:
            if "time" in limits:
                time_limit = float(limits.get("time"))
                has_meta_time_limit = True
            if "memory" in limits:
                memory_limit = int(float(limits.get("memory")))
                has_meta_memory_limit = True
        except (TypeError, ValueError):
            pass

    meta_path = os.path.join(prob_dir, "meta.json")
    if (not has_meta_time_limit or not has_meta_memory_limit) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            problem = meta.get("problem", {})
            if not has_meta_time_limit and "time_limit" in problem:
                time_limit = float(problem.get("time_limit"))
                has_meta_time_limit = True
            if not has_meta_memory_limit and "memory_limit" in problem:
                memory_limit = int(float(problem.get("memory_limit")))
                has_meta_memory_limit = True
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            pass

    ini_path = os.path.join(prob_dir, "domjudge-problem.ini")
    if not has_meta_time_limit and os.path.exists(ini_path):
        try:
            with open(ini_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"timelimit\s*=\s*['\"]?([0-9.]+)", text)
            if m:
                time_limit = float(m.group(1))
        except (TypeError, ValueError, OSError):
            pass

    yaml_path = os.path.join(prob_dir, "problem.yaml")
    if not has_meta_memory_limit and os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"(?m)^\s*memory\s*:\s*([0-9]+)", text)
            if m:
                memory_limit = int(m.group(1))
        except (TypeError, ValueError, OSError):
            pass

    time_limit = max(time_limit, 0.1)
    memory_limit = max(memory_limit, 1)
    if float(time_limit).is_integer():
        time_limit = int(time_limit)
    return time_limit, memory_limit


def enrich_problem_limits(problem_entries):
    name_to_dir = find_problem_dirs()
    for entry in problem_entries:
        problem = entry.setdefault("problem", {})
        if "time_limit" in problem and "memory_limit" in problem:
            continue
        display_name = problem.get("display_name", "")
        prob_dir = name_to_dir.get(display_name)
        if prob_dir:
            time_limit, memory_limit = read_problem_limits_from_dir(prob_dir)
        else:
            time_limit, memory_limit = problem_limits(entry)
        if "time_limit" not in problem:
            problem["time_limit"] = time_limit
        if "memory_limit" not in problem:
            problem["memory_limit"] = memory_limit
    return problem_entries

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
    """动态扫描 typst-statement 目录下所有的子文件夹（即排版集）"""
    if not os.path.exists(BASE_DIR):
        return jsonify([])
    # 仅返回是目录的名称
    subs = [d for d in os.listdir(BASE_DIR) if os.path.isdir(os.path.join(BASE_DIR, d))]
    return jsonify(sorted(subs))

@app.route('/api/data', methods=['GET'])
def get_data():
    subtitle = request.args.get('subtitle')
    if not subtitle:
        return jsonify([])
    try:
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, workspace = schema
            return jsonify(load_editor_data(root, workspace))
        json_path = secure_path(subtitle, "problems.json")
        if not os.path.exists(json_path):
            return jsonify([])
        with open(json_path, 'r', encoding='utf-8') as f:
            return jsonify(enrich_problem_limits(json.load(f)))
    except Exception as e:
        print(f"[-] Data Load Error: {e}")
        return jsonify([])


@app.route('/api/problem-assets/<subtitle>/<problem_id>/<path:asset_path>', methods=['GET'])
def get_problem_asset(subtitle, problem_id, asset_path):
    try:
        schema = _schema_workspace(subtitle)
        if schema is None:
            return "Not found", 404
        root, workspace = schema
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
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, _ = schema
            with workspace_build_lock(root):
                _, live_workspace = load_workspace(root)
                problems = save_editor_data(root, live_workspace, new_data)
            return jsonify({"success": True, "problems": problems})
        json_path = secure_path(subtitle, "problems.json")
        for entry in new_data:
            entry.setdefault("problem", {})
            time_limit, memory_limit = problem_limits(entry)
            entry["problem"]["time_limit"] = time_limit
            entry["problem"]["memory_limit"] = memory_limit
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        tmp_path = json_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)  # atomic replace
        sync_problem_limits_to_files(new_data)
        return jsonify({"success": True})
    except ProbHubError as e:
        status = 409 if e.code in {"source_conflict", "build_busy"} else 400
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
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, _ = schema
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
        typst_main = secure_path(subtitle, "main.typ")
        ret = subprocess.run(
            ["typst", "compile", "--root", ".", typst_main],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        return jsonify({"success": True, "stdout": ret.stdout, "stderr": ret.stderr})
    except ProbHubError as e:
        detail = analyze_compile_error(str(e))
        status = 409 if e.code in {"artifact_busy", "build_busy", "inputs_changed"} else 400
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
    """仅分发单题 PDF + 注入同名 zip，不编译。"""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, workspace = schema
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
        dist_results = distribute_problems(subtitle)
        return jsonify({"success": True, "distributed": dist_results})
    except ProbHubError as e:
        status = 409 if e.code in {"artifact_busy", "build_busy", "inputs_changed"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def find_problem_dirs():
    """Scan workspace root for problem directories (must contain meta.json and data/)."""
    problem_dirs = {}
    for entry in os.listdir("."):
        if not os.path.isdir(entry):
            continue
        meta = os.path.join(entry, "meta.json")
        data_dir = os.path.join(entry, "data")
        if os.path.isfile(meta) and os.path.isdir(data_dir):
            try:
                with open(meta, "r", encoding="utf-8") as f:
                    info = json.load(f)
                name = info.get("problem", {}).get("display_name", "")
                if name:
                    problem_dirs[name] = entry
            except Exception:
                pass
    return problem_dirs


def _zip_candidate_names(base, filename):
    return {filename, f"{base}/{filename}"}


def _zip_preferred_arcname(infos, base, filename):
    names = {item.filename for item in infos}
    if filename in names:
        return filename
    based = f"{base}/{filename}"
    if based in names:
        return based
    root_style_markers = {"problem.yaml", "domjudge-problem.ini", "problem.pdf", "data/"}
    if any(name in root_style_markers or name.startswith("data/") for name in names):
        return filename
    if any(name.startswith(f"{base}/") for name in names):
        return based
    return filename


def sync_problem_limits_to_files(problem_entries):
    """Best-effort sync of per-problem limits to meta.json and DOMjudge config files."""
    name_to_dir = find_problem_dirs()
    for entry in problem_entries:
        display_name = entry.get("problem", {}).get("display_name", "")
        prob_dir = name_to_dir.get(display_name)
        if not prob_dir:
            continue
        time_limit, memory_limit = problem_limits(entry)

        meta_path = os.path.join(prob_dir, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta.setdefault("problem", {})
                meta["problem"]["time_limit"] = time_limit
                meta["problem"]["memory_limit"] = memory_limit
                tmp = meta_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                os.replace(tmp, meta_path)
            except Exception as e:
                print(f"[-] Limit sync meta failed for {prob_dir}: {e}")

        ini_path = os.path.join(prob_dir, "domjudge-problem.ini")
        try:
            if os.path.exists(ini_path):
                with open(ini_path, "r", encoding="utf-8") as f:
                    text = f.read()
                if re.search(r"(?m)^timelimit\s*=", text):
                    text = re.sub(r"(?m)^timelimit\s*=.*$", f"timelimit='{time_limit:g}'", text)
                else:
                    text = (text.rstrip() + f"\ntimelimit='{time_limit:g}'\n").lstrip()
            else:
                text = f"timelimit='{time_limit:g}'\n"
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"[-] Limit sync ini failed for {prob_dir}: {e}")

        yaml_path = os.path.join(prob_dir, "problem.yaml")
        try:
            if os.path.exists(yaml_path):
                with open(yaml_path, "r", encoding="utf-8") as f:
                    text = f.read()
                if re.search(r"(?m)^\s*memory\s*:", text):
                    text = re.sub(r"(?m)^(\s*)memory\s*:.*$", rf"\1memory: {memory_limit}", text)
                elif re.search(r"(?m)^limits\s*:", text):
                    text = re.sub(r"(?m)^limits\s*:\s*$", f"limits:\n  memory: {memory_limit}", text)
                else:
                    text = text.rstrip() + f"\nlimits:\n  memory: {memory_limit}\n"
            else:
                safe_name = str(display_name).replace("'", "''")
                text = f"name: '{safe_name}'\nlimits:\n  memory: {memory_limit}\n"
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"[-] Limit sync yaml failed for {prob_dir}: {e}")

        zip_path = os.path.join(os.path.basename(os.path.normpath(prob_dir)) + ".zip")
        if os.path.exists(zip_path):
            try:
                import zipfile as zf
                base = os.path.basename(os.path.normpath(prob_dir))
                replacements = {}
                for filename in ("domjudge-problem.ini", "problem.yaml"):
                    path = os.path.join(prob_dir, filename)
                    if os.path.exists(path):
                        with open(path, "rb") as f:
                            replacements[filename] = f.read()
                if replacements:
                    tmp = zip_path + ".tmp"
                    with zf.ZipFile(zip_path, "r") as zin, zf.ZipFile(tmp, "w", compression=zf.ZIP_DEFLATED) as zout:
                        infos = zin.infolist()
                        arc_replacements = {
                            _zip_preferred_arcname(infos, base, filename): content
                            for filename, content in replacements.items()
                        }
                        skip_names = set()
                        for filename in replacements:
                            skip_names.update(_zip_candidate_names(base, filename))
                        for item in zin.infolist():
                            if item.filename not in skip_names:
                                zout.writestr(item, zin.read(item.filename))
                        for arcname, content in arc_replacements.items():
                            zout.writestr(arcname, content)
                    os.replace(tmp, zip_path)
            except Exception as e:
                print(f"[-] Limit sync zip failed for {prob_dir}: {e}")


def _load_problem_by_index(subtitle, index):
    schema = _schema_workspace(subtitle)
    if schema is not None:
        root, workspace = schema
        entries = problem_entries(workspace)
        if index < 0 or index >= len(entries):
            raise ValueError("Problem index out of range")
        data = load_editor_data(root, workspace)
        return data[index]
    json_path = secure_path(subtitle, "problems.json")
    with open(json_path, "r", encoding="utf-8") as f:
        problems = json.load(f)
    if index < 0 or index >= len(problems):
        raise ValueError("Problem index out of range")
    return problems[index]


def _sandbox_problem_info(subtitle, index):
    problem = _load_problem_by_index(subtitle, index)
    display_name = problem.get("problem", {}).get("display_name", "")
    schema = _schema_workspace(subtitle)
    if schema is not None:
        root, workspace = schema
        entries = problem_entries(workspace)
        prob_dir = str(root / entries[index].get("directory", entries[index]["id"]))
    else:
        name_to_dir = find_problem_dirs()
        prob_dir = name_to_dir.get(display_name)
    script_path = os.path.join("scripts", "local_judge.py")
    info = {
        "name": display_name,
        "matched": bool(prob_dir),
        "dir": prob_dir,
        "runnable": False,
        "limits": {"time": problem_limits(problem)[0], "memory": problem_limits(problem)[1]},
        "data_count": 0,
        "sample_count": 0,
        "secret_count": 0,
        "cases": [],
        "files": {"validator": False, "std": [], "brute": [], "wrong": [], "other": []},
        "script_exists": os.path.exists(script_path),
    }
    if not prob_dir:
        info["reason"] = "no matching directory"
        return info

    for suite in ("sample", "secret"):
        data_dir = os.path.join(prob_dir, "data", suite)
        if os.path.isdir(data_dir):
            suite_cases = [f"{suite}/{f[:-3]}" for f in os.listdir(data_dir) if f.endswith(".in")]
            info["cases"].extend(sorted(suite_cases))
            info[f"{suite}_count"] = len(suite_cases)
    info["data_count"] = len(info["cases"])

    config = read_probhub_config_from_dir(prob_dir)
    if config is not None:
        judge = config.get("judge") or {}
        validator = _normalized_config_path(judge.get("validator"))
        if validator and os.path.isfile(os.path.join(prob_dir, validator)):
            info["files"]["validator"] = validator

        solutions = config.get("solutions") or {}
        for config_key, display_key in (("accepted", "std"), ("brute", "brute"), ("wrong", "wrong")):
            for entry in _config_entries(solutions.get(config_key)):
                source = _normalized_config_path(entry)
                if source and os.path.isfile(os.path.join(prob_dir, source)):
                    info["files"][display_key].append(source)

        configured = set(sum((info["files"][key] for key in ("std", "brute", "wrong")), []))
        if info["files"]["validator"]:
            configured.add(info["files"]["validator"])
        for entry in _config_entries(config.get("generators")):
            source = _normalized_config_path(entry)
            if source and source not in configured and os.path.isfile(os.path.join(prob_dir, source)):
                info["files"]["other"].append(source)
    else:
        for file in sorted(os.listdir(prob_dir)):
            if not file.endswith(".cpp"):
                continue
            if file == "validator.cpp":
                info["files"]["validator"] = file
            elif file.startswith("std"):
                info["files"]["std"].append(file)
            elif file.startswith("brute"):
                info["files"]["brute"].append(file)
            elif file.startswith("wrong"):
                info["files"]["wrong"].append(file)
            else:
                info["files"]["other"].append(file)

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


def _run_sandbox_job(job_id, subtitle, index):
    try:
        info = _sandbox_problem_info(subtitle, index)
        result = _empty_sandbox_result(info)
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id]["result"] = result
            SANDBOX_JOBS[job_id]["logs"] = ""

        if not info.get("runnable"):
            message = info.get("reason") or "problem is not runnable"
            result["final"] = {"ok": False, "message": message}
            with SANDBOX_LOCK:
                SANDBOX_JOBS[job_id].update(status="failed", result=result, logs=message)
            return

        script_path = os.path.join("scripts", "local_judge.py")
        proc = subprocess.Popen(
            [sys.executable, script_path, info["dir"], "--jsonl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        logs = []
        assert proc.stdout is not None
        for line in proc.stdout:
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
                _apply_sandbox_event(result, event)
                logs.append(_sandbox_log_line(event))
            except json.JSONDecodeError:
                logs.append(raw)
            with SANDBOX_LOCK:
                SANDBOX_JOBS[job_id]["logs"] = "\n".join(logs)
                SANDBOX_JOBS[job_id]["result"] = result

        return_code = proc.wait()
        final = result.get("final") or {}
        final_ok = bool(final.get("ok")) and return_code == 0
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id]["status"] = "success" if final_ok else "failed"
            SANDBOX_JOBS[job_id]["logs"] = "\n".join(logs)
            SANDBOX_JOBS[job_id]["result"] = result
    except Exception as e:
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id].update(status="failed", logs=str(e), result={"final": {"ok": False, "message": str(e)}})


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
        job_id = uuid.uuid4().hex
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id] = {"status": "running", "logs": "", "result": None}
        Thread(target=_run_sandbox_job, args=(job_id, subtitle, index), daemon=True).start()
        return jsonify({"success": True, "job_id": job_id})
    except RequestEntityTooLarge:
        raise
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sandbox/job/<job_id>')
def sandbox_job(job_id):
    with SANDBOX_LOCK:
        job = SANDBOX_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        return jsonify({"success": True, **job})


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


def _prune_submission_jobs():
    now = time.time()
    expired = [
        job_id for job_id, job in SUBMISSION_JOBS.items()
        if job.get("status") not in {"queued", "running", "cancelling"}
        and now - float(job.get("finished_at") or job.get("created_at") or now) >= SUBMISSION_RESULT_TTL
    ]
    for job_id in expired:
        SUBMISSION_JOBS.pop(job_id, None)
    if len(SUBMISSION_JOBS) < 100:
        return
    finished = sorted(
        (
            (job_id, job)
            for job_id, job in SUBMISSION_JOBS.items()
            if job.get("status") not in {"queued", "running", "cancelling"}
        ),
        key=lambda item: item[1].get("created_at", 0),
    )
    for job_id, _ in finished[: max(1, len(SUBMISSION_JOBS) - 99)]:
        SUBMISSION_JOBS.pop(job_id, None)


def _force_cancel_submission(job_id, proc, known_pids):
    time.sleep(SUBMISSION_FORCE_CANCEL_AFTER)
    with SUBMISSION_LOCK:
        job = SUBMISSION_JOBS.get(job_id)
        still_cancelling = bool(job and job.get("status") in {"running", "cancelling"})
    if still_cancelling and proc.poll() is None:
        terminate_external_process_tree(proc, known_pids)


def _request_submission_cancel(job_id, proc, cancel_file):
    if cancel_file is not None:
        try:
            Path(cancel_file).touch(exist_ok=True)
        except OSError:
            pass
    if proc is None or proc.poll() is not None:
        return
    known_pids = snapshot_process_tree(proc.pid)
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass
    Thread(
        target=_force_cancel_submission,
        args=(job_id, proc, known_pids),
        daemon=True,
    ).start()


def _run_submission_job(job_id, subtitle, index, filename, source):
    with SUBMISSION_SLOTS:
        with SUBMISSION_LOCK:
            job = SUBMISSION_JOBS.get(job_id)
            if not job:
                return
            if job.get("cancel_requested"):
                job.update(status="cancelled", verdict="CANCELLED", finished_at=time.time())
                return
            job["status"] = "running"
        result = None
        logs = []
        return_code = None
        try:
            info = _sandbox_problem_info(subtitle, index)
            result = _empty_sandbox_result(info)
            result["submission"] = {
                "task_id": job_id,
                "filename": filename,
                "workspace_cleaned": False,
            }
            with SUBMISSION_LOCK:
                SUBMISSION_JOBS[job_id]["result"] = result

            if not info.get("runnable"):
                raise ValueError(info.get("reason") or "problem is not runnable")

            with SUBMISSION_LOCK:
                if SUBMISSION_JOBS[job_id].get("cancel_requested"):
                    result["submission"].update(workspace_cleaned=True, verdict="CANCELLED")
                    SUBMISSION_JOBS[job_id].update(
                        status="cancelled",
                        verdict="CANCELLED",
                        result=result,
                        finished_at=time.time(),
                    )
                    return

            with temporary_submission_workspace(Path.cwd(), info["dir"], job_id, filename, source) as prepared:
                cancel_file = prepared.root / "cancel.requested"
                with SUBMISSION_LOCK:
                    if SUBMISSION_JOBS[job_id].get("cancel_requested"):
                        raise RuntimeError("submission cancelled before judge startup")
                command = [
                    sys.executable,
                    str(LOCAL_JUDGE_SCRIPT),
                    str(prepared.problem_dir),
                    "--jsonl",
                    "--no-cache",
                    "--cancellable",
                ]
                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
                popen_options = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "cwd": str(Path.cwd()),
                    "env": env,
                }
                if os.name == "nt":
                    popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_options["start_new_session"] = True
                proc = subprocess.Popen(command, **popen_options)
                with SUBMISSION_LOCK:
                    SUBMISSION_PROCESSES[job_id] = proc
                    SUBMISSION_CANCEL_FILES[job_id] = cancel_file
                    cancel_requested = bool(SUBMISSION_JOBS[job_id].get("cancel_requested"))
                if cancel_requested:
                    _request_submission_cancel(job_id, proc, cancel_file)

                assert proc.stdout is not None
                try:
                    for line in proc.stdout:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                            _apply_sandbox_event(result, event)
                            logs.append(_sandbox_log_line(event))
                        except json.JSONDecodeError:
                            logs.append(raw)
                        with SUBMISSION_LOCK:
                            SUBMISSION_JOBS[job_id]["logs"] = "\n".join(logs)
                            SUBMISSION_JOBS[job_id]["result"] = result
                finally:
                    proc.stdout.close()
                return_code = proc.wait()

            result["submission"]["workspace_cleaned"] = True
            with SUBMISSION_LOCK:
                cancel_requested = bool(SUBMISSION_JOBS[job_id].get("cancel_requested"))
            cancelled = cancel_requested or (result.get("final") or {}).get("status") == "cancelled"
            verdict = "CANCELLED" if cancelled else _submission_verdict(result)
            result["submission"]["verdict"] = verdict
            with SUBMISSION_LOCK:
                SUBMISSION_JOBS[job_id].update(
                    status="cancelled" if cancelled else "completed",
                    verdict=verdict,
                    returncode=return_code,
                    logs="\n".join(logs),
                    result=result,
                    finished_at=time.time(),
                )
        except Exception as exc:
            with SUBMISSION_LOCK:
                cancel_requested = bool(SUBMISSION_JOBS.get(job_id, {}).get("cancel_requested"))
            status = "cancelled" if cancel_requested else "failed"
            verdict = "CANCELLED" if cancel_requested else "FAIL"
            if result is not None and result.get("submission"):
                result["submission"]["workspace_cleaned"] = True
                result["submission"]["verdict"] = verdict
            with SUBMISSION_LOCK:
                if job_id in SUBMISSION_JOBS:
                    SUBMISSION_JOBS[job_id].update(
                        status=status,
                        verdict=verdict,
                        logs=("\n".join(logs + ([] if cancel_requested else [str(exc)]))).strip(),
                        result=result or {"final": {"ok": False, "message": str(exc)}},
                        finished_at=time.time(),
                    )
        finally:
            with SUBMISSION_LOCK:
                SUBMISSION_PROCESSES.pop(job_id, None)
                SUBMISSION_CANCEL_FILES.pop(job_id, None)


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
                "created_at": time.time(),
                "cancel_requested": False,
            }
        Thread(
            target=_run_submission_job,
            args=(job_id, subtitle, index, filename, source),
            daemon=True,
        ).start()
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
    with SUBMISSION_LOCK:
        job = SUBMISSION_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        return jsonify({"success": True, **job})


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
        proc = SUBMISSION_PROCESSES.get(job_id)
        cancel_file = SUBMISSION_CANCEL_FILES.get(job_id)
    _request_submission_cancel(job_id, proc, cancel_file)
    return jsonify({"success": True, "status": "cancelling"})


def inject_pdf_to_zip(pdf_path, prob_dir):
    """将 problem.pdf 注入同名的 .zip 压缩包（如果存在）。"""
    import zipfile as zf
    base = os.path.basename(os.path.normpath(prob_dir))
    zip_path = os.path.join(base + ".zip")
    if not os.path.exists(zip_path):
        return None
    try:
        tmp = zip_path + ".tmp"
        with zf.ZipFile(zip_path, "r") as zin, zf.ZipFile(tmp, "w", compression=zf.ZIP_DEFLATED) as zout:
            infos = zin.infolist()
            arcname = _zip_preferred_arcname(infos, base, "problem.pdf")
            skip_names = _zip_candidate_names(base, "problem.pdf")
            for item in zin.infolist():
                if item.filename not in skip_names:
                    zout.writestr(item, zin.read(item.filename))
            zout.write(pdf_path, arcname)
        os.replace(tmp, zip_path)
        return "updated"
    except Exception as e:
        return f"error: {e}"


def distribute_problems(subtitle):
    """Run extract_new_problem.py for each problem in the current subtitle."""
    # Find the extract script (same dir as this ui.py or in skill scripts)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    extract_script = os.path.join(script_dir, "extract_new_problem.py")

    # If not in workspace, look in skill scripts
    if not os.path.exists(extract_script):
        skill_scripts = os.path.join(
            os.path.expanduser("~"), ".claude", "skills", "probhub", "scripts", "extract_new_problem.py"
        )
        if os.path.exists(skill_scripts):
            extract_script = skill_scripts
        else:
            return {"error": "extract_new_problem.py not found"}

    # Match problem names to directories
    name_to_dir = find_problem_dirs()

    # Read current problems.json to get active problems in order
    typst_subdir = os.path.join(BASE_DIR, subtitle)
    problems_json = os.path.join(typst_subdir, "problems.json")
    if not os.path.exists(problems_json):
        return {"error": "problems.json not found"}

    with open(problems_json, "r", encoding="utf-8") as f:
        problems = json.load(f)

    results = []
    for p in problems:
        display_name = p.get("problem", {}).get("display_name", "")
        if not display_name:
            results.append({"name": "?", "status": "skipped", "reason": "no display_name"})
            continue
        prob_dir = name_to_dir.get(display_name)
        if not prob_dir:
            results.append({"name": display_name, "status": "skipped", "reason": "no matching directory"})
            continue

        typst_dir = os.path.join(BASE_DIR, subtitle)
        try:
            subprocess.run(
                [sys.executable, extract_script, typst_dir, prob_dir],
                check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            # Inject problem.pdf into matching .zip if it exists
            pdf_path = os.path.join(prob_dir, "problem.pdf")
            zip_status = inject_pdf_to_zip(pdf_path, prob_dir) if os.path.exists(pdf_path) else None
            results.append({
                "name": display_name, "status": "ok", "dir": prob_dir,
                "zip": zip_status  # None=no zip, "updated"=done, "error:..."=fail
            })
        except subprocess.CalledProcessError as e:
            results.append({"name": display_name, "status": "failed", "reason": e.stderr[:200]})

    return results

# ── Contest Config (main.typ + lib.typ) ──────────────────────────

def _read_contest_config(subtitle):
    """Parse main.typ and lib.typ, return config dict."""
    main_path = secure_path(subtitle, "main.typ")
    lib_path = os.path.join(BASE_DIR, "lib.typ")
    config = {
        "title": "", "subtitle": subtitle, "author": "", "date": "",
        "logo": "usts.png", "logo_width": "9cm",
        "logo_space_above": "0em", "logo_space_below": "0em",
    }

    if os.path.exists(main_path):
        with open(main_path, "r", encoding="utf-8") as f:
            text = f.read()
        for key in ("title", "subtitle", "author", "date"):
            m = re.search(rf'{key}:\s*"([^"]*)"', text)
            if m:
                config[key] = m.group(1)

    if os.path.exists(lib_path):
        with open(lib_path, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'image\("([^"]+)"\s*(?:,\s*width:\s*([^,)]+))?', text)
        if m:
            config["logo"] = m.group(1)
            if m.group(2):
                config["logo_width"] = m.group(2).strip()
        # Parse space above/below logo
        # Look for v(Xem) on the line immediately before/after align(center, image(...))
        m = re.search(r'v\(([^)]+)\)\s*(?://[^\n]*)?\s*\n\s*(?://[^\n]*\n\s*)?align\(center,\s*image\(', text)
        if m:
            config["logo_space_above"] = m.group(1).strip()
        m = re.search(r'align\(center,\s*image\([^)]+\)\)\s*\n\s*v\(([^)]+)\)', text)
        if m:
            config["logo_space_below"] = m.group(1).strip()

    return config


def _write_contest_config(subtitle, config):
    """Write updated values back to main.typ and lib.typ."""
    main_path = secure_path(subtitle, "main.typ")
    lib_path = os.path.join(BASE_DIR, "lib.typ")

    if os.path.exists(main_path):
        with open(main_path, "r", encoding="utf-8") as f:
            text = f.read()
        for key in ("title", "subtitle", "author", "date"):
            if key in config:
                text = re.sub(rf'({key}:\s*)"[^"]*"', rf'\1"{config[key]}"', text)
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(text)

    if os.path.exists(lib_path):
        with open(lib_path, "r", encoding="utf-8") as f:
            text = f.read()
        logo = config.get("logo", "usts.png")
        width = config.get("logo_width", "9cm")
        space_above = config.get("logo_space_above", "0em")
        space_below = config.get("logo_space_below", "0em")
        # Update image path/width
        text = re.sub(r'image\("[^"]+"\s*(?:,\s*width:\s*[^,)]+)?', f'image("{logo}", width: {width}', text)
        # Update space above logo (v() before align(center, image())
        text = re.sub(
            r'v\([^)]+\)(\s*(?://[^\n]*)?\s*\n\s*(?://[^\n]*\n\s*)?align\(center,\s*image\()',
            f'v({space_above})\\1', text
        )
        # Update space below logo (v() after image line)
        text = re.sub(
            r'(align\(center,\s*image\([^)]+\)\)\s*\n\s*)v\([^)]+\)',
            f'\\1v({space_below})', text
        )
        with open(lib_path, "w", encoding="utf-8") as f:
            f.write(text)


@app.route('/api/config/<subtitle>', methods=['GET'])
def get_contest_config(subtitle):
    try:
        defaults = _read_contest_config(subtitle)
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, workspace = schema
            return jsonify({"success": True, "config": load_contest_config(root, workspace, defaults)})
        return jsonify({"success": True, "config": defaults})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/config/<subtitle>', methods=['POST'])
def save_contest_config(subtitle):
    try:
        config = request.json
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, _ = schema
            with workspace_build_lock(root):
                _, live_workspace = load_workspace(root)
                saved = save_schema_contest_config(root, live_workspace, config)
            return jsonify({"success": True, "config": saved})
        _write_contest_config(subtitle, config)
        return jsonify({"success": True})
    except RequestEntityTooLarge:
        raise
    except ProbHubError as e:
        status = 409 if e.code in {"source_conflict", "build_busy"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/pdf-pages/<subtitle>')
def pdf_page_count(subtitle):
    """Return the number of pages in main.pdf."""
    import pypdf
    preview_path = _preview_pdf_path(subtitle)
    pdf_path = str(preview_path if preview_path.is_file() else Path(secure_path(subtitle, "main.pdf")))
    if not os.path.exists(pdf_path):
        return jsonify({"pages": 0})
    try:
        reader = pypdf.PdfReader(pdf_path)
        return jsonify({"pages": len(reader.pages)})
    except Exception:
        return jsonify({"pages": 0})


@app.route('/api/pdf-page/<subtitle>/<int:page>')
def serve_pdf_page(subtitle, page):
    """Render a single PDF page through Poppler into the process temp cache."""
    from flask import send_file
    import pypdf

    preview_path = _preview_pdf_path(subtitle)
    pdf_path = str(preview_path if preview_path.is_file() else Path(secure_path(subtitle, "main.pdf")))
    if not os.path.exists(pdf_path):
        return "PDF not compiled", 404

    # Validate page number
    try:
        reader = pypdf.PdfReader(pdf_path)
        total = len(reader.pages)
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
    app.run(host="127.0.0.1", port=port, debug=False)

if __name__ == '__main__':
    run_server()
