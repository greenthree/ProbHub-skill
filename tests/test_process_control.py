import os
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from probhub.process_control import (
    CANCEL_FILE_ENV,
    ProcessCancelled,
    process_alive,
    run_managed_to_files,
    snapshot_process_tree,
    terminate_external_process_tree,
)


class ProcessControlTests(unittest.TestCase):
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

    def test_fast_output_flood_is_ole_and_capture_is_truncated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_command(
                root,
                "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()",
                output_limit_bytes=1024,
            )
            self.assertEqual(result["reason"], "output_limit", result)
            self.assertLessEqual((root / "stdout.txt").stat().st_size, 1024)

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
            timer = threading.Timer(0.3, lambda: cancel_file.touch())
            timer.start()
            try:
                with self.assertRaises(ProcessCancelled):
                    self.run_command(root, code, timeout=10)
            finally:
                timer.cancel()
                timer.join()
                if previous is None:
                    os.environ.pop(CANCEL_FILE_ENV, None)
                else:
                    os.environ[CANCEL_FILE_ENV] = previous
            self.assertTrue(pid_file.is_file(), "managed process did not start")
            self.wait_until_dead(int(pid_file.read_text(encoding="utf-8")))

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
