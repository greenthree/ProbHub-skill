import json
import os
import re
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


def write_yaml(path, data):
    _atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))


def write_json(path, data):
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_write_json(path, data):
    write_json(path, data)
