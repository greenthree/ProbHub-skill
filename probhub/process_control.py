"""Cross-platform process-tree control for ProbHub sandboxes."""

import os
import platform
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PROCESS_LIMIT = 32
VALIDATOR_TIMEOUT_SECONDS = 5.0
VALIDATOR_MEMORY_LIMIT_MB = 512
VALIDATOR_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
DEFAULT_POLL_INTERVAL = 0.005
CANCEL_FILE_ENV = "PROBHUB_CANCEL_FILE"
WINDOWS_CREATE_SUSPENDED = 0x00000004
PYTHON_OPTIONS_WITH_ARGUMENT = frozenset(("-W", "-X", "--check-hash-based-pycs"))
UNIX_EXEC_START_TIMEOUT_SECONDS = 10.0
UNIX_EXEC_READY = b"PROBHUB_UNIX_EXEC_READY_V1\n"
UNIX_EXEC_STATUS_LIMIT = 64 * 1024
UNIX_EXEC_HELPER_CODE = r'''
import os
import signal
import sys

READY = b"PROBHUB_UNIX_EXEC_READY_V1\n"


def report_failure(status_fd, message):
    try:
        os.write(status_fd, ("Unix execution helper failed: " + message).encode("utf-8"))
    except OSError:
        pass


def main():
    argv = sys.argv[1:]
    status_fd = None
    try:
        if len(argv) < 7 or argv[0] != "--memory-limit-bytes" or argv[2] != "--status-fd":
            raise ValueError("invalid helper arguments")
        limit_bytes = int(argv[1])
        status_fd = int(argv[3])
        if argv[4] not in ("--restore-signals", "--keep-signals"):
            raise ValueError("invalid signal restore mode")
        restore_signals = argv[4] == "--restore-signals"
        if argv[5] != "--" or not argv[6:]:
            raise ValueError("target command is missing")
        if limit_bytes <= 0:
            raise ValueError("memory limit must be positive")

        import resource

        os.set_inheritable(status_fd, False)
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        if restore_signals:
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    signal.signal(signum, signal.SIG_DFL)
        os.write(status_fd, READY)
        os.execvpe(argv[6], argv[6:], os.environ)
    except BaseException as exc:
        if status_fd is not None:
            report_failure(status_fd, str(exc))
        return 127


raise SystemExit(main())
'''.lstrip()


class ProcessCancelled(Exception):
    """Raised when a supervising submission task requests cancellation."""


class OutputBudgetError(OSError):
    """Raised when captured output cannot be measured or bounded safely."""


def cancellation_requested():
    path = os.environ.get(CANCEL_FILE_ENV)
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _cancellation_requested(cancel_check):
    check = cancellation_requested if cancel_check is None else cancel_check
    return bool(check())


def _same_executable(left, right):
    try:
        return os.path.samefile(left, right)
    except (OSError, TypeError, ValueError):
        try:
            return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
                os.path.abspath(os.fspath(right))
            )
        except (TypeError, ValueError):
            return False


def _prepare_windows_python_command(command, env):
    """Bypass Windows venv redirectors so Job Object containment reaches Python."""
    if platform.system() != "Windows" or isinstance(command, (str, bytes, os.PathLike)):
        return command, env
    command = list(command)
    if not command or sys.prefix == sys.base_prefix:
        return command, env
    if not _same_executable(command[0], sys.executable):
        return command, env

    base_executable = Path(sys.base_prefix) / Path(sys.executable).name
    helper = Path(__file__).with_name("_windows_python_exec.py")
    if not base_executable.is_file():
        raise OSError(f"Windows virtualenv base interpreter is missing: {base_executable}")
    if not helper.is_file():
        raise OSError(f"Windows Python containment helper is missing: {helper}")

    arguments = command[1:]
    interpreter_options = []
    while arguments:
        option = arguments[0]
        if option == "--":
            arguments = arguments[1:]
            break
        if option in ("-", "-c", "-m") or not str(option).startswith("-"):
            break
        interpreter_options.append(option)
        arguments = arguments[1:]
        if option in PYTHON_OPTIONS_WITH_ARGUMENT:
            if not arguments:
                raise ValueError(f"Python option requires an argument: {option}")
            interpreter_options.append(arguments[0])
            arguments = arguments[1:]

    # A Windows venv python.exe can be a redirector whose Popen handle belongs
    # to a launcher instead of the real interpreter. Assigning only that handle
    # to a Job lets the interpreter and its descendants escape all limits.
    original_executable = str(command[0])
    command = [
        str(base_executable),
        *interpreter_options,
        str(helper),
        str(base_executable),
        *arguments,
    ]
    child_env = dict(os.environ if env is None else env)
    # CPython uses this path to discover pyvenv.cfg before the helper starts.
    # The helper then points descendants at the base executable while the root
    # process keeps its original venv prefix and processed site-packages.
    child_env["__PYVENV_LAUNCHER__"] = original_executable
    return command, child_env


