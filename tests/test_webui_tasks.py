import io
import threading
import time
import unittest

from probhub.webui_tasks import BoundedPipeCapture, BoundedTaskExecutor, BoundedUtf8Log


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


if __name__ == "__main__":
    unittest.main()
