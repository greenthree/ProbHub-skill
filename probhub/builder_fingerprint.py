"""Deterministic identities for the PDF and package artifact builder."""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from importlib import metadata as importlib_metadata
from pathlib import Path, PureWindowsPath

from . import __version__
from .errors import ProbHubError
from .process_control import run_managed_to_files


BUILDER_FINGERPRINT_SCHEMA_VERSION = 1
BUILD_MANIFEST_SCHEMA_VERSION = 4
GENERATION_SCHEMA_VERSION = 3

TYPST_TIMEOUT_SECONDS = 10
TYPST_OUTPUT_LIMIT_BYTES = 1024 * 1024
TYPST_VERSION_PATTERN = re.compile(r"^typst\s+(\d+\.\d+\.\d+)(?:\s|$)")
FIXED_FONT_FAMILY = "Noto Sans CJK SC"
FIXED_FONT_POLICY = "bundled-only-v1"
FIXED_FONT_SHA256 = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
FIXED_FONT_FILENAME = "NotoSansCJKsc-Regular.otf"
TYPST_TEXT_TEMPLATE_SUFFIXES = {
    ".csv",
    ".json",
    ".md",
    ".svg",
    ".toml",
    ".txt",
    ".typ",
    ".xml",
    ".yaml",
    ".yml",
}


def fixed_font_path():
    return Path(__file__).resolve().parent / "assets" / "fonts" / FIXED_FONT_FILENAME


def fixed_font_directory():
    return fixed_font_path().parent


