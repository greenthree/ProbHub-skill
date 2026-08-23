import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from probhub.webui_tasks import (
    BoundedPipeCapture,
    BoundedTaskExecutor,
    BoundedUtf8Log,
    JudgeSupervisorLimits,
    acquire_task_lock,
    claim_task_execution,
    discard_queued_job,
    prune_job_registry,
    public_job_payload,
    publish_task_progress,
    supervise_judge_process,
    task_executor_error,
)


class TaskLifecycleTests(unittest.TestCase):
    def test_registry_pruning_keeps_active_and_newest_completed(self):
        jobs = {
            "active": {"status": "running", "created_at": 1},
            "expired": {"status": "success", "finished_at": 1},
            "older": {"status": "failed", "finished_at": 90},
            "newer": {"status": "success", "finished_at": 95},
        }
        prune_job_registry(jobs, result_ttl=50, max_completed=1, now=100)
        self.assertEqual(set(jobs), {"active", "newer"})

    def test_public_payload_and_progress_are_immutable_snapshots(self):
        jobs = {"task": {"status": "running", "_deadline_monotonic": 50}}
        result = {"cases": [{"status": "AC"}]}
        log = BoundedUtf8Log(64)
        log.append("running")
        publish_task_progress(jobs, threading.Lock(), "task", result, log)
        result["cases"][0]["status"] = "WA"
        payload = public_job_payload(jobs["task"])
        self.assertNotIn("_deadline_monotonic", payload)
        self.assertEqual(payload["result"]["cases"][0]["status"], "AC")

    def test_claim_uses_monotonic_deadline_and_rejects_wall_clock_rollback(self):
        jobs = {
            "task": {
                "status": "queued",
                "deadline_at": 10**12,
                "_deadline_monotonic": 50.0,
                "cancel_requested": False,
            }
        }
        deadline, failure = claim_task_execution(
            jobs,
            threading.Lock(),
            "task",
            execution_deadline=30,
            wall_time=lambda: -10**12,
            monotonic_time=lambda: 51.0,
        )
        self.assertIsNone(deadline)
        self.assertEqual(failure, "deadline")

    def test_discard_queued_submission_publishes_cancelled_cleanup_state(self):
        jobs = {
            "task": {
                "status": "cancelling",
                "filename": "answer.cpp",
                "cancel_requested": True,
                "result": {},
                "created_at": 90,
            }
        }
        discard_queued_job(
            jobs,
            threading.Lock(),
            "task",
            "cancelled",
            submission=True,
            result_ttl=100,
            max_completed=4,
            now=100,
        )
        job = jobs["task"]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["verdict"], "CANCELLED")
        self.assertTrue(job["result"]["submission"]["workspace_cleaned"])

    def test_worker_error_reports_submission_cleanup_failure_truthfully(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_root = root / ".probhub" / "submissions" / "task"
            task_root.mkdir(parents=True)
            jobs = {
                "task": {
                    "status": "running",
                    "filename": "answer.cpp",
                    "result": {},
                    "created_at": 90,
                }
            }
            task_executor_error(
                jobs,
                threading.Lock(),
                "task",
                RuntimeError("boom"),
                submission=True,
                workspace_root=root,
                error_limit_bytes=64,
                result_ttl=100,
                max_completed=4,
                now=100,
            )
            job = jobs["task"]
            self.assertEqual(job["failure_code"], "submission_cleanup_failed")
            self.assertFalse(job["result"]["submission"]["workspace_cleaned"])

    def test_task_lock_honors_cancellation_before_acquisition(self):
        task_lock = threading.Lock()
        task_lock.acquire()
        try:
            jobs = {"task": {"cancel_requested": True}}
            self.assertEqual(
                acquire_task_lock(
                    task_lock,
                    jobs,
                    threading.Lock(),
                    "task",
                    10,
                    monotonic_time=lambda: 1,
                ),
                "cancelled",
            )
        finally:
            task_lock.release()


class BoundedTaskExecutorTests(unittest.TestCase):
    def test_concurrent_flood_accepts_only_fixed_capacity(self):
        executor = BoundedTaskExecutor(max_workers=2, max_queued=4, name="flood-test")
        gate = threading.Event()
        start = threading.Event()
        accepted = []
        accepted_lock = threading.Lock()

        def submit(index):
            start.wait(2)
            result = executor.submit(str(index), lambda: gate.wait(5), queue_timeout=5)
            with accepted_lock:
                accepted.append(result)

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(100)]
        try:
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sum(accepted), executor.capacity)
            stats = executor.stats()
            self.assertEqual(stats["outstanding"], executor.capacity)
            self.assertLessEqual(stats["running"], executor.max_workers)
            self.assertEqual(stats["threads"], executor.max_workers)
        finally:
            gate.set()
            deadline = time.time() + 3
            while executor.stats()["outstanding"] and time.time() < deadline:
                time.sleep(0.01)
            executor.shutdown(wait=True)

    def test_capacity_and_thread_count_are_bounded(self):
        executor = BoundedTaskExecutor(max_workers=2, max_queued=2, name="bounded-test")
        gate = threading.Event()
        active = 0
        active_lock = threading.Lock()
        two_started = threading.Event()

        def block():
            nonlocal active
            with active_lock:
                active += 1
                if active == 2:
                    two_started.set()
            gate.wait(5)

        try:
            for index in range(4):
                self.assertTrue(executor.submit(str(index), block, queue_timeout=5))
            self.assertTrue(two_started.wait(2))
            self.assertFalse(executor.submit("overflow", block, queue_timeout=5))
            stats = executor.stats()
            self.assertEqual(stats["capacity"], 4)
            self.assertEqual(stats["running"], 2)
            self.assertEqual(stats["queued"], 2)
            self.assertEqual(stats["threads"], 2)
            self.assertTrue(stats["reaper_alive"])
        finally:
            gate.set()
            deadline = time.time() + 3
            while executor.stats()["outstanding"] and time.time() < deadline:
                time.sleep(0.01)
            executor.shutdown(wait=True)

    def test_cancel_pending_releases_capacity_and_reports_reason(self):
        executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="cancel-test")
        gate = threading.Event()
        started = threading.Event()
        discarded = []

        def block():
            started.set()
            gate.wait(5)

        try:
            self.assertTrue(executor.submit("running", block, queue_timeout=5))
            self.assertTrue(started.wait(2))
            self.assertTrue(executor.submit(
                "queued", lambda: None, queue_timeout=5, on_discard=discarded.append
            ))
            self.assertTrue(executor.cancel_pending("queued"))
            self.assertEqual(discarded, ["cancelled"])
            self.assertTrue(executor.submit("replacement", lambda: None, queue_timeout=5))
        finally:
            gate.set()
            deadline = time.time() + 3
            while executor.stats()["outstanding"] and time.time() < deadline:
                time.sleep(0.01)
            executor.shutdown(wait=True)

    def test_queue_deadline_expires_without_running_callback(self):
        executor = BoundedTaskExecutor(max_workers=1, max_queued=1, name="expiry-test")
        gate = threading.Event()
        started = threading.Event()
        expired = threading.Event()
        ran = threading.Event()
        reasons = []

        def block():
            started.set()
            gate.wait(5)

        def discarded(reason):
            reasons.append(reason)
            expired.set()

        try:
            self.assertTrue(executor.submit("running", block, queue_timeout=5))
            self.assertTrue(started.wait(2))
            self.assertTrue(executor.submit(
                "expiring", ran.set, queue_timeout=0.05, on_discard=discarded
            ))
            self.assertTrue(expired.wait(2))
            self.assertEqual(reasons, ["queue_timeout"])
            self.assertFalse(ran.is_set())
            self.assertEqual(executor.stats()["queued"], 0)
        finally:
            gate.set()
            deadline = time.time() + 3
            while executor.stats()["outstanding"] and time.time() < deadline:
                time.sleep(0.01)
            executor.shutdown(wait=True)


