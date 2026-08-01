import json
import os
import re
import stat
import uuid
from pathlib import Path

import yaml

from .errors import ProbHubError


def read_yaml(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ProbHubError(
            f"failed to read YAML {path}: {exc}",
            code="invalid_yaml",
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProbHubError(
            f"invalid YAML in {path}: {exc}",
            code="invalid_yaml",
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProbHubError(
            f"invalid YAML in {path}: expected a mapping at the document root",
            code="invalid_yaml",
        )
    return data


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        # LF also on Windows: our own YAML/JSON must hash identically across
        # platforms, or source_hash forks between Windows and Linux.
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path, text):
    _atomic_write_text(path, text)


def atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_CR_RUN_BEFORE_LF = re.compile(rb"\r+\n")


def normalize_newlines(payload):
    """Normalise CRLF to LF so generated data is byte-identical across platforms.

    A run of CRs before an LF collapses with it (Windows text-mode CRT turns an
    explicit "\\r\\n" into "\\r\\r\\n"); a bare CR with no following LF is data
    and is preserved.
    """
    return _CR_RUN_BEFORE_LF.sub(b"\n", payload)


def read_bounded_text(path, limit_bytes):
    """Read a UTF-8 diagnostic prefix without loading an unbounded file."""
    path = Path(path)
    limit = max(int(limit_bytes), 0)
    try:
        info = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or (
            reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            raise OSError(f"diagnostic path is a link or reparse point: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"diagnostic path is not a regular file: {path}")
        observed = info.st_size
        with path.open("rb") as stream:
            sample = stream.read(limit + 1)
    except FileNotFoundError:
        return {
            "text": "",
            "observed_bytes": 0,
            "retained_bytes": 0,
            "truncated": False,
            "exists": False,
        }
    retained = sample[:limit]
    return {
        "text": retained.decode("utf-8", errors="replace"),
        "observed_bytes": max(int(observed), len(sample)),
        "retained_bytes": len(retained),
        "truncated": int(observed) > len(retained) or len(sample) > limit,
        "exists": True,
    }


def write_yaml(path, data):
    _atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))


def write_json(path, data):
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_write_json(path, data):
    write_json(path, data)
