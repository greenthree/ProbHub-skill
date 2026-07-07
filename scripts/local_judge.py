import json
import os
import platform
import re
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REFERENCES_DIR = os.path.join(SCRIPT_DIR, "..", "references")
DEFAULT_TIME_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = 256


class Reporter:
    def __init__(self, jsonl=False):
        self.jsonl = jsonl

    def text(self, msg=""):
        if not self.jsonl:
            print(msg, flush=True)

    def event(self, type_, **payload):
        if self.jsonl:
            print(json.dumps({"type": type_, **payload}, ensure_ascii=False), flush=True)


def compile_cpp(src_file, out_bin, reporter=None, kind=None):
    if not os.path.exists(src_file):
        return False
    reporter = reporter or Reporter(False)
    file_name = os.path.basename(src_file)
    reporter.text(f"[*] Compiling {file_name}...")
    flags = ["g++", src_file, "-o", out_bin, "-O2", "-std=c++17", "-I", REFERENCES_DIR]
    if platform.system() == "Windows":
        flags.insert(4, "-static")
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


def read_problem_limits(prob_dir):
    time_limit = DEFAULT_TIME_LIMIT
    memory_limit = DEFAULT_MEMORY_LIMIT
    has_meta_time_limit = False
    has_meta_memory_limit = False

    meta_path = os.path.join(prob_dir, "meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            problem = meta.get("problem", {})
            if "time_limit" in problem:
                time_limit = _safe_float(problem.get("time_limit"), time_limit)
                has_meta_time_limit = True
            if "memory_limit" in problem:
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
    temp_out = os.path.join(os.path.dirname(bin_path), "temp.out")
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
    except subprocess.TimeoutExpired:
        return "TLE", time_limit, peak_memory_mb, memory_enforced
    except subprocess.CalledProcessError:
        return "RE", time.time() - start, peak_memory_mb, memory_enforced
    finally:
        _close_windows_handle(job_handle)

    run_time = time.time() - start
    with open(ans_file, "r") as fa, open(temp_out, "r") as ft:
        if fa.read().strip() == ft.read().strip():
            return "AC", run_time, peak_memory_mb, memory_enforced
        return "WA", run_time, peak_memory_mb, memory_enforced


def run_validator(val_bin, in_file):
    """Validate input format using testlib-style exit codes."""
    try:
        with open(in_file, "r") as fin:
            subprocess.run([val_bin], stdin=fin, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def collect_testcases(prob_dir):
    testcases = []
    for suite in ("sample", "secret"):
        data_dir = os.path.join(prob_dir, "data", suite)
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


def finish(reporter, ok, message, code):
    reporter.event("final", ok=ok, message=message)
    reporter.text(message)
    sys.exit(code)


def main():
    jsonl = "--jsonl" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg != "--jsonl"]
    reporter = Reporter(jsonl)

    if len(args) != 1:
        reporter.text("Usage: python scripts/local_judge.py <problem_dir> [--jsonl]")
        finish(reporter, False, "Invalid arguments", 1)

    prob_dir = args[0]
    sample_dir = os.path.join(prob_dir, "data", "sample")
    secret_dir = os.path.join(prob_dir, "data", "secret")
    if not os.path.isdir(sample_dir) and not os.path.isdir(secret_dir):
        finish(reporter, False, "data/sample and data/secret not found", 1)

    time_limit, memory_limit = read_problem_limits(prob_dir)
    testcases = collect_testcases(prob_dir)
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
    val_cpp = os.path.join(prob_dir, "validator.cpp")
    val_bin = os.path.join(prob_dir, "validator.exe")
    if os.path.exists(val_cpp):
        if compile_cpp(val_cpp, val_bin, reporter, "validator"):
            reporter.text("\nRunning validator...")
            all_ok = True
            for testcase in testcases:
                ok = run_validator(val_bin, testcase["in_path"])
                reporter.event("validator", case=testcase["case"], ok=ok)
                if not ok:
                    all_ok = False
                    reporter.text(f"[-] FATAL: {testcase['case']} violates validator constraints! Fix the data generator.")
                    finish(reporter, False, f"{testcase['case']} violates validator constraints", 1)
            if all_ok:
                reporter.text("[+] Validator: ALL PASS")
    elif jsonl:
        reporter.event("compile", kind="validator", file="validator.cpp", ok=None, stderr="validator.cpp not found")

    # 2. Discover and compile all solutions
    solutions = {"std": [], "brute": [], "wrong": []}
    for file in os.listdir(prob_dir):
        if file.endswith(".cpp") and file != "validator.cpp":
            for kind in solutions.keys():
                if file.startswith(kind):
                    bin_name = os.path.join(prob_dir, file.replace(".cpp", ".exe"))
                    if compile_cpp(os.path.join(prob_dir, file), bin_name, reporter, kind):
                        solutions[kind].append((file, bin_name))

    if not solutions["std"]:
        finish(reporter, False, "std.cpp not found. Aborting.", 1)

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
