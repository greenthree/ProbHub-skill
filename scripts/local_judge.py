import json
import os
import platform
import re
import subprocess
import sys
import time

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REFERENCES_DIR = os.path.join(SCRIPT_DIR, "..", "references")
DEFAULT_TIME_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = 256
PROTOCOL_NAME = "probhub.local_judge"
PROTOCOL_VERSION = 1


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


def compile_cpp(src_file, out_bin, reporter=None, kind=None, display_file=None):
    if not os.path.exists(src_file):
        return False
    reporter = reporter or Reporter(False)
    file_name = display_file or os.path.basename(src_file)
    reporter.text(f"[*] Compiling {file_name}...")
    flags = ["g++", src_file, "-o", out_bin, "-O2", "-std=c++17", "-I", REFERENCES_DIR]
    if platform.system() == "Windows":
        flags.insert(4, "-static")
        if kind == "validator":
            # DOMjudge validators run on Linux and accept LF line endings. Emulate
            # that behavior so Git-normalized test data is validated consistently.
            flags.append("-DFOR_LINUX")
    try:
        ret = subprocess.run(flags, capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = ret.returncode == 0
        reporter.event("compile", kind=kind or "unknown", file=file_name, ok=ok, stderr=ret.stderr)
        if not ok:
            reporter.text(f"[-] Compile failed:\n{ret.stderr}")
            return False
        return True
    except Exception as e:
        reporter.event("compile", kind=kind or "unknown", file=file_name, ok=False, stderr=str(e))
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


def discover_solution_sources(prob_dir, config=None):
    solutions = {"std": [], "brute": [], "wrong": []}
    if config is not None:
        configured = config.get("solutions") or {}
        for config_key, kind in (("accepted", "std"), ("brute", "brute"), ("wrong", "wrong")):
            for entry in _config_entries(configured.get(config_key)):
                source_path = resolve_problem_path(prob_dir, entry)
                if source_path:
                    solutions[kind].append(source_path)
        return solutions

    for file_name in os.listdir(prob_dir):
        if not file_name.endswith(".cpp") or file_name == "validator.cpp":
            continue
        for kind in solutions:
            if file_name.startswith(kind):
                solutions[kind].append(os.path.join(prob_dir, file_name))
                break
    return solutions


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


def _unix_preexec_memory_limit(memory_limit_mb):
    if platform.system() == "Windows":
        return None
    try:
        import resource
    except Exception:
        return None

    def apply_limit():
        limit_bytes = int(memory_limit_mb * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    return apply_limit


def _windows_assign_job_memory_limit(proc, memory_limit_mb):
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        SIZE_T = ctypes.c_size_t
        ULONG_PTR = ctypes.c_size_t

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", SIZE_T),
                ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ULONG_PTR),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE_T),
                ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T),
                ("PeakJobMemoryUsed", SIZE_T),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00000200 | 0x00002000  # JOB_MEMORY | KILL_ON_JOB_CLOSE
        info.JobMemoryLimit = int(memory_limit_mb * 1024 * 1024)
        ok = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None

        ok = kernel32.AssignProcessToJobObject(job, int(proc._handle))
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _windows_query_job_peak_memory(handle):
    if not handle or platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        SIZE_T = ctypes.c_size_t
        ULONG_PTR = ctypes.c_size_t

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", SIZE_T),
                ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ULONG_PTR),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE_T),
                ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T),
                ("PeakJobMemoryUsed", SIZE_T),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ok = kernel32.QueryInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info), None
        )
        if not ok:
            return None
        peak_bytes = int(info.PeakJobMemoryUsed or info.PeakProcessMemoryUsed or 0)
        return round(peak_bytes / 1024 / 1024, 2) if peak_bytes else None
    except Exception:
        return None


def _close_windows_handle(handle):
    if handle and platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def _looks_like_memory_error(stderr):
    if not stderr:
        return False
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    text = text.lower()
    markers = ("bad_alloc", "out of memory", "cannot allocate", "memoryerror", "std::bad_alloc")
    return any(marker in text for marker in markers)


