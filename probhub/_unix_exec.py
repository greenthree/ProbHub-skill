"""Apply Unix-only limits, then replace this helper with the target process."""

import os
import signal
import sys


READY = b"PROBHUB_UNIX_EXEC_READY_V1\n"


def _report_failure(status_fd, message):
    try:
        os.write(status_fd, ("Unix execution helper failed: " + message).encode("utf-8"))
    except OSError:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    status_fd = None
    try:
        if len(argv) < 7 or argv[0] != "--memory-limit-bytes" or argv[2] != "--status-fd":
            raise ValueError("invalid helper arguments")
        limit_bytes = int(argv[1])
        status_fd = int(argv[3])
        if argv[4] not in ("--restore-signals", "--keep-signals"):
            raise ValueError("invalid signal restore mode")
        restore_signals = argv[4] == "--restore-signals"
        if argv[5] != "--" or not argv[6:]:
            raise ValueError("target command is missing")
        if limit_bytes <= 0:
            raise ValueError("memory limit must be positive")

        import resource

        os.set_inheritable(status_fd, False)
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        if restore_signals:
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    signal.signal(signum, signal.SIG_DFL)
        os.write(status_fd, READY)
        os.execvpe(argv[6], argv[6:], os.environ)
    except BaseException as exc:
        if status_fd is not None:
            _report_failure(status_fd, str(exc))
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
