import socket
import unittest
from unittest import mock

from werkzeug.serving import ThreadedWSGIServer

from probhub.webui_server import BoundedThreadedWSGIServer


def empty_app(_environ, start_response):
    start_response("204 No Content", [("Content-Length", "0")])
    return [b""]


class BoundedThreadedWSGIServerTests(unittest.TestCase):
    def test_rejects_invalid_limits_before_binding(self):
        cases = (
            ({"max_request_threads": True}, "max_request_threads"),
            ({"max_request_threads": 0}, "max_request_threads"),
            ({"max_request_threads": 1.5}, "max_request_threads"),
            (
                {"max_request_threads": 1, "request_idle_timeout": float("inf")},
                "request_idle_timeout",
            ),
            (
                {"max_request_threads": 1, "request_idle_timeout": 0},
                "request_idle_timeout",
            ),
            (
                {"max_request_threads": 1, "overload_response_timeout": True},
                "overload_response_timeout",
            ),
            (
                {"max_request_threads": 1, "overload_retry_after": 0},
                "overload_retry_after",
            ),
        )
        for options, name in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, name):
                    BoundedThreadedWSGIServer(
                        "127.0.0.1",
                        0,
                        empty_app,
                        **options,
                    )

    def test_thread_start_failure_releases_admission_slot(self):
        server = BoundedThreadedWSGIServer(
            "127.0.0.1",
            0,
            empty_app,
            max_request_threads=1,
        )
        request_socket = mock.Mock()
        try:
            with mock.patch.object(
                ThreadedWSGIServer,
                "process_request",
                side_effect=RuntimeError("thread start failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                    server.process_request(request_socket, ("127.0.0.1", 1))
            request_socket.settimeout.assert_called_once_with(30.0)
            self.assertTrue(server._request_slots.acquire(blocking=False))
            server._request_slots.release()
        finally:
            server.server_close()

    def test_handler_failure_releases_exactly_one_admission_slot(self):
        server = BoundedThreadedWSGIServer(
            "127.0.0.1",
            0,
            empty_app,
            max_request_threads=1,
        )
        request_socket = mock.Mock()
        self.assertTrue(server._request_slots.acquire(blocking=False))
        try:
            with mock.patch.object(
                ThreadedWSGIServer,
                "process_request_thread",
                side_effect=RuntimeError("handler failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "handler failed"):
                    server.process_request_thread(request_socket, ("127.0.0.1", 1))
            self.assertTrue(server._request_slots.acquire(blocking=False))
            self.assertFalse(server._request_slots.acquire(blocking=False))
            server._request_slots.release()
            self.assertEqual(server.request_thread_stats()["active"], 0)
        finally:
            server.server_close()

    def test_overload_send_failure_is_bounded_and_socket_is_closed(self):
        server = BoundedThreadedWSGIServer(
            "127.0.0.1",
            0,
            empty_app,
            max_request_threads=1,
            overload_response_timeout=0.1,
        )
        request_socket = mock.Mock()
        request_socket.sendall.side_effect = OSError("client stopped reading")
        self.assertTrue(server._request_slots.acquire(blocking=False))
        try:
            server.process_request(request_socket, ("127.0.0.1", 1))
            self.assertTrue(request_socket.settimeout.called)
            request_socket.shutdown.assert_not_called()
            request_socket.close.assert_called_once_with()
            stats = server.request_thread_stats()
            self.assertEqual(stats["active"], 0)
            self.assertEqual(stats["rejected"], 1)
            self.assertEqual(stats["overload_response_failures"], 1)
        finally:
            server._request_slots.release()
            server.server_close()