def _failed_status(returncode, stderr, memory_enforced, peak_memory_mb, memory_limit):
    if not memory_enforced:
        return "RE"
    if peak_memory_mb is not None and peak_memory_mb >= memory_limit * 0.98:
        return "MLE"
    if returncode < 0 or not stderr or _looks_like_memory_error(stderr):
        return "MLE"
    return "RE"


def run_testcase(bin_path, in_file, ans_file, time_limit=DEFAULT_TIME_LIMIT, memory_limit=DEFAULT_MEMORY_LIMIT):
    """Run a test case and return (status, elapsed_time, peak_memory_mb, memory_enforced)."""
    temp_out = os.path.join(os.path.dirname(bin_path), ".probhub-temp.out")
    start = time.time()
    memory_enforced = False
    peak_memory_mb = None
    job_handle = None
    try:
        with open(in_file, "r") as fin, open(temp_out, "w") as fout:
            kwargs = {
                "stdin": fin,
                "stdout": fout,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "preexec_fn": _unix_preexec_memory_limit(memory_limit),
            }
            if platform.system() == "Windows":
                kwargs.pop("preexec_fn", None)
            proc = subprocess.Popen([bin_path], **kwargs)
            if platform.system() == "Windows":
                job_handle = _windows_assign_job_memory_limit(proc, memory_limit)
                memory_enforced = job_handle is not None
            else:
                memory_enforced = kwargs.get("preexec_fn") is not None

            try:
                _, stderr = proc.communicate(timeout=time_limit)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                peak_memory_mb = _windows_query_job_peak_memory(job_handle)
                return "TLE", time_limit, peak_memory_mb, memory_enforced

            run_time = time.time() - start
            peak_memory_mb = _windows_query_job_peak_memory(job_handle)
            if proc.returncode != 0:
                status = _failed_status(proc.returncode, stderr, memory_enforced, peak_memory_mb, memory_limit)
                return status, run_time, peak_memory_mb, memory_enforced

        with open(ans_file, "r") as fa, open(temp_out, "r") as ft:
            status = "AC" if fa.read().strip() == ft.read().strip() else "WA"
        return status, time.time() - start, peak_memory_mb, memory_enforced
    except subprocess.TimeoutExpired:
        return "TLE", time_limit, peak_memory_mb, memory_enforced
    except (OSError, subprocess.CalledProcessError):
        return "RE", time.time() - start, peak_memory_mb, memory_enforced
    finally:
        _close_windows_handle(job_handle)
        for _ in range(20):
            try:
                os.remove(temp_out)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05)


