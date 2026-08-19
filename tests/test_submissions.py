import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import yaml

from probhub.process_control import process_alive
from probhub.submissions import (
    MAX_SOURCE_BYTES,
    SubmissionPreparationCancelled,
    SubmissionPreparationDeadlineExceeded,
    cleanup_stale_submission_workspaces,
    temporary_submission_workspace,
    validate_cpp_upload,
)
from probhub.webui_tasks import BoundedTaskExecutor


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
        with self.assertRaisesRegex(ValueError, "filename exceeds"):
            validate_cpp_upload("x" * 252 + ".cpp", b"int main(){}")

    def test_workspace_preparation_honors_cancel_and_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.write_problem(root)
            for exc, options in (
                (SubmissionPreparationCancelled, {"cancel_requested": lambda: True}),
                (SubmissionPreparationDeadlineExceeded, {"deadline_monotonic": time.monotonic() - 1}),
            ):
                task_id = uuid.uuid4().hex
                with self.assertRaises(exc):
                    with temporary_submission_workspace(
                        root,
                        problem,
                        task_id,
                        "answer.cpp",
                        b"int main(){}\n",
                        **options,
                    ):
                        self.fail("cancelled preparation must not yield a workspace")
                self.assertFalse((root / ".probhub" / "submissions" / task_id).exists())

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
        cls.client.environ_base["HTTP_X_PROBHUB_CSRF"] = cls.ui.WEBUI_CSRF_TOKEN

    @classmethod
    def tearDownClass(cls):
        cls.ui.shutdown_webui_tasks()

    def setUp(self):
        deadline = time.time() + 5
        while self.ui.WEBUI_TASK_EXECUTOR.stats()["outstanding"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.ui.WEBUI_TASK_EXECUTOR.stats()["outstanding"], 0)
        with self.ui.SANDBOX_LOCK:
            self.ui.SANDBOX_JOBS.clear()
            self.ui.SANDBOX_PROCESSES.clear()
            self.ui.SANDBOX_CANCEL_FILES.clear()
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

    def _wait_for_sandbox_job(self, job_id, timeout=30):
        deadline = time.time() + timeout
        payload = None
        while time.time() < deadline:
            payload = self.client.get(f"/api/sandbox/job/{job_id}").get_json()
            if payload["status"] not in {"queued", "running", "cancelling"}:
                return payload
            time.sleep(0.05)
        self.fail(f"sandbox job did not finish: {payload}")

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

    def test_jsonl_details_and_visible_log_have_hard_limits(self):
        info = self._problem_info(Path("A"))
        result = self.ui._empty_sandbox_result(info)
        log = self.ui.BoundedUtf8Log(96)
        state = {"buffer": b"", "event_bytes": 0}
        events = b"".join(
            json.dumps({
                "type": "case",
                "kind": "std",
                "program": "answer.cpp",
                "case": f"secret/{index}",
                "status": "AC",
                "message": "x" * 40,
            }).encode("utf-8") + b"\n"
            for index in range(20)
        )
        with mock.patch.object(self.ui, "WEBUI_TASK_EVENT_BYTES", 256):
            self.ui._consume_jsonl_chunk(result, log, state, events, final=True)
        self.assertTrue(result["details_truncated"])
        self.assertLess(len(result["cases"]), 20)
        self.assertTrue(log.truncated)
        self.assertLessEqual(log.size_bytes, 96)

    def test_oversized_final_event_is_replaced_by_bounded_failure(self):
        info = self._problem_info(Path("A"))
        result = self.ui._empty_sandbox_result(info)
        log = self.ui.BoundedUtf8Log(256)
        state = {"buffer": b"", "event_bytes": 0}
        event = json.dumps({
            "type": "final",
            "ok": True,
            "message": "x" * 1024,
        }).encode("utf-8") + b"\n"
        with mock.patch.object(self.ui, "WEBUI_TASK_FINAL_EVENT_BYTES", 128):
            self.ui._consume_jsonl_chunk(result, log, state, event, final=True)
        self.assertTrue(result["details_truncated"])
        self.assertEqual(result["final"]["code"], "task_event_limit")
        self.assertLessEqual(log.size_bytes, 256)

    def test_protocol_error_is_sticky_across_later_final_events(self):
        info = self._problem_info(Path("A"))
        result = self.ui._empty_sandbox_result(info)
        log = self.ui.BoundedUtf8Log(512)
        state = {"buffer": b"", "event_bytes": 0, "final_seen": False, "protocol_error": None}
        oversized = json.dumps({
            "type": "final",
            "ok": True,
            "message": "x" * 1024,
        }).encode("utf-8") + b"\n"
        valid = json.dumps({
            "type": "final",
            "ok": True,
            "status": "passed",
            "code": "all_expectations_met",
        }).encode("utf-8") + b"\n"
        with mock.patch.object(self.ui, "WEBUI_TASK_FINAL_EVENT_BYTES", 128):
            self.ui._consume_jsonl_chunk(result, log, state, oversized + valid, final=True)
        self.assertEqual(state["protocol_error"], "task_event_limit")
        self.assertEqual(result["final"]["code"], "task_event_limit")
        self.assertFalse(result["final"]["ok"])

    def test_duplicate_final_event_is_a_sticky_protocol_error(self):
        info = self._problem_info(Path("A"))
        result = self.ui._empty_sandbox_result(info)
        log = self.ui.BoundedUtf8Log(512)
        state = {"buffer": b"", "event_bytes": 0, "final_seen": False, "protocol_error": None}
        valid = json.dumps({
            "type": "final",
            "ok": True,
            "status": "passed",
            "code": "all_expectations_met",
        }).encode("utf-8") + b"\n"
        self.ui._consume_jsonl_chunk(result, log, state, valid + valid, final=True)
        self.assertEqual(state["protocol_error"], "duplicate_final_event")
        self.assertEqual(result["final"]["code"], "judge_protocol_error")
        self.assertFalse(result["final"]["ok"])

    def test_monotonic_deadline_cannot_be_extended_by_wall_clock_rollback(self):
        jobs = {
            "task": {
                "status": "queued",
                "deadline_at": 10**12,
                "_deadline_monotonic": 50.0,
                "cancel_requested": False,
            }
        }
        with (
            mock.patch.object(self.ui.time, "time", return_value=-10**12),
            mock.patch.object(self.ui.time, "monotonic", return_value=51.0),
        ):
            deadline, failure = self.ui._claim_task_execution(jobs, threading.Lock(), "task")
        self.assertIsNone(deadline)
        self.assertEqual(failure, "deadline")

    def test_private_deadline_is_not_exposed_by_job_api(self):
        job_id = uuid.uuid4().hex
        with self.ui.SANDBOX_LOCK:
            self.ui.SANDBOX_JOBS[job_id] = {
                "status": "queued",
                "created_at": time.time(),
                "_deadline_monotonic": time.monotonic() + 60,
            }
        payload = self.client.get(f"/api/sandbox/job/{job_id}").get_json()
        self.assertTrue(payload["success"])
        self.assertNotIn("_deadline_monotonic", payload)

    def test_worker_exception_text_is_bounded_in_result_and_logs(self):
        jobs = {"task": {"status": "running", "result": None}}
        with mock.patch.object(self.ui, "WEBUI_TASK_ERROR_BYTES", 128):
            self.ui._task_executor_error(jobs, threading.Lock(), "task", RuntimeError("x" * 4096))
        self.assertLessEqual(len(jobs["task"]["logs"].encode("utf-8")), 128)
        self.assertLessEqual(
            len(jobs["task"]["result"]["final"]["message"].encode("utf-8")),
            128,
        )

    def test_problem_info_bounds_file_lists_without_misclassifying_omitted_solutions(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            accepted = []
            for index in range(80):
                source = f"code/std-{index:03d}.cpp"
                path = problem / source
                path.parent.mkdir(exist_ok=True)
                path.write_text("int main(){}\n", encoding="utf-8")
                accepted.append(source)
            config = {
                "schema_version": 1,
                "id": "A",
                "judge": {"type": "standard"},
                "solutions": {"accepted": accepted},
                "generators": [accepted[-1]],
            }
            with (
                mock.patch.object(
                    self.ui,
                    "_load_problem_by_index",
                    return_value={"problem": {"display_name": "A"}},
                ),
                mock.patch.object(
                    self.ui,
                    "_schema_workspace",
                    return_value=(Path(temp), {"problems": [{"id": "A", "directory": "A"}]}),
                ),
                mock.patch.object(self.ui, "read_probhub_config_from_dir", return_value=config),
                mock.patch.object(self.ui, "WEBUI_TASK_INFO_FILES_PER_ROLE", 8),
            ):
                info = self.ui._sandbox_problem_info("contest", 0)
        self.assertEqual(info["file_counts"]["std"], 80)
        self.assertEqual(len(info["files"]["std"]), 8)
        self.assertEqual(info["file_counts"]["other"], 0)
        self.assertTrue(info["files_truncated"])

    def test_real_http_flood_caps_request_threads(self):
        server = self.ui._create_webui_server(0, max_request_threads=4)
        port = server.server_port
        served_by = []
        served_lock = threading.Lock()
        release = threading.Event()
        first_entered = threading.Event()

        def problem_info(_subtitle, _index):
            with served_lock:
                served_by.append(threading.get_ident())
            first_entered.set()
            release.wait(5)
            return {"runnable": False, "reason": "test"}

        server_thread = threading.Thread(
            target=server.serve_forever,
            name="bounded-http-server",
            daemon=True,
        )
        responses = []
        request_errors = []

        def request_problem():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sandbox/problem?subtitle=contest&index=0",
                    timeout=10,
                ) as response:
                    responses.append(response.status)
            except Exception as exc:
                request_errors.append(exc)

        clients = [threading.Thread(target=request_problem) for _ in range(4)]
        try:
            with mock.patch.object(self.ui, "_sandbox_problem_info", side_effect=problem_info):
                server_thread.start()
                for client in clients:
                    client.start()
                self.assertTrue(first_entered.wait(3))
                deadline = time.time() + 3
                while server.request_thread_stats()["active"] < 4 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.request_thread_stats()["active"], 4)

                overload_started = time.monotonic()
                overloads = []
                for _ in range(4):
                    try:
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/sandbox/problem?subtitle=contest&index=0",
                            timeout=2,
                        )
                    except urllib.error.HTTPError as exc:
                        try:
                            overloads.append(
                                (
                                    exc.code,
                                    json.loads(exc.read().decode("utf-8")),
                                    exc.headers.get("Retry-After"),
                                )
                            )
                        finally:
                            exc.close()
                    else:
                        self.fail("overloaded request unexpectedly succeeded")
                self.assertLess(time.monotonic() - overload_started, 2)
                self.assertEqual([item[0] for item in overloads], [503] * 4)
                self.assertTrue(
                    all(item[1]["code"] == "http_request_limit" for item in overloads)
                )
                self.assertTrue(all(item[1]["retryable"] for item in overloads))
                self.assertEqual([item[2] for item in overloads], ["1"] * 4)

                release.set()
                for client in clients:
                    client.join(10)
                deadline = time.time() + 3
                while server.request_thread_stats()["active"] and time.time() < deadline:
                    time.sleep(0.01)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sandbox/problem?subtitle=contest&index=0",
                    timeout=3,
                ) as response:
                    self.assertEqual(response.status, 200)
                deadline = time.time() + 3
                while server.request_thread_stats()["active"] and time.time() < deadline:
                    time.sleep(0.01)
            self.assertTrue(all(not client.is_alive() for client in clients))
            self.assertEqual(request_errors, [])
            self.assertEqual(responses, [200] * len(clients))
            self.assertEqual(len(served_by), 5)
            stats = server.request_thread_stats()
            self.assertEqual(stats["active"], 0)
            self.assertEqual(stats["peak"], 4)
            self.assertEqual(stats["limit"], 4)
            self.assertEqual(stats["rejected"], 4)
            self.assertEqual(stats["overload_response_failures"], 0)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_overload_does_not_block_server_shutdown(self):
        server = self.ui._create_webui_server(0, max_request_threads=1)
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        release = threading.Event()
        entered = threading.Event()
        first_result = []
        overload_result = []

        def problem_info(_subtitle, _index):
            entered.set()
            release.wait(5)
            return {"runnable": False, "reason": "test"}

        def first_request():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sandbox/problem?subtitle=contest&index=0",
                    timeout=10,
                ) as response:
                    first_result.append(response.status)
            except Exception as exc:
                first_result.append(exc)

        def overload_request():
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sandbox/problem?subtitle=contest&index=0",
                    timeout=3,
                )
            except urllib.error.HTTPError as exc:
                try:
                    overload_result.append(
                        (exc.code, json.loads(exc.read().decode("utf-8")))
                    )
                finally:
                    exc.close()
            except Exception as exc:
                overload_result.append(exc)

        first_client = threading.Thread(target=first_request)
        overload_client = threading.Thread(target=overload_request)
        shutdown_thread = threading.Thread(target=server.shutdown)
        try:
            with mock.patch.object(self.ui, "_sandbox_problem_info", side_effect=problem_info):
                server_thread.start()
                first_client.start()
                self.assertTrue(entered.wait(3))
                overload_client.start()
                overload_client.join(3)
                self.assertFalse(overload_client.is_alive())
                self.assertEqual(overload_result[0][0], 503)
                self.assertEqual(overload_result[0][1]["code"], "http_request_limit")

                shutdown_thread.start()
                shutdown_thread.join(2)
                self.assertFalse(shutdown_thread.is_alive())
                self.assertFalse(server_thread.is_alive())
        finally:
            release.set()
            first_client.join(5)
            if overload_client.ident is not None:
                overload_client.join(3)
            if shutdown_thread.ident is not None:
                shutdown_thread.join(3)
            if server_thread.is_alive():
                server.shutdown()
            server.server_close()
            server_thread.join(3)
        self.assertEqual(first_result, [200])
        deadline = time.time() + 3
        while server.request_thread_stats()["active"] and time.time() < deadline:
            time.sleep(0.01)
        stats = server.request_thread_stats()
        self.assertEqual(stats["active"], 0)
        self.assertEqual(stats["rejected"], 1)

    def test_idle_request_socket_releases_slot_and_server_recovers(self):
        server = self.ui._create_webui_server(
            0,
            max_request_threads=2,
            request_idle_timeout=0.2,
        )
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        idle_sockets = []
        try:
            with mock.patch.object(
                self.ui,
                "_sandbox_problem_info",
                return_value={"runnable": False, "reason": "test"},
            ):
                server_thread.start()
                idle_sockets = [
                    socket.create_connection(("127.0.0.1", port), timeout=2)
                    for _ in range(2)
                ]
                deadline = time.time() + 3
                while server.request_thread_stats()["active"] < 2 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.request_thread_stats()["active"], 2)

                deadline = time.time() + 3
                while server.request_thread_stats()["active"] and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.request_thread_stats()["active"], 0)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sandbox/problem?subtitle=contest&index=0",
                    timeout=3,
                ) as response:
                    self.assertEqual(response.status, 200)
                deadline = time.time() + 3
                while server.request_thread_stats()["active"] and time.time() < deadline:
                    time.sleep(0.01)
            stats = server.request_thread_stats()
            self.assertEqual(stats["active"], 0)
            self.assertEqual(stats["peak"], 2)
            self.assertEqual(stats["rejected"], 0)
        finally:
            for idle_socket in idle_sockets:
                idle_socket.close()
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_blocked_compile_does_not_block_sandbox_poll_or_cancel(self):
        server = self.ui._create_webui_server(0, max_request_threads=4)
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        sandbox_started = threading.Event()
        compile_entered = threading.Event()
        release_compile = threading.Event()

        def post_json(path, payload):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-ProbHub-CSRF": self.ui.WEBUI_CSRF_TOKEN,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))

        def run_sandbox(job_id, _info):
            with self.ui.SANDBOX_LOCK:
                self.ui.SANDBOX_JOBS[job_id]["status"] = "running"
            sandbox_started.set()
            while not self.ui._task_cancel_requested(
                self.ui.SANDBOX_JOBS, self.ui.SANDBOX_LOCK, job_id
            ):
                time.sleep(0.01)
            with self.ui.SANDBOX_LOCK:
                self.ui.SANDBOX_JOBS[job_id].update(
                    status="cancelled",
                    finished_at=time.time(),
                )

        def block_compile(_subtitle):
            compile_entered.set()
            release_compile.wait(5)
            raise self.ui.ProbHubError("compile unblocked", code="build_busy")

        compile_result = []

        def request_compile():
            try:
                compile_result.append(post_json("/api/compile", {"subtitle": "contest"}))
            except urllib.error.HTTPError as exc:
                try:
                    compile_result.append(
                        (exc.code, json.loads(exc.read().decode("utf-8")))
                    )
                finally:
                    exc.close()
            except Exception as exc:
                compile_result.append(exc)

        try:
            with (
                mock.patch.object(
                    self.ui,
                    "_sandbox_problem_info",
                    return_value=self._problem_info(Path("A").resolve(), 30),
                ),
                mock.patch.object(self.ui, "_run_sandbox_job", side_effect=run_sandbox),
                mock.patch.object(self.ui, "_schema_workspace", side_effect=block_compile),
                mock.patch.object(
                    self.ui.subprocess,
                    "run",
                    return_value=mock.Mock(stdout="", stderr=""),
                ),
            ):
                server_thread.start()
                started = post_json("/api/sandbox/run", {"subtitle": "contest", "index": 0})
                job_id = started["job_id"]
                self.assertTrue(sandbox_started.wait(2))
                compile_thread = threading.Thread(target=request_compile)
                compile_thread.start()
                self.assertTrue(compile_entered.wait(2))

                began = time.monotonic()
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sandbox/job/{job_id}", timeout=2
                ) as response:
                    polled = json.loads(response.read().decode("utf-8"))
                cancelled = post_json(f"/api/sandbox/job/{job_id}/cancel", {})
                self.assertLess(time.monotonic() - began, 2)
                self.assertIn(polled["status"], {"running", "cancelling"})
                self.assertIn(cancelled["status"], {"cancelling", "cancelled"})
                release_compile.set()
                compile_thread.join(3)
                self.assertFalse(compile_thread.is_alive())
                self.assertEqual(compile_result[0][0], 409)
                self.assertEqual(compile_result[0][1]["code"], "build_busy")
        finally:
            release_compile.set()
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_request_slot_is_transferred_before_inline_executor_callback(self):
        original_executor = self.ui.WEBUI_TASK_EXECUTOR
        original_slots = self.ui.WEBUI_REQUEST_SLOTS
        slots = threading.BoundedSemaphore(1)
        test_case = self

        class InlineExecutor:
            capacity = 1

            def submit(self, _task_id, callback, **_kwargs):
                test_case.assertIsNone(getattr(test_case.ui.g, "webui_task_slot", None))
                callback()
                return True

        self.ui.WEBUI_TASK_EXECUTOR = InlineExecutor()
        self.ui.WEBUI_REQUEST_SLOTS = slots
        try:
            with (
                mock.patch.object(
                    self.ui,
                    "_sandbox_problem_info",
                    return_value=self._problem_info(Path("A").resolve(), 30),
                ),
                mock.patch.object(self.ui, "_run_sandbox_job"),
                mock.patch.object(self.ui, "_run_submission_job"),
            ):
                sandbox = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                submission = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
            self.assertEqual(sandbox.status_code, 200, sandbox.get_data(as_text=True))
            self.assertEqual(submission.status_code, 200, submission.get_data(as_text=True))
            self.assertTrue(slots.acquire(blocking=False))
            self.assertFalse(slots.acquire(blocking=False))
            slots.release()
        finally:
            self.ui.WEBUI_TASK_EXECUTOR = original_executor
            self.ui.WEBUI_REQUEST_SLOTS = original_slots

    def test_progress_publication_snapshots_mutable_result(self):
        jobs = {"task": {"status": "running"}}
        result = {"cases": []}
        log = self.ui.BoundedUtf8Log(64)
        self.ui._publish_task_progress(jobs, threading.Lock(), "task", result, log)
        result["cases"].append({"status": "AC"})
        self.assertEqual(jobs["task"]["result"], {"cases": []})

    def test_finished_task_registry_is_bounded_and_keeps_active_jobs(self):
        jobs = {
            f"done-{index}": {
                "status": "completed",
                "created_at": float(index),
                "finished_at": time.time(),
            }
            for index in range(15)
        }
        jobs["active"] = {"status": "running", "created_at": 0.0}
        with mock.patch.object(self.ui, "WEBUI_TASK_MAX_COMPLETED", 5):
            self.ui._prune_job_registry(jobs)
        self.assertIn("active", jobs)
        self.assertEqual(sum(job["status"] == "completed" for job in jobs.values()), 5)
        self.assertEqual(len(jobs), 6)

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
            original_executor = self.ui.WEBUI_TASK_EXECUTOR
            executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="queued-submission-test")
            blocker = threading.Event()
            started = threading.Event()
            self.ui.WEBUI_TASK_EXECUTOR = executor
            try:
                self.assertTrue(executor.submit(
                    "blocker",
                    lambda: (started.set(), blocker.wait(10)),
                    queue_timeout=10,
                ))
                self.assertTrue(started.wait(2))
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
                    self.assertEqual(cancel.get_json()["status"], "cancelled")
                    payload = self._wait_for_job(job_id, timeout=10)
            finally:
                blocker.set()
                executor.shutdown(wait=True)
                self.ui.WEBUI_TASK_EXECUTOR = original_executor
            self.assertEqual(payload["status"], "cancelled", payload)
            self.assertEqual(payload["verdict"], "CANCELLED")
            self.assertFalse((ROOT / ".probhub" / "submissions" / job_id).exists())

    def test_queue_full_is_bounded_and_does_not_retain_upload(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            (problem / "data" / "sample").mkdir(parents=True)
            (problem / "data" / "secret").mkdir(parents=True)
            info = self._problem_info(problem, 30)
            original_executor = self.ui.WEBUI_TASK_EXECUTOR
            executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="queue-full-test")
            blocker = threading.Event()
            started = threading.Event()
            self.ui.WEBUI_TASK_EXECUTOR = executor
            try:
                self.assertTrue(executor.submit(
                    "running", lambda: (started.set(), blocker.wait(10)), queue_timeout=10
                ))
                self.assertTrue(started.wait(2))
                self.assertTrue(executor.submit("queued", lambda: None, queue_timeout=10))
                with mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info):
                    response = self.client.post(
                        "/api/submission/run",
                        data={
                            "subtitle": "contest",
                            "index": "0",
                            "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                        },
                        content_type="multipart/form-data",
                    )
                self.assertEqual(response.status_code, 429, response.get_data(as_text=True))
                payload = response.get_json()
                self.assertEqual(payload["code"], "queue_full")
                self.assertTrue(payload["retryable"])
                self.assertEqual(response.headers["Retry-After"], "2")
                self.assertEqual(self.ui.SUBMISSION_JOBS, {})
                self.assertFalse((ROOT / ".probhub" / "submissions").exists())
                stats = executor.stats()
                self.assertEqual(stats["outstanding"], 2)
                self.assertEqual(stats["threads"], 1)
            finally:
                blocker.set()
                executor.shutdown(wait=True)
                self.ui.WEBUI_TASK_EXECUTOR = original_executor

    def test_request_admission_rejects_before_problem_or_upload_processing(self):
        original_slots = self.ui.WEBUI_REQUEST_SLOTS
        blocked_slots = threading.BoundedSemaphore(1)
        blocked_slots.acquire()
        self.ui.WEBUI_REQUEST_SLOTS = blocked_slots
        try:
            with mock.patch.object(self.ui, "_sandbox_problem_info") as problem_info:
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
            self.assertEqual(response.status_code, 429, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["code"], "queue_full")
            problem_info.assert_not_called()
        finally:
            blocked_slots.release()
            self.ui.WEBUI_REQUEST_SLOTS = original_slots

    def test_admission_slot_is_held_until_task_completion(self):
        problem = Path("A").resolve()
        info = self._problem_info(problem, 30)
        original_executor = self.ui.WEBUI_TASK_EXECUTOR
        original_slots = self.ui.WEBUI_REQUEST_SLOTS
        executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="admission-lifetime-test")
        slots = threading.BoundedSemaphore(executor.capacity)
        gate = threading.Event()
        started = threading.Event()

        def run_job(job_id, _info):
            started.set()
            gate.wait(10)
            with self.ui.SANDBOX_LOCK:
                job = self.ui.SANDBOX_JOBS.get(job_id)
                if job is not None:
                    job.update(status="success", finished_at=time.time())

        self.ui.WEBUI_TASK_EXECUTOR = executor
        self.ui.WEBUI_REQUEST_SLOTS = slots
        try:
            with (
                mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info) as problem_info,
                mock.patch.object(self.ui, "_run_sandbox_job", side_effect=run_job),
            ):
                first = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
                self.assertTrue(started.wait(2))
                second = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
                third = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                self.assertEqual(third.status_code, 429, third.get_data(as_text=True))
                self.assertEqual(problem_info.call_count, 2)
            gate.set()
            deadline = time.time() + 3
            while executor.stats()["outstanding"] and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(executor.stats()["outstanding"], 0)
            self.assertTrue(slots.acquire(blocking=False))
            self.assertTrue(slots.acquire(blocking=False))
            self.assertFalse(slots.acquire(blocking=False))
            slots.release()
            slots.release()
        finally:
            gate.set()
            executor.shutdown(wait=True)
            self.ui.WEBUI_TASK_EXECUTOR = original_executor
            self.ui.WEBUI_REQUEST_SLOTS = original_slots

    def test_queued_sandbox_can_be_cancelled(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            info = self._problem_info(problem, 30)
            original_executor = self.ui.WEBUI_TASK_EXECUTOR
            executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="queued-sandbox-test")
            blocker = threading.Event()
            started = threading.Event()
            self.ui.WEBUI_TASK_EXECUTOR = executor
            try:
                self.assertTrue(executor.submit(
                    "blocker", lambda: (started.set(), blocker.wait(10)), queue_timeout=10
                ))
                self.assertTrue(started.wait(2))
                with mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info):
                    response = self.client.post(
                        "/api/sandbox/run",
                        json={"subtitle": "contest", "index": 0},
                    )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                job_id = response.get_json()["job_id"]
                cancel = self.client.post(f"/api/sandbox/job/{job_id}/cancel")
                self.assertEqual(cancel.status_code, 200)
                self.assertEqual(cancel.get_json()["status"], "cancelled")
                payload = self._wait_for_sandbox_job(job_id, timeout=5)
                self.assertEqual(payload["status"], "cancelled", payload)
                self.assertEqual(payload["result"]["final"]["code"], "cancelled")
                self.assertEqual(self.ui.SANDBOX_PROCESSES, {})
            finally:
                blocker.set()
                executor.shutdown(wait=True)
                self.ui.WEBUI_TASK_EXECUTOR = original_executor

    def test_sandbox_cancel_wins_terminal_publish_race(self):
        entered = threading.Event()
        release = threading.Event()

        def successful_supervisor(_job_id, _command, _env, _cancel_file, result, **_kwargs):
            result["final"] = {
                "type": "final",
                "ok": True,
                "status": "passed",
                "code": "all_expectations_met",
            }
            entered.set()
            release.wait(5)
            return {"returncode": 0, "stop_reason": None, "logs": ""}

        with (
            mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(Path("A"), 30)),
            mock.patch.object(self.ui, "_supervise_judge_process", side_effect=successful_supervisor),
        ):
            response = self.client.post(
                "/api/sandbox/run",
                json={"subtitle": "contest", "index": 0},
            )
            job_id = response.get_json()["job_id"]
            self.assertTrue(entered.wait(2))
            cancel = self.client.post(f"/api/sandbox/job/{job_id}/cancel")
            self.assertEqual(cancel.get_json()["status"], "cancelling")
            release.set()
            payload = self._wait_for_sandbox_job(job_id, timeout=5)
        self.assertEqual(payload["status"], "cancelled", payload)
        self.assertEqual(payload["result"]["final"]["code"], "cancelled")

    def test_same_problem_sandboxes_are_serialized_and_waiter_can_cancel(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_supervisor(_job_id, _command, _env, _cancel_file, result, **_kwargs):
            calls.append(_job_id)
            entered.set()
            release.wait(5)
            result["final"] = {
                "type": "final",
                "ok": True,
                "status": "passed",
                "code": "all_expectations_met",
            }
            return {"returncode": 0, "stop_reason": None, "logs": ""}

        info = self._problem_info(Path("A").resolve(), 30)
        with (
            mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info),
            mock.patch.object(self.ui, "_supervise_judge_process", side_effect=blocking_supervisor),
        ):
            first = self.client.post("/api/sandbox/run", json={"subtitle": "contest", "index": 0})
            self.assertTrue(entered.wait(2))
            second = self.client.post("/api/sandbox/run", json={"subtitle": "contest", "index": 0})
            second_id = second.get_json()["job_id"]
            time.sleep(0.15)
            self.assertEqual(len(calls), 1)
            self.client.post(f"/api/sandbox/job/{second_id}/cancel")
            second_payload = self._wait_for_sandbox_job(second_id, timeout=5)
            release.set()
            first_payload = self._wait_for_sandbox_job(first.get_json()["job_id"], timeout=5)
        self.assertEqual(second_payload["status"], "cancelled", second_payload)
        self.assertEqual(first_payload["status"], "success", first_payload)
        self.assertEqual(len(calls), 1)

    def test_submission_preparation_cancel_releases_worker(self):
        started = threading.Event()

        @contextmanager
        def blocked_preparation(*_args, cancel_requested=None, **_kwargs):
            started.set()
            while not cancel_requested():
                time.sleep(0.01)
            raise SubmissionPreparationCancelled("cancelled in preparation")
            yield

        info = self._problem_info(Path("A"), 30)
        with (
            mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info),
            mock.patch.object(self.ui, "temporary_submission_workspace", blocked_preparation),
        ):
            response = self.client.post(
                "/api/submission/run",
                data={
                    "subtitle": "contest",
                    "index": "0",
                    "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                },
                content_type="multipart/form-data",
            )
            job_id = response.get_json()["job_id"]
            self.assertTrue(started.wait(2))
            cancel = self.client.post(f"/api/submission/job/{job_id}/cancel")
            self.assertEqual(cancel.get_json()["status"], "cancelling")
            payload = self._wait_for_job(job_id, timeout=5)
        self.assertEqual(payload["status"], "cancelled", payload)
        self.assertEqual(payload["verdict"], "CANCELLED")

    def test_submission_cancel_wins_terminal_publish_race(self):
        entered = threading.Event()
        release = threading.Event()

        def successful_supervisor(_job_id, _command, _env, _cancel_file, result, **_kwargs):
            result["final"] = {
                "type": "final",
                "ok": True,
                "status": "passed",
                "code": "all_expectations_met",
            }
            entered.set()
            release.wait(5)
            return {"returncode": 0, "stop_reason": None, "logs": ""}

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            with (
                mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)),
                mock.patch.object(self.ui, "_supervise_judge_process", side_effect=successful_supervisor),
            ):
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
                job_id = response.get_json()["job_id"]
                self.assertTrue(entered.wait(2))
                cancel = self.client.post(f"/api/submission/job/{job_id}/cancel")
                self.assertEqual(cancel.get_json()["status"], "cancelling")
                release.set()
                payload = self._wait_for_job(job_id, timeout=5)
        self.assertEqual(payload["status"], "cancelled", payload)
        self.assertEqual(payload["verdict"], "CANCELLED")
        self.assertTrue(payload["result"]["submission"]["workspace_cleaned"])

    def test_sandbox_execution_deadline_stops_silent_process(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            temp_root = Path(temp)
            problem = temp_root / "A"
            (problem / "code").mkdir(parents=True)
            (problem / "code" / "std.cpp").write_text("// untouched\n", encoding="utf-8")
            before = code_tree_hash(problem)
            pid_file = temp_root / "judge.pid"
            fake_judge = temp_root / "fake_judge.py"
            fake_judge.write_text(
                "import os, time\n"
                "from pathlib import Path\n"
                f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            info = self._problem_info(problem, 30)
            with (
                mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info),
                mock.patch.object(self.ui, "LOCAL_JUDGE_SCRIPT", fake_judge),
                mock.patch.object(self.ui, "WEBUI_TASK_EXECUTION_DEADLINE", 0.2),
                mock.patch.object(self.ui, "SUBMISSION_FORCE_CANCEL_AFTER", 0.1),
            ):
                response = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                job_id = response.get_json()["job_id"]
                payload = self._wait_for_sandbox_job(job_id, timeout=10)
            self.assertEqual(payload["status"], "failed", payload)
            self.assertEqual(payload["result"]["final"]["code"], "task_deadline_exceeded")
            self.assertTrue(pid_file.is_file())
            pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 3
            while process_alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_alive(pid), f"deadline process {pid} survived")
            self.assertEqual(code_tree_hash(problem), before)
            self.assertEqual(self.ui.SANDBOX_PROCESSES, {})

    def test_sandbox_protocol_output_flood_is_stopped_and_bounded(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            temp_root = Path(temp)
            problem = temp_root / "A"
            (problem / "code").mkdir(parents=True)
            fake_judge = temp_root / "flood_judge.py"
            fake_judge.write_text(
                "import sys, time\n"
                "sys.stdout.write('x' * 65536)\n"
                "sys.stdout.flush()\n"
                "time.sleep(10)\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)),
                mock.patch.object(self.ui, "LOCAL_JUDGE_SCRIPT", fake_judge),
                mock.patch.object(self.ui, "WEBUI_TASK_PROCESS_OUTPUT_BYTES", 1024),
                mock.patch.object(self.ui, "WEBUI_TASK_LOG_BYTES", 128),
            ):
                response = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                payload = self._wait_for_sandbox_job(response.get_json()["job_id"], timeout=10)
            self.assertEqual(payload["status"], "failed", payload)
            self.assertEqual(payload["result"]["final"]["code"], "task_output_limit")
            self.assertLessEqual(len(payload["logs"].encode("utf-8")), 128)
            self.assertEqual(self.ui.SANDBOX_PROCESSES, {})

    def test_fast_exit_protocol_output_flood_is_still_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            temp_root = Path(temp)
            problem = temp_root / "A"
            (problem / "code").mkdir(parents=True)
            fake_judge = temp_root / "fast_flood_judge.py"
            fake_judge.write_text(
                "import json, sys\n"
                "print(json.dumps({'type': 'final', 'ok': True, 'status': 'passed', "
                "'code': 'all_expectations_met'}), flush=True)\n"
                "sys.stdout.write('x' * 65536)\n"
                "sys.stdout.flush()\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    self.ui,
                    "_sandbox_problem_info",
                    return_value=self._problem_info(problem, 30),
                ),
                mock.patch.object(self.ui, "LOCAL_JUDGE_SCRIPT", fake_judge),
                mock.patch.object(self.ui, "WEBUI_TASK_PROCESS_OUTPUT_BYTES", 1024),
            ):
                response = self.client.post(
                    "/api/sandbox/run",
                    json={"subtitle": "contest", "index": 0},
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                payload = self._wait_for_sandbox_job(response.get_json()["job_id"], timeout=10)
            self.assertEqual(payload["status"], "failed", payload)
            self.assertEqual(payload["result"]["final"]["code"], "task_output_limit")
            self.assertEqual(self.ui.SANDBOX_PROCESSES, {})

    def test_executor_submit_failure_does_not_leave_queued_records(self):
        info = self._problem_info(Path("A"), 30)
        with (
            mock.patch.object(self.ui, "_sandbox_problem_info", return_value=info),
            mock.patch.object(
                self.ui.WEBUI_TASK_EXECUTOR,
                "submit",
                side_effect=RuntimeError("worker startup failed"),
            ),
        ):
            sandbox_response = self.client.post(
                "/api/sandbox/run",
                json={"subtitle": "contest", "index": 0},
            )
            submission_response = self.client.post(
                "/api/submission/run",
                data={
                    "subtitle": "contest",
                    "index": "0",
                    "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                },
                content_type="multipart/form-data",
            )
        self.assertFalse(sandbox_response.get_json()["success"])
        self.assertEqual(submission_response.status_code, 500)
        self.assertEqual(self.ui.SANDBOX_JOBS, {})
        self.assertEqual(self.ui.SUBMISSION_JOBS, {})

    def test_shutdown_waits_for_active_sandbox_process_and_workers(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            temp_root = Path(temp)
            problem = temp_root / "A"
            (problem / "code").mkdir(parents=True)
            pid_file = temp_root / "shutdown.pid"
            fake_judge = temp_root / "shutdown_judge.py"
            fake_judge.write_text(
                "import os, time\n"
                "from pathlib import Path\n"
                f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            original_executor = self.ui.WEBUI_TASK_EXECUTOR
            executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="shutdown-test")
            self.ui.WEBUI_TASK_EXECUTOR = executor
            try:
                with (
                    mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)),
                    mock.patch.object(self.ui, "LOCAL_JUDGE_SCRIPT", fake_judge),
                ):
                    response = self.client.post(
                        "/api/sandbox/run",
                        json={"subtitle": "contest", "index": 0},
                    )
                    job_id = response.get_json()["job_id"]
                    deadline = time.time() + 5
                    while not pid_file.is_file() and time.time() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(pid_file.is_file())
                    pid = int(pid_file.read_text(encoding="utf-8"))
                    self.ui.shutdown_webui_tasks()
                    payload = self.client.get(f"/api/sandbox/job/{job_id}").get_json()
                self.assertEqual(payload["status"], "cancelled", payload)
                self.assertFalse(process_alive(pid), f"shutdown process {pid} survived")
                stats = executor.stats()
                self.assertEqual(stats["threads"], 0)
                self.assertFalse(stats["reaper_alive"])
            finally:
                self.ui.WEBUI_TASK_EXECUTOR = original_executor

    def test_submission_missing_final_event_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            temp_root = Path(temp)
            problem = temp_root / "A"
            (problem / "code").mkdir(parents=True)
            fake_judge = temp_root / "no_final.py"
            fake_judge.write_text("pass\n", encoding="utf-8")
            with (
                mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)),
                mock.patch.object(self.ui, "LOCAL_JUDGE_SCRIPT", fake_judge),
            ):
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
                payload = self._wait_for_job(response.get_json()["job_id"], timeout=10)
            self.assertEqual(payload["status"], "failed", payload)
            self.assertEqual(payload["verdict"], "FAIL")
            self.assertEqual(payload["result"]["final"]["code"], "missing_final_event")
            self.assertTrue(payload["result"]["submission"]["workspace_cleaned"])

    def test_submission_cleanup_failure_is_reported_truthfully(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)

            def successful_supervisor(_job_id, _command, _env, _cancel_file, result, **_kwargs):
                result["final"] = {
                    "type": "final",
                    "ok": True,
                    "status": "passed",
                    "code": "all_expectations_met",
                }
                return {"returncode": 0, "stop_reason": None, "logs": ""}

            with (
                mock.patch.object(self.ui, "_sandbox_problem_info", return_value=self._problem_info(problem, 30)),
                mock.patch.object(self.ui, "_supervise_judge_process", side_effect=successful_supervisor),
                mock.patch("probhub.submissions._remove_submission_tree", side_effect=OSError("busy")),
            ):
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
                job_id = response.get_json()["job_id"]
                payload = self._wait_for_job(job_id, timeout=10)
            task_root = ROOT / ".probhub" / "submissions" / job_id
            try:
                self.assertEqual(payload["status"], "failed", payload)
                self.assertEqual(payload["result"]["final"]["code"], "submission_cleanup_failed")
                self.assertFalse(payload["result"]["submission"]["workspace_cleaned"])
                self.assertTrue(task_root.is_dir())
            finally:
                shutil.rmtree(task_root, ignore_errors=True)

    def test_submission_cleanup_failure_overrides_cancellation(self):
        entered = threading.Event()
        release = threading.Event()

        def cancelled_supervisor(_job_id, _command, _env, _cancel_file, result, **_kwargs):
            entered.set()
            release.wait(5)
            return {"returncode": 1, "stop_reason": "cancelled", "logs": "cancelled"}

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            with (
                mock.patch.object(
                    self.ui,
                    "_sandbox_problem_info",
                    return_value=self._problem_info(problem, 30),
                ),
                mock.patch.object(
                    self.ui,
                    "_supervise_judge_process",
                    side_effect=cancelled_supervisor,
                ),
                mock.patch("probhub.submissions._remove_submission_tree", side_effect=OSError("busy")),
            ):
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
                job_id = response.get_json()["job_id"]
                self.assertTrue(entered.wait(2))
                self.client.post(f"/api/submission/job/{job_id}/cancel")
                release.set()
                payload = self._wait_for_job(job_id, timeout=10)
            task_root = ROOT / ".probhub" / "submissions" / job_id
            try:
                self.assertEqual(payload["status"], "failed", payload)
                self.assertEqual(payload["verdict"], "FAIL")
                self.assertEqual(payload["result"]["final"]["code"], "submission_cleanup_failed")
                self.assertEqual(
                    payload["result"]["submission"]["original_failure_code"],
                    "cancelled",
                )
                self.assertFalse(payload["result"]["submission"]["workspace_cleaned"])
            finally:
                shutil.rmtree(task_root, ignore_errors=True)

    def test_submission_cleanup_failure_overrides_deadline(self):
        def deadline_supervisor(_job_id, _command, _env, _cancel_file, result, **_kwargs):
            return {"returncode": 1, "stop_reason": "deadline", "logs": "deadline"}

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            with (
                mock.patch.object(
                    self.ui,
                    "_sandbox_problem_info",
                    return_value=self._problem_info(problem, 30),
                ),
                mock.patch.object(
                    self.ui,
                    "_supervise_judge_process",
                    side_effect=deadline_supervisor,
                ),
                mock.patch("probhub.submissions._remove_submission_tree", side_effect=OSError("busy")),
            ):
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
                job_id = response.get_json()["job_id"]
                payload = self._wait_for_job(job_id, timeout=10)
            task_root = ROOT / ".probhub" / "submissions" / job_id
            try:
                self.assertEqual(payload["status"], "failed", payload)
                self.assertEqual(payload["result"]["final"]["code"], "submission_cleanup_failed")
                self.assertEqual(
                    payload["result"]["submission"]["original_failure_code"],
                    "task_deadline_exceeded",
                )
                self.assertFalse(payload["result"]["submission"]["workspace_cleaned"])
            finally:
                shutil.rmtree(task_root, ignore_errors=True)

    def test_submission_cleanup_failure_overrides_base_exception(self):
        def abort_supervisor(*_args, **_kwargs):
            raise KeyboardInterrupt("aborted worker")

        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            with (
                mock.patch.object(
                    self.ui,
                    "_sandbox_problem_info",
                    return_value=self._problem_info(problem, 30),
                ),
                mock.patch.object(
                    self.ui,
                    "_supervise_judge_process",
                    side_effect=abort_supervisor,
                ),
                mock.patch("probhub.submissions._remove_submission_tree", side_effect=OSError("busy")),
            ):
                response = self.client.post(
                    "/api/submission/run",
                    data={
                        "subtitle": "contest",
                        "index": "0",
                        "source": (io.BytesIO(b"int main(){}\n"), "answer.cpp"),
                    },
                    content_type="multipart/form-data",
                )
                job_id = response.get_json()["job_id"]
                payload = self._wait_for_job(job_id, timeout=10)
            task_root = ROOT / ".probhub" / "submissions" / job_id
            try:
                self.assertEqual(payload["status"], "failed", payload)
                self.assertEqual(payload["result"]["final"]["code"], "submission_cleanup_failed")
                self.assertEqual(
                    payload["result"]["submission"]["original_failure_code"],
                    "task_worker_failed",
                )
                self.assertFalse(payload["result"]["submission"]["workspace_cleaned"])
            finally:
                shutil.rmtree(task_root, ignore_errors=True)

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
