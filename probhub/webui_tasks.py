"""Bounded in-process task execution for the local WebUI."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from .process_control import (
    PROCESS_CLEANUP_FAILED,
    snapshot_process_tree_identities,
    spawn_managed,
    terminate_external_process_tree,
)
from .submissions import (
    SubmissionPreparationCancelled,
    SubmissionPreparationDeadlineExceeded,
    cleanup_stale_submission_workspaces,
    temporary_submission_workspace,
)
from .webui_judge_protocol import (
    consume_jsonl_chunk,
    empty_sandbox_result,
    finalize_judge_protocol,
    submission_verdict,
)


DiscardCallback = Callable[[str], None]
ErrorCallback = Callable[[BaseException], None]
TaskCallback = Callable[[], None]
ACTIVE_TASK_STATUSES = frozenset({"queued", "running", "cancelling"})


@dataclass(frozen=True)
class JudgeSupervisorLimits:
    """Explicit WebUI process and protocol budgets for one Judge task."""

    log_bytes: int
    event_bytes: int
    final_event_bytes: int
    process_output_bytes: int
    force_cancel_after: float = 3.0
    process_limit: int = 64

    def __post_init__(self):
        for field_name in (
            "log_bytes",
            "event_bytes",
            "final_event_bytes",
            "process_output_bytes",
            "process_limit",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if (
            isinstance(self.force_cancel_after, bool)
            or not isinstance(self.force_cancel_after, (int, float))
            or self.force_cancel_after < 0
        ):
            raise ValueError("force_cancel_after must be a non-negative number")


def task_failure_result(result, code, message, *, status="failed"):
    """Attach the stable terminal failure shape to a task result."""

    if result is None:
        result = {}
    result["final"] = {
        "ok": False,
        "status": status,
        "code": code,
        "message": message,
    }
    return result


def bounded_error_text(value, limit_bytes):
    """Render an exception or diagnostic within one exact UTF-8 budget."""

    log = BoundedUtf8Log(limit_bytes)
    log.append(str(value))
    return log.text


def public_job_payload(job):
    """Return an immutable public snapshot without adapter-private fields."""

    payload = copy.deepcopy(job)
    for key in list(payload):
        if str(key).startswith("_"):
            payload.pop(key, None)
    return payload


def prune_job_registry(
    jobs,
    *,
    result_ttl,
    max_completed,
    now=None,
):
    """Drop expired results and cap completed records without touching active jobs."""

    current = time.time() if now is None else float(now)
    expired = [
        job_id
        for job_id, job in jobs.items()
        if job.get("status") not in ACTIVE_TASK_STATUSES
        and current
        - float(job.get("finished_at") or job.get("created_at") or current)
        >= float(result_ttl)
    ]
    for job_id in expired:
        jobs.pop(job_id, None)
    finished = sorted(
        (
            (job_id, job)
            for job_id, job in jobs.items()
            if job.get("status") not in ACTIVE_TASK_STATUSES
        ),
        key=lambda item: float(
            item[1].get("finished_at") or item[1].get("created_at") or 0
        ),
    )
    remove_count = max(0, len(finished) - max(0, int(max_completed)))
    for job_id, _ in finished[:remove_count]:
        jobs.pop(job_id, None)


def task_cancel_requested(jobs, lock, job_id):
    with lock:
        return bool(jobs.get(job_id, {}).get("cancel_requested"))


def claim_task_execution(
    jobs,
    lock,
    job_id,
    *,
    execution_deadline,
    wall_time=None,
    monotonic_time=None,
):
    """Atomically move a queued task to running using a monotonic deadline."""

    with lock:
        job = jobs.get(job_id)
        if not job:
            return None, "missing"
        if job.get("cancel_requested"):
            return None, "cancelled"
        now = time.time() if wall_time is None else float(wall_time())
        now_monotonic = (
            time.monotonic() if monotonic_time is None else float(monotonic_time())
        )
        deadline_monotonic = job.get("_deadline_monotonic")
        if deadline_monotonic is None:
            remaining_wall = max(0.0, float(job.get("deadline_at") or now) - now)
            deadline_monotonic = now_monotonic + remaining_wall
            job["_deadline_monotonic"] = deadline_monotonic
        remaining_total = float(deadline_monotonic) - now_monotonic
        if remaining_total <= 0:
            return None, "deadline"
        execution_seconds = min(float(execution_deadline), remaining_total)
        job.update(
            status="running",
            started_at=now,
            execution_deadline_at=now + execution_seconds,
        )
        return now_monotonic + execution_seconds, None


def acquire_task_lock(
    task_lock,
    jobs,
    jobs_lock,
    job_id,
    execution_deadline,
    *,
    monotonic_time=None,
    poll_interval=0.05,
):
    """Acquire a per-problem lock while respecting cancellation and deadline."""

    monotonic = monotonic_time or time.monotonic
    while monotonic() < execution_deadline:
        if task_cancel_requested(jobs, jobs_lock, job_id):
            return "cancelled"
        remaining = max(0.001, execution_deadline - monotonic())
        if task_lock.acquire(timeout=min(float(poll_interval), remaining)):
            return "acquired"
    return "deadline"


def publish_task_progress(jobs, lock, job_id, result, log):
    """Publish an immutable progress snapshot for concurrent HTTP polling."""

    with lock:
        job = jobs.get(job_id)
        if job is not None:
            job["logs"] = log.text
            job["logs_truncated"] = log.truncated
            job["result"] = copy.deepcopy(result)


def discard_queued_job(
    jobs,
    lock,
    job_id,
    reason,
    *,
    submission=False,
    result_ttl,
    max_completed,
    now=None,
):
    """Publish the terminal result for queue timeout, cancellation, or shutdown."""

    current = time.time() if now is None else float(now)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") not in {"queued", "cancelling"}:
            return
        cancelled = reason in {"cancelled", "shutdown"} or bool(
            job.get("cancel_requested")
        )
        code = "cancelled" if cancelled else "queue_timeout"
        message = (
            "task cancelled before execution"
            if cancelled
            else "task exceeded its queue deadline"
        )
        result = task_failure_result(job.get("result"), code, message, status=code)
        if submission:
            result.setdefault("submission", {}).update(
                task_id=job_id,
                filename=job.get("filename", ""),
                verdict="CANCELLED" if cancelled else "FAIL",
                workspace_cleaned=True,
            )
        job.update(
            status="cancelled" if cancelled else "failed",
            verdict=("CANCELLED" if cancelled else "FAIL")
            if submission
            else job.get("verdict"),
            result=result,
            logs=message,
            finished_at=current,
            failure_code=code,
        )
        prune_job_registry(
            jobs,
            result_ttl=result_ttl,
            max_completed=max_completed,
            now=current,
        )


def task_executor_error(
    jobs,
    lock,
    job_id,
    exc,
    *,
    submission=False,
    workspace_root=None,
    error_limit_bytes,
    result_ttl,
    max_completed,
    now=None,
):
    """Publish a bounded worker failure, preserving submission cleanup truth."""

    current = time.time() if now is None else float(now)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") not in ACTIVE_TASK_STATUSES:
            return
        message = bounded_error_text(
            f"WebUI task worker failed: {exc}", error_limit_bytes
        )
        failure_code = "task_worker_failed"
        result = job.get("result") or {}
        if submission:
            if workspace_root is None:
                raise ValueError("workspace_root is required for submission task errors")
            task_root = workspace_root / ".probhub" / "submissions" / job_id
            workspace_cleaned = not task_root.exists()
            submission_result = result.setdefault("submission", {})
            submission_result.update(
                task_id=job_id,
                filename=job.get("filename", ""),
                verdict="FAIL",
                workspace_cleaned=workspace_cleaned,
            )
            if not workspace_cleaned:
                failure_code = "submission_cleanup_failed"
                submission_result.update(
                    cleanup_error="submission workspace still exists after worker failure",
                    original_failure_code="task_worker_failed",
                )
        result = task_failure_result(
            result,
            failure_code,
            "submission workspace cleanup failed"
            if failure_code == "submission_cleanup_failed"
            else message,
        )
        job.update(
            status="failed",
            verdict="FAIL" if submission else job.get("verdict"),
            result=result,
            logs=message,
            finished_at=current,
            failure_code=failure_code,
        )
        prune_job_registry(
            jobs,
            result_ttl=result_ttl,
            max_completed=max_completed,
            now=current,
        )


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


def touch_cancel_file(cancel_file) -> None:
    """Best-effort cooperative cancellation signal for a cancellable worker."""

    try:
        Path(cancel_file).touch(exist_ok=True)
    except OSError:
        pass


def supervise_judge_process(
    job_id,
    command,
    env,
    cancel_file,
    result,
    *,
    jobs,
    lock,
    processes,
    cancel_files,
    execution_deadline,
    limits: JudgeSupervisorLimits,
    cwd=None,
    spawn_process=None,
    snapshot_identities=None,
    terminate_process_tree=None,
    monotonic_time=None,
    sleep=None,
):
    """Run one cancellable Judge process and publish bounded WebUI progress.

    The adapter owns task registries and display constants. This Core routine
    owns subprocess containment, protocol draining, cancellation/deadline
    enforcement, and terminal failure priority.
    """

    spawn_process = spawn_process or spawn_managed
    snapshot_identities = snapshot_identities or snapshot_process_tree_identities
    terminate_process_tree = terminate_process_tree or terminate_external_process_tree
    monotonic_time = monotonic_time or time.monotonic
    sleep = sleep or time.sleep

    log = BoundedUtf8Log(limits.log_bytes)
    parse_state = {
        "buffer": b"",
        "event_bytes": 0,
        "final_seen": False,
        "protocol_error": None,
    }
    return_code = None
    stop_reason = None
    stop_started = None
    last_identity_sample = 0.0
    known_identities = set()
    cleanup_failure_message = None
    managed = None
    capture = None

    def record_cleanup(cleanup):
        nonlocal cleanup_failure_message, stop_reason
        if isinstance(cleanup, dict) and not cleanup.get("ok", False):
            errors = cleanup.get("errors") or ()
            cleanup_failure_message = (
                str(errors[0])
                if errors
                else "process tree cleanup could not be confirmed"
            )
            stop_reason = PROCESS_CLEANUP_FAILED

    if task_cancel_requested(jobs, lock, job_id):
        task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
        publish_task_progress(jobs, lock, job_id, result, log)
        return {"returncode": None, "stop_reason": "cancelled", "logs": log.text}
    if monotonic_time() >= execution_deadline:
        task_failure_result(
            result,
            "task_deadline_exceeded",
            "task exceeded its total deadline",
        )
        publish_task_progress(jobs, lock, job_id, result, log)
        return {"returncode": None, "stop_reason": "deadline", "logs": log.text}

    try:
        managed = spawn_process(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=Path.cwd() if cwd is None else Path(cwd),
            env=env,
            memory_limit_mb=None,
            process_limit=limits.process_limit,
        )
        proc = managed.proc
        capture = BoundedPipeCapture(
            proc.stdout,
            limits.process_output_bytes,
            name=f"probhub-webui-pipe-{str(job_id)[:8]}",
        ).start()
        with lock:
            processes[job_id] = proc
            cancel_files[job_id] = Path(cancel_file)
        while True:
            published = False
            for chunk in capture.drain_chunks():
                consume_jsonl_chunk(
                    result,
                    log,
                    parse_state,
                    chunk,
                    event_limit_bytes=limits.event_bytes,
                    final_event_limit_bytes=limits.final_event_bytes,
                )
                published = True
            if published:
                publish_task_progress(jobs, lock, job_id, result, log)

            now_mono = monotonic_time()
            if now_mono - last_identity_sample >= 0.05 and proc.poll() is None:
                managed.sample()
                last_identity_sample = now_mono
            if capture.overflow and stop_reason is None:
                stop_reason = "output_limit"
                stop_started = now_mono
                known_identities = snapshot_identities(proc.pid)
            elif task_cancel_requested(jobs, lock, job_id) and stop_reason is None:
                stop_reason = "cancelled"
                stop_started = now_mono
                known_identities = snapshot_identities(proc.pid)
                touch_cancel_file(cancel_file)
            elif now_mono >= execution_deadline and stop_reason is None:
                stop_reason = "deadline"
                stop_started = now_mono
                known_identities = snapshot_identities(proc.pid)
                touch_cancel_file(cancel_file)

            if stop_reason == "output_limit" and proc.poll() is None:
                record_cleanup(terminate_process_tree(proc, known_identities))
            elif (
                stop_reason is not None
                and proc.poll() is None
                and stop_started is not None
                and now_mono - stop_started >= limits.force_cancel_after
            ):
                record_cleanup(terminate_process_tree(proc, known_identities))

            return_code = proc.poll()
            if return_code is not None:
                break
            sleep(0.05)
    finally:
        if managed is not None:
            if managed.proc.poll() is None:
                record_cleanup(
                    terminate_process_tree(managed.proc, known_identities)
                )
            record_cleanup(managed.terminate())
            managed.close()
        if capture is not None:
            if not capture.join(timeout=2.0):
                try:
                    capture.stream.close()
                except (OSError, ValueError):
                    pass
                capture.join(timeout=1.0)
            for chunk in capture.drain_chunks():
                consume_jsonl_chunk(
                    result,
                    log,
                    parse_state,
                    chunk,
                    event_limit_bytes=limits.event_bytes,
                    final_event_limit_bytes=limits.final_event_bytes,
                )
            consume_jsonl_chunk(
                result,
                log,
                parse_state,
                b"",
                final=True,
                event_limit_bytes=limits.event_bytes,
                final_event_limit_bytes=limits.final_event_bytes,
            )
            # A short-lived process can exit before overflow is observed in
            # the monitor loop. Re-check after the reader fully drains.
            if capture.overflow and stop_reason is None:
                stop_reason = "output_limit"
            try:
                capture.stream.close()
            except (OSError, ValueError):
                pass
        with lock:
            processes.pop(job_id, None)
            cancel_files.pop(job_id, None)

    if stop_reason is None and task_cancel_requested(jobs, lock, job_id):
        stop_reason = "cancelled"
    if stop_reason == PROCESS_CLEANUP_FAILED:
        task_failure_result(
            result,
            PROCESS_CLEANUP_FAILED,
            cleanup_failure_message or "process tree cleanup could not be confirmed",
        )
    elif stop_reason == "cancelled":
        task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
    elif stop_reason == "deadline":
        task_failure_result(
            result,
            "task_deadline_exceeded",
            "task exceeded its execution deadline",
        )
    elif stop_reason == "output_limit":
        task_failure_result(
            result,
            "task_output_limit",
            "task protocol output exceeded its limit",
        )
    elif (result.get("final") or {}).get("code") == "task_event_limit":
        stop_reason = "protocol_error"
    else:
        stop_reason = finalize_judge_protocol(
            result,
            parse_state,
            return_code,
            read_error=capture is not None and capture.error is not None,
        )
    publish_task_progress(jobs, lock, job_id, result, log)
    return {"returncode": return_code, "stop_reason": stop_reason, "logs": log.text}


class WebUiTaskService:
    """Core orchestration for WebUI sandbox and temporary submissions.

    The service deliberately has no Flask dependency.  Adapters provide the
    already validated problem description and may inject a supervisor hook for
    tests or alternate launchers; all task state, queue callbacks, worker
    lifecycles and cleanup decisions remain here.
    """

    def __init__(
        self,
        *,
        workspace_root,
        local_judge_script,
        executor,
        result_ttl=60 * 60,
        max_completed=32,
        queue_deadline=5 * 60,
        execution_deadline=30 * 60,
        total_deadline=35 * 60,
        force_cancel_after=3.0,
        log_bytes=256 * 1024,
        event_bytes=2 * 1024 * 1024,
        final_event_bytes=64 * 1024,
        process_output_bytes=16 * 1024 * 1024,
        error_bytes=64 * 1024,
    ):
        self.workspace_root = Path(workspace_root)
        self.local_judge_script = Path(local_judge_script)
        self.executor = executor
        self.result_ttl = float(result_ttl)
        self.max_completed = int(max_completed)
        self.queue_deadline = float(queue_deadline)
        self.execution_deadline = float(execution_deadline)
        self.total_deadline = float(total_deadline)
        self.force_cancel_after = float(force_cancel_after)
        self.error_bytes = int(error_bytes)
        self.limits = JudgeSupervisorLimits(
            log_bytes=int(log_bytes),
            event_bytes=int(event_bytes),
            final_event_bytes=int(final_event_bytes),
            process_output_bytes=int(process_output_bytes),
            force_cancel_after=self.force_cancel_after,
        )
        self.sandbox_jobs = {}
        self.sandbox_lock = threading.Lock()
        self.sandbox_processes = {}
        self.sandbox_cancel_files = {}
        self.submission_jobs = {}
        self.submission_lock = threading.Lock()
        self.submission_processes = {}
        self.submission_cancel_files = {}
        self._problem_locks = {}
        self._problem_locks_guard = threading.Lock()
        self.supervisor_hook = None
        try:
            cleanup_stale_submission_workspaces(self._root())
        except OSError:
            pass

    def _root(self):
        return Path(self.workspace_root).resolve()

    def _registry(self, kind):
        if kind == "sandbox":
            return self.sandbox_jobs, self.sandbox_lock
        if kind == "submission":
            return self.submission_jobs, self.submission_lock
        raise ValueError("unknown WebUI task kind")

    def _process_state(self, kind):
        if kind == "sandbox":
            return self.sandbox_processes, self.sandbox_cancel_files
        if kind == "submission":
            return self.submission_processes, self.submission_cancel_files
        raise ValueError("unknown WebUI task kind")

    def _prune(self, jobs):
        prune_job_registry(
            jobs,
            result_ttl=self.result_ttl,
            max_completed=self.max_completed,
            now=time.time(),
        )

    def _claim(self, jobs, lock, job_id):
        return claim_task_execution(
            jobs,
            lock,
            job_id,
            execution_deadline=self.execution_deadline,
            wall_time=time.time,
            monotonic_time=time.monotonic,
        )

    def _discard(self, jobs, lock, job_id, reason, *, submission=False):
        return discard_queued_job(
            jobs,
            lock,
            job_id,
            reason,
            submission=submission,
            result_ttl=self.result_ttl,
            max_completed=self.max_completed,
            now=time.time(),
        )

    def _worker_error(self, jobs, lock, job_id, exc, *, submission=False):
        return task_executor_error(
            jobs,
            lock,
            job_id,
            exc,
            submission=submission,
            workspace_root=self._root(),
            error_limit_bytes=self.error_bytes,
            result_ttl=self.result_ttl,
            max_completed=self.max_completed,
            now=time.time(),
        )

    @staticmethod
    def _release_slot(task_slot):
        if task_slot is not None:
            task_slot.release()

    def _run_with_slot(self, task_slot, callback):
        try:
            return callback()
        finally:
            self._release_slot(task_slot)

    def _discard_with_slot(self, task_slot, callback, reason):
        try:
            return callback(reason)
        finally:
            self._release_slot(task_slot)

    def _problem_lock(self, problem_dir):
        key = str(Path(problem_dir).resolve())
        with self._problem_locks_guard:
            return self._problem_locks.setdefault(key, threading.Lock())

    def _acquire_problem_lock(self, lock, jobs, jobs_lock, job_id, deadline):
        return acquire_task_lock(
            lock,
            jobs,
            jobs_lock,
            job_id,
            deadline,
            monotonic_time=time.monotonic,
        )

    def _supervise(self, job_id, command, env, cancel_file, result, **kwargs):
        if self.supervisor_hook is not None:
            return self.supervisor_hook(
                job_id, command, env, cancel_file, result, **kwargs
            )
        return supervise_judge_process(
            job_id,
            command,
            env,
            cancel_file,
            result,
            limits=self.limits,
            **kwargs,
        )

    def _finish_failure(
        self,
        jobs,
        lock,
        job_id,
        result,
        code,
        message,
        *,
        job_status="failed",
        final_status="failed",
        verdict=None,
    ):
        task_failure_result(result, code, message, status=final_status)
        with lock:
            job = jobs.get(job_id)
            if job is not None:
                update = {
                    "status": job_status,
                    "result": result,
                    "logs": message,
                    "finished_at": time.time(),
                    "failure_code": code,
                }
                if verdict is not None:
                    update["verdict"] = verdict
                job.update(update)
                self._prune(jobs)

    def run_sandbox_job(self, job_id, info):
        jobs, jobs_lock = self.sandbox_jobs, self.sandbox_lock
        processes, cancel_files = self.sandbox_processes, self.sandbox_cancel_files
        result = empty_sandbox_result(info)
        problem_lock = None
        acquired = False
        try:
            execution_deadline, start_failure = self._claim(jobs, jobs_lock, job_id)
            if start_failure == "cancelled":
                self._discard(jobs, jobs_lock, job_id, "cancelled")
                return
            if start_failure is not None:
                code = "task_deadline_exceeded" if start_failure == "deadline" else "task_worker_failed"
                message = "task exceeded its total deadline" if start_failure == "deadline" else "task is unavailable"
                self._finish_failure(jobs, jobs_lock, job_id, result, code, message)
                return
            problem_lock = self._problem_lock(info["dir"])
            lock_status = self._acquire_problem_lock(
                problem_lock, jobs, jobs_lock, job_id, execution_deadline
            )
            if lock_status != "acquired":
                code = "cancelled" if lock_status == "cancelled" else "task_deadline_exceeded"
                message = "task cancelled" if lock_status == "cancelled" else "task exceeded its total deadline"
                self._finish_failure(
                    jobs,
                    jobs_lock,
                    job_id,
                    result,
                    code,
                    message,
                    job_status="cancelled" if lock_status == "cancelled" else "failed",
                    final_status=code,
                )
                return
            acquired = True
            with tempfile.TemporaryDirectory(prefix="probhub-webui-sandbox-control-") as temp:
                cancel_file = Path(temp) / "cancel.requested"
                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
                run = self._supervise(
                    job_id,
                    [sys.executable, str(self.local_judge_script), info["dir"], "--jsonl", "--cancellable"],
                    env,
                    cancel_file,
                    result,
                    jobs=jobs,
                    lock=jobs_lock,
                    processes=processes,
                    cancel_files=cancel_files,
                    execution_deadline=execution_deadline,
                )
            final = result.get("final") or {}
            with jobs_lock:
                job = jobs.get(job_id)
                if job is not None:
                    cancelled = (
                        bool(job.get("cancel_requested"))
                        or run["stop_reason"] == "cancelled"
                        or final.get("status") == "cancelled"
                    )
                    if cancelled:
                        task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
                        final = result["final"]
                    final_ok = bool(final.get("ok")) and run["returncode"] == 0
                    job.update(
                        status="cancelled" if cancelled else ("success" if final_ok else "failed"),
                        returncode=run["returncode"],
                        result=result,
                        logs=run["logs"],
                        finished_at=time.time(),
                        failure_code=None if final_ok else final.get("code"),
                    )
                    self._prune(jobs)
        except Exception as exc:
            cancelled = task_cancel_requested(jobs, jobs_lock, job_id)
            code = "cancelled" if cancelled else "task_worker_failed"
            message = "task cancelled" if cancelled else bounded_error_text(exc, self.error_bytes)
            self._finish_failure(
                jobs,
                jobs_lock,
                job_id,
                result,
                code,
                message,
                job_status="cancelled" if cancelled else "failed",
                final_status=code,
            )
        finally:
            if acquired:
                problem_lock.release()

    def run_submission_job(self, job_id, info, filename, source):
        jobs, jobs_lock = self.submission_jobs, self.submission_lock
        processes, cancel_files = self.submission_processes, self.submission_cancel_files
        result = empty_sandbox_result(info)
        result["submission"] = {
            "task_id": job_id,
            "filename": filename,
            "workspace_cleaned": False,
        }
        task_root = self._root() / ".probhub" / "submissions" / job_id
        try:
            execution_deadline, start_failure = self._claim(jobs, jobs_lock, job_id)
            if start_failure == "cancelled":
                self._discard(jobs, jobs_lock, job_id, "cancelled", submission=True)
                return
            if start_failure is not None:
                code = "task_deadline_exceeded" if start_failure == "deadline" else "task_worker_failed"
                message = "task exceeded its total deadline" if start_failure == "deadline" else "task is unavailable"
                result["submission"].update(workspace_cleaned=not task_root.exists(), verdict="FAIL")
                self._finish_failure(jobs, jobs_lock, job_id, result, code, message, verdict="FAIL")
                return
            with temporary_submission_workspace(
                self._root(),
                info["dir"],
                job_id,
                filename,
                source,
                cancel_requested=lambda: task_cancel_requested(jobs, jobs_lock, job_id),
                deadline_monotonic=execution_deadline,
            ) as prepared:
                cancel_file = prepared.root / "cancel.requested"
                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
                run = self._supervise(
                    job_id,
                    [sys.executable, str(self.local_judge_script), str(prepared.problem_dir), "--jsonl", "--no-cache", "--cancellable"],
                    env,
                    cancel_file,
                    result,
                    jobs=jobs,
                    lock=jobs_lock,
                    processes=processes,
                    cancel_files=cancel_files,
                    execution_deadline=execution_deadline,
                )
            workspace_cleaned = bool(prepared.cleanup_succeeded)
            result["submission"]["workspace_cleaned"] = workspace_cleaned
            if prepared.cleanup_error:
                result["submission"]["cleanup_error"] = bounded_error_text(prepared.cleanup_error, self.error_bytes)
            with jobs_lock:
                job = jobs.get(job_id)
                if job is not None:
                    cancelled = (
                        bool(job.get("cancel_requested"))
                        or run["stop_reason"] == "cancelled"
                        or (result.get("final") or {}).get("status") == "cancelled"
                    )
                    deadline_failed = run["stop_reason"] == "deadline" or time.monotonic() >= execution_deadline
                    infrastructure_failed = run["stop_reason"] in {"deadline", "output_limit", "missing", "protocol_error", PROCESS_CLEANUP_FAILED}
                    if not workspace_cleaned:
                        original = "cancelled" if cancelled else ("task_deadline_exceeded" if deadline_failed else (result.get("final") or {}).get("code"))
                        if original:
                            result["submission"]["original_failure_code"] = original
                        task_failure_result(result, "submission_cleanup_failed", "submission workspace cleanup failed")
                        infrastructure_failed = True
                        cancelled = False
                        verdict = "FAIL"
                    elif cancelled:
                        verdict = "CANCELLED"
                        task_failure_result(result, "cancelled", "task cancelled", status="cancelled")
                    elif deadline_failed:
                        verdict = "FAIL"
                        task_failure_result(result, "task_deadline_exceeded", "task exceeded its total deadline")
                        infrastructure_failed = True
                    else:
                        verdict = submission_verdict(result)
                    result["submission"]["verdict"] = verdict
                    status = "cancelled" if cancelled else ("failed" if infrastructure_failed else "completed")
                    job.update(
                        status=status,
                        verdict=verdict,
                        returncode=run["returncode"],
                        logs=run["logs"],
                        result=result,
                        finished_at=time.time(),
                        failure_code=(result.get("final") or {}).get("code") or None,
                    )
                    self._prune(jobs)
        except SubmissionPreparationCancelled as exc:
            workspace_cleaned = not task_root.exists()
            verdict = "CANCELLED" if workspace_cleaned else "FAIL"
            code = "cancelled" if workspace_cleaned else "submission_cleanup_failed"
            result["submission"].update(workspace_cleaned=workspace_cleaned, verdict=verdict)
            if not workspace_cleaned:
                result["submission"].update(cleanup_error="submission workspace still exists after cancellation", original_failure_code="cancelled")
            task_failure_result(result, code, "task cancelled" if workspace_cleaned else "submission workspace cleanup failed", status="cancelled" if workspace_cleaned else "failed")
            self._publish_exception_result(
                jobs,
                jobs_lock,
                job_id,
                result,
                verdict,
                bounded_error_text(exc, self.error_bytes),
                code,
                cancelled=workspace_cleaned,
            )
        except SubmissionPreparationDeadlineExceeded as exc:
            workspace_cleaned = not task_root.exists()
            result["submission"].update(workspace_cleaned=workspace_cleaned, verdict="FAIL")
            code = "task_deadline_exceeded" if workspace_cleaned else "submission_cleanup_failed"
            if not workspace_cleaned:
                result["submission"].update(cleanup_error="submission workspace still exists after deadline", original_failure_code="task_deadline_exceeded")
            task_failure_result(result, code, "task exceeded its total deadline" if workspace_cleaned else "submission workspace cleanup failed")
            self._publish_exception_result(jobs, jobs_lock, job_id, result, "FAIL", bounded_error_text(exc, self.error_bytes), code)
        except Exception as exc:
            cancelled = task_cancel_requested(jobs, jobs_lock, job_id)
            verdict = "CANCELLED" if cancelled else "FAIL"
            code = "cancelled" if cancelled else "task_worker_failed"
            message = "task cancelled" if cancelled else bounded_error_text(exc, self.error_bytes)
            workspace_cleaned = not task_root.exists()
            if not workspace_cleaned:
                result["submission"]["original_failure_code"] = code
                code = "submission_cleanup_failed"
                verdict = "FAIL"
                message = "submission workspace cleanup failed"
                cancelled = False
            result["submission"].update(workspace_cleaned=workspace_cleaned, verdict=verdict)
            if not workspace_cleaned:
                result["submission"]["cleanup_error"] = "submission workspace still exists after failure"
            task_failure_result(result, code, message, status=code)
            self._publish_exception_result(jobs, jobs_lock, job_id, result, verdict, message, code, cancelled=cancelled)

    def _publish_exception_result(self, jobs, lock, job_id, result, verdict, logs, code, *, cancelled=False):
        with lock:
            if job_id in jobs:
                jobs[job_id].update(
                    status="cancelled" if cancelled else "failed",
                    verdict=verdict,
                    logs=logs,
                    result=result,
                    finished_at=time.time(),
                    failure_code=code,
                )
                self._prune(jobs)

    def _enqueue(self, kind, info, *, filename=None, source=None, task_slot=None, worker=None):
        jobs, jobs_lock = self._registry(kind)
        submission = kind == "submission"
        job_id = uuid.uuid4().hex
        now = time.time()
        try:
            with jobs_lock:
                self._prune(jobs)
                if submission:
                    protected = [
                        current_id
                        for current_id, current_job in jobs.items()
                        if current_job.get("status") in ACTIVE_TASK_STATUSES
                    ]
                    cleanup_stale_submission_workspaces(
                        self._root(), protected_task_ids=protected
                    )
                job = {
                    "status": "queued",
                    "logs": "",
                    "logs_truncated": False,
                    "result": empty_sandbox_result(info) if not submission else None,
                    "created_at": now,
                    "queue_deadline_at": now + self.queue_deadline,
                    "deadline_at": now + self.total_deadline,
                    "_deadline_monotonic": time.monotonic() + self.total_deadline,
                    "cancel_requested": False,
                }
                if submission:
                    job.update(verdict="PENDING", filename=filename)
                jobs[job_id] = job

            runner = worker or (
                self.run_submission_job if submission else self.run_sandbox_job
            )
            callback = lambda: self._run_with_slot(
                task_slot,
                lambda: runner(job_id, info, filename, source)
                if submission
                else runner(job_id, info),
            )

            def on_discard(reason):
                return self._discard_with_slot(
                    task_slot,
                    lambda discard_reason: self._discard(
                        jobs,
                        jobs_lock,
                        job_id,
                        discard_reason,
                        submission=submission,
                    ),
                    reason,
                )

            def on_error(exc):
                self._worker_error(
                    jobs, jobs_lock, job_id, exc, submission=submission
                )

            accepted = self.executor.submit(
                job_id,
                callback,
                queue_timeout=self.queue_deadline,
                on_discard=on_discard,
                on_error=on_error,
            )
        except BaseException:
            self._release_slot(task_slot)
            with jobs_lock:
                jobs.pop(job_id, None)
            raise
        if not accepted:
            self._release_slot(task_slot)
            with jobs_lock:
                jobs.pop(job_id, None)
        return accepted, job_id

    def enqueue_sandbox(self, info, **kwargs):
        return self._enqueue("sandbox", info, **kwargs)

    def enqueue_submission(self, info, filename, source, **kwargs):
        return self._enqueue(
            "submission", info, filename=filename, source=source, **kwargs
        )

    def get(self, kind, job_id):
        self.executor.expire_pending()
        jobs, jobs_lock = self._registry(kind)
        with jobs_lock:
            self._prune(jobs)
            job = jobs.get(job_id)
            return None if job is None else public_job_payload(job)

    def cancel(self, kind, job_id):
        jobs, jobs_lock = self._registry(kind)
        _, cancel_files = self._process_state(kind)
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return None
            if job.get("status") not in ACTIVE_TASK_STATUSES:
                state = {"status": job.get("status")}
                if kind == "submission":
                    state["verdict"] = job.get("verdict")
                return state
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            cancel_file = cancel_files.get(job_id)
        if cancel_file is not None:
            touch_cancel_file(cancel_file)
        self.executor.cancel_pending(job_id)
        with jobs_lock:
            current = jobs.get(job_id, {})
            state = {"status": current.get("status", "cancelling")}
            if kind == "submission":
                state["verdict"] = current.get("verdict")
            return state

    def shutdown(self):
        process_items = []
        cancel_paths = []
        for jobs, processes, cancel_files, lock in (
            (self.sandbox_jobs, self.sandbox_processes, self.sandbox_cancel_files, self.sandbox_lock),
            (self.submission_jobs, self.submission_processes, self.submission_cancel_files, self.submission_lock),
        ):
            with lock:
                for job in jobs.values():
                    if job.get("status") in ACTIVE_TASK_STATUSES:
                        job["cancel_requested"] = True
                        job["status"] = "cancelling"
                process_items.extend(processes.values())
                cancel_paths.extend(cancel_files.values())
        for cancel_path in cancel_paths:
            touch_cancel_file(cancel_path)
        self.executor.shutdown(wait=False, cancel_pending=True)
        for proc in process_items:
            if proc.poll() is None:
                terminate_external_process_tree(proc, snapshot_process_tree_identities(proc.pid))
        self.executor.shutdown(wait=True, cancel_pending=True)
