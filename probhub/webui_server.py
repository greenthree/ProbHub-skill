import json
import math
import socket
import time
from threading import BoundedSemaphore, Lock

from werkzeug.serving import ThreadedWSGIServer


HTTP_REQUEST_LIMIT_CODE = "http_request_limit"
DEFAULT_REQUEST_IDLE_TIMEOUT = 30.0
DEFAULT_OVERLOAD_RESPONSE_TIMEOUT = 0.25
DEFAULT_OVERLOAD_RETRY_AFTER = 1
MAX_OVERLOAD_DRAIN_BYTES = 64 * 1024


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _overload_response(retry_after):
    body = json.dumps(
        {
            "success": False,
            "code": HTTP_REQUEST_LIMIT_CODE,
            "error": "WebUI HTTP request capacity is full; retry later",
            "retryable": True,
            "retry_after": retry_after,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    headers = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Retry-After: {retry_after}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + body


class BoundedThreadedWSGIServer(ThreadedWSGIServer):
    """Werkzeug server with bounded request admission and idle I/O."""

    daemon_threads = True

    def __init__(
        self,
        host,
        port,
        application,
        *,
        max_request_threads,
        request_idle_timeout=DEFAULT_REQUEST_IDLE_TIMEOUT,
        overload_response_timeout=DEFAULT_OVERLOAD_RESPONSE_TIMEOUT,
        overload_retry_after=DEFAULT_OVERLOAD_RETRY_AFTER,
    ):
        self.max_request_threads = _positive_integer(
            max_request_threads, "max_request_threads"
        )
        self.request_idle_timeout = _positive_finite_number(
            request_idle_timeout, "request_idle_timeout"
        )
        self.overload_response_timeout = _positive_finite_number(
            overload_response_timeout, "overload_response_timeout"
        )
        self.overload_retry_after = _positive_integer(
            overload_retry_after, "overload_retry_after"
        )
        self._overload_response_bytes = _overload_response(
            self.overload_retry_after
        )
        self._request_slots = BoundedSemaphore(self.max_request_threads)
        self._request_stats_lock = Lock()
        self._active_request_threads = 0
        self._peak_request_threads = 0
        self._rejected_requests = 0
        self._overload_response_failures = 0
        super().__init__(host, port, application)

    def _reject_overloaded_request(self, request_socket):
        with self._request_stats_lock:
            self._rejected_requests += 1
        response_sent = False
        deadline = time.monotonic() + self.overload_response_timeout
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("overload response deadline exceeded")
            request_socket.settimeout(remaining)
            request_socket.sendall(self._overload_response_bytes)
            response_sent = True
        except OSError:
            with self._request_stats_lock:
                self._overload_response_failures += 1
        finally:
            if response_sent:
                try:
                    request_socket.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                drained = 0
                while drained < MAX_OVERLOAD_DRAIN_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        request_socket.settimeout(remaining)
                        chunk = request_socket.recv(
                            min(4096, MAX_OVERLOAD_DRAIN_BYTES - drained)
                        )
                        if not chunk:
                            break
                        drained += len(chunk)
                    except OSError:
                        break
            try:
                self.close_request(request_socket)
            except OSError:
                try:
                    request_socket.close()
                except OSError:
                    pass

    def process_request(self, request_socket, client_address):
        if not self._request_slots.acquire(blocking=False):
            self._reject_overloaded_request(request_socket)
            return
        try:
            request_socket.settimeout(self.request_idle_timeout)
            super().process_request(request_socket, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request_socket, client_address):
        with self._request_stats_lock:
            self._active_request_threads += 1
            self._peak_request_threads = max(
                self._peak_request_threads,
                self._active_request_threads,
            )
        try:
            super().process_request_thread(request_socket, client_address)
        finally:
            with self._request_stats_lock:
                self._active_request_threads -= 1
            self._request_slots.release()

    def request_thread_stats(self):
        with self._request_stats_lock:
            return {
                "active": self._active_request_threads,
                "peak": self._peak_request_threads,
                "limit": self.max_request_threads,
                "rejected": self._rejected_requests,
                "overload_response_failures": self._overload_response_failures,
            }
