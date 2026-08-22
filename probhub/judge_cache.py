"""Content-addressed Judge cache primitives shared by Core adapters.

The local Judge remains responsible for protocol and event presentation, but
cache identity and persistence are Core concerns.  This module intentionally
contains no subprocess or UI code so Judge QA, stress and future adapters can
reuse the exact same cache semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from typing import Any

from .calibration import SANDBOX_CACHE_SCHEMA_VERSION


CACHE_SCHEMA_VERSION = SANDBOX_CACHE_SCHEMA_VERSION
CACHE_FILENAME = "sandbox-cache-v1.json"
CACHE_LIMITS = {"validator": 4000, "case": 12000, "probe": 2000}

_FILE_DIGEST_CACHE: dict[tuple[Any, ...], str] = {}


def hash_parts(*parts: Any) -> str:
    """Hash length-delimited values without ambiguity between fields."""

    digest = hashlib.sha256()
    for part in parts:
        payload = part if isinstance(part, bytes) else str(part).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def file_digest(path: str | os.PathLike[str]) -> str:
    """Return a memoized SHA-256 digest keyed by the file identity metadata."""

    absolute = os.path.abspath(os.fspath(path))
    stat = os.stat(absolute)
    cache_key = (
        absolute,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
        getattr(stat, "st_ino", 0),
    )
    cached = _FILE_DIGEST_CACHE.get(cache_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(absolute, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if len(_FILE_DIGEST_CACHE) > 4096:
        _FILE_DIGEST_CACHE.clear()
    _FILE_DIGEST_CACHE[cache_key] = value
    return value


def validator_cache_key(program_fingerprint: str, in_file: str | os.PathLike[str]) -> str:
    return hash_parts(
        CACHE_SCHEMA_VERSION,
        "validator",
        platform.system(),
        program_fingerprint,
        file_digest(in_file),
    )


def testcase_cache_key(
    program_fingerprint: str,
    in_file: str | os.PathLike[str],
    ans_file: str | os.PathLike[str],
    time_limit: float | int,
    memory_limit: float | int,
    judge_type: str = "standard",
    judge_fingerprint: str = "",
    judge_options_fingerprint: str = "",
) -> str:
    return hash_parts(
        CACHE_SCHEMA_VERSION,
        "case",
        platform.system(),
        platform.machine(),
        program_fingerprint,
        judge_type,
        judge_fingerprint,
        judge_options_fingerprint,
        file_digest(in_file),
        file_digest(ans_file),
        f"{float(time_limit):.9g}",
        int(memory_limit),
    )


def calibration_probe_cache_key(
    program_fingerprint: str,
    in_file: str | os.PathLike[str],
    time_limit: float | int,
    memory_limit: float | int,
    output_limit: float | int,
    process_limit: int,
    probe_factor: float,
) -> str:
    return hash_parts(
        CACHE_SCHEMA_VERSION,
        "calibration-probe",
        platform.system(),
        platform.machine(),
        program_fingerprint,
        file_digest(in_file),
        f"{float(time_limit):.9g}",
        int(memory_limit),
        int(output_limit),
        int(process_limit),
        f"{float(probe_factor):.9g}",
    )


class SandboxCache:
    """Bounded, versioned cache for compile and per-case Judge results."""

    def __init__(
        self,
        prob_dir: str | os.PathLike[str],
        enabled: bool = True,
        read_enabled: bool | None = None,
        preserve_existing: bool = False,
    ):
        self.enabled = enabled
        self.read_enabled = enabled if read_enabled is None else bool(read_enabled and enabled)
        self.path = os.path.join(os.fspath(prob_dir), ".probhub", CACHE_FILENAME)
        self.data = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "compile": {},
            "validator": {},
            "case": {},
            "probe": {},
        }
        self.dirty = False
        self.stats = {
            "enabled": enabled,
            "read_enabled": self.read_enabled,
            "mode": "normal" if self.read_enabled else ("refresh" if enabled else "disabled"),
            "compile_hits": 0,
            "compile_misses": 0,
            "validator_hits": 0,
            "validator_misses": 0,
            "case_hits": 0,
            "case_misses": 0,
            "probe_hits": 0,
            "probe_misses": 0,
        }
        if self.read_enabled or (self.enabled and preserve_existing):
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if loaded.get("schema_version") != CACHE_SCHEMA_VERSION:
                return
            for section in ("compile", "validator", "case", "probe"):
                if isinstance(loaded.get(section), dict):
                    self.data[section] = loaded[section]
        except (OSError, ValueError, TypeError):
            pass

    def get(self, section: str, key: str) -> Any:
        if not self.enabled:
            return None
        if not self.read_enabled:
            self.stats[f"{section}_misses"] += 1
            return None
        value = self.data.get(section, {}).get(key)
        counter = f"{section}_hits" if value is not None else f"{section}_misses"
        self.stats[counter] += 1
        return value

    def set(self, section: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        values = self.data.setdefault(section, {})
        values.pop(key, None)
        values[key] = value
        limit = CACHE_LIMITS.get(section)
        if limit and len(values) > limit:
            for old_key in list(values)[: len(values) - limit]:
                values.pop(old_key, None)
        self.dirty = True

    def delete(self, section: str, key: str) -> None:
        if not self.enabled:
            return
        if self.data.get(section, {}).pop(key, None) is not None:
            self.dirty = True

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, self.path)
        self.dirty = False
