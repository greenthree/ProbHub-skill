import ctypes
import json
import os
import platform
import signal
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import probhub.process_control as process_control
from probhub.process_control import (
    CANCEL_FILE_ENV,
    ProcessCancelled,
    allocate_shared_prefix_bytes,
    process_alive,
    run_managed_to_files,
    snapshot_process_tree,
    terminate_external_process_tree,
    wait_managed,
)


class ProcessControlTests(unittest.TestCase):
    def test_shared_prefix_budget_is_fair_and_deterministic(self):
        self.assertEqual(allocate_shared_prefix_bytes([2000, 2000], 1025), [513, 512])
        self.assertEqual(allocate_shared_prefix_bytes([100, 2000], 1024), [100, 924])
        self.assertEqual(allocate_shared_prefix_bytes([0, 100, 2000], 1024), [0, 100, 924])
        self.assertEqual(allocate_shared_prefix_bytes([10, 20], 100), [10, 20])

    def test_output_budget_rejects_non_regular_capture_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture = root / "capture"
            capture.mkdir()
            with self.assertRaisesRegex(
                process_control.OutputBudgetError, "not a regular file"
            ):
                process_control._truncate_paths((capture,), 1024)

    def test_required_capture_path_cannot_disappear(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing"
            with self.assertRaisesRegex(
                process_control.OutputBudgetError, "required captured output is missing"
            ):
                process_control._truncate_paths((missing,), 1024)

    def test_wait_fails_closed_and_terminates_when_required_capture_disappears(self):
        class FakeProcess:
            returncode = 0

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_limit_mb = None
            process_limit = None
            memory_enforced = False
            process_limit_enforced = False
            peak_memory_mb = None

            def __init__(self):
                self.proc = FakeProcess()
                self.terminated = False

            def sample(self):
                return None, None

            def terminate(self):
                self.terminated = True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stderr = root / "stderr"
            stderr.write_bytes(b"")
            managed = FakeManaged()
            with self.assertRaisesRegex(
                process_control.OutputBudgetError, "required captured output is missing"
            ):
                wait_managed(
                    managed,
                    1,
                    output_paths=(root / "missing-stdout", stderr),
                    output_limit_bytes=1024,
                )
            self.assertTrue(managed.terminated)

    def test_output_budget_truncation_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp) / "capture"
            capture.write_bytes(b"x" * 2048)
            with mock.patch("builtins.open", side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(
                    process_control.OutputBudgetError, "locked"
                ):
                    process_control._truncate_paths((capture,), 1024)

    def test_windows_virtualenv_python_redirector_is_replaced_with_base_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            venv = root / "venv"
            current = venv / "Scripts" / "python.exe"
            site_packages = venv / "Lib" / "site-packages"
            base = root / "base"
            base_python = base / "python.exe"
            helper = root / "_windows_python_exec.py"
            current.parent.mkdir(parents=True)
            site_packages.mkdir(parents=True)
            base.mkdir()
            current.touch()
            base_python.touch()
            helper.touch()

            with (
                mock.patch.object(process_control.platform, "system", return_value="Windows"),
                mock.patch.object(process_control.sys, "executable", str(current)),
                mock.patch.object(process_control.sys, "prefix", str(venv)),
                mock.patch.object(process_control.sys, "base_prefix", str(base)),
                mock.patch.object(
                    process_control.Path,
                    "with_name",
                    return_value=helper,
                ),
            ):
                command, env = process_control._prepare_windows_python_command(
                    [str(current), "-c", "print('ok')"],
                    {"PYTHONPATH": "existing"},
                )

            self.assertEqual(command[0], str(base_python))
            self.assertEqual(
                command[1:], [str(helper), str(base_python), "-c", "print('ok')"]
            )
            self.assertEqual(env["__PYVENV_LAUNCHER__"], str(current))
            self.assertEqual(env["PYTHONPATH"], "existing")

    def test_windows_virtualenv_python_options_precede_containment_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            venv = root / "venv"
            current = venv / "Scripts" / "python.exe"
            base = root / "base"
            base_python = base / "python.exe"
            helper = root / "_windows_python_exec.py"
            current.parent.mkdir(parents=True)
            base.mkdir()
            current.touch()
            base_python.touch()
            helper.touch()

            with (
                mock.patch.object(process_control.platform, "system", return_value="Windows"),
                mock.patch.object(process_control.sys, "executable", str(current)),
                mock.patch.object(process_control.sys, "prefix", str(venv)),
                mock.patch.object(process_control.sys, "base_prefix", str(base)),
                mock.patch.object(process_control.Path, "with_name", return_value=helper),
            ):
                command, _ = process_control._prepare_windows_python_command(
                    [str(current), "-u", "-X", "utf8", "-c", "print('ok')"],
                    None,
                )

            self.assertEqual(
                command,
                [
                    str(base_python),
                    "-u",
                    "-X",
                    "utf8",
                    str(helper),
                    str(base_python),
                    "-c",
                    "print('ok')",
                ],
            )

    def test_windows_virtualenv_missing_helper_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            venv = root / "venv"
            current = venv / "Scripts" / "python.exe"
            base = root / "base"
            base_python = base / "python.exe"
            current.parent.mkdir(parents=True)
            base.mkdir()
            current.touch()
            base_python.touch()

            with (
                mock.patch.object(process_control.platform, "system", return_value="Windows"),
                mock.patch.object(process_control.sys, "executable", str(current)),
                mock.patch.object(process_control.sys, "prefix", str(venv)),
                mock.patch.object(process_control.sys, "base_prefix", str(base)),
                mock.patch.object(
                    process_control.Path,
                    "with_name",
                    return_value=root / "missing-helper.py",
                ),
                self.assertRaisesRegex(OSError, "containment helper is missing"),
            ):
                process_control._prepare_windows_python_command(
                    [str(current), "-c", "print('unsafe')"], None
                )

    @unittest.skipUnless(
        platform.system() == "Windows" and sys.prefix != sys.base_prefix,
        "Windows virtualenv containment only",
    )
    def test_windows_virtualenv_python_keeps_prefix_inside_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cwd_probe.py").write_text("VALUE = 'cwd-ok'\n", encoding="utf-8")
            result = self.run_command(
                root,
                "import cwd_probe,json,sys; "
                "print(json.dumps([sys.prefix, sys.executable, cwd_probe.VALUE]))",
            )
            prefix, executable, cwd_value = json.loads(
                (root / "stdout.txt").read_text(encoding="utf-8")
            )
            self.assertEqual(result["reason"], "completed", result)
            self.assertEqual(cwd_value, "cwd-ok")
            self.assertTrue(os.path.samefile(prefix, sys.prefix))
            self.assertTrue(
                os.path.samefile(
                    executable, Path(sys.base_prefix) / Path(sys.executable).name
                )
            )

            (root / "cwd_probe_module.py").write_text(
                "import json,sys; print(json.dumps(sys.argv))\n", encoding="utf-8"
            )
            module_result = run_managed_to_files(
                [sys.executable, "-u", "-X", "utf8", "-m", "cwd_probe_module", "arg"],
                stdout_path=root / "module-stdout.txt",
                stderr_path=root / "module-stderr.txt",
                timeout=5,
                memory_limit_mb=256,
                output_limit_bytes=1024 * 1024,
                process_limit=8,
                cwd=root,
            )
            self.assertEqual(module_result["reason"], "completed", module_result)
            module_argv = json.loads(
                (root / "module-stdout.txt").read_text(encoding="utf-8")
            )
            self.assertTrue(os.path.samefile(module_argv[0], root / "cwd_probe_module.py"))
            self.assertEqual(module_argv[1:], ["arg"])

            version_result = run_managed_to_files(
                [sys.executable, "-V"],
                stdout_path=root / "version-stdout.txt",
                stderr_path=root / "version-stderr.txt",
                timeout=5,
                memory_limit_mb=256,
                output_limit_bytes=1024 * 1024,
                process_limit=8,
                cwd=root,
            )
            self.assertEqual(version_result["reason"], "completed", version_result)
            self.assertIn(
                "Python ",
                (root / "version-stdout.txt").read_text(encoding="utf-8")
                + (root / "version-stderr.txt").read_text(encoding="utf-8"),
            )

    def test_spawn_rejects_caller_preexec_fn(self):
        with self.assertRaisesRegex(ValueError, "preexec_fn is not supported"):
            process_control.spawn_managed(
                [sys.executable, "-c", "print('x')"],
                preexec_fn=None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def test_unix_memory_limit_helper_is_passed_inline(self):
        command = process_control._unix_memory_limited_command(
            ["/bin/true"],
            256 * 1024 * 1024,
            17,
            True,
        )
        self.assertEqual(command[:4], [sys.executable, "-I", "-S", "-c"])
        self.assertEqual(command[4], process_control.UNIX_EXEC_HELPER_CODE)
        self.assertNotIn("_unix_exec.py", command)
        self.assertEqual(command[-2:], ["--", "/bin/true"])

    @mock.patch.object(process_control.platform, "system", return_value="Linux")
    @mock.patch.object(process_control.os, "pipe")
    def test_invalid_unix_memory_limited_command_does_not_open_status_pipe(
        self, pipe, _system
    ):
        for command, limit in (("echo x", 256), ([sys.executable], "bad"), ([], 256)):
            with self.subTest(command=command, limit=limit):
                with self.assertRaises((TypeError, ValueError)):
                    process_control.spawn_managed(command, memory_limit_mb=limit)
        pipe.assert_not_called()

    @unittest.skipIf(platform.system() == "Windows", "Unix exec helper only")
    def test_unix_exec_helper_failure_is_fail_closed(self):
        with self.assertRaisesRegex(OSError, "Unix execution helper failed"):
            process_control.spawn_managed(
                ["probhub-command-that-does-not-exist"],
                memory_limit_mb=256,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    @unittest.skipIf(platform.system() == "Windows", "Unix exec helper only")
    def test_unix_exec_helper_applies_address_space_limit(self):
        limit_mb = 384
        managed = process_control.spawn_managed(
            [
                sys.executable,
                "-c",
                "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])",
            ],
            memory_limit_mb=limit_mb,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = managed.proc.communicate(timeout=10)
        finally:
            managed.close()
        self.assertEqual(managed.proc.returncode, 0, stderr)
        self.assertEqual(int(stdout.strip()), limit_mb * 1024 * 1024)

    @unittest.skipIf(platform.system() == "Windows", "Unix exec helper only")
    def test_unix_exec_status_fd_closes_before_target_exits(self):
        managed = process_control.spawn_managed(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            memory_limit_mb=256,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertIsNone(managed.proc.poll())
        finally:
            managed.terminate()
            managed.close()

    @unittest.skipUnless(Path("/proc/self/status").is_file(), "Linux proc status only")
    def test_unix_exec_helper_restores_popen_signal_defaults(self):
        managed = process_control.spawn_managed(
            ["/bin/sh", "-c", "grep '^SigIgn:' /proc/self/status"],
            memory_limit_mb=256,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = managed.proc.communicate(timeout=10)
        finally:
            managed.close()
        self.assertEqual(managed.proc.returncode, 0, stderr)
        ignored = int(stdout.split()[-1], 16)
        for name in ("SIGPIPE", "SIGXFSZ"):
            signum = getattr(signal, name, None)
            if signum is not None:
                self.assertEqual(ignored & (1 << (signum - 1)), 0, name)

    @unittest.skipUnless(Path("/proc/self/status").is_file(), "Linux proc status only")
    def test_unix_exec_helper_can_preserve_ignored_signals(self):
        managed = process_control.spawn_managed(
            ["/bin/sh", "-c", "grep '^SigIgn:' /proc/self/status"],
            memory_limit_mb=256,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            restore_signals=False,
        )
        try:
            stdout, stderr = managed.proc.communicate(timeout=10)
        finally:
            managed.close()
        self.assertEqual(managed.proc.returncode, 0, stderr)
        ignored = int(stdout.split()[-1], 16)
        sigpipe = getattr(signal, "SIGPIPE", None)
        if sigpipe is not None:
            self.assertNotEqual(ignored & (1 << (sigpipe - 1)), 0)

    @unittest.skipIf(platform.system() == "Windows", "Unix pass_fds only")
    def test_unix_exec_helper_preserves_caller_pass_fds(self):
        read_fd, write_fd = os.pipe()
        try:
            managed = process_control.spawn_managed(
                [
                    sys.executable,
                    "-c",
                    f"import os; os.write({write_fd}, b'caller-fd')",
                ],
                memory_limit_mb=256,
                pass_fds=(write_fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            os.close(write_fd)
            write_fd = None
            try:
                _, stderr = managed.proc.communicate(timeout=10)
            finally:
                managed.close()
            self.assertEqual(managed.proc.returncode, 0, stderr)
            self.assertEqual(os.read(read_fd, 64), b"caller-fd")
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    @unittest.skipIf(platform.system() == "Windows", "Unix selector only")
    def test_unix_exec_status_supports_high_numbered_fd(self):
        try:
            import fcntl
            import resource
        except ImportError as exc:
            self.skipTest(str(exc))
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit <= 2048:
            self.skipTest("RLIMIT_NOFILE is too low for a high-fd regression")

        read_fd, write_fd = os.pipe()
        high_fd = None
        try:
            high_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD, 2048)
            os.close(read_fd)
            read_fd = None
            os.write(write_fd, process_control.UNIX_EXEC_READY)
            os.close(write_fd)
            write_fd = None
            process_control._await_unix_exec(mock.Mock(), high_fd)
        finally:
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            if high_fd is not None:
                os.close(high_fd)

    @unittest.skipIf(platform.system() == "Windows", "Unix selector only")
    def test_unix_exec_empty_status_is_fail_closed(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        proc = mock.Mock()
        with mock.patch.object(process_control, "_terminate_failed_unix_start") as terminate:
            with self.assertRaisesRegex(OSError, "did not report startup readiness"):
                process_control._await_unix_exec(proc, read_fd)
        os.close(read_fd)
        terminate.assert_called_once_with(proc)

    @unittest.skipIf(platform.system() == "Windows", "Unix exec helper only")
    def test_unix_concurrent_threaded_launches_never_use_preexec_fn(self):
        real_popen = subprocess.Popen
        calls = []
        calls_lock = threading.Lock()

        def recording_popen(command, **kwargs):
            with calls_lock:
                calls.append(dict(kwargs))
            return real_popen(command, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def run_one(index):
                case_dir = root / str(index)
                case_dir.mkdir()
                return run_managed_to_files(
                    [sys.executable, "-c", f"print({index})"],
                    stdout_path=case_dir / "stdout.txt",
                    stderr_path=case_dir / "stderr.txt",
                    timeout=10,
                    memory_limit_mb=256,
                    output_limit_bytes=1024,
                    process_limit=8,
                    cwd=case_dir,
                )

            with mock.patch.object(
                process_control.subprocess,
                "Popen",
                side_effect=recording_popen,
            ):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(run_one, range(16)))

        self.assertTrue(all(item["reason"] == "completed" for item in results), results)
        self.assertEqual(len(calls), 16)
        self.assertTrue(all("preexec_fn" not in item for item in calls), calls)
        self.assertTrue(all(item.get("start_new_session") is True for item in calls), calls)

    def test_windows_job_termination_waits_for_all_processes_before_closing(self):
        class Accounting(ctypes.Structure):
            _fields_ = [("ActiveProcesses", ctypes.c_uint32)]

        class Kernel32:
            def __init__(self):
                self.active = iter((2, 1, 0))
                self.calls = []

            def TerminateJobObject(self, handle, exit_code):
                self.calls.append(("terminate", handle, exit_code))
                return True

            def QueryInformationJobObject(self, handle, info_class, pointer, size, returned):
                active = next(self.active)
                ctypes.cast(pointer, ctypes.POINTER(Accounting)).contents.ActiveProcesses = active
                self.calls.append(("query", active))
                return True

            def CloseHandle(self, handle):
                self.calls.append(("close", handle))
                return True

        kernel32 = Kernel32()
        fake_api = {
            "ctypes": ctypes,
            "kernel32": kernel32,
            "accounting_type": Accounting,
        }
        with mock.patch.object(process_control, "_WINDOWS_API", fake_api):
            process_control.terminate_windows_job(123, timeout=0.2)

        self.assertEqual(
            kernel32.calls,
            [
                ("terminate", 123, 1),
                ("query", 2),
                ("query", 1),
                ("query", 0),
                ("close", 123),
            ],
        )

    def test_windows_resume_process_resumes_and_closes_handles(self):
        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ThreadID", ctypes.c_uint32),
                ("th32OwnerProcessID", ctypes.c_uint32),
                ("tpBasePri", ctypes.c_int32),
                ("tpDeltaPri", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
            ]

        class Kernel32:
            def __init__(self):
                self.threads = iter(((11, 999), (22, 1234)))
                self.calls = []

            def CreateToolhelp32Snapshot(self, flags, pid):
                self.calls.append(("snapshot", flags, pid))
                return 777

            def _next_entry(self, pointer):
                try:
                    tid, owner = next(self.threads)
                except StopIteration:
                    return False
                entry = ctypes.cast(pointer, ctypes.POINTER(ThreadEntry)).contents
                entry.th32ThreadID = tid
                entry.th32OwnerProcessID = owner
                return True

            def Thread32First(self, snapshot, pointer):
                return self._next_entry(pointer)

            def Thread32Next(self, snapshot, pointer):
                return self._next_entry(pointer)

            def OpenThread(self, access, inherit, tid):
                self.calls.append(("open", access, inherit, tid))
                return 555

            def ResumeThread(self, handle):
                self.calls.append(("resume", handle))
                return 1

            def CloseHandle(self, handle):
                self.calls.append(("close", handle))
                return True

        kernel32 = Kernel32()
        fake_api = {
            "ctypes": ctypes,
            "kernel32": kernel32,
            "thread_entry_type": ThreadEntry,
        }
        with mock.patch.object(process_control, "_WINDOWS_API", fake_api):
            self.assertTrue(process_control.windows_resume_process(1234))

        self.assertEqual(
            kernel32.calls,
            [
                ("snapshot", 0x00000004, 0),
                ("open", 0x0002, False, 22),
                ("resume", 555),
                ("close", 555),
                ("close", 777),
            ],
        )

    def test_windows_resume_process_snapshot_failure_returns_false(self):
        class Kernel32:
            def __init__(self):
                self.calls = []

            def CreateToolhelp32Snapshot(self, flags, pid):
                self.calls.append(("snapshot", flags, pid))
                return ctypes.c_void_p(-1).value

            def CloseHandle(self, handle):
                self.calls.append(("close", handle))
                return True

        kernel32 = Kernel32()
        fake_api = {
            "ctypes": ctypes,
            "kernel32": kernel32,
            "thread_entry_type": None,
        }
        with mock.patch.object(process_control, "_WINDOWS_API", fake_api):
            self.assertFalse(process_control.windows_resume_process(1234))

        self.assertEqual(kernel32.calls, [("snapshot", 0x00000004, 0)])

    @unittest.skipUnless(platform.system() == "Windows", "Windows job containment only")
    def test_windows_spawn_resume_failure_kills_process(self):
        procs = []
        real_assign = process_control.windows_assign_job

        def record_assign(proc, memory_limit_mb=None, process_limit=None):
            procs.append(proc)
            return real_assign(proc, memory_limit_mb, process_limit)

        with (
            mock.patch.object(process_control, "windows_assign_job", side_effect=record_assign),
            mock.patch.object(process_control, "windows_resume_process", return_value=False),
        ):
            with self.assertRaises(OSError):
                process_control.spawn_managed(
                    [sys.executable, "-c", "print('x')"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        self.assertEqual(len(procs), 1)
        self.wait_until_reaped(procs[0])

    @unittest.skipUnless(platform.system() == "Windows", "Windows job containment only")
    def test_windows_containment_failure_kills_suspended_process(self):
        procs = []

        def fail_assign(proc, memory_limit_mb=None, process_limit=None):
            procs.append(proc)
            return None

        with mock.patch.object(process_control, "windows_assign_job", side_effect=fail_assign):
            with self.assertRaises(OSError):
                process_control.spawn_managed(
                    [sys.executable, "-c", "print('x')"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        self.assertEqual(len(procs), 1)
        self.wait_until_reaped(procs[0])

    @unittest.skipUnless(platform.system() == "Windows", "Windows-specific liveness semantics")
    def test_process_alive_reports_exited_child_dead_while_handle_is_held(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('x')"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=10)
            # The Popen object still holds the process handle, which keeps the
            # PID reserved; a correct implementation must still report it dead.
            self.assertFalse(process_alive(proc.pid))
        finally:
            proc.kill()

    @unittest.skipUnless(platform.system() == "Windows", "Windows-specific liveness semantics")
    def test_process_alive_reports_exit_code_259_child_dead(self):
        # 259 == STILL_ACTIVE: GetExitCodeProcess alone cannot tell this exit
        # apart from a running process; the signalled handle must disambiguate.
        proc = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(259)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=10)
            self.assertFalse(process_alive(proc.pid))
        finally:
            proc.kill()

    @unittest.skipUnless(platform.system() == "Windows", "Windows job containment only")
    def test_windows_process_cannot_spawn_child_before_job_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child_pid_file = root / "child.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable, '-c', {child!r}]);"
                "time.sleep(30)"
            )
            original_assign = process_control.windows_assign_job
            child_existed_at_assignment = []

            def inspect_then_assign(proc, memory_limit_mb=None, process_limit=None):
                child_existed_at_assignment.append(child_pid_file.exists())
                return original_assign(proc, memory_limit_mb, process_limit)

            with mock.patch.object(
                process_control,
                "windows_assign_job",
                side_effect=inspect_then_assign,
            ):
                managed = process_control.spawn_managed(
                    [sys.executable, "-c", parent],
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not child_pid_file.is_file():
                    time.sleep(0.05)
                self.assertTrue(child_pid_file.is_file(), "child process did not start after resume")
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            finally:
                managed.terminate()

            self.assertEqual(child_existed_at_assignment, [False])
            self.wait_until_dead(child_pid)

    def run_command(self, root, code, **limits):
        root = Path(root)
        return run_managed_to_files(
            [sys.executable, "-c", code],
            stdout_path=root / "stdout.txt",
            stderr_path=root / "stderr.txt",
            timeout=limits.get("timeout", 5),
            memory_limit_mb=limits.get("memory_limit_mb", 256),
            output_limit_bytes=limits.get("output_limit_bytes", 1024 * 1024),
            process_limit=limits.get("process_limit", 8),
            cwd=root,
        )

    def wait_until_dead(self, pid):
        deadline = time.time() + 5
        while process_alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_alive(pid), f"child process {pid} survived")

    def wait_until_reaped(self, proc):
        # Holding the Popen reference pins the PID (no reuse) and, with the
        # exit-code-aware process_alive, makes the death check deterministic.
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(proc.poll(), f"child process {proc.pid} survived")
        self.assertFalse(process_alive(proc.pid), f"exited child {proc.pid} still reported alive")

    def test_fast_output_flood_is_ole_and_capture_is_truncated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_command(
                root,
                "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()",
                output_limit_bytes=1024,
            )
            self.assertEqual(result["reason"], "output_limit", result)
            self.assertGreater(result["output_bytes"], 1024)
            self.assertLessEqual((root / "stdout.txt").stat().st_size, 1024)

    def test_fast_dual_stream_flood_shares_one_output_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_command(
                root,
                "import sys; "
                "sys.stdout.buffer.write(b'o' * 100000); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'e' * 100000); sys.stderr.flush()",
                output_limit_bytes=1025,
            )
            stdout_size = (root / "stdout.txt").stat().st_size
            stderr_size = (root / "stderr.txt").stat().st_size
            self.assertEqual(result["reason"], "output_limit", result)
            self.assertGreaterEqual(result["output_bytes"], 200000)
            self.assertEqual((stdout_size, stderr_size), (513, 512))
            self.assertEqual(result["retained_output_bytes"], 1025)
            self.assertEqual(result["stdout_retained_bytes"], 513)
            self.assertEqual(result["stderr_retained_bytes"], 512)
            self.assertTrue(result["output_truncated"])

    def test_terminal_race_output_is_truncated_without_overriding_timeout_or_cancel(self):
        class FakeProcess:
            returncode = None

            def poll(self):
                return None

        class FakeManaged:
            memory_limit_mb = None
            process_limit = None
            memory_enforced = False
            process_limit_enforced = False
            peak_memory_mb = None

            def __init__(self, paths):
                self.proc = FakeProcess()
                self.paths = paths

            def sample(self):
                return None, None

            def terminate(self):
                self.paths[0].write_bytes(b'o' * 1000)
                self.paths[1].write_bytes(b'e' * 1000)
                self.proc.returncode = 1

        for cancelled, expected_reason in ((False, "time_limit"), (True, "cancelled")):
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = (root / "stdout.txt", root / "stderr.txt")
                for path in paths:
                    path.write_bytes(b"")
                with mock.patch.object(
                    process_control,
                    "cancellation_requested",
                    return_value=cancelled,
                ):
                    result = wait_managed(
                        FakeManaged(paths),
                        0,
                        output_paths=paths,
                        output_limit_bytes=1024,
                    )
                self.assertEqual(result["reason"], expected_reason, result)
                self.assertEqual(result["output_bytes"], 2000)
                self.assertEqual(sum(path.stat().st_size for path in paths), 1024)
                self.assertTrue(result["output_truncated"])

    def test_managed_quick_process_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_command(root, "print('ok')")
            self.assertEqual(result["reason"], "completed", result)
            self.assertEqual(result["returncode"], 0, result)
            self.assertEqual((root / "stdout.txt").read_text(encoding="utf-8").strip(), "ok")

    def test_timeout_kills_spawned_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "child.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "time.sleep(0.2);"
                f"subprocess.Popen([sys.executable, '-c', {child!r}]);"
                "time.sleep(30)"
            )
            result = self.run_command(root, parent, timeout=0.8)
            self.assertEqual(result["reason"], "time_limit", result)
            self.assertTrue(pid_file.is_file(), "child did not start")
            self.wait_until_dead(int(pid_file.read_text(encoding="utf-8")))

    def test_timeout_kills_immediately_spawned_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "child.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable, '-c', {child!r}]);"
                "time.sleep(30)"
            )
            result = self.run_command(root, parent, timeout=0.8)
            self.assertEqual(result["reason"], "time_limit", result)
            self.assertTrue(pid_file.is_file(), "child did not start")
            self.wait_until_dead(int(pid_file.read_text(encoding="utf-8")))

    def test_normal_parent_exit_still_cleans_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "child.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "time.sleep(0.2);"
                f"subprocess.Popen([sys.executable, '-c', {child!r}]);"
                "time.sleep(0.2)"
            )
            result = self.run_command(root, parent, timeout=5)
            self.assertEqual(result["reason"], "completed", result)
            self.assertEqual(result["returncode"], 0, result)
            self.assertTrue(pid_file.is_file(), "child did not start")
            self.wait_until_dead(int(pid_file.read_text(encoding="utf-8")))


    def test_cancel_file_stops_managed_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "cancelled.pid"
            cancel_file = root / "cancel.requested"
            code = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            previous = os.environ.get(CANCEL_FILE_ENV)
            os.environ[CANCEL_FILE_ENV] = str(cancel_file)
            started_pid = []

            def cancel_after_process_start():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        started_pid.append(int(pid_file.read_text(encoding="utf-8")))
                        break
                    except (FileNotFoundError, OSError, UnicodeError, ValueError):
                        time.sleep(0.02)
                # Always unblock the managed wait. If startup genuinely failed,
                # the assertion below still reports that instead of hanging.
                cancel_file.touch()

            canceller = threading.Thread(target=cancel_after_process_start)
            canceller.start()
            try:
                with self.assertRaises(ProcessCancelled):
                    self.run_command(root, code, timeout=10)
            finally:
                canceller.join()
                if previous is None:
                    os.environ.pop(CANCEL_FILE_ENV, None)
                else:
                    os.environ[CANCEL_FILE_ENV] = previous
            self.assertTrue(started_pid, "managed process did not start")
            self.wait_until_dead(started_pid[0])

    def test_external_force_cancel_kills_detached_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "detached.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            if os.name == "nt":
                parent = (
                    "import subprocess,sys,time;"
                    f"subprocess.Popen([sys.executable, '-c', {child!r}], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP);"
                    "time.sleep(30)"
                )
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            else:
                parent = (
                    "import subprocess,sys,time;"
                    f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True);"
                    "time.sleep(30)"
                )
                options = {"start_new_session": True}
            proc = subprocess.Popen(
                [sys.executable, "-c", parent],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **options,
            )
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not pid_file.is_file():
                    time.sleep(0.05)
                self.assertTrue(pid_file.is_file(), "detached child did not start")
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                known_pids = snapshot_process_tree(proc.pid)
                terminate_external_process_tree(proc, known_pids)
                self.wait_until_dead(child_pid)
            finally:
                if proc.poll() is None:
                    terminate_external_process_tree(proc)


if __name__ == "__main__":
    unittest.main()