class BoundedUtf8LogTests(unittest.TestCase):
    def test_utf8_log_never_exceeds_exact_budget(self):
        log = BoundedUtf8Log(64)
        log.append("first")
        log.append("中文" * 100)
        self.assertTrue(log.truncated)
        self.assertLessEqual(log.size_bytes, 64)
        self.assertLessEqual(len(log.text.encode("utf-8")), 64)
        self.assertIn("log truncated", log.text)


class BoundedPipeCaptureTests(unittest.TestCase):
    def test_pipe_capture_retains_one_exact_bounded_prefix(self):
        capture = BoundedPipeCapture(io.BytesIO(b"x" * 4096), 128, name="capture-test").start()
        self.assertTrue(capture.join(2))
        payload = b"".join(capture.drain_chunks())
        self.assertEqual(payload, b"x" * 128)
        self.assertEqual(capture.observed_bytes, 4096)
        self.assertEqual(capture.retained_bytes, 128)
        self.assertLessEqual(capture.peak_buffered_bytes, 128)
        self.assertTrue(capture.overflow)


class JudgeProcessSupervisorTests(unittest.TestCase):
    @staticmethod
    def _limits(*, process_output_bytes=4096):
        return JudgeSupervisorLimits(
            log_bytes=1024,
            event_bytes=2048,
            final_event_bytes=512,
            process_output_bytes=process_output_bytes,
            force_cancel_after=0.05,
        )

    def test_supervisor_limits_reject_boolean_string_and_negative_values(self):
        invalid_fields = {
            "log_bytes": True,
            "event_bytes": "2048",
            "final_event_bytes": 0,
            "process_output_bytes": -1,
            "force_cancel_after": False,
            "process_limit": 0,
        }
        baseline = {
            "log_bytes": 1024,
            "event_bytes": 2048,
            "final_event_bytes": 512,
            "process_output_bytes": 4096,
            "force_cancel_after": 0.05,
            "process_limit": 64,
        }
        for field_name, invalid_value in invalid_fields.items():
            with self.subTest(field=field_name):
                values = {**baseline, field_name: invalid_value}
                with self.assertRaises(ValueError):
                    JudgeSupervisorLimits(**values)

    def test_cancelled_task_does_not_spawn_and_publishes_terminal_progress(self):
        jobs = {"task": {"status": "cancelling", "cancel_requested": True}}
        result = {}

        def unexpected_spawn(*_args, **_kwargs):
            raise AssertionError("cancelled task must not spawn")

        run = supervise_judge_process(
            "task",
            [sys.executable, "-c", "raise SystemExit(0)"],
            os.environ.copy(),
            Path("cancel.requested"),
            result,
            jobs=jobs,
            lock=threading.Lock(),
            processes={},
            cancel_files={},
            execution_deadline=time.monotonic() + 5,
            limits=self._limits(),
            spawn_process=unexpected_spawn,
        )

        self.assertEqual(run["stop_reason"], "cancelled")
        self.assertEqual(result["final"]["code"], "cancelled")
        self.assertEqual(jobs["task"]["result"]["final"]["code"], "cancelled")

    def test_valid_final_event_completes_and_clears_process_registries(self):
        jobs = {"task": {"status": "running", "cancel_requested": False}}
        processes = {}
        cancel_files = {}
        result = {}
        command = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'type':'final','ok':True,"
            "'status':'passed','code':'all_expectations_met'}), flush=True)",
        ]
        with tempfile.TemporaryDirectory() as temp:
            run = supervise_judge_process(
                "task",
                command,
                os.environ.copy(),
                Path(temp) / "cancel.requested",
                result,
                jobs=jobs,
                lock=threading.Lock(),
                processes=processes,
                cancel_files=cancel_files,
                execution_deadline=time.monotonic() + 5,
                limits=self._limits(),
            )

        self.assertIsNone(run["stop_reason"])
        self.assertEqual(run["returncode"], 0)
        self.assertEqual(result["final"]["code"], "all_expectations_met")
        self.assertEqual(processes, {})
        self.assertEqual(cancel_files, {})
        self.assertEqual(
            jobs["task"]["result"]["final"]["code"],
            "all_expectations_met",
        )

    def test_fast_output_flood_is_rejected_after_pipe_drain(self):
        jobs = {"task": {"status": "running", "cancel_requested": False}}
        result = {}
        command = [
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps({'type':'final','ok':True,"
            "'status':'passed','code':'all_expectations_met'}), flush=True); "
            "sys.stdout.write('x'*65536); sys.stdout.flush()",
        ]
        with tempfile.TemporaryDirectory() as temp:
            run = supervise_judge_process(
                "task",
                command,
                os.environ.copy(),
                Path(temp) / "cancel.requested",
                result,
                jobs=jobs,
                lock=threading.Lock(),
                processes={},
                cancel_files={},
                execution_deadline=time.monotonic() + 5,
                limits=self._limits(process_output_bytes=256),
            )

        self.assertEqual(run["stop_reason"], "output_limit")
        self.assertEqual(result["final"]["code"], "task_output_limit")

    def test_cleanup_failure_overrides_successful_protocol(self):
        jobs = {"task": {"status": "running", "cancel_requested": False}}
        processes = {}
        cancel_files = {}
        result = {}

        class CleanupFailureManaged:
            def __init__(self, command, **kwargs):
                kwargs.pop("memory_limit_mb", None)
                kwargs.pop("process_limit", None)
                self.proc = subprocess.Popen(command, **kwargs)

            def sample(self):
                return None, None

            def terminate(self):
                self.proc.wait(timeout=5)
                return {"ok": False, "errors": ["cleanup identity unavailable"]}

            def close(self):
                return None

        command = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'type':'final','ok':True,"
            "'status':'passed','code':'all_expectations_met'}), flush=True)",
        ]
        with tempfile.TemporaryDirectory() as temp:
            run = supervise_judge_process(
                "task",
                command,
                os.environ.copy(),
                Path(temp) / "cancel.requested",
                result,
                jobs=jobs,
                lock=threading.Lock(),
                processes=processes,
                cancel_files=cancel_files,
                execution_deadline=time.monotonic() + 5,
                limits=self._limits(),
                spawn_process=CleanupFailureManaged,
            )

        self.assertEqual(run["stop_reason"], "process_cleanup_failed")
        self.assertEqual(result["final"]["code"], "process_cleanup_failed")
        self.assertIn("cleanup identity unavailable", result["final"]["message"])
        self.assertEqual(processes, {})
        self.assertEqual(cancel_files, {})
        self.assertEqual(
            jobs["task"]["result"]["final"]["code"],
            "process_cleanup_failed",
        )


if __name__ == "__main__":
    unittest.main()
