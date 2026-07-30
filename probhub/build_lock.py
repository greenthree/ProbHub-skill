import errno
import os
import stat
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


def _open_lock_stream(path, *, no_follow=False):
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    if no_follow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ProbHubError(
            f"failed to open ProbHub workspace lock {path}: {exc}",
            code="workspace_lock_failed",
        ) from exc
    try:
        if no_follow:
            opened = os.fstat(descriptor)
            linked = os.lstat(path)
            parent = path.parent.absolute()
            if (
                not stat.S_ISREG(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                or parent.resolve() != parent
            ):
                raise ProbHubError(
                    f"ProbHub workspace lock must not use a symlink or junction: {path}",
                    code="workspace_lock_unsafe",
                )
        return os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise


def _verify_lock_identity(stream, path, *, no_follow=False):
    if not no_follow:
        return
    try:
        opened = os.fstat(stream.fileno())
        linked = os.lstat(path)
    except OSError as exc:
        raise ProbHubError(
            f"ProbHub workspace lock changed while being acquired: {path}: {exc}",
            code="workspace_lock_unsafe",
        ) from exc
    if (
        not stat.S_ISREG(linked.st_mode)
        or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
    ):
        raise ProbHubError(
            f"ProbHub workspace lock changed while being acquired: {path}",
            code="workspace_lock_unsafe",
        )


@contextmanager
def workspace_file_lock(
    root,
    relative_path,
    *,
    busy_code="workspace_busy",
    busy_message="another ProbHub operation is already running",
    wait_timeout=0,
    poll_interval=0.1,
    no_follow=False,
):
    path = Path(root).resolve() / Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = _open_lock_stream(path, no_follow=no_follow)
    try:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0, float(wait_timeout))
        while True:
            try:
                _acquire(stream)
                _verify_lock_identity(stream, path, no_follow=no_follow)
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
