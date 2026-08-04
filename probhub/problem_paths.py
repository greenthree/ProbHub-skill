import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)


class ProblemPathError(ValueError):
    """A configured problem-local path failed a stable safety check."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def resolve_problem_regular_file(problem_dir, value):
    """Resolve a problem-local regular file without traversing link-like paths."""

    if not isinstance(value, str) or not value.strip():
        raise ProblemPathError("invalid")

    normalized = value.strip().replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ProblemPathError("outside")
    if ".." in posix_path.parts:
        raise ProblemPathError("outside")

    try:
        problem_root = Path(problem_dir).resolve(strict=True)
    except (TypeError, ValueError) as exc:
        raise ProblemPathError("invalid") from exc
    except (OSError, RuntimeError) as exc:
        raise ProblemPathError("missing") from exc
    if not problem_root.is_dir():
        raise ProblemPathError("non_regular")

    parts = posix_path.parts
    if not parts:
        raise ProblemPathError("non_regular")

    current = problem_root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except ValueError as exc:
            raise ProblemPathError("invalid") from exc
        except NotADirectoryError as exc:
            raise ProblemPathError("non_regular") from exc
        except OSError as exc:
            raise ProblemPathError("missing") from exc

        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProblemPathError("link")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ProblemPathError("non_regular")
        elif not stat.S_ISREG(info.st_mode):
            raise ProblemPathError("non_regular")

    try:
        resolved = current.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ProblemPathError("missing") from exc
    except RuntimeError as exc:
        raise ProblemPathError("link") from exc
    except OSError as exc:
        raise ProblemPathError("missing") from exc
    try:
        resolved.relative_to(problem_root)
    except ValueError as exc:
        raise ProblemPathError("outside") from exc
    return resolved
