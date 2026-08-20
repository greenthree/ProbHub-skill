"""Safe, content-stable paths for the Schema v1 WebUI preview cache.

The browser supplies a display subtitle, but that value is never suitable as a
filesystem component.  This module validates the workspace first and derives
an opaque key from the normalized workspace identity.  All cache paths are
then checked before a directory is created or an external renderer is given a
path.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import ProbHubError


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PREVIEW_KEY_SCHEMA = "probhub-webui-preview-v1"


def _is_link_like(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        _FILE_ATTRIBUTE_REPARSE_POINT
        and getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _fail(message: str, code: str = "invalid_preview_path"):
    raise ProbHubError(message, code=code)


def _resolved_directory(path: Path, *, label: str) -> tuple[Path, Path]:
    """Return the caller spelling and resolved directory after link checks."""

    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError, OSError) as exc:
        _fail(f"{label} is not a valid path")
        raise AssertionError from exc
    if _is_link_like(absolute):
        _fail(f"{label} must not be a link or reparse point")
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _fail(f"{label} is missing or cannot be resolved")
        raise AssertionError from exc
    if not resolved.is_dir() or _is_link_like(resolved):
        _fail(f"{label} must be a regular directory")
    return absolute, resolved


def _validate_relative_path(root: Path, resolved_root: Path, value, *, label: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be a normalized relative POSIX path", "invalid_workspace_path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail(f"{label} contains a control character", "invalid_workspace_path")
    # Schema v1 stores relative paths with POSIX separators.  Refusing the
    # other separator and a drive/colon avoids Windows' ambiguous path forms
    # such as C:relative and encoded path components.
    if "\\" in value or ":" in value or "%" in value:
        _fail(f"{label} contains an ambiguous path character", "invalid_workspace_path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or not posix.parts
        or posix.as_posix() != value
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        _fail(f"{label} must stay inside the workspace", "invalid_workspace_path")

    candidate = root.joinpath(*posix.parts)
    cursor = root
    for part in posix.parts:
        cursor /= part
        if _is_link_like(cursor):
            _fail(f"{label} must not traverse a link or reparse point", "invalid_workspace_path")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(f"{label} must resolve inside the workspace", "invalid_workspace_path")
        raise AssertionError from exc
    if not resolved.is_dir() or _is_link_like(resolved):
        _fail(f"{label} must resolve to a regular directory", "invalid_workspace_path")
    return candidate


def validate_typst_directory(root, workspace) -> tuple[Path, str]:
    """Validate and return the configured Typst directory and its POSIX value."""

    root_path, resolved_root = _resolved_directory(Path(root), label="workspace root")
    typst = workspace.get("typst") if isinstance(workspace, dict) else None
    value = typst.get("directory") if isinstance(typst, dict) else None
    if value is None:
        value = "typst-statement/正式赛"
    candidate = _validate_relative_path(root_path, resolved_root, value, label="typst.directory")
    return candidate, PurePosixPath(value).as_posix()


def _validate_subtitle(subtitle: str) -> str:
    if not isinstance(subtitle, str) or not subtitle:
        _fail("subtitle is required", "unknown_subtitle")
    if any(ord(char) < 32 or ord(char) == 127 for char in subtitle):
        _fail("subtitle contains a control character", "unknown_subtitle")
    if any(char in subtitle for char in ("/", "\\", ":", "%")):
        _fail("subtitle contains an ambiguous path character", "unknown_subtitle")
    if subtitle in {".", ".."}:
        _fail("subtitle is not a valid workspace identity", "unknown_subtitle")
    return unicodedata.normalize("NFC", subtitle)


def workspace_preview_key(root, workspace, subtitle: str) -> str:
    """Return the opaque, deterministic cache key for a validated workspace."""

    normalized_subtitle = _validate_subtitle(subtitle)
    typst_dir, typst_value = validate_typst_directory(root, workspace)
    if typst_dir.name != subtitle:
        _fail(f"unknown Schema v1 subtitle: {subtitle}", "unknown_subtitle")

    root_path, resolved_root = _resolved_directory(Path(root), label="workspace root")
    # The complete parsed workspace is used instead of the request string.  It
    # makes title/date/ordering changes produce a new cache namespace while
    # never placing user-controlled text in a path component.
    def normalize(value):
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        return value

    payload = {
        "schema": _PREVIEW_KEY_SCHEMA,
        "root": os.path.normcase(resolved_root.as_posix()),
        "typst_directory": typst_value,
        "subtitle": normalized_subtitle,
        "workspace": normalize(workspace),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checked_preview_root(preview_root) -> tuple[Path, Path]:
    return _resolved_directory(Path(preview_root), label="WebUI preview root")


def _check_preview_path(path: Path, root: Path, resolved_root: Path) -> Path:
    """Check every existing component and keep the path inside preview_root."""

    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
        absolute.relative_to(root)
    except (TypeError, ValueError, OSError) as exc:
        _fail("WebUI preview path escapes the preview root")
        raise AssertionError from exc

    components = []
    cursor = absolute
    while cursor != root:
        components.append(cursor)
        cursor = cursor.parent
        if cursor == cursor.parent and cursor != root:
            _fail("WebUI preview path has an invalid parent")
    for component in reversed(components):
        if _is_link_like(component):
            _fail("WebUI preview path must not traverse a link or reparse point")
    try:
        resolved = absolute.resolve(strict=False)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail("WebUI preview path resolves outside the preview root")
        raise AssertionError from exc
    return absolute


def preview_cache_dir(preview_root, root, workspace, subtitle: str, *, create: bool = False) -> Path:
    """Return the safe per-workspace cache directory, optionally creating it."""

    safe_root, resolved_root = _checked_preview_root(preview_root)
    key = workspace_preview_key(root, workspace, subtitle)
    cache = _check_preview_path(safe_root / key, safe_root, resolved_root)
    if create:
        try:
            cache.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            _fail(f"cannot create WebUI preview cache: {exc}")
        _check_preview_path(cache, safe_root, resolved_root)
        if not cache.is_dir() or _is_link_like(cache):
            _fail("WebUI preview cache is not a regular directory")
    return cache


def preview_pdf_path(preview_root, root, workspace, subtitle: str, *, create: bool = False) -> Path:
    cache = preview_cache_dir(preview_root, root, workspace, subtitle, create=create)
    safe_root, resolved_root = _checked_preview_root(preview_root)
    return _check_preview_path(cache / "main.pdf", safe_root, resolved_root)


def preview_pages_dir(preview_root, root, workspace, subtitle: str, *, create: bool = False) -> Path:
    cache = preview_cache_dir(preview_root, root, workspace, subtitle, create=create)
    safe_root, resolved_root = _checked_preview_root(preview_root)
    pages = _check_preview_path(cache / ".pages", safe_root, resolved_root)
    if create:
        try:
            pages.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            _fail(f"cannot create WebUI page cache: {exc}")
        _check_preview_path(pages, safe_root, resolved_root)
        if not pages.is_dir() or _is_link_like(pages):
            _fail("WebUI page cache is not a regular directory")
    return pages


def safe_preview_file(preview_root, path: Path) -> Path:
    """Validate a generated cache file before it is opened or replaced."""

    safe_root, resolved_root = _checked_preview_root(preview_root)
    return _check_preview_path(Path(path), safe_root, resolved_root)