def _prepare_unix_memory_limited_command(command, memory_limit_mb):
    if isinstance(command, (str, bytes, os.PathLike)):
        raise TypeError("memory-limited Unix commands must be an argument sequence")
    command = list(command)
    if not command:
        raise ValueError("memory-limited Unix target command is missing")
    limit_bytes = int(float(memory_limit_mb) * 1024 * 1024)
    if limit_bytes <= 0:
        raise ValueError("memory limit must be positive")
    return command, limit_bytes


def _unix_memory_limited_command(command, limit_bytes, status_fd, restore_signals):
    return [
        sys.executable,
        "-I",
        "-S",
        "-c",
        UNIX_EXEC_HELPER_CODE,
        "--memory-limit-bytes",
        str(limit_bytes),
        "--status-fd",
        str(status_fd),
        "--restore-signals" if restore_signals else "--keep-signals",
        "--",
        *command,
    ]


def _terminate_failed_unix_start(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _await_unix_exec(proc, status_fd):
    deadline = time.monotonic() + UNIX_EXEC_START_TIMEOUT_SECONDS
    chunks = []
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(status_fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                _terminate_failed_unix_start(proc)
                raise OSError(
                    "Unix execution helper did not reach exec before the startup deadline"
                )
            detail = os.read(status_fd, 4096)
            if not detail:
                break
            chunks.append(detail)
            total += len(detail)
            if total > UNIX_EXEC_STATUS_LIMIT:
                _terminate_failed_unix_start(proc)
                raise OSError("Unix execution helper returned an oversized startup status")

    detail = b"".join(chunks)
    if detail != UNIX_EXEC_READY:
        _terminate_failed_unix_start(proc)
        message = detail.decode("utf-8", errors="replace").strip()
        raise OSError(message or "Unix execution helper did not report startup readiness")


_WINDOWS_API = None


def _windows_api():
    global _WINDOWS_API
    if _WINDOWS_API is not None:
        return _WINDOWS_API
    if platform.system() != "Windows":
        _WINDOWS_API = {}
        return _WINDOWS_API
    try:
        import ctypes
        from ctypes import wintypes

        size_t = ctypes.c_size_t
        ulong_ptr = ctypes.c_size_t

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", size_t),
                ("MaximumWorkingSetSize", size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ulong_ptr),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", size_t),
                ("JobMemoryLimit", size_t),
                ("PeakProcessMemoryUsed", size_t),
                ("PeakJobMemoryUsed", size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        class ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        _WINDOWS_API = {
            "ctypes": ctypes,
            "kernel32": kernel32,
            "info_type": ExtendedLimitInformation,
            "accounting_type": BasicAccountingInformation,
            "thread_entry_type": ThreadEntry32,
        }
    except Exception:
        _WINDOWS_API = {}
    return _WINDOWS_API


def windows_assign_job(proc, memory_limit_mb=None, process_limit=DEFAULT_PROCESS_LIMIT):
    api = _windows_api()
    if not api:
        return None
    ctypes = api["ctypes"]
    kernel32 = api["kernel32"]
    info_type = api["info_type"]
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = info_type()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if memory_limit_mb is not None:
            info.BasicLimitInformation.LimitFlags |= 0x00000200  # JOB_MEMORY
            info.JobMemoryLimit = int(float(memory_limit_mb) * 1024 * 1024)
        if process_limit is not None and int(process_limit) > 0:
            info.BasicLimitInformation.LimitFlags |= 0x00000008  # ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = int(process_limit)
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def windows_resume_process(pid):
    api = _windows_api()
    if not api:
        return False
    ctypes = api["ctypes"]
    kernel32 = api["kernel32"]
    entry_type = api["thread_entry_type"]
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False
        resumed = False
        try:
            entry = entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while has_entry:
                if int(entry.th32OwnerProcessID) == int(pid):
                    thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if thread:
                        try:
                            for _ in range(16):
                                previous = int(kernel32.ResumeThread(thread))
                                if previous == 0xFFFFFFFF:
                                    return False
                                resumed = True
                                if previous <= 1:
                                    break
                        finally:
                            kernel32.CloseHandle(thread)
                entry.dwSize = ctypes.sizeof(entry)
                has_entry = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return resumed
    except Exception:
        return False


def windows_query_job_peak_memory(handle):
    api = _windows_api()
    if not handle or not api:
        return None
    ctypes = api["ctypes"]
    kernel32 = api["kernel32"]
    info_type = api["info_type"]
    try:
        info = info_type()
        if not kernel32.QueryInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return None
        peak_bytes = int(info.PeakJobMemoryUsed or info.PeakProcessMemoryUsed or 0)
        return peak_bytes / (1024 * 1024)
    except Exception:
        return None


def close_windows_handle(handle):
    api = _windows_api()
    if not handle or not api:
        return
    try:
        api["kernel32"].CloseHandle(handle)
    except Exception:
        pass


def terminate_windows_job(handle, timeout=2.0):
    """Terminate a Windows Job and wait until every associated process exits."""
    api = _windows_api()
    if not handle or not api:
        close_windows_handle(handle)
        return
    ctypes = api["ctypes"]
    kernel32 = api["kernel32"]
    accounting_type = api["accounting_type"]
    try:
        if kernel32.TerminateJobObject(handle, 1):
            deadline = time.perf_counter() + float(timeout)
            while True:
                accounting = accounting_type()
                queried = kernel32.QueryInformationJobObject(
                    handle,
                    1,
                    ctypes.byref(accounting),
                    ctypes.sizeof(accounting),
                    None,
                )
                if not queried or int(accounting.ActiveProcesses) == 0:
                    break
                if time.perf_counter() >= deadline:
                    break
                time.sleep(0.01)
    except Exception:
        pass
    finally:
        close_windows_handle(handle)


def terminate_process(proc, job_handle=None):
    """Terminate a direct process and every descendant, then reap it."""
    if proc is None:
        terminate_windows_job(job_handle)
        return
    if platform.system() == "Windows":
        if job_handle:
            terminate_windows_job(job_handle)
            job_handle = None
        elif proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


def process_alive(pid):
    if not pid:
        return False
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            ERROR_ACCESS_DENIED = 5
            STILL_ACTIVE = 259
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            WAIT_OBJECT_0 = 0
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
            )
            if not handle:
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                # A live process owned by another user opens with ACCESS_DENIED;
                # report it alive instead of pretending it exited.
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    # Cannot query: fall back to the signal probe alone.
                    return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
                # An exited process stays openable while any handle is held
                # (including our own Popen object); a real exit code means dead.
                # This must come first: a just-terminated (e.g. suspended) process
                # reports its exit code before the process object is signalled,
                # and Popen.poll() follows the same exit-code semantics.
                if exit_code.value != STILL_ACTIVE:
                    return False
                # STILL_ACTIVE (259) is ambiguous — a process that exited with
                # code 259 reports it forever; the signalled handle disambiguates.
                return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _linux_process_table():
    if platform.system() == "Windows" or not Path("/proc").is_dir():
        return {}
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            close = stat.rfind(")")
            fields = stat[close + 2 :].split()
            ppid = int(fields[1])
            rss_pages = int(fields[21])
            table[int(entry.name)] = (ppid, rss_pages)
        except (OSError, ValueError, IndexError):
            continue
    return table


def _process_tree_pids_from_table(root_pid, table):
    root_pid = int(root_pid)
    children = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    pending = [root_pid]
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.extend(children.get(pid, ()))
    return seen


def snapshot_process_tree(root_pid):
    """Return the known root/descendant PIDs where the platform permits it."""
    root_pid = int(root_pid)
    if platform.system() == "Windows":
        return {root_pid}
    table = _linux_process_table()
    if not table:
        return {root_pid}
    return _process_tree_pids_from_table(root_pid, table)


def terminate_external_process_tree(proc, known_pids=()):
    """Force-stop a supervisor process and detached descendant groups."""
    if proc is None:
        return
    root_pid = int(proc.pid)
    pids = set(int(pid) for pid in (known_pids or ())) | snapshot_process_tree(root_pid)
    if platform.system() == "Windows":
        targets = [root_pid] + sorted((pid for pid in pids if pid != root_pid), reverse=True)
        for pid in targets:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        current_group = os.getpgrp()
        groups = set()
        for pid in pids:
            try:
                group = os.getpgid(pid)
                if group != current_group:
                    groups.add(group)
            except OSError:
                pass
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        for pid in sorted(pids, reverse=True):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass


def process_tree_metrics(root_pid):
    """Return descendant count and aggregate RSS in MB where /proc is available."""
    table = _linux_process_table()
    if not table:
        return None, None
    seen = _process_tree_pids_from_table(root_pid, table)
    rss_pages = sum(table.get(pid, (0, 0))[1] for pid in seen)
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError):
        page_size = 4096
    return len(seen), rss_pages * page_size / (1024 * 1024)


@dataclass
class ManagedProcess:
    proc: subprocess.Popen
    job_handle: object = None
    memory_limit_mb: float | None = None
    process_limit: int | None = DEFAULT_PROCESS_LIMIT
    memory_enforced: bool = False
    process_limit_enforced: bool = False
    peak_memory_mb: float | None = None

    def sample(self):
        if platform.system() == "Windows":
            current = windows_query_job_peak_memory(self.job_handle)
            count = None
        else:
            count, current = process_tree_metrics(self.proc.pid)
        if current is not None:
            self.peak_memory_mb = max(self.peak_memory_mb or 0.0, current)
        return count, current

    def terminate(self):
        terminate_process(self.proc, self.job_handle)
        self.job_handle = None

    def close(self):
        close_windows_handle(self.job_handle)
        self.job_handle = None


def spawn_managed(
    command,
    *,
    memory_limit_mb=None,
    process_limit=DEFAULT_PROCESS_LIMIT,
    containment_required=True,
    **kwargs,
):
    kwargs = dict(kwargs)
    if "preexec_fn" in kwargs:
        raise ValueError("preexec_fn is not supported; use managed process limits")
    unix_status_fds = None
    if platform.system() == "Windows":
        command, kwargs["env"] = _prepare_windows_python_command(
            command, kwargs.get("env")
        )
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | WINDOWS_CREATE_SUSPENDED
    else:
        kwargs.setdefault("start_new_session", True)
        if memory_limit_mb is not None:
            if kwargs.get("shell"):
                raise ValueError("memory-limited Unix commands do not support shell=True")
            if kwargs.get("executable") is not None:
                raise ValueError("memory-limited Unix commands do not support executable=")
            pass_fds = tuple(kwargs.get("pass_fds") or ())
            command, limit_bytes = _prepare_unix_memory_limited_command(
                command, memory_limit_mb
            )
            read_fd, write_fd = os.pipe()
            unix_status_fds = (read_fd, write_fd)
            try:
                kwargs["pass_fds"] = (*pass_fds, write_fd)
                kwargs["close_fds"] = True
                command = _unix_memory_limited_command(
                    command,
                    limit_bytes,
                    write_fd,
                    kwargs.get("restore_signals", True),
                )
            except BaseException:
                os.close(read_fd)
                os.close(write_fd)
                raise
    try:
        proc = subprocess.Popen(command, **kwargs)
    except BaseException:
        if unix_status_fds is not None:
            os.close(unix_status_fds[0])
            os.close(unix_status_fds[1])
        raise
    if unix_status_fds is not None:
        read_fd, write_fd = unix_status_fds
        os.close(write_fd)
        try:
            _await_unix_exec(proc, read_fd)
        except BaseException:
            _terminate_failed_unix_start(proc)
            raise
        finally:
            os.close(read_fd)
    job_handle = None
    if platform.system() == "Windows":
        started = False
        try:
            job_handle = windows_assign_job(proc, memory_limit_mb, process_limit)
            if containment_required and job_handle is None:
                raise OSError("Windows Job Object containment is unavailable")
            if not windows_resume_process(proc.pid):
                raise OSError("failed to resume suspended Windows process")
            started = True
        finally:
            if not started:
                if job_handle is not None:
                    terminate_windows_job(job_handle)
                    job_handle = None
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
    return ManagedProcess(
        proc=proc,
        job_handle=job_handle,
        memory_limit_mb=memory_limit_mb,
        process_limit=process_limit,
        memory_enforced=(job_handle is not None if platform.system() == "Windows" else memory_limit_mb is not None),
        process_limit_enforced=(job_handle is not None if platform.system() == "Windows" else Path("/proc").is_dir()),
    )


def output_path_size(path, *, required=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise OutputBudgetError(f"required captured output is missing: {path}")
        return 0, False
    except OSError as exc:
        raise OutputBudgetError(f"cannot inspect captured output {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    ):
        raise OutputBudgetError(f"captured output path is a link or reparse point: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise OutputBudgetError(f"captured output path is not a regular file: {path}")
    return int(info.st_size), True


def _files_size(required_paths, optional_paths=()):
    total = 0
    for path in required_paths or ():
        size, _ = output_path_size(path, required=True)
        total += size
    for path in optional_paths or ():
        size, _ = output_path_size(path)
        total += size
    return total


def allocate_shared_prefix_bytes(sizes, limit_bytes):
    """Allocate a deterministic shared prefix budget across byte streams.

    Each stream receives a prefix, with unused quota from short streams
    redistributed before the remaining streams are split evenly. The input
    order breaks odd-byte ties, so stdout/stderr retention is reproducible.
    """
    normalized = [max(int(size), 0) for size in sizes]
    if limit_bytes is None:
        return normalized
    remaining = max(int(limit_bytes), 0)
    allocations = [0] * len(normalized)
    active = [index for index, size in enumerate(normalized) if size]
    while active and remaining:
        share = remaining // len(active)
        if share <= 0:
            break
        short = [index for index in active if normalized[index] <= share]
        if not short:
            break
        for index in short:
            allocations[index] = normalized[index]
            remaining -= normalized[index]
            active.remove(index)
    if active and remaining:
        base, extra = divmod(remaining, len(active))
        for position, index in enumerate(active):
            allocations[index] = min(
                normalized[index], base + (1 if position < extra else 0)
            )
    return allocations


def _truncate_paths(paths, limit_bytes, *, required_count=None):
    if limit_bytes is None:
        return []
    paths = tuple(paths or ())
    if required_count is None:
        required_count = len(paths)
    required_count = max(0, min(int(required_count), len(paths)))
    sizes = []
    existed = []
    for index, path in enumerate(paths):
        size, present = output_path_size(path, required=index < required_count)
        sizes.append(size)
        existed.append(present)
    allocations = allocate_shared_prefix_bytes(sizes, limit_bytes)
    failures = []
    for path, size, allocation in zip(paths, sizes, allocations):
        if size <= allocation:
            continue
        try:
            with open(path, "r+b") as stream:
                stream.truncate(allocation)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    retained = []
    for index, (path, was_present) in enumerate(zip(paths, existed)):
        try:
            size, _ = output_path_size(
                path, required=was_present or index < required_count
            )
            retained.append(size)
        except OutputBudgetError as exc:
            failures.append(str(exc))
            retained.append(0)
    if failures or sum(retained) > int(limit_bytes):
        detail = "; ".join(failures) or "shared output budget could not be enforced"
        raise OutputBudgetError(detail)
    return retained


def wait_managed(
    managed,
    timeout,
    *,
    output_paths=(),
    optional_output_paths=(),
    output_limit_bytes=None,
    poll_interval=DEFAULT_POLL_INTERVAL,
    cancel_check=None,
):
    """Wait for a managed process and always clean the complete process tree."""
    started = time.perf_counter()
    deadline = started + float(timeout)
    reason = "completed"
    message = ""
    last_resource_sample = started
    returncode = None
    elapsed = 0.0
    output_bytes = 0
    retained_output_bytes = 0
    retained_paths = []
    output_paths = tuple(output_paths or ())
    optional_output_paths = tuple(optional_output_paths or ())
    all_output_paths = (*output_paths, *optional_output_paths)
    try:
        while managed.proc.poll() is None:
            now = time.perf_counter()
            if _cancellation_requested(cancel_check):
                reason, message = "cancelled", "execution cancelled"
                break
            count = current_memory = None
            if now - last_resource_sample >= 0.05:
                count, current_memory = managed.sample()
                last_resource_sample = now
            if (
                managed.memory_limit_mb is not None
                and current_memory is not None
                and current_memory >= float(managed.memory_limit_mb)
            ):
                reason, message = "memory_limit", "memory limit exceeded"
                break
            if (
                managed.process_limit is not None
                and count is not None
                and count > int(managed.process_limit)
            ):
                reason, message = "process_limit", "process limit exceeded"
                break
            if output_limit_bytes is not None and _files_size(
                output_paths, optional_output_paths
            ) > int(output_limit_bytes):
                reason, message = "output_limit", "output limit exceeded"
                break
            if now >= deadline:
                reason, message = "time_limit", "time limit exceeded"
                break
            time.sleep(min(float(poll_interval), max(deadline - now, 0.0)))

        elapsed = time.perf_counter() - started
        # A short process can exit before the first polling iteration. Recheck
        # file size and deadline after exit so fast output floods cannot bypass OLE.
        observed_output_bytes = _files_size(output_paths, optional_output_paths)
        if _cancellation_requested(cancel_check):
            reason, message = "cancelled", "execution cancelled"
        elif output_limit_bytes is not None and observed_output_bytes > int(output_limit_bytes):
            reason, message = "output_limit", "output limit exceeded"
        elif elapsed > float(timeout) and reason == "completed":
            reason, message = "time_limit", "time limit exceeded"
        returncode = managed.proc.poll()
    finally:
        managed.terminate()
        if returncode is None:
            returncode = managed.proc.returncode
        output_bytes = _files_size(output_paths, optional_output_paths)
        if output_limit_bytes is not None:
            retained_paths = _truncate_paths(
                all_output_paths,
                output_limit_bytes,
                required_count=len(output_paths),
            )
            retained_output_bytes = sum(retained_paths)
        else:
            retained_paths = [
                output_path_size(path, required=index < len(output_paths))[0]
                for index, path in enumerate(all_output_paths)
            ]
            retained_output_bytes = sum(retained_paths)

    return {
        "reason": reason,
        "message": message,
        "returncode": returncode,
        "time": elapsed,
        "memory": managed.peak_memory_mb,
        "memory_enforced": managed.memory_enforced,
        "process_limit_enforced": managed.process_limit_enforced,
        "output_bytes": output_bytes,
        "retained_output_bytes": retained_output_bytes,
        "stdout_retained_bytes": retained_paths[0] if retained_paths else 0,
        "stderr_retained_bytes": retained_paths[1] if len(retained_paths) > 1 else 0,
        "output_truncated": retained_output_bytes < output_bytes,
    }


def run_managed_to_files(
    command,
    *,
    input_path=None,
    input_data=None,
    stdout_path,
    stderr_path,
    additional_output_paths=(),
    timeout,
    memory_limit_mb=None,
    output_limit_bytes=None,
    process_limit=DEFAULT_PROCESS_LIMIT,
    cwd=None,
    env=None,
    cancel_check=None,
):
    input_stream = None
    temporary_input = None
    try:
        if _cancellation_requested(cancel_check):
            raise ProcessCancelled("execution cancelled")
        if input_path is not None:
            input_stream = open(input_path, "rb")
        elif input_data is not None:
            temporary_input = tempfile.TemporaryFile()
            temporary_input.write(input_data)
            temporary_input.seek(0)
            input_stream = temporary_input
        with open(stdout_path, "wb") as stdout_stream, open(stderr_path, "wb") as stderr_stream:
            managed = spawn_managed(
                command,
                stdin=input_stream or subprocess.DEVNULL,
                stdout=stdout_stream,
                stderr=stderr_stream,
                cwd=cwd,
                env=env,
                memory_limit_mb=memory_limit_mb,
                process_limit=process_limit,
            )
            result = wait_managed(
                managed,
                timeout,
                output_paths=(stdout_path, stderr_path),
                optional_output_paths=tuple(additional_output_paths or ()),
                output_limit_bytes=output_limit_bytes,
                cancel_check=cancel_check,
            )
            if result.get("reason") == "cancelled":
                raise ProcessCancelled(result.get("message") or "execution cancelled")
            return result
    finally:
        if input_stream is not None and input_stream is not temporary_input:
            input_stream.close()
        if temporary_input is not None:
            temporary_input.close()
