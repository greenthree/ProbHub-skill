import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import yaml

from probhub.process_control import process_alive
from probhub.submissions import (
    MAX_SOURCE_BYTES,
    cleanup_stale_submission_workspaces,
    temporary_submission_workspace,
    validate_cpp_upload,
)


ROOT = Path(__file__).resolve().parents[1]
LOCAL_JUDGE = ROOT / "scripts" / "local_judge.py"


def code_tree_hash(problem):
    digest = hashlib.sha256()
    for path in sorted((problem / "code").rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(problem).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


class SubmissionWorkspaceTests(unittest.TestCase):
    def write_problem(self, root, judge=None):
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data" / "sample").mkdir(parents=True)
        (problem / "data" / "secret").mkdir(parents=True)
        (problem / "data" / "sample" / "1.in").write_text("1\n", encoding="utf-8")
        (problem / "data" / "sample" / "1.ans").write_text("1\n", encoding="utf-8")
        (problem / "code" / "std.cpp").write_text("// original accepted\n", encoding="utf-8")
        (problem / "code" / "checker.cpp").write_text("// checker\n", encoding="utf-8")
        (problem / "code" / "old.exe").write_bytes(b"do not copy")
        config = {
            "schema_version": 1,
            "id": "A",
            "limits": {"time": 1, "memory": 256, "output": 4, "processes": 8},
            "judge": judge or {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"]},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        }
        (problem / "probhub.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return problem

    def test_upload_validation(self):
        self.assertIn("main", validate_cpp_upload("answer.cpp", b"int main(){}"))
        for filename in ("answer.c", "../answer.cpp", "dir\\answer.cpp"):
            with self.assertRaises(ValueError):
                validate_cpp_upload(filename, b"int main(){}")
        with self.assertRaises(ValueError):
            validate_cpp_upload("answer.cpp", b"")
        with self.assertRaises(ValueError):
            validate_cpp_upload("answer.cpp", b"x" * (MAX_SOURCE_BYTES + 1))
        with self.assertRaises(ValueError):
            validate_cpp_upload("answer.cpp", b"\xff")

    def test_workspace_is_isolated_and_cleaned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.write_problem(
                root,
                {"type": "custom", "validator": "code/validator.cpp", "checker": "code/checker.cpp"},
            )
            before = code_tree_hash(problem)
            task_id = uuid.uuid4().hex
            with temporary_submission_workspace(
                root, problem, task_id, "answer.cpp", b"int main(){return 0;}\n"
            ) as prepared:
                self.assertTrue(prepared.source_path.is_file())
                self.assertTrue((prepared.problem_dir / "code" / "checker.cpp").is_file())
                self.assertFalse((prepared.problem_dir / "code" / "old.exe").exists())
                config = yaml.safe_load((prepared.problem_dir / "probhub.yaml").read_text(encoding="utf-8"))
                self.assertNotIn("validator", config["judge"])
                self.assertEqual(config["solutions"]["accepted"][0]["file"], "code/submission.cpp")
                self.assertTrue(Path(config["data"]["sample_dir"]).is_absolute())
                self.assertEqual(code_tree_hash(problem), before)
                self.assertFalse((problem / "code" / "submission.cpp").exists())
            self.assertFalse((root / ".probhub" / "submissions" / task_id).exists())
            self.assertEqual(code_tree_hash(problem), before)

    def test_stale_cleanup_only_removes_safe_expired_task_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submissions = root / ".probhub" / "submissions"
            submissions.mkdir(parents=True)
            now = time.time()
            old_id = uuid.uuid4().hex
            protected_id = uuid.uuid4().hex
            fresh_id = uuid.uuid4().hex
            unknown = submissions / "manual-notes"
            for name in (old_id, protected_id, fresh_id):
                (submissions / name).mkdir()
            unknown.mkdir()
            for name in (old_id, protected_id):
                os.utime(submissions / name, (now - 7200, now - 7200))
            os.utime(submissions / fresh_id, (now - 10, now - 10))
            os.utime(unknown, (now - 7200, now - 7200))

            removed = cleanup_stale_submission_workspaces(
                root, max_age_seconds=3600, now=now, protected_task_ids={protected_id}
            )

            self.assertEqual(removed, [old_id])
            self.assertFalse((submissions / old_id).exists())
            self.assertTrue((submissions / protected_id).is_dir())
            self.assertTrue((submissions / fresh_id).is_dir())
            self.assertTrue(unknown.is_dir())

    def test_submission_workspace_rejects_code_tree_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.write_problem(root)
            target = root / "outside.cpp"
            target.write_text("int main(){}\n", encoding="utf-8")
            link = problem / "code" / "linked.cpp"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                with temporary_submission_workspace(
                    root, problem, uuid.uuid4().hex, "answer.cpp", b"int main(){}\n"
                ):
                    pass

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for submission integration tests")
    def test_only_uploaded_solution_is_judged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.write_problem(root)
            # The original accepted source would print the correct answer. The
            # uploaded source is deliberately wrong, so an AC would prove that
            # the evaluator accidentally used the original code/ file.
            (problem / "code" / "std.cpp").write_text(
                "#include <iostream>\nint main(){std::cout << 1 << '\\n';}\n", encoding="utf-8"
            )
            before = code_tree_hash(problem)
            wrong = b"#include <iostream>\nint main(){std::cout << 2 << '\\n';}\n"
            with temporary_submission_workspace(root, problem, uuid.uuid4().hex, "wrong.cpp", wrong) as prepared:
                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                completed = subprocess.run(
                    [sys.executable, str(LOCAL_JUDGE), str(prepared.problem_dir), "--jsonl", "--no-cache"],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
                events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
                cases = [event for event in events if event.get("type") == "case"]
                self.assertEqual([event["status"] for event in cases], ["WA"])
                self.assertTrue(any(event.get("kind") == "std" and event.get("ok") for event in events if event.get("type") == "compile"))
            self.assertEqual(code_tree_hash(problem), before)
            self.assertFalse((problem / "code" / "submission.exe").exists())


class SubmissionUiApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("submission_ui", ROOT / "scripts" / "ui.py")
        cls.ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ui)
        cls.client = cls.ui.app.test_client()

    def setUp(self):
        with self.ui.SUBMISSION_LOCK:
            self.ui.SUBMISSION_JOBS.clear()
            self.ui.SUBMISSION_PROCESSES.clear()
            self.ui.SUBMISSION_CANCEL_FILES.clear()

    def _problem_info(self, problem, time_limit=1):
        return {
            "name": "A", "matched": True, "runnable": True, "dir": str(problem),
            "limits": {"time": time_limit, "memory": 256}, "data_count": 1,
            "sample_count": 1, "secret_count": 0, "cases": ["sample/1"],
            "files": {"validator": False, "std": ["code/std.cpp"], "brute": [], "wrong": [], "other": []},
        }

    def _wait_for_job(self, job_id, timeout=30):
        deadline = time.time() + timeout
        payload = None
        while time.time() < deadline:
            payload = self.client.get(f"/api/submission/job/{job_id}").get_json()
            if payload["status"] not in {"queued", "running", "cancelling"}:
                return payload
            time.sleep(0.05)
        self.fail(f"submission job did not finish: {payload}")

    def test_route_rejects_non_cpp_and_traversal_filenames(self):
        for filename in ("answer.txt", "../answer.cpp"):
            response = self.client.post(
                "/api/submission/run",
                data={
                    "subtitle": "contest",
                    "index": "0",
                    "source": (io.BytesIO(b"int main(){}"), filename),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()["success"])

    def test_verdict_distinguishes_compile_error_and_runtime_results(self):
        self.assertEqual(self.ui._submission_verdict({"compiles": [{"kind": "std", "ok": False}]}), "CE")
        self.assertEqual(self.ui._submission_verdict({"compiles": [{"kind": "std", "ok": True}], "cases": [{"status": "AC"}]}), "AC")
        self.assertEqual(self.ui._submission_verdict({"compiles": [{"kind": "std", "ok": True}], "cases": [{"status": "OLE"}]}), "OLE")

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for submission API integration tests")
    def test_api_judges_upload_and_cleans_workspace(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            (problem / "data" / "sample").mkdir(parents=True)
            (problem / "data" / "secret").mkdir(parents=True)
            (problem / "data" / "sample" / "1.in").write_text("1\n", encoding="utf-8")
            (problem / "data" / "sample" / "1.ans").write_text("1\n", encoding="utf-8")
            (problem / "code" / "std.cpp").write_text("// must remain untouched\n", encoding="utf-8")
            (problem / "probhub.yaml").write_text(
                yaml.safe_dump({
                    "schema_version": 1,
                    "id": "A",
                    "limits": {"time": 1, "memory": 256, "output": 4, "processes": 8},
                    "judge": {"type": "standard"},
                    "solutions": {"accepted": ["code/std.cpp"]},
                    "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
                }, sort_keys=False),
                encoding="utf-8",
            )
            before = code_tree_hash(problem)
            info = self._problem_info(problem)
            wrong = b"#include <iostream>\nint main(){std::cout << 2 << '\\n';}\n"
            with mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info):
                response = self.client.post(
                    "/api/submission/run",
                    data={"subtitle": "contest", "index": "0", "source": (io.BytesIO(wrong), "wrong.cpp")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                job_id = response.get_json()["job_id"]
                payload = self._wait_for_job(job_id)
                self.assertEqual(payload["status"], "completed", payload)
                self.assertEqual(payload["verdict"], "WA")
                self.assertTrue(payload["result"]["submission"]["workspace_cleaned"])
                self.assertEqual(payload["result"]["cases"][0]["status"], "WA")
            self.assertEqual(code_tree_hash(problem), before)
            self.assertFalse((problem / "code" / "submission.cpp").exists())
            self.assertFalse((ROOT / ".probhub" / "submissions" / job_id).exists())


    def test_queued_submission_can_be_cancelled_without_creating_workspace(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            (problem / "data" / "sample").mkdir(parents=True)
            (problem / "data" / "secret").mkdir(parents=True)
            (problem / "data" / "sample" / "1.in").write_text("1\n", encoding="utf-8")
            (problem / "data" / "sample" / "1.ans").write_text("1\n", encoding="utf-8")
            (problem / "code" / "std.cpp").write_text("// untouched\n", encoding="utf-8")
            (problem / "probhub.yaml").write_text(
                yaml.safe_dump({
                    "schema_version": 1,
                    "id": "A",
                    "limits": {"time": 30, "memory": 256, "output": 4, "processes": 8},
                    "judge": {"type": "standard"},
                    "solutions": {"accepted": ["code/std.cpp"]},
                    "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
                }, sort_keys=False),
                encoding="utf-8",
            )
            original_slots = self.ui.SUBMISSION_SLOTS
            blocked_slots = threading.BoundedSemaphore(1)
            blocked_slots.acquire()
            self.ui.SUBMISSION_SLOTS = blocked_slots
            try:
                with mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)):
                    response = self.client.post(
                        "/api/submission/run",
                        data={"subtitle": "contest", "index": "0", "source": (io.BytesIO(b"int main(){}\n"), "queued.cpp")},
                        content_type="multipart/form-data",
                    )
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    job_id = response.get_json()["job_id"]
                    cancel = self.client.post(f"/api/submission/job/{job_id}/cancel")
                    self.assertEqual(cancel.status_code, 200)
                    self.assertEqual(cancel.get_json()["status"], "cancelling")
                    blocked_slots.release()
                    payload = self._wait_for_job(job_id, timeout=10)
            finally:
                self.ui.SUBMISSION_SLOTS = original_slots
                try:
                    blocked_slots.release()
                except ValueError:
                    pass
            self.assertEqual(payload["status"], "cancelled", payload)
            self.assertEqual(payload["verdict"], "CANCELLED")
            self.assertFalse((ROOT / ".probhub" / "submissions" / job_id).exists())

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for cancellation integration tests")
    def test_running_submission_cancel_kills_program_and_cleans_workspace(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            (problem / "data" / "sample").mkdir(parents=True)
            (problem / "data" / "secret").mkdir(parents=True)
            (problem / "data" / "sample" / "1.in").write_text("1\n", encoding="utf-8")
            (problem / "data" / "sample" / "1.ans").write_text("1\n", encoding="utf-8")
            (problem / "code" / "std.cpp").write_text("// untouched\n", encoding="utf-8")
            (problem / "probhub.yaml").write_text(
                yaml.safe_dump({
                    "schema_version": 1,
                    "id": "A",
                    "limits": {"time": 30, "memory": 256, "output": 4, "processes": 8},
                    "judge": {"type": "standard"},
                    "solutions": {"accepted": ["code/std.cpp"]},
                    "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
                }, sort_keys=False),
                encoding="utf-8",
            )
            before = code_tree_hash(problem)
            pid_file = Path(temp) / "submission.pid"
            source = f'''#include <fstream>
#ifdef _WIN32
#include <process.h>
#define PROBHUB_GETPID _getpid
#else
#include <unistd.h>
#define PROBHUB_GETPID getpid
#endif
int main() {{
    std::ofstream("{pid_file.as_posix()}") << PROBHUB_GETPID() << std::flush;
    while (true) {{}}
}}
'''.encode("utf-8")
            with mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)):
                response = self.client.post(
                    "/api/submission/run",
                    data={"subtitle": "contest", "index": "0", "source": (io.BytesIO(source), "loop.cpp")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                job_id = response.get_json()["job_id"]
                deadline = time.time() + 30
                while time.time() < deadline and not pid_file.is_file():
                    time.sleep(0.05)
                self.assertTrue(pid_file.is_file(), "uploaded program did not start")
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                cancel = self.client.post(f"/api/submission/job/{job_id}/cancel")
                self.assertEqual(cancel.status_code, 200)
                payload = self._wait_for_job(job_id, timeout=15)
            deadline = time.time() + 5
            while process_alive(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(payload["status"], "cancelled", payload)
            self.assertEqual(payload["verdict"], "CANCELLED")
            self.assertTrue(payload["result"]["submission"]["workspace_cleaned"])
            self.assertFalse(process_alive(child_pid), f"submission process {child_pid} survived cancellation")
            self.assertEqual(code_tree_hash(problem), before)
            self.assertFalse((ROOT / ".probhub" / "submissions" / job_id).exists())


if __name__ == "__main__":
    unittest.main()
