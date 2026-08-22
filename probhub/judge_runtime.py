"""Core compilation and binary-identity primitives for local Judge adapters.

This module owns compilation, compiler/source identity and atomic executable
publication.  Callers may inject reporter, process and identity callbacks so
the legacy JSONL adapter can preserve its observable protocol while the same
implementation is reused by Judge QA and future Core entry points.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from typing import Any, Callable

from .judge_cache import CACHE_SCHEMA_VERSION, file_digest, hash_parts
from .process_control import DEFAULT_PROCESS_LIMIT, run_managed_to_files


class BinaryChangedError(RuntimeError):
    """The executable changed or could not be restored during publication."""


_COMPILER_IDENTITY: str | None = None


class _QuietReporter:
    def text(self, _message: str = "") -> None:
        return None

    def event(self, _type: str, **_payload: Any) -> None:
        return None


def binary_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return the published executable's absolute path, size and SHA-256."""

    absolute = os.path.abspath(os.fspath(path))
    import hashlib

    digest = hashlib.sha256()
    with open(absolute, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        stat = os.fstat(stream.fileno())
    return {
        "path": absolute,
        "size": int(stat.st_size),
        "digest": digest.hexdigest(),
    }


def verify_binary_identities(
    entries: list[dict[str, Any]],
    *,
    identity_fn: Callable[[str], dict[str, Any]] = binary_identity,
) -> dict[str, Any] | None:
    """Check all compiled binaries before the first Judge execution."""

    for entry in entries:
        expected = entry.get("identity") or {}
        path = expected.get("path")
        try:
            actual = identity_fn(path)
        except (OSError, TypeError, ValueError) as exc:
            return {
                "code": "binary_changed",
                "failure_kind": "infrastructure",
                "role": entry.get("role"),
                "program": entry.get("program"),
                "path": path,
                "expected_digest": expected.get("digest"),
                "actual_digest": None,
                "message": f"compiled binary is unavailable before first execution: {path} ({exc})",
            }
        if (
            actual.get("digest") != expected.get("digest")
            or actual.get("size") != expected.get("size")
        ):
            return {
                "code": "binary_changed",
                "failure_kind": "infrastructure",
                "role": entry.get("role"),
                "program": entry.get("program"),
                "path": path,
                "expected_digest": expected.get("digest"),
                "actual_digest": actual.get("digest"),
                "expected_size": expected.get("size"),
                "actual_size": actual.get("size"),
                "message": f"compiled binary changed before first execution: {path}",
            }
    return None


def publish_compiled_binary(
    staged_binary: str,
    out_bin: str,
    staged_identity: dict[str, Any],
    *,
    identity_fn: Callable[[str], dict[str, Any]] = binary_identity,
) -> dict[str, Any]:
    """Atomically publish a compiled binary and restore the old one on mismatch."""

    previous = staged_binary + ".previous"
    had_previous = os.path.isfile(out_bin)
    if had_previous:
        shutil.copyfile(out_bin, previous)
    published = False
    try:
        os.replace(staged_binary, out_bin)
        published = True
        actual = identity_fn(out_bin)
        if (
            actual["digest"] != staged_identity["digest"]
            or actual["size"] != staged_identity["size"]
        ):
            raise BinaryChangedError("published binary identity differs from staged binary")
        return actual
    except Exception:
        if not published:
            raise
        try:
            if had_previous and os.path.isfile(previous):
                os.replace(previous, out_bin)
            elif not had_previous and os.path.lexists(out_bin):
                os.remove(out_bin)
        except OSError as rollback_error:
            raise BinaryChangedError(
                "compiled binary publish failed and the previous binary could not be restored: "
                f"{rollback_error}"
            ) from rollback_error
        raise


def compiler_identity(
    *,
    run_managed_fn: Callable[..., dict[str, Any]] = run_managed_to_files,
) -> str:
    """Probe and memoize the compiler identity used by cache fingerprints."""

    global _COMPILER_IDENTITY
    if _COMPILER_IDENTITY is not None:
        return _COMPILER_IDENTITY
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-compiler-version-") as temp:
            stdout_path = os.path.join(temp, "stdout")
            stderr_path = os.path.join(temp, "stderr")
            result = run_managed_fn(
                ["g++", "--version"],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=10,
                memory_limit_mb=512,
                output_limit_bytes=1024 * 1024,
                process_limit=8,
            )
            with open(stdout_path, "r", encoding="utf-8", errors="replace") as stream:
                stdout = stream.read()
            with open(stderr_path, "r", encoding="utf-8", errors="replace") as stream:
                stderr = stream.read()
        if result["reason"] != "completed" or result["returncode"] != 0:
            raise RuntimeError(result.get("message") or result["reason"])
        first_line = (stdout or stderr).splitlines()[0]
        _COMPILER_IDENTITY = first_line.strip()
    except Exception as exc:
        _COMPILER_IDENTITY = f"g++-unknown:{exc}"
    return _COMPILER_IDENTITY


def source_fingerprint(
    src_file: str | os.PathLike[str],
    prob_dir: str | os.PathLike[str],
    kind: str | None = None,
    *,
    reference_dir: str | os.PathLike[str] | None = None,
    compiler_identity_fn: Callable[[], str] = compiler_identity,
) -> str:
    """Hash source, local headers, testlib and compiler/build identity."""

    src_file = os.path.abspath(os.fspath(src_file))
    prob_dir = os.path.abspath(os.fspath(prob_dir))
    reference_dir = os.fspath(reference_dir) if reference_dir is not None else None
    dependencies = [src_file]
    source_text = ""
    try:
        with open(src_file, "r", encoding="utf-8", errors="replace") as stream:
            source_text = stream.read()
    except OSError:
        pass

    for root, directories, files in os.walk(prob_dir):
        directories[:] = [name for name in directories if name != ".probhub"]
        for name in files:
            if name.endswith((".h", ".hpp")):
                dependencies.append(os.path.join(root, name))
    if "testlib.h" in source_text and reference_dir is not None:
        dependencies.append(os.path.join(reference_dir, "testlib.h"))

    parts = [
        CACHE_SCHEMA_VERSION,
        platform.system(),
        platform.machine(),
        compiler_identity_fn(),
        kind or "unknown",
        "-O2",
        "-std=c++17",
        "-static" if platform.system() == "Windows" else "dynamic",
        "-DFOR_LINUX" if platform.system() == "Windows" and kind == "validator" else "native-lines",
    ]
    for path in sorted(set(os.path.abspath(item) for item in dependencies)):
        if os.path.isfile(path):
            try:
                label = os.path.relpath(path, prob_dir).replace(os.sep, "/")
            except ValueError:
                label = path.replace(os.sep, "/")
            parts.extend((label, file_digest(path)))
    return hash_parts(*parts)


def compile_cpp(
    src_file: str,
    out_bin: str,
    reporter: Any = None,
    kind: str | None = None,
    display_file: str | None = None,
    cache: Any = None,
    fingerprint: str | None = None,
    *,
    reference_dir: str | os.PathLike[str] | None = None,
    source_fingerprint_fn: Callable[..., str] | None = None,
    binary_identity_fn: Callable[[str], dict[str, Any]] = binary_identity,
    publish_fn: Callable[..., dict[str, Any]] | None = None,
    run_managed_fn: Callable[..., dict[str, Any]] = run_managed_to_files,
    process_limit: int = DEFAULT_PROCESS_LIMIT,
) -> dict[str, Any] | bool:
    """Compile one C++ source with cache validation and atomic publication."""

    reporter = reporter or _QuietReporter()
    if not os.path.exists(src_file):
        return False
    file_name = display_file or os.path.basename(src_file)
    if fingerprint is None:
        fingerprint_builder = source_fingerprint_fn or source_fingerprint
        fingerprint = fingerprint_builder(
            src_file,
            os.path.dirname(src_file),
            kind,
            **({"reference_dir": reference_dir} if source_fingerprint_fn is None else {}),
        )
    cache_key = str(file_name).replace(os.sep, "/")

    if cache and cache.enabled:
        cached = cache.get("compile", cache_key)
        binary_matches = False
        if cached and cached.get("fingerprint") == fingerprint and os.path.isfile(out_bin):
            try:
                current_identity = binary_identity_fn(out_bin)
                binary_matches = (
                    cached.get("binary_digest") == current_identity["digest"]
                    and (
                        cached.get("binary_size") is None
                        or cached.get("binary_size") == current_identity["size"]
                    )
                )
            except OSError:
                binary_matches = False
        if binary_matches:
            reporter.text(f"[*] Compiling {file_name}... cached")
            reporter.event(
                "compile",
                kind=kind or "unknown",
                file=file_name,
                ok=True,
                stderr="",
                cached=True,
                binary_digest=current_identity["digest"],
                binary_size=current_identity["size"],
            )
            return current_identity
        if cached is not None:
            cache.stats["compile_hits"] -= 1
            cache.stats["compile_misses"] += 1
            cache.delete("compile", cache_key)

    reporter.text(f"[*] Compiling {file_name}...")
    source_dir = os.path.dirname(os.path.abspath(src_file))
    compile_root = os.path.join(source_dir, ".probhub", "compile")
    reference_dir = os.fspath(reference_dir) if reference_dir is not None else None
    try:
        os.makedirs(compile_root, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=compile_root) as temp:
            staged_binary = os.path.join(
                temp,
                "program.exe" if platform.system() == "Windows" else "program",
            )
            if reference_dir is not None:
                shutil.copyfile(
                    os.path.join(reference_dir, "testlib.h"),
                    os.path.join(temp, "testlib.h"),
                )
            source_arg = os.path.relpath(os.path.abspath(src_file), source_dir)
            output_arg = os.path.relpath(staged_binary, source_dir)
            include_arg = os.path.relpath(temp, source_dir)
            flags = [
                "g++", source_arg, "-o", output_arg,
                "-O2", "-std=c++17", "-I", include_arg,
            ]
            if platform.system() == "Windows":
                flags.insert(4, "-static")
                if kind == "validator":
                    flags.append("-DFOR_LINUX")
            stdout_path = os.path.join(temp, "compiler.out")
            stderr_path = os.path.join(temp, "compiler.stderr")
            ret = run_managed_fn(
                flags,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=60.0,
                memory_limit_mb=2048,
                output_limit_bytes=8 * 1024 * 1024,
                process_limit=process_limit,
                cwd=source_dir,
            )
            try:
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as stream:
                    stderr = stream.read()
            except OSError:
                stderr = ""
            ok = ret["reason"] == "completed" and ret["returncode"] == 0
            staged_identity = None
            if ok:
                staged_identity = binary_identity_fn(staged_binary)
                if publish_fn is None:
                    published_identity = publish_compiled_binary(
                        staged_binary,
                        out_bin,
                        staged_identity,
                        identity_fn=binary_identity_fn,
                    )
                else:
                    published_identity = publish_fn(staged_binary, out_bin, staged_identity)
            else:
                published_identity = None
        if ret["reason"] != "completed":
            stderr = (stderr + "\n" + (ret["message"] or ret["reason"])).strip()
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=ok,
            stderr=stderr,
            cached=False,
            **(
                {
                    "binary_digest": published_identity["digest"],
                    "binary_size": published_identity["size"],
                }
                if published_identity is not None
                else {}
            ),
        )
        if not ok:
            if cache:
                cache.delete("compile", cache_key)
            reporter.text(f"[-] Compile failed:\n{stderr}")
            return False
        if cache:
            cache.set(
                "compile",
                cache_key,
                {
                    "fingerprint": fingerprint,
                    "binary_digest": published_identity["digest"],
                    "binary_size": published_identity["size"],
                },
            )
        return published_identity
    except BinaryChangedError as exc:
        if cache:
            cache.delete("compile", cache_key)
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=False,
            stderr=str(exc),
            cached=False,
            code="binary_changed",
            failure_kind="infrastructure",
        )
        reporter.text(f"[-] Compile error: {exc}")
        raise
    except Exception as exc:
        if cache:
            cache.delete("compile", cache_key)
        reporter.event(
            "compile",
            kind=kind or "unknown",
            file=file_name,
            ok=False,
            stderr=str(exc),
            cached=False,
        )
        reporter.text(f"[-] Compile error: {exc}")
        return False
