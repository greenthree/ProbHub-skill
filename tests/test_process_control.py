import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from probhub.process_control import process_alive, run_managed_to_files


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


if __name__ == "__main__":
    unittest.main()
