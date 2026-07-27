import fnmatch
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import sys
import time

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(SCRIPT_DIR)
if PACKAGE_ROOT in sys.path:
    sys.path.remove(PACKAGE_ROOT)
sys.path.insert(0, PACKAGE_ROOT)

from probhub.output_compare import compare_standard_output, normalize_standard_output
from probhub.process_control import (
    DEFAULT_PROCESS_LIMIT,
    ProcessCancelled,
    cancellation_requested,
    process_alive as _process_alive,
    run_managed_to_files,
    spawn_managed,
    terminate_process as _terminate_process,
    wait_managed,
    windows_assign_job as _windows_assign_job,
)

REFERENCES_DIR = os.path.join(PACKAGE_ROOT, "references")
DEFAULT_TIME_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = 256
DEFAULT_OUTPUT_LIMIT = 64
PROTOCOL_NAME = "probhub.local_judge"
PROTOCOL_VERSION = 1
CACHE_SCHEMA_VERSION = 2
CACHE_FILENAME = "sandbox-cache-v1.json"
CACHE_LIMITS = {"validator": 4000, "case": 12000}
_COMPILER_IDENTITY = None
_FILE_DIGEST_CACHE = {}


def _cancel_signal_handler(_signum, _frame):
    raise ProcessCancelled("execution cancelled")


def install_cancellation_handlers():
    signal.signal(signal.SIGTERM, _cancel_signal_handler)
    if platform.system() == "Windows" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _cancel_signal_handler)


def _hash_parts(*parts):
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            payload = part
        else:
            payload = str(part).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _file_digest(path):
    path = os.path.abspath(path)
    stat = os.stat(path)
    cache_key = (path, stat.st_mtime_ns, stat.st_size)
    cached = _FILE_DIGEST_CACHE.get(cache_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if len(_FILE_DIGEST_CACHE) > 4096:
        _FILE_DIGEST_CACHE.clear()
    _FILE_DIGEST_CACHE[cache_key] = value
    return value


def compiler_identity():
    global _COMPILER_IDENTITY
    if _COMPILER_IDENTITY is not None:
        return _COMPILER_IDENTITY
    try:
        result = subprocess.run(
            ["g++", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        first_line = (result.stdout or result.stderr).splitlines()[0]
        _COMPILER_IDENTITY = first_line.strip()
    except Exception as exc:
        _COMPILER_IDENTITY = f"g++-unknown:{exc}"
    return _COMPILER_IDENTITY


def source_fingerprint(src_file, prob_dir, kind=None):
    src_file = os.path.abspath(src_file)
    prob_dir = os.path.abspath(prob_dir)
    dependencies = [src_file]
    source_text = ""
    try:
        with open(src_file, "r", encoding="utf-8", errors="replace") as stream:
            source_text = stream.read()
    except OSError:
        pass

    for root, _, files in os.walk(prob_dir):
        for name in files:
            if name.endswith((".h", ".hpp")):
                dependencies.append(os.path.join(root, name))
    if "testlib.h" in source_text:
        dependencies.append(os.path.join(REFERENCES_DIR, "testlib.h"))

    parts = [
        CACHE_SCHEMA_VERSION,
        platform.system(),
        platform.machine(),
        compiler_identity(),
        kind or "unknown",
        "-O2",
        "-std=c++17",
        "-static" if platform.system() == "Windows" else "dynamic",
        "-DFOR_LINUX" if platform.system() == "Windows" and kind == "validator" else "native-lines",
    ]
    for path in sorted(set(os.path.abspath(item) for item in dependencies)):
        if os.path.isfile(path):
            try:
                label = os.path.relpath(path, prob_dir).replace(os.sep, "/")
            except ValueError:
                label = path.replace(os.sep, "/")
            parts.extend((label, _file_digest(path)))
    return _hash_parts(*parts)


def validator_cache_key(program_fingerprint, in_file):
    return _hash_parts(
        CACHE_SCHEMA_VERSION,
        "validator",
        platform.system(),
        program_fingerprint,
        _file_digest(in_file),
    )


def testcase_cache_key(
    program_fingerprint,
    in_file,
    ans_file,
    time_limit,
    memory_limit,
    judge_type="standard",
    judge_fingerprint="",
    judge_options_fingerprint="",
):
    return _hash_parts(
        CACHE_SCHEMA_VERSION,
        "case",
        platform.system(),
        platform.machine(),
        program_fingerprint,
        judge_type,
        judge_fingerprint,
        judge_options_fingerprint,
        _file_digest(in_file),
        _file_digest(ans_file),
        f"{float(time_limit):.9g}",
        int(memory_limit),
    )


class SandboxCache:
    def __init__(self, prob_dir, enabled=True, read_enabled=None):
        self.enabled = enabled
        self.read_enabled = enabled if read_enabled is None else bool(read_enabled and enabled)
        self.path = os.path.join(prob_dir, ".probhub", CACHE_FILENAME)
        self.data = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "compile": {},
            "validator": {},
            "case": {},
        }
        self.dirty = False
        self.stats = {
            "enabled": enabled,
            "read_enabled": self.read_enabled,
            "mode": "normal" if self.read_enabled else ("refresh" if enabled else "disabled"),
            "compile_hits": 0,
            "compile_misses": 0,
            "validator_hits": 0,
            "validator_misses": 0,
            "case_hits": 0,
            "case_misses": 0,
        }
        if self.read_enabled:
            self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if loaded.get("schema_version") != CACHE_SCHEMA_VERSION:
                return
            for section in ("compile", "validator", "case"):
                if isinstance(loaded.get(section), dict):
                    self.data[section] = loaded[section]
        except (OSError, ValueError, TypeError):
            pass

    def get(self, section, key):
        if not self.enabled:
            return None
        if not self.read_enabled:
            self.stats[f"{section}_misses"] += 1
            return None
        value = self.data.get(section, {}).get(key)
        counter = f"{section}_hits" if value is not None else f"{section}_misses"
        self.stats[counter] += 1
        return value

    def set(self, section, key, value):
        if not self.enabled:
            return
        values = self.data.setdefault(section, {})
        values.pop(key, None)
        values[key] = value
        limit = CACHE_LIMITS.get(section)
        if limit and len(values) > limit:
            for old_key in list(values)[: len(values) - limit]:
                values.pop(old_key, None)
        self.dirty = True

    def delete(self, section, key):
        if not self.enabled:
            return
        if self.data.get(section, {}).pop(key, None) is not None:
            self.dirty = True

    def save(self):
        if not self.enabled or not self.dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, self.path)
        self.dirty = False


class Reporter:
    def __init__(self, jsonl=False):
        self.jsonl = jsonl

    def text(self, msg=""):
        if not self.jsonl:
            print(msg, flush=True)

    def event(self, type_, **payload):
        if self.jsonl:
            event = {
                "protocol": PROTOCOL_NAME,
                "protocol_version": PROTOCOL_VERSION,
                "type": type_,
                **payload,
            }
            print(json.dumps(event, ensure_ascii=False), flush=True)


def compile_cpp(
    src_file,
    out_bin,
    reporter=None,
    kind=None,
    display_file=None,
    cache=None,
    fingerprint=None,
):
    if not os.path.exists(src_file):
        return False
    reporter = reporter or Reporter(False)
    file_name = display_file or os.path.basename(src_file)
    fingerprint = fingerprint or source_fingerprint(src_file, os.path.dirname(src_file), kind)
    cache_key = str(file_name).replace(os.sep, "/")

    if cache and cache.enabled:
        cached = cache.get("compile", cache_key)
        binary_matches = False
        if cached and cached.get("fingerprint") == fingerprint and os.path.isfile(out_bin):
            try:
                binary_matches = cached.get("binary_digest") == _file_digest(out_bin)
            except OSError:
                binary_matches = False
        if binary_matches:
            reporter.text(f"[*] Compiling {file_name}... cached")
            reporter.event("compile", kind=kind or "unknown", file=file_name, ok=True, stderr="", cached=True)
            return True
        if cached is not None:
            cache.stats["compile_hits"] -= 1
            cache.stats["compile_misses"] += 1
            cache.delete("compile", cache_key)

    reporter.text(f"[*] Compiling {file_name}...")
    flags = ["g++", src_file, "-o", out_bin, "-O2", "-std=c++17", "-I", REFERENCES_DIR]
    if platform.system() == "Windows":
        flags.insert(4, "-static")
        if kind == "validator":
            # DOMjudge validators run on Linux and accept LF line endings. Emulate
            # that behavior so Git-normalized test data is validated consistently.
            flags.append("-DFOR_LINUX")
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-compile-") as temp:
            stdout_path = os.path.join(temp, "compiler.out")
            stderr_path = os.path.join(temp, "compiler.stderr")
            ret = run_managed_to_files(
                flags,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=60.0,
                memory_limit_mb=2048,
                output_limit_bytes=8 * 1024 * 1024,
                process_limit=DEFAULT_PROCESS_LIMIT,
            )
            try:
                stderr = open(stderr_path, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                stderr = ""
        ok = ret["reason"] == "completed" and ret["returncode"] == 0
        if ret["reason"] != "completed":
            stderr = (stderr + "\n" + (ret["message"] or ret["reason"])).strip()
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=ok,
            stderr=stderr,
            cached=False,
        )
        if not ok:
            if cache:
                cache.delete("compile", cache_key)
            try:
                os.remove(out_bin)
            except FileNotFoundError:
                pass
            reporter.text(f"[-] Compile failed:\n{stderr}")
            return False
        if cache:
            cache.set(
                "compile",
                cache_key,
                {"fingerprint": fingerprint, "binary_digest": _file_digest(out_bin)},
            )
        return True
    except Exception as e:
        if cache:
            cache.delete("compile", cache_key)
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=False,
            stderr=str(e),
            cached=False,
        )
        reporter.text(f"[-] Compile error: {e}")
        return False


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def read_probhub_config(prob_dir):
    config_path = os.path.join(prob_dir, "probhub.yaml")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read probhub.yaml: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("probhub.yaml must contain a mapping")
    return config


