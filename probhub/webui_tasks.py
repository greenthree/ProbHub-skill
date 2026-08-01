"""Bounded in-process task execution for the local WebUI."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO, Callable


DiscardCallback = Callable[[str], None]
ErrorCallback = Callable[[BaseException], None]
TaskCallback = Callable[[], None]


@dataclass(frozen=True)
class _QueuedTask:
    task_id: str
    callback: TaskCallback
    expires_at: float
    on_discard: DiscardCallback | None
    on_error: ErrorCallback | None


class BoundedTaskExecutor:
    """A fixed worker pool with a strict bound on running and queued tasks."""

    def __init__(self, *, max_workers: int, max_queued: int, name: str = "probhub-webui"):
        if isinstance(max_workers, bool) or int(max_workers) <= 0:
            raise ValueError("max_workers must be a positive integer")
        if isinstance(max_queued, bool) or int(max_queued) < 0:
            raise ValueError("max_queued must be a non-negative integer")
        self.max_workers = int(max_workers)
        self.max_queued = int(max_queued)
        self.capacity = self.max_workers + self.max_queued
        self.name = str(name)
        self._condition = threading.Condition()
        self._pending: deque[_QueuedTask] = deque()
        self._active: set[str] = set()
        self._task_ids: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._reaper: threading.Thread | None = None
        self._started = False
        self._closed = False

    def _start_locked(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self.max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"{self.name}-worker-{index + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()
        self._reaper = threading.Thread(
            target=self._reaper_loop,
            name=f"{self.name}-reaper",
            daemon=True,
        )
        self._reaper.start()

    def submit(
        self,
        task_id: str,
        callback: TaskCallback,
        *,
        queue_timeout: float,
        on_discard: DiscardCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> bool:
        if not callable(callback):
            raise TypeError("callback must be callable")
        if float(queue_timeout) <= 0:
            raise ValueError("queue_timeout must be positive")
        task_id = str(task_id)
        expired: list[_QueuedTask]
        with self._condition:
            expired = self._expire_pending_locked(time.monotonic())
            if self._closed or task_id in self._task_ids or len(self._task_ids) >= self.capacity:
                accepted = False
            else:
                self._start_locked()
                self._pending.append(
                    _QueuedTask(
                        task_id=task_id,
                        callback=callback,
                        expires_at=time.monotonic() + float(queue_timeout),
                        on_discard=on_discard,
                        on_error=on_error,
                    )
                )
                self._task_ids.add(task_id)
                self._condition.notify_all()
                accepted = True
        self._notify_discarded(expired, "queue_timeout")
        return accepted

    def cancel_pending(self, task_id: str) -> bool:
        task_id = str(task_id)
        removed = None
        with self._condition:
            for task in self._pending:
                if task.task_id == task_id:
                    removed = task
                    break
            if removed is not None:
                self._pending.remove(removed)
                self._task_ids.discard(task_id)
                self._condition.notify_all()
        if removed is not None:
            self._notify_discarded([removed], "cancelled")
            return True
        return False

    def expire_pending(self) -> int:
        with self._condition:
            expired = self._expire_pending_locked(time.monotonic())
            self._condition.notify_all()
        self._notify_discarded(expired, "queue_timeout")
        return len(expired)

    def stats(self) -> dict:
        with self._condition:
            return {
                "max_workers": self.max_workers,
                "max_queued": self.max_queued,
                "capacity": self.capacity,
                "running": len(self._active),
                "queued": len(self._pending),
                "outstanding": len(self._task_ids),
                "threads": sum(thread.is_alive() for thread in self._workers),
                "reaper_alive": bool(self._reaper and self._reaper.is_alive()),
                "closed": self._closed,
            }

    def shutdown(self, *, wait: bool = False, cancel_pending: bool = True) -> None:
        discarded = []
        with self._condition:
            if self._closed:
                threads = [*self._workers, self._reaper]
            else:
                self._closed = True
                if cancel_pending:
                    discarded = list(self._pending)
                    self._pending.clear()
                    for task in discarded:
                        self._task_ids.discard(task.task_id)
                threads = [*self._workers, self._reaper]
                self._condition.notify_all()
        self._notify_discarded(discarded, "shutdown")
        if wait:
            for thread in threads:
                if thread is not None and thread is not threading.current_thread():
                    thread.join()

    def _expire_pending_locked(self, now: float) -> list[_QueuedTask]:
        expired = [task for task in self._pending if task.expires_at <= now]
        if not expired:
            return []
        expired_ids = {task.task_id for task in expired}
        self._pending = deque(task for task in self._pending if task.task_id not in expired_ids)
        self._task_ids.difference_update(expired_ids)
        return expired

    @staticmethod
    def _notify_discarded(tasks: list[_QueuedTask], reason: str) -> None:
        for task in tasks:
            if task.on_discard is None:
                continue
            try:
                task.on_discard(reason)
            except Exception:
                pass

    def _worker_loop(self) -> None:
        while True:
            expired = []
            task = None
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed and not self._pending:
                    return
                expired = self._expire_pending_locked(time.monotonic())
                if self._pending:
                    task = self._pending.popleft()
                    self._active.add(task.task_id)
            self._notify_discarded(expired, "queue_timeout")
            if task is None:
                continue
            try:
                task.callback()
            except BaseException as exc:
                if task.on_error is not None:
                    try:
                        task.on_error(exc)
                    except Exception:
                        pass
            finally:
                with self._condition:
                    self._active.discard(task.task_id)
                    self._task_ids.discard(task.task_id)
                    self._condition.notify_all()

    def _reaper_loop(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                expired = self._expire_pending_locked(time.monotonic())
                if self._pending:
                    next_expiry = min(task.expires_at for task in self._pending)
                    wait_for = max(0.01, min(1.0, next_expiry - time.monotonic()))
                else:
                    wait_for = 1.0
                if not expired:
                    self._condition.wait(timeout=wait_for)
                    continue
            self._notify_discarded(expired, "queue_timeout")


class BoundedUtf8Log:
    """Keep a UTF-8 log prefix within one exact byte budget."""

    _MARKER = b"\n...[log truncated]"

    def __init__(self, limit_bytes: int):
        if isinstance(limit_bytes, bool) or int(limit_bytes) <= 0:
            raise ValueError("limit_bytes must be a positive integer")
        self.limit_bytes = int(limit_bytes)
        self._data = bytearray()
        self.truncated = False

    def append(self, text: str) -> None:
        if self.truncated:
            return
        chunk = str(text).encode("utf-8", errors="replace")
        if self._data:
            chunk = b"\n" + chunk
        if len(self._data) + len(chunk) <= self.limit_bytes:
            self._data.extend(chunk)
            return
        self.truncated = True
        marker = self._MARKER[: self.limit_bytes]
        prefix_limit = max(0, self.limit_bytes - len(marker))
        if len(self._data) > prefix_limit:
            del self._data[prefix_limit:]
        elif len(self._data) < prefix_limit:
            self._data.extend(chunk[: prefix_limit - len(self._data)])
        self._data.extend(marker)

    @property
    def text(self) -> str:
        return bytes(self._data).decode("utf-8", errors="ignore")

    @property
    def size_bytes(self) -> int:
        return len(self._data)


class BoundedPipeCapture:
    """Drain a binary pipe while retaining at most one fixed byte prefix."""

    def __init__(self, stream: BinaryIO, limit_bytes: int, *, name: str = "probhub-webui-pipe"):
        if isinstance(limit_bytes, bool) or int(limit_bytes) <= 0:
            raise ValueError("limit_bytes must be a positive integer")
        self.stream = stream
        self.limit_bytes = int(limit_bytes)
        self._lock = threading.Lock()
        self._chunks: deque[bytes] = deque()
        self._buffered_bytes = 0
        self._retained_bytes = 0
        self._observed_bytes = 0
        self._peak_buffered_bytes = 0
        self._overflow = False
        self._error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._drain_loop, name=name, daemon=True)

    def start(self) -> "BoundedPipeCapture":
        self._thread.start()
        return self

    def _drain_loop(self) -> None:
        try:
            read = getattr(self.stream, "read1", self.stream.read)
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    return
                with self._lock:
                    self._observed_bytes += len(chunk)
                    remaining = max(0, self.limit_bytes - self._retained_bytes)
                    if remaining:
                        retained = bytes(chunk[:remaining])
                        self._chunks.append(retained)
                        self._buffered_bytes += len(retained)
                        self._retained_bytes += len(retained)
                        self._peak_buffered_bytes = max(
                            self._peak_buffered_bytes,
                            self._buffered_bytes,
                        )
                    if self._observed_bytes > self.limit_bytes:
                        self._overflow = True
        except (OSError, ValueError) as exc:
            with self._lock:
                self._error = exc
        finally:
            self._done.set()

    def drain_chunks(self) -> list[bytes]:
        with self._lock:
            chunks = list(self._chunks)
            self._chunks.clear()
            self._buffered_bytes = 0
            return chunks

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def overflow(self) -> bool:
        with self._lock:
            return self._overflow

    @property
    def error(self) -> BaseException | None:
        with self._lock:
            return self._error

    @property
    def observed_bytes(self) -> int:
        with self._lock:
            return self._observed_bytes

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes

    @property
    def peak_buffered_bytes(self) -> int:
        with self._lock:
            return self._peak_buffered_bytes