def _is_link_like(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fixed_font_identity():
    path = fixed_font_path()
    if path.is_symlink() or not path.is_file():
        raise ProbHubError(
            f"bundled fixed Typst font is missing or unsafe: {path}",
            code="builder_fingerprint_failed",
        )
    actual = _hash_file(path)
    if actual != FIXED_FONT_SHA256:
        raise ProbHubError(
            "bundled fixed Typst font checksum mismatch",
            code="builder_fingerprint_failed",
        )
    return {
        "family": FIXED_FONT_FAMILY,
        "policy": FIXED_FONT_POLICY,
        "sha256": actual,
    }


def _typst_probe():
    executable = shutil.which("typst")
    if not executable:
        return {"ok": False, "path": None, "version": None, "diagnostic": "typst was not found"}
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-builder-fingerprint-") as temp:
            stdout_path = Path(temp) / "stdout"
            stderr_path = Path(temp) / "stderr"
            result = run_managed_to_files(
                [executable, "--version"],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=TYPST_TIMEOUT_SECONDS,
                memory_limit_mb=None,
                output_limit_bytes=TYPST_OUTPUT_LIMIT_BYTES,
                process_limit=8,
            )
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    except OSError as exc:
        return {"ok": False, "path": executable, "version": None, "diagnostic": str(exc)}

    line = (stdout or stderr).strip().splitlines()
    version_line = line[0].strip() if line else ""
    match = TYPST_VERSION_PATTERN.match(version_line)
    if result["reason"] != "completed":
        detail = "timed out after 10s" if result["reason"] == "time_limit" else (result.get("message") or result["reason"])
        return {"ok": False, "path": executable, "version": None, "diagnostic": detail}
    if result["returncode"] != 0 or match is None:
        return {
            "ok": False,
            "path": executable,
            "version": None,
            "diagnostic": version_line or "typst --version returned invalid output",
        }
    return {"ok": True, "path": executable, "version": match.group(1), "diagnostic": None}


def builder_toolchain_status():
    """Return the shared, bounded Typst and bundled-font probe used by doctor."""
    typst = _typst_probe()
    try:
        font = {"ok": True, **fixed_font_identity(), "diagnostic": None}
    except ProbHubError as exc:
        font = {
            "ok": False,
            "family": FIXED_FONT_FAMILY,
            "policy": FIXED_FONT_POLICY,
            # Keep the pinned expected checksum visible to Doctor even when the
            # packaged asset is absent or has been modified.
            "sha256": FIXED_FONT_SHA256,
            "diagnostic": str(exc),
        }
    try:
        pypdf_version = importlib_metadata.version("pypdf")
    except importlib_metadata.PackageNotFoundError:
        pypdf_version = None
    return {"typst": typst, "font": font, "pypdf_version": pypdf_version}


def typst_template_paths(root, workspace):
    root = Path(root).resolve()
    typst_value = (workspace.get("typst") or {}).get("directory", "typst-statement/正式赛")
    relative = Path(typst_value)
    if relative.is_absolute() or PureWindowsPath(str(typst_value)).is_absolute():
        raise ProbHubError(
            f"Typst directory must stay inside the workspace: {typst_value}",
            code="builder_fingerprint_failed",
        )
    candidate = root / relative
    cursor = root
    for part in relative.parts:
        cursor /= part
        if _is_link_like(cursor):
            raise ProbHubError(
                f"Typst template path must not traverse a link or reparse point: {cursor}",
                code="builder_fingerprint_failed",
            )
    typst_dir = candidate.resolve()
    try:
        typst_dir.relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            f"Typst directory must stay inside the workspace: {typst_dir}",
            code="builder_fingerprint_failed",
        ) from exc
    # Keep the caller's spelling of the workspace path. On Windows runners,
    # resolving a descendant can expand an 8.3 parent (RUNNER~1) while the
    # workspace root remains short; paths returned by os.walk then cannot be
    # made relative to the original root even though they name the same tree.
    typst_root = candidate.parent
    if not typst_root.is_dir() or _is_link_like(typst_root):
        raise ProbHubError(
            f"Typst template directory is missing or unsafe: {typst_root}",
            code="builder_fingerprint_failed",
        )

    paths = []
    generated = {
        (candidate / "main.pdf").resolve(),
        (candidate / "problems.json").resolve(),
    }

    try:
        pending = [typst_root]
        while pending:
            current = pending.pop()
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
            for entry in entries:
                path = current / entry.name
                if _is_link_like(path):
                    raise ProbHubError(
                        f"Typst template tree must not contain links or reparse points: {path}",
                        code="builder_fingerprint_failed",
                    )
                info = os.lstat(path)
                if stat.S_ISDIR(info.st_mode):
                    if entry.name != ".preview":
                        pending.append(path)
                    continue
                if path.resolve() in generated:
                    continue
                if path.name.startswith(".probhub-") and path.suffix.lower() == ".typ":
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ProbHubError(
                        f"Typst template input is not a regular file: {path}",
                        code="builder_fingerprint_failed",
                    )
                paths.append(path)
    except ProbHubError:
        raise
    except OSError as exc:
        raise ProbHubError(
            f"cannot inspect Typst template inputs: {exc}",
            code="builder_fingerprint_failed",
        ) from exc
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def compute_typst_template_hash(root, workspace):
    root = Path(root).resolve()
    digest = hashlib.sha256()
    paths = typst_template_paths(root, workspace)
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        if path.suffix.lower() in TYPST_TEXT_TEMPLATE_SUFFIXES:
            content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _fingerprint_digest(fields):
    payload = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_builder_fingerprint(root, workspace):
    toolchain = builder_toolchain_status()
    typst = toolchain["typst"]
    font = toolchain["font"]
    if not typst["ok"]:
        raise ProbHubError(
            f"cannot determine Typst builder identity: {typst['diagnostic']}",
            code="builder_fingerprint_failed",
        )
    if not font["ok"]:
        raise ProbHubError(
            f"cannot determine fixed font identity: {font['diagnostic']}",
            code="builder_fingerprint_failed",
        )
    if not toolchain["pypdf_version"]:
        raise ProbHubError(
            "cannot determine pypdf builder identity",
            code="builder_fingerprint_failed",
        )
    fields = {
        "schema_version": BUILDER_FINGERPRINT_SCHEMA_VERSION,
        "probhub_version": __version__,
        "build_manifest_schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
        "generation_schema_version": GENERATION_SCHEMA_VERSION,
        "typst_version": typst["version"],
        "pypdf_version": toolchain["pypdf_version"],
        "template_hash": compute_typst_template_hash(root, workspace),
        "font": {
            "family": font["family"],
            "policy": font["policy"],
            "sha256": font["sha256"],
        },
    }
    return {**fields, "digest": _fingerprint_digest(fields)}


def builder_fingerprint_stale_fields(recorded, current, prefix="builder_fingerprint"):
    """Return stable, field-level stale reasons without trusting stored input."""
    if not isinstance(recorded, dict):
        return [prefix]
    fields = []
    for key in (
        "schema_version",
        "probhub_version",
        "build_manifest_schema_version",
        "generation_schema_version",
        "typst_version",
        "pypdf_version",
        "template_hash",
    ):
        if recorded.get(key) != current.get(key):
            fields.append(f"{prefix}.{key}")
    recorded_font = recorded.get("font")
    current_font = current.get("font")
    if not isinstance(recorded_font, dict) or not isinstance(current_font, dict):
        fields.append(f"{prefix}.font")
    else:
        for key in ("family", "policy", "sha256"):
            if recorded_font.get(key) != current_font.get(key):
                fields.append(f"{prefix}.font.{key}")
    if recorded.get("digest") != current.get("digest") and not fields:
        fields.append(f"{prefix}.digest")
    return fields