def _entry_file(entry):
    if isinstance(entry, dict):
        return entry.get("file")
    return entry


def _config_entries(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def resolve_problem_path(prob_dir, entry):
    relative = _entry_file(entry)
    if not relative:
        return None
    return os.path.normpath(os.path.join(prob_dir, str(relative)))


def display_problem_path(prob_dir, path):
    try:
        return os.path.relpath(path, prob_dir).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def binary_path_for_source(source_path):
    return os.path.splitext(source_path)[0] + ".exe"


def _expected_defaults(kind):
    if kind == "std":
        return {
            "status": ["AC"],
            "all": True,
            "forbid": ["WA", "TLE", "MLE", "OLE", "RE", "FAIL"],
        }
    if kind == "brute":
        return {
            "status": ["TLE", "MLE", "OLE"],
            "all": False,
            "forbid": ["WA", "FAIL"],
        }
    return {
        "status": ["WA", "TLE", "MLE", "OLE", "RE"],
        "all": False,
        "forbid": ["FAIL"],
    }


def _status_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).upper() for item in value]


def _string_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value]


def _normalize_expected(kind, entry):
    expected = _expected_defaults(kind)
    configured = entry.get("expected") if isinstance(entry, dict) else None
    if isinstance(configured, dict):
        expected.update(configured)
    expected["status"] = _status_list(expected.get("status"))
    expected["forbid"] = _status_list(expected.get("forbid"))
    expected["groups"] = _string_list(expected.get("groups"))
    expected["all"] = bool(expected.get("all"))
    return expected


def discover_solution_entries(prob_dir, config=None):
    solutions = {"std": [], "brute": [], "wrong": []}
    if config is not None:
        configured = config.get("solutions") or {}
        for config_key, kind in (("accepted", "std"), ("brute", "brute"), ("wrong", "wrong")):
            for entry in _config_entries(configured.get(config_key)):
                source_path = resolve_problem_path(prob_dir, entry)
                if source_path:
                    solutions[kind].append({
                        "path": source_path,
                        "expected": _normalize_expected(kind, entry),
                    })
        return solutions

    search_dir = os.path.join(prob_dir, "code")
    if not os.path.isdir(search_dir):
        search_dir = prob_dir
    for file_name in os.listdir(search_dir):
        if not file_name.endswith(".cpp") or file_name == "validator.cpp":
            continue
        for kind in solutions:
            if file_name.startswith(kind):
                solutions[kind].append({
                    "path": os.path.join(search_dir, file_name),
                    "expected": _normalize_expected(kind, None),
                })
                break
    return solutions