def run_validator(val_bin, in_file):
    """Validate input using Linux-style LF endings on every host."""
    try:
        with open(in_file, "rb") as fin:
            data = fin.read().replace(b"\r\n", b"\n")
        ret = subprocess.run(
            [val_bin],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        stderr = ret.stderr.decode("utf-8", errors="replace").strip()
        return ret.returncode == 0, stderr
    except OSError as exc:
        return False, str(exc)


def collect_testcases(prob_dir, config=None):
    testcases = []
    data_config = (config or {}).get("data") or {}
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        data_dir = os.path.join(prob_dir, data_config.get(f"{suite}_dir", default))
        if not os.path.isdir(data_dir):
            continue
        for in_name in sorted([f for f in os.listdir(data_dir) if f.endswith(".in")]):
            base_name = in_name[:-3]
            testcases.append({
                "suite": suite,
                "name": base_name,
                "case": f"{suite}/{base_name}",
                "in_path": os.path.join(data_dir, in_name),
                "ans_path": os.path.join(data_dir, f"{base_name}.ans"),
            })
    return testcases


def finish(reporter, ok, message, exit_code, result_code=None):
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
    args = [arg for arg in sys.argv[1:] if arg != "--jsonl"]
    reporter = Reporter(jsonl)

    if len(args) != 1:
        reporter.text("Usage: python scripts/local_judge.py <problem_dir> [--jsonl]")
        finish(reporter, False, "Invalid arguments", 1)

    prob_dir = args[0]
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
    testcases = collect_testcases(prob_dir, config)
    if not testcases:
        finish(reporter, False, "No .in test cases found under data/sample or data/secret", 1)

    reporter.text("\n" + "=" * 50)
    reporter.text("ProbHub Validation Sandbox")
    reporter.text(f"Limits: {time_limit:g}s / {memory_limit}MB")
    reporter.text(
        f"Cases: {sum(1 for t in testcases if t['suite'] == 'sample')} sample / "
        f"{sum(1 for t in testcases if t['suite'] == 'secret')} secret"
    )
    reporter.text("=" * 50)
    reporter.event("limits", time_limit=time_limit, memory_limit=memory_limit)

    # 1. Compile and run Validator
    validator_entry = ((config or {}).get("judge") or {}).get("validator") if config is not None else "validator.cpp"
    val_cpp = resolve_problem_path(prob_dir, validator_entry)
    if val_cpp and os.path.exists(val_cpp):
        val_bin = binary_path_for_source(val_cpp)
        if compile_cpp(
            val_cpp, val_bin, reporter, "validator", display_problem_path(prob_dir, val_cpp)
        ):
            reporter.text("\nRunning validator...")
            all_ok = True
            for testcase in testcases:
                ok, validator_message = run_validator(val_bin, testcase["in_path"])
                reporter.event("validator", case=testcase["case"], ok=ok, message=validator_message)
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
    elif jsonl:
        validator_name = _entry_file(validator_entry) or "validator not configured"
        reporter.event(
            "compile",
            kind="validator",
            file=str(validator_name).replace(os.sep, "/"),
            ok=None,
            stderr=f"{validator_name} not found",
        )

    # 2. Discover and compile all solutions
    source_groups = discover_solution_sources(prob_dir, config)
    solutions = {"std": [], "brute": [], "wrong": []}
    for kind, source_paths in source_groups.items():
        for source_path in source_paths:
            program_name = display_problem_path(prob_dir, source_path)
            if not os.path.isfile(source_path):
                reporter.event("compile", kind=kind, file=program_name, ok=False, stderr="source file not found")
                reporter.text(f"[-] Source file not found: {program_name}")
                continue
            bin_name = binary_path_for_source(source_path)
            if compile_cpp(source_path, bin_name, reporter, kind, program_name):
                solutions[kind].append((program_name, bin_name))

    if not solutions["std"]:
        finish(reporter, False, "accepted solution not found or failed to compile. Aborting.", 1)

    # 3. Evaluate all solutions
    for kind, progs in solutions.items():
        for prog_name, bin_path in progs:
            reporter.text(f"\nEvaluating: {prog_name}")
            stats = {"AC": 0, "WA": 0, "TLE": 0, "RE": 0, "MLE": 0}

            for testcase in testcases:
                if not os.path.exists(testcase["ans_path"]):
                    finish(reporter, False, f"{testcase['case']}.ans not found", 1)

                status, t, memory, memory_enforced = run_testcase(
                    bin_path, testcase["in_path"], testcase["ans_path"], time_limit, memory_limit
                )
                stats[status] += 1
                reporter.event(
                    "case",
                    kind=kind,
                    program=prog_name,
                    case=testcase["case"],
                    status=status,
                    time=round(t, 6),
                    time_limit=time_limit,
                    memory=memory,
                    memory_limit=memory_limit,
                    memory_enforced=memory_enforced,
                )
                if status != "AC" or kind == "std":
                    reporter.text(f"  - {testcase['case'].ljust(16)} : {status} ({t:.3f}s)")

            reporter.event("summary", kind=kind, program=prog_name, stats=stats)
            reporter.text(f"  Summary: {stats}")

            # Fate assertions
            if kind == "std" and stats["AC"] != len(testcases):
                finish(reporter, False, "std not 100% AC! Fix the standard solution immediately.", 1)
            if kind == "brute":
                if stats["WA"] > 0:
                    finish(reporter, False, "Brute force has WA! Fix the brute or std logic.", 1)
                if stats["TLE"] + stats["MLE"] == 0:
                    finish(reporter, False, "WARNING: Brute force passes all tests without TLE/MLE. Data is too weak!", 1)
            if kind == "wrong" and stats["AC"] == len(testcases):
                finish(reporter, False, "WARNING: Wrong solution passes all tests! Add corner cases to break it.", 1)

    reporter.text("\n" + "=" * 50)
    finish(reporter, True, "[+] 恭喜！所有代码均符合预期宿命", 0)


if __name__ == "__main__":
    main()
