import ctypes
import os
import platform
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import probhub.process_control as process_control
from probhub.process_control import (
    CANCEL_FILE_ENV,
    ProcessCancelled,
    process_alive,
    run_managed_to_files,
    snapshot_process_tree,
    terminate_external_process_tree,
)


class ProcessControlTests(unittest.TestCase):
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