def discover_solution_sources(prob_dir, config=None):
    """Compatibility view used by existing callers/tests."""
    return {
        kind: [entry["path"] for entry in entries]
        for kind, entries in discover_solution_entries(prob_dir, config).items()
    }

def discover_validator_source(prob_dir, config=None):
    if config is not None:
        return resolve_problem_path(prob_dir, ((config.get("judge") or {}).get("validator")))
    code_validator = os.path.join(prob_dir, "code", "validator.cpp")
    if os.path.isfile(code_validator):
        return code_validator
    return os.path.join(prob_dir, "validator.cpp")


def configured_judge_type(config=None):
    if config is None:
        return "standard"
    value = str(((config.get("judge") or {}).get("type", "standard"))).strip().lower()
    return "custom" if value == "checker" else value


def discover_judge_source(prob_dir, config, key):
    if config is not None:
        return resolve_problem_path(prob_dir, ((config.get("judge") or {}).get(key)))
    candidate = os.path.join(prob_dir, "code", f"{key}.cpp")
    if os.path.isfile(candidate):
        return candidate
    return os.path.join(prob_dir, f"{key}.cpp")


def read_problem_limits(prob_dir, config=None):
    time_limit = DEFAULT_TIME_LIMIT
    memory_limit = DEFAULT_MEMORY_LIMIT
    has_meta_time_limit = False
    has_meta_memory_limit = False

    if config is not None:
        limits = config.get("limits") or {}
        if "time" in limits:
            time_limit = _safe_float(limits.get("time"), time_limit)
            has_meta_time_limit = True
        if "memory" in limits:
            memory_limit = _safe_int(limits.get("memory"), memory_limit)
            has_meta_memory_limit = True

    meta_path = os.path.join(prob_dir, "meta.json")
    if (not has_meta_time_limit or not has_meta_memory_limit) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            problem = meta.get("problem", {})
            if not has_meta_time_limit and "time_limit" in problem:
                time_limit = _safe_float(problem.get("time_limit"), time_limit)
                has_meta_time_limit = True
            if not has_meta_memory_limit and "memory_limit" in problem:
                memory_limit = _safe_int(problem.get("memory_limit"), memory_limit)
                has_meta_memory_limit = True
        except Exception:
            pass

    ini_path = os.path.join(prob_dir, "domjudge-problem.ini")
    if not has_meta_time_limit and os.path.exists(ini_path):
        try:
            with open(ini_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"timelimit\s*=\s*['\"]?([0-9.]+)", text)
            if m:
                time_limit = _safe_float(m.group(1), time_limit)
        except Exception:
            pass

    yaml_path = os.path.join(prob_dir, "problem.yaml")
    if not has_meta_memory_limit and os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"(?m)^\s*memory\s*:\s*([0-9]+)", text)
            if m:
                memory_limit = _safe_int(m.group(1), memory_limit)
        except Exception:
            pass

    return max(time_limit, 0.1), max(memory_limit, 1)


def read_problem_output_limit(prob_dir, config=None):
    limits = (config or {}).get("limits") or {}
    output_limit = _safe_int(limits.get("output"), DEFAULT_OUTPUT_LIMIT)
    process_limit = _safe_int(limits.get("processes"), DEFAULT_PROCESS_LIMIT)
    return max(output_limit, 1), max(process_limit, 1)


def _failed_status(returncode, stderr, memory_enforced, peak_memory_mb, memory_limit):
    """MLE requires peak-memory evidence; everything else is RE (matches stress)."""
    if (
        memory_enforced
        and peak_memory_mb is not None
        and peak_memory_mb >= memory_limit * 0.98
    ):
        return "MLE"
    return "RE"


def _remove_file_with_retries(path):
    for _ in range(20):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            return

