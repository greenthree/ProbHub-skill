import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import ProbHubError


BUILD_LOCK_FILE = Path(".probhub/build.lock")
GENERATION_LOCK_FILE = Path(".probhub/generation.lock")


def _acquire(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def workspace_file_lock(
    root,
    relative_path,
    *,
    busy_code="workspace_busy",
    busy_message="another ProbHub operation is already running",
    wait_timeout=0,
    poll_interval=0.1,
):
    path = Path(root).resolve() / Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0, float(wait_timeout))
        while True:
            try:
                _acquire(stream)
                break
            except OSError as exc:
                busy = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                if busy and time.monotonic() < deadline:
                    time.sleep(max(0.01, float(poll_interval)))
                    continue
                code = busy_code if busy else "workspace_lock_failed"
                message = (
                    busy_message
                    if busy
                    else f"failed to acquire ProbHub workspace lock {path}: {exc}"
                )
                raise ProbHubError(message, code=code) from exc
        try:
            yield path
        finally:
            try:
                _release(stream)
            except OSError:
                pass
    finally:
        stream.close()


@contextmanager
def workspace_build_lock(root):
    with workspace_file_lock(
        root,
        BUILD_LOCK_FILE,
        busy_code="build_busy",
        busy_message="another ProbHub writer is already running",
    ) as path:
        yield path


@contextmanager
def workspace_generation_lock(root):
    with workspace_file_lock(
        root,
        GENERATION_LOCK_FILE,
        busy_code="generation_busy",
        busy_message="another ProbHub exam generation is already running",
        wait_timeout=120,
    ) as path:
        yield path
