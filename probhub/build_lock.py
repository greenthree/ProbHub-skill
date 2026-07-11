import errno
import os
from contextlib import contextmanager
from pathlib import Path

from .errors import ProbHubError


BUILD_LOCK_FILE = Path(".probhub/build.lock")


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
def workspace_build_lock(root):
    path = Path(root).resolve() / BUILD_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        try:
            _acquire(stream)
        except OSError as exc:
            busy = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
            code = "build_busy" if busy else "build_lock_failed"
            message = (
                "another ProbHub writer is already running"
                if busy
                else f"failed to acquire ProbHub build lock: {exc}"
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