def run_program_to_file(
    bin_path,
    in_file,
    output_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    """Run one contestant process with tree-wide time, memory and output controls."""
    stderr_path = output_file + ".stderr"
    try:
        result = run_managed_to_files(
            [bin_path],
            input_path=in_file,
            stdout_path=output_file,
            stderr_path=stderr_path,
            timeout=time_limit,
            memory_limit_mb=memory_limit,
            output_limit_bytes=int(output_limit * 1024 * 1024),
            process_limit=process_limit,
        )
        try:
            stderr = open(stderr_path, "r", encoding="utf-8", errors="replace").read().strip()
        except OSError:
            stderr = ""
        reason = result["reason"]
        if reason == "time_limit":
            return "TLE", result["time"], result["memory"], result["memory_enforced"], result["message"]
        if reason == "output_limit":
            return "OLE", result["time"], result["memory"], result["memory_enforced"], result["message"]
        if reason == "memory_limit":
            return "MLE", result["time"], result["memory"], result["memory_enforced"], result["message"]
        if reason == "process_limit":
            return "RE", result["time"], result["memory"], result["memory_enforced"], result["message"]
        if result["returncode"] != 0:
            status = _failed_status(
                result["returncode"], stderr, result["memory_enforced"], result["memory"], memory_limit
            )
            return status, result["time"], result["memory"], result["memory_enforced"], stderr
        return "AC", result["time"], result["memory"], result["memory_enforced"], ""
    except OSError as exc:
        return "RE", 0.0, None, False, str(exc)
    finally:
        _remove_file_with_retries(stderr_path)


def _temporary_output_path(bin_path):
    descriptor, path = tempfile.mkstemp(
        prefix=".probhub-output-", suffix=".out", dir=os.path.dirname(bin_path)
    )
    os.close(descriptor)
    return path


def run_standard_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    output_file = _temporary_output_path(bin_path)
    try:
        status, elapsed, memory, memory_enforced, message = run_program_to_file(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        if status != "AC":
            return status, elapsed, memory, memory_enforced, message
        with open(ans_file, "r", encoding="utf-8", errors="replace") as answer, open(
            output_file, "r", encoding="utf-8", errors="replace"
        ) as actual:
            matched, message = compare_standard_output(answer.read(), actual.read())
        status = "AC" if matched else "WA"
        return status, elapsed, memory, memory_enforced, message
    finally:
        for _ in range(20):
            try:
                os.remove(output_file)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05)


def _checker_result(returncode, message):
    message = (message or "").strip()
    if returncode in {0, 42}:
        return "AC", message
    if returncode in {1, 2, 43}:
        return "WA", message
    return "FAIL", message or f"checker exited with code {returncode}"


def _feedback_message(feedback_dir, fallback=""):
    for name in ("judgemessage.txt", "teammessage.txt"):
        path = os.path.join(feedback_dir, name)
        if os.path.isfile(path):
            try:
                message = open(path, "r", encoding="utf-8", errors="replace").read().strip()
                if message:
                    return message
            except OSError:
                pass
    return (fallback or "").strip()


def run_custom_testcase(
    bin_path,
    checker_bin,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    """Run a DOMjudge/testlib-style output validator.

    The validator receives <input> <answer> <feedback-dir> as argv and reads
    contestant output from stdin, matching DOMjudge's custom validation protocol.
    """
    output_file = _temporary_output_path(bin_path)
    feedback_dir = tempfile.mkdtemp(prefix=".probhub-feedback-", dir=os.path.dirname(bin_path))
    try:
        status, elapsed, memory, memory_enforced, message = run_program_to_file(
            bin_path, in_file, output_file, time_limit, memory_limit, output_limit, process_limit
        )
        if status != "AC":
            return status, elapsed, memory, memory_enforced, message
        checker_stdout = output_file + ".checker.out"
        checker_stderr = output_file + ".checker.stderr"
        try:
            checker = run_managed_to_files(
                [checker_bin, in_file, ans_file, feedback_dir],
                input_path=output_file,
                stdout_path=checker_stdout,
                stderr_path=checker_stderr,
                timeout=max(5.0, float(time_limit)),
                memory_limit_mb=memory_limit,
                output_limit_bytes=min(int(output_limit * 1024 * 1024), 8 * 1024 * 1024),
                process_limit=process_limit,
            )
            try:
                stderr = open(checker_stderr, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                stderr = ""
            checker_message = _feedback_message(feedback_dir, stderr)
            if checker["reason"] != "completed":
                detail = checker["message"] or checker["reason"].replace("_", " ")
                return "FAIL", elapsed, memory, memory_enforced, f"checker {detail}"
            checker_status, checker_message = _checker_result(
                checker["returncode"], checker_message
            )
            return checker_status, elapsed, memory, memory_enforced, checker_message
        except OSError as exc:
            return "FAIL", elapsed, memory, memory_enforced, f"checker failed to start: {exc}"
        finally:
            for file_name in (checker_stdout, checker_stderr):
                _remove_file_with_retries(file_name)
    finally:
        _remove_file_with_retries(output_file)
        shutil.rmtree(feedback_dir, ignore_errors=True)

def _pump_interactive_stream(source, destination, direction, transcript, activity, traffic):
    """Forward bytes while recording a bounded transcript and last activity time."""
    try:
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            activity["last"] = time.monotonic()
            traffic[direction] = traffic.get(direction, 0) + len(chunk)
            if traffic[direction] > traffic["limit"]:
                traffic["exceeded"] = direction
                break
            if transcript["bytes"] < transcript["limit"]:
                remaining = transcript["limit"] - transcript["bytes"]
                saved = chunk[:remaining]
                transcript["entries"].append({
                    "direction": direction,
                    "data": saved.decode("utf-8", errors="replace"),
                })
                transcript["bytes"] += len(saved)
                if len(saved) < len(chunk):
                    transcript["truncated"] = True
            else:
                transcript["truncated"] = True
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            destination.close()
        except (OSError, ValueError):
            pass
        try:
            source.close()
        except (OSError, ValueError):
            pass


def _interactive_result(status, elapsed, memory, memory_enforced, message, transcript, timeout_kind=None):
    details = {
        "transcript": transcript["entries"],
        "transcript_truncated": transcript["truncated"],
    }
    if timeout_kind:
        details["timeout_kind"] = timeout_kind
    return status, elapsed, memory, memory_enforced, message, details


def _interactive_output_status(traffic, solution_stderr, interactor_stderr):
    """Classify combined protocol/stderr output after every lifecycle edge."""
    try:
        solution_stderr_size = os.path.getsize(solution_stderr)
    except OSError:
        solution_stderr_size = 0
    try:
        interactor_stderr_size = os.path.getsize(interactor_stderr)
    except OSError:
        interactor_stderr_size = 0

    limit = int(traffic["limit"])
    interactor_bytes = traffic.get("interactor_to_solution", 0) + interactor_stderr_size
    solution_bytes = traffic.get("solution_to_interactor", 0) + solution_stderr_size
    if interactor_bytes > limit:
        return "FAIL", "interactor output limit exceeded"
    if solution_bytes > limit:
        return "OLE", "interactive output limit exceeded"
    return None, ""


def _remove_interactive_temp(path):
    """Best-effort cleanup for Windows handles that may close asynchronously."""
    if not path:
        return
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            return


def run_interactive_testcase(
    bin_path,
    interactor_bin,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    idle_limit=None,
    transcript_limit=65536,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    """Connect contestant and interactor with transcript, idle and total deadlines."""
    start = time.time()
    monotonic_start = time.monotonic()
    idle_limit = max(float(idle_limit if idle_limit is not None else min(time_limit, 2.0)), 0.1)
    transcript = {"entries": [], "bytes": 0, "limit": max(int(transcript_limit), 0), "truncated": False}
    activity = {"last": monotonic_start}
    traffic = {"limit": int(output_limit * 1024 * 1024), "exceeded": None}
    last_resource_sample = monotonic_start
    solution_managed = None
    interactor_managed = None
    memory_enforced = False
    peak_memory_mb = None
    threads = []
    runtime_dir = tempfile.mkdtemp(prefix=".probhub-interactive-", dir=os.path.dirname(bin_path))
    solution_stderr = os.path.join(runtime_dir, "solution.stderr")
    interactor_stderr = os.path.join(runtime_dir, "interactor.stderr")
    feedback_dir = os.path.join(runtime_dir, "feedback")
    os.mkdir(feedback_dir)
    try:
        with open(solution_stderr, "wb") as solution_err, open(interactor_stderr, "wb") as interactor_err:
            solution_managed = spawn_managed(
                [bin_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=solution_err,
                bufsize=0,
                memory_limit_mb=memory_limit,
                process_limit=process_limit,
            )
            memory_enforced = solution_managed.memory_enforced
            solution = solution_managed.proc
            interactor_managed = spawn_managed(
                [interactor_bin, in_file, ans_file, feedback_dir],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=interactor_err,
                bufsize=0,
                memory_limit_mb=memory_limit,
                process_limit=process_limit,
            )
            interactor = interactor_managed.proc

            threads = [
                threading.Thread(
                    target=_pump_interactive_stream,
                    args=(solution.stdout, interactor.stdin, "solution_to_interactor", transcript, activity, traffic),
                    daemon=True,
                ),
                threading.Thread(
                    target=_pump_interactive_stream,
                    args=(interactor.stdout, solution.stdin, "interactor_to_solution", transcript, activity, traffic),
                    daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()
            # Process creation and Windows Job setup are infrastructure time, not
            # protocol idleness. Start the idle clock once both pumps can observe
            # traffic so a tight idle limit does not erase the first transcript.
            activity["last"] = time.monotonic()

            deadline = monotonic_start + float(time_limit)
            while solution.poll() is None or interactor.poll() is None:
                if cancellation_requested():
                    raise ProcessCancelled("execution cancelled")
                now = time.monotonic()
                resource_status = None
                resource_message = ""
                resource_status, resource_message = _interactive_output_status(
                    traffic, solution_stderr, interactor_stderr
                )
                if now - last_resource_sample >= 0.05:
                    last_resource_sample = now
                    solution_count, solution_memory = solution_managed.sample()
                    peak_memory_mb = solution_managed.peak_memory_mb
                    if solution_memory is not None and solution_memory >= memory_limit:
                        resource_status, resource_message = "MLE", "memory limit exceeded"
                    elif solution_count is not None and solution_count > process_limit:
                        resource_status, resource_message = "RE", "process limit exceeded"
                    interactor_count, interactor_memory = interactor_managed.sample()
                    if interactor_memory is not None and interactor_memory >= memory_limit:
                        resource_status, resource_message = "FAIL", "interactor memory limit exceeded"
                    elif interactor_count is not None and interactor_count > process_limit:
                        resource_status, resource_message = "FAIL", "interactor process limit exceeded"
                    elif interactor.poll() is not None:
                        interactor_status, interactor_message = _checker_result(
                            interactor.returncode, ""
                        )
                        if interactor_status == "FAIL":
                            resource_status = "FAIL"
                            resource_message = interactor_message
                if resource_status:
                    solution_managed.terminate()
                    solution_managed = None
                    interactor_managed.terminate()
                    interactor_managed = None
                    for thread in threads:
                        thread.join(timeout=1)
                    return _interactive_result(
                        resource_status,
                        time.time() - start,
                        peak_memory_mb,
                        memory_enforced,
                        resource_message,
                        transcript,
                    )
                timeout_kind = None
                if now >= deadline:
                    timeout_kind = "total"
                elif now - activity["last"] >= idle_limit:
                    timeout_kind = "idle"
                if timeout_kind:
                    solution_managed.sample()
                    peak_memory_mb = solution_managed.peak_memory_mb
                    solution_managed.terminate()
                    solution_managed = None
                    interactor_managed.terminate()
                    interactor_managed = None
                    for thread in threads:
                        thread.join(timeout=1)
                    message = (
                        "interactive idle timeout exceeded"
                        if timeout_kind == "idle"
                        else "interactive time limit exceeded"
                    )
                    elapsed = min(time.time() - start, float(time_limit))
                    return _interactive_result(
                        "TLE",
                        elapsed,
                        peak_memory_mb,
                        memory_enforced,
                        message,
                        transcript,
                        timeout_kind=timeout_kind,
                    )
                time.sleep(0.005)

            for thread in threads:
                thread.join(timeout=1)

            # Both direct processes may exit before the monitor observes a final
            # output burst. Sample and classify once more while Job telemetry and
            # stderr files are still available.
            solution_managed.sample()
            peak_memory_mb = solution_managed.peak_memory_mb
            interactor_managed.sample()
            resource_status, resource_message = _interactive_output_status(
                traffic, solution_stderr, interactor_stderr
            )
            interactor_peak_memory = interactor_managed.peak_memory_mb
            if (
                interactor.returncode != 0
                and interactor_managed.memory_enforced
                and interactor_peak_memory is not None
                and interactor_peak_memory >= memory_limit * 0.98
            ):
                resource_status = "FAIL"
                resource_message = "interactor memory limit exceeded"

        elapsed = time.time() - start
        solution_message = ""
        interactor_message = ""
        try:
            solution_message = open(solution_stderr, "r", encoding="utf-8", errors="replace").read().strip()
        except OSError:
            pass
        try:
            interactor_message = open(interactor_stderr, "r", encoding="utf-8", errors="replace").read().strip()
        except OSError:
            pass
        interactor_message = _feedback_message(feedback_dir, interactor_message)

        if resource_status:
            return _interactive_result(
                resource_status,
                elapsed,
                peak_memory_mb,
                memory_enforced,
                resource_message,
                transcript,
            )

        if solution.returncode != 0:
            status = _failed_status(
                solution.returncode,
                solution_message,
                memory_enforced,
                peak_memory_mb,
                memory_limit,
            )
            return _interactive_result(
                status, elapsed, peak_memory_mb, memory_enforced, solution_message, transcript
            )
        interactor_status, message = _checker_result(interactor.returncode, interactor_message)
        return _interactive_result(
            interactor_status, elapsed, peak_memory_mb, memory_enforced, message, transcript
        )
    except OSError as exc:
        return _interactive_result(
            "FAIL",
            time.time() - start,
            peak_memory_mb,
            memory_enforced,
            str(exc),
            transcript,
        )
    finally:
        if solution_managed is not None:
            solution_managed.terminate()
        if interactor_managed is not None:
            interactor_managed.terminate()
        for thread in threads:
            thread.join(timeout=1)
        _remove_interactive_temp(runtime_dir)

def run_testcase(
    bin_path,
    in_file,
    ans_file,
    time_limit=DEFAULT_TIME_LIMIT,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    output_limit=DEFAULT_OUTPUT_LIMIT,
    process_limit=DEFAULT_PROCESS_LIMIT,
    judge_type="standard",
    judge_bin=None,
    idle_limit=None,
    transcript_limit=65536,
):
    if judge_type == "custom":
        status, elapsed, memory, memory_enforced, message = run_custom_testcase(
            bin_path, judge_bin, in_file, ans_file, time_limit, memory_limit, output_limit, process_limit
        )
        return status, elapsed, memory, memory_enforced, message, {}
    if judge_type == "interactive":
        return run_interactive_testcase(
            bin_path,
            judge_bin,
            in_file,
            ans_file,
            time_limit,
            memory_limit,
            idle_limit=idle_limit,
            transcript_limit=transcript_limit,
            output_limit=output_limit,
            process_limit=process_limit,
        )
    status, elapsed, memory, memory_enforced, message = run_standard_testcase(
        bin_path, in_file, ans_file, time_limit, memory_limit, output_limit, process_limit
    )
    return status, elapsed, memory, memory_enforced, message, {}

def run_validator(val_bin, in_file, timeout=5.0):
    """Validate LF-normalized input under the shared process-tree controller."""
    try:
        with open(in_file, "rb") as fin:
            data = fin.read().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory(prefix="probhub-validator-") as temp:
            stdout_path = os.path.join(temp, "validator.out")
            stderr_path = os.path.join(temp, "validator.stderr")
            result = run_managed_to_files(
                [val_bin],
                input_data=data,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=timeout,
                memory_limit_mb=512,
                output_limit_bytes=8 * 1024 * 1024,
                process_limit=DEFAULT_PROCESS_LIMIT,
            )
            try:
                stderr = open(stderr_path, "r", encoding="utf-8", errors="replace").read().strip()
            except OSError:
                stderr = ""
            if result["reason"] != "completed":
                return False, result["message"] or result["reason"].replace("_", " ")
            return result["returncode"] == 0, stderr
    except OSError as exc:
        return False, str(exc)


def _data_groups(config=None):
    result = []
    raw_groups = (((config or {}).get("data") or {}).get("groups") or [])
    if not isinstance(raw_groups, list):
        return result
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        patterns = _string_list(raw.get("patterns") or raw.get("cases") or [])
        targets = [item.replace("\\", "/") for item in _string_list(raw.get("targets") or [])]
        result.append({
            "name": name,
            "role": raw.get("role"),
            "patterns": patterns,
            "targets": targets,
        })
    return result


def _testcase_groups(case_name, suite, base_name, groups):
    matched = []
    for group in groups:
        patterns = group["patterns"] or [f"{group['name']}*", f"*/{group['name']}*"]
        values = (case_name, base_name, f"{suite}/{base_name}")
        if any(any(fnmatch.fnmatch(value, pattern) for value in values) for pattern in patterns):
            matched.append(group["name"])
    return matched


def collect_testcases(prob_dir, config=None):
    testcases = []
    data_config = (config or {}).get("data") or {}
    groups = _data_groups(config)
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        data_dir = os.path.join(prob_dir, data_config.get(f"{suite}_dir", default))
        if not os.path.isdir(data_dir):
            continue
        for in_name in sorted([f for f in os.listdir(data_dir) if f.endswith(".in")]):
            base_name = in_name[:-3]
            case_name = f"{suite}/{base_name}"
            testcases.append({
                "suite": suite,
                "name": base_name,
                "case": case_name,
                "groups": _testcase_groups(case_name, suite, base_name, groups),
                "in_path": os.path.join(data_dir, in_name),
                "ans_path": os.path.join(data_dir, f"{base_name}.ans"),
            })
    return testcases


def _targeted_group_names(groups, program_name):
    program_name = program_name.replace("\\", "/")
    return [
        group["name"]
        for group in groups
        if program_name in group["targets"]
    ]


def _expectation_cases(kind, testcases, expected, group_definitions, program_name):
    # Accepted solutions remain responsible for the complete data set. Targeted
    # groups are intended to express where brute/wrong solutions must be killed.
    if kind == "std":
        return list(testcases), []
    groups = list(expected.get("groups") or [])
    if not groups:
        groups = _targeted_group_names(group_definitions, program_name)
    if groups:
        selected = [case for case in testcases if set(case.get("groups") or []) & set(groups)]
        return selected, groups
    return list(testcases), []


def _result_snapshot(item):
    if item is None:
        return None
    return {
        "case": item.get("case"),
        "groups": list(item.get("groups") or []),
        "status": item.get("status"),
        "message": item.get("message", ""),
    }


def evaluate_expectation(kind, program_name, expected, case_results, testcases, group_definitions):
    selected_cases, groups = _expectation_cases(
        kind, testcases, expected, group_definitions, program_name
    )
    selected_names = {case["case"] for case in selected_cases}
    selected_results = [item for item in case_results if item["case"] in selected_names]
    statuses = set(expected.get("status") or [])
    forbidden_statuses = set(expected.get("forbid") or [])
    all_required = bool(expected.get("all"))
    matched = [item for item in selected_results if item["status"] in statuses]
    forbidden = [item for item in case_results if item["status"] in forbidden_statuses]
    status_ok = bool(selected_results) and (
        len(matched) == len(selected_results) if all_required else bool(matched)
    )
    first_non_ac = next((item for item in case_results if item["status"] != "AC"), None)
    first_expected_match = matched[0] if matched else None
    first_forbidden = forbidden[0] if forbidden else None
    return {
        "kind": kind,
        "program": program_name,
        "expected_statuses": sorted(statuses),
        "forbidden_statuses": sorted(forbidden_statuses),
        "groups": groups,
        "all": all_required,
        "ok": status_ok and not forbidden,
        "matched_cases": [item["case"] for item in matched],
        "selected_cases": [item["case"] for item in selected_results],
        "forbidden_cases": [item["case"] for item in forbidden],
        "first_non_ac": _result_snapshot(first_non_ac),
        "first_expected_match": _result_snapshot(first_expected_match),
        "first_forbidden": _result_snapshot(first_forbidden),
    }

def finish(reporter, ok, message, exit_code, result_code=None):
    cache = getattr(reporter, "cache", None)
    if cache is not None:
        cache.save()
        reporter.event("cache", **cache.stats)
        reporter.text(
            "Cache: "
            f"compile {cache.stats['compile_hits']} hit/{cache.stats['compile_misses']} miss, "
            f"validator {cache.stats['validator_hits']} hit/{cache.stats['validator_misses']} miss, "
            f"case {cache.stats['case_hits']} hit/{cache.stats['case_misses']} miss"
        )
    result_code = result_code or ("all_expectations_met" if ok else "validation_failed")
    reporter.event(
        "final",
        ok=ok,
        status="passed" if ok else "failed",
        code=result_code,
        exit_code=exit_code,
        message=message,
    )
    reporter.text(message)
    sys.exit(exit_code)


def main():
    jsonl = "--jsonl" in sys.argv
    no_cache = "--no-cache" in sys.argv
    cancellable = "--cancellable" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg not in {"--jsonl", "--no-cache", "--cancellable"}]
    reporter = Reporter(jsonl)
    if cancellable:
        install_cancellation_handlers()

    if len(args) != 1:
        reporter.text("Usage: python scripts/local_judge.py <problem_dir> [--jsonl] [--no-cache] [--cancellable]")
        finish(reporter, False, "Invalid arguments", 1)

    prob_dir = args[0]
    cache = SandboxCache(prob_dir, enabled=True, read_enabled=not no_cache)
    reporter.cache = cache
    try:
        config = read_probhub_config(prob_dir)
    except ValueError as exc:
        finish(reporter, False, str(exc), 1)

    data_config = (config or {}).get("data") or {}
    sample_dir = os.path.join(prob_dir, data_config.get("sample_dir", "data/sample"))
    secret_dir = os.path.join(prob_dir, data_config.get("secret_dir", "data/secret"))
    if not os.path.isdir(sample_dir) and not os.path.isdir(secret_dir):
        finish(reporter, False, "configured sample and secret data directories not found", 1)

    time_limit, memory_limit = read_problem_limits(prob_dir, config)
    output_limit, process_limit = read_problem_output_limit(prob_dir, config)
    testcases = collect_testcases(prob_dir, config)
    if not testcases:
        finish(reporter, False, "No .in test cases found under data/sample or data/secret", 1)

    reporter.text("\n" + "=" * 50)
    reporter.text("ProbHub Validation Sandbox")
    reporter.text(
        f"Limits: {time_limit:g}s / {memory_limit}MB / {output_limit}MB output / "
        f"{process_limit} processes"
    )
    reporter.text(
        f"Cases: {sum(1 for t in testcases if t['suite'] == 'sample')} sample / "
        f"{sum(1 for t in testcases if t['suite'] == 'secret')} secret"
    )
    reporter.text("=" * 50)
    judge_type = configured_judge_type(config)
    judge_config = (config or {}).get("judge") or {}
    interactive_config = judge_config.get("interactive") or {}
    idle_limit = _safe_float(interactive_config.get("idle_limit"), min(time_limit, 2.0))
    idle_limit = max(idle_limit, 0.1)
    transcript_limit = max(_safe_int(interactive_config.get("transcript_limit"), 65536), 0)
    judge_options_fingerprint = _hash_parts(
        judge_type,
        f"{idle_limit:.9g}",
        transcript_limit,
        output_limit,
        process_limit,
    )
    reporter.event(
        "limits",
        time_limit=time_limit,
        memory_limit=memory_limit,
        output_limit=output_limit,
        process_limit=process_limit,
        judge_type=judge_type,
        idle_limit=idle_limit if judge_type == "interactive" else None,
        transcript_limit=transcript_limit if judge_type == "interactive" else None,
    )

    # 1. Compile and run Validator
    validator_entry = ((config or {}).get("judge") or {}).get("validator") if config is not None else None
    val_cpp = discover_validator_source(prob_dir, config)
    if val_cpp and os.path.exists(val_cpp):
        val_bin = binary_path_for_source(val_cpp)
        val_name = display_problem_path(prob_dir, val_cpp)
        val_fingerprint = source_fingerprint(val_cpp, prob_dir, "validator")
        if compile_cpp(
            val_cpp,
            val_bin,
            reporter,
            "validator",
            val_name,
            cache=cache,
            fingerprint=val_fingerprint,
        ):
            reporter.text("\nRunning validator...")
            all_ok = True
            for testcase in testcases:
                key = validator_cache_key(val_fingerprint, testcase["in_path"])
                cached_result = cache.get("validator", key)
                if cached_result is not None:
                    ok = bool(cached_result.get("ok"))
                    validator_message = cached_result.get("message", "")
                    cached = True
                else:
                    ok, validator_message = run_validator(val_bin, testcase["in_path"])
                    cache.set("validator", key, {"ok": ok, "message": validator_message})
                    cached = False
                reporter.event(
                    "validator",
                    case=testcase["case"],
                    ok=ok,
                    message=validator_message,
                    cached=cached,
                )
                if not ok:
                    all_ok = False
                    detail = f" ({validator_message})" if validator_message else ""
                    reporter.text(
                        f"[-] FATAL: {testcase['case']} violates validator constraints{detail}! "
                        "Fix the data generator."
                    )
                    finish(
                        reporter,
                        False,
                        f"{testcase['case']} violates validator constraints{detail}",
                        1,
                    )
            if all_ok:
                reporter.text("[+] Validator: ALL PASS")
    elif jsonl and (validator_entry or val_cpp):
        validator_name = _entry_file(validator_entry) or display_problem_path(prob_dir, val_cpp)
        reporter.event(
            "compile",
            kind="validator",
            file=str(validator_name).replace(os.sep, "/"),
            ok=None,
            stderr=f"{validator_name} not found",
        )

    if judge_type not in {"standard", "custom", "interactive"}:
        finish(reporter, False, f"unsupported judge.type: {judge_type}", 1, "invalid_judge_type")

    judge_bin = None
    judge_fingerprint = "standard-v2-ignore-line-trailing-whitespace"
    if judge_type in {"custom", "interactive"}:
        source_key = "checker" if judge_type == "custom" else "interactor"
        judge_source = discover_judge_source(prob_dir, config, source_key)
        judge_name = display_problem_path(prob_dir, judge_source) if judge_source else source_key
        if not judge_source or not os.path.isfile(judge_source):
            finish(reporter, False, f"{source_key} not found: {judge_name}", 1, f"{source_key}_missing")
        judge_bin = binary_path_for_source(judge_source)
        judge_fingerprint = source_fingerprint(judge_source, prob_dir, source_key)
        if not compile_cpp(
            judge_source,
            judge_bin,
            reporter,
            source_key,
            judge_name,
            cache=cache,
            fingerprint=judge_fingerprint,
        ):
            finish(reporter, False, f"{source_key} failed to compile", 1, f"{source_key}_compile_failed")

    # 2. Discover and compile all solutions
    source_groups = discover_solution_entries(prob_dir, config)
    group_definitions = _data_groups(config)
    reporter.event("groups", groups=group_definitions)
    solutions = {"std": [], "brute": [], "wrong": []}
    for kind, source_entries in source_groups.items():
        for source_entry in source_entries:
            source_path = source_entry["path"]
            expected = source_entry["expected"]
            program_name = display_problem_path(prob_dir, source_path)
            if not os.path.isfile(source_path):
                reporter.event("compile", kind=kind, file=program_name, ok=False, stderr="source file not found")
                reporter.text(f"[-] Source file not found: {program_name}")
                continue
            bin_name = binary_path_for_source(source_path)
            fingerprint = source_fingerprint(source_path, prob_dir, kind)
            if compile_cpp(
                source_path,
                bin_name,
                reporter,
                kind,
                program_name,
                cache=cache,
                fingerprint=fingerprint,
            ):
                solutions[kind].append((program_name, bin_name, fingerprint, expected))

    if not solutions["std"]:
        finish(reporter, False, "accepted solution not found or failed to compile. Aborting.", 1)

    # 3. Evaluate all solutions
    for kind, progs in solutions.items():
        for prog_name, bin_path, fingerprint, expected in progs:
            reporter.text(f"\nEvaluating: {prog_name}")
            stats = {"AC": 0, "WA": 0, "TLE": 0, "RE": 0, "MLE": 0, "OLE": 0, "FAIL": 0}
            case_results = []

            for testcase in testcases:
                if not os.path.exists(testcase["ans_path"]):
                    finish(reporter, False, f"{testcase['case']}.ans not found", 1)

                key = testcase_cache_key(
                    fingerprint,
                    testcase["in_path"],
                    testcase["ans_path"],
                    time_limit,
                    memory_limit,
                    judge_type=judge_type,
                    judge_fingerprint=judge_fingerprint,
                    judge_options_fingerprint=judge_options_fingerprint,
                )
                cached_result = cache.get("case", key)
                if cached_result is not None:
                    status = cached_result["status"]
                    t = float(cached_result.get("time") or 0)
                    memory = cached_result.get("memory")
                    memory_enforced = bool(cached_result.get("memory_enforced"))
                    judge_message = cached_result.get("message", "")
                    judge_details = cached_result.get("details", {})
                    cached = True
                else:
                    status, t, memory, memory_enforced, judge_message, judge_details = run_testcase(
                        bin_path,
                        testcase["in_path"],
                        testcase["ans_path"],
                        time_limit,
                        memory_limit,
                        output_limit,
                        process_limit,
                        judge_type=judge_type,
                        judge_bin=judge_bin,
                        idle_limit=idle_limit,
                        transcript_limit=transcript_limit,
                    )
                    cache.set(
                        "case",
                        key,
                        {
                            "status": status,
                            "time": round(t, 6),
                            "memory": memory,
                            "memory_enforced": memory_enforced,
                            "message": judge_message,
                            "details": judge_details,
                        },
                    )
                    cached = False
                stats[status] += 1
                reporter.event(
                    "case",
                    kind=kind,
                    program=prog_name,
                    case=testcase["case"],
                    groups=testcase.get("groups", []),
                    status=status,
                    time=round(t, 6),
                    time_limit=time_limit,
                    memory=memory,
                    memory_limit=memory_limit,
                    memory_enforced=memory_enforced,
                    judge_type=judge_type,
                    message=judge_message,
                    timeout_kind=judge_details.get("timeout_kind"),
                    transcript_truncated=judge_details.get("transcript_truncated", False),
                    cached=cached,
                )
                case_results.append({
                    "case": testcase["case"],
                    "groups": testcase.get("groups", []),
                    "status": status,
                    "message": judge_message,
                })
                if judge_type == "interactive" and judge_details.get("transcript"):
                    reporter.event(
                        "transcript",
                        kind=kind,
                        program=prog_name,
                        case=testcase["case"],
                        entries=judge_details.get("transcript", []),
                        truncated=judge_details.get("transcript_truncated", False),
                        cached=cached,
                    )
                if status != "AC" or kind == "std":
                    suffix = " [cached]" if cached else ""
                    detail = f" - {judge_message}" if judge_message else ""
                    reporter.text(f"  - {testcase['case'].ljust(16)} : {status} ({t:.3f}s){suffix}{detail}")
                if status == "FAIL":
                    finish(
                        reporter,
                        False,
                        f"{judge_type} judge failed on {testcase['case']}: {judge_message or 'unknown failure'}",
                        1,
                        "judge_failed",
                    )

            expectation = evaluate_expectation(
                kind,
                prog_name,
                expected,
                case_results,
                testcases,
                group_definitions,
            )
            reporter.event(
                "summary",
                kind=kind,
                program=prog_name,
                stats=stats,
                expectation=expectation,
            )
            reporter.event("expectation", **expectation)
            reporter.text(f"  Summary: {stats}")
            reporter.text(
                "  Expectation: "
                f"{'PASS' if expectation['ok'] else 'FAIL'} "
                f"statuses={expectation['expected_statuses']} "
                f"forbid={expectation['forbidden_statuses']} "
                f"groups={expectation['groups'] or ['all']}"
            )

            if not expectation["selected_cases"]:
                finish(
                    reporter,
                    False,
                    f"expectation for {prog_name} selected no test cases",
                    1,
                    "expectation_has_no_cases",
                )
            if not expectation["ok"]:
                failure = expectation.get("first_forbidden") or expectation.get("first_non_ac")
                detail = f"; first relevant case: {failure}" if failure else ""
                finish(
                    reporter,
                    False,
                    f"expectation not met for {prog_name}{detail}",
                    1,
                    "expectation_not_met",
                )

    reporter.text("\n" + "=" * 50)
    finish(reporter, True, "[+] 恭喜！所有代码均符合预期宿命", 0)


if __name__ == "__main__":
    try:
        main()
    except ProcessCancelled:
        reporter = Reporter("--jsonl" in sys.argv)
        reporter.event(
            "final",
            ok=False,
            status="cancelled",
            code="cancelled",
            exit_code=130,
            message="Submission judging cancelled",
        )
        reporter.text("Submission judging cancelled")
        sys.exit(130)
