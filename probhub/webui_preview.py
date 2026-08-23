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
import secrets
import shutil
import stat
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Lock

from .errors import ProbHubError
from .pdf_processing import pdf_page_count as bounded_pdf_page_count
from .process_control import OutputBudgetError, run_managed_to_files


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PREVIEW_KEY_SCHEMA = "probhub-webui-preview-v1"
_RENDER_LOCKS = tuple(Lock() for _ in range(64))
_PDF_HASH_CHUNK_BYTES = 1024 * 1024
_RENDER_TIMEOUT_SECONDS = 30
_RENDER_MEMORY_LIMIT_MB = 512
_RENDER_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
_RENDER_PROCESS_LIMIT = 8


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


def _is_regular_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISREG(info.st_mode) and not (
        stat.S_ISLNK(info.st_mode)
        or (reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag)
    )


def select_preview_pdf(preview_root, root, workspace, subtitle: str) -> Path | None:
    """Select the isolated preview PDF, falling back to the formal collection PDF."""

    typst_dir, _ = validate_typst_directory(root, workspace)
    preview = preview_pdf_path(preview_root, root, workspace, subtitle)
    if _is_regular_file(preview):
        return preview
    formal = typst_dir / "main.pdf"
    return formal if _is_regular_file(formal) else None


def publish_preview_pdf(
    preview_root,
    root,
    workspace,
    subtitle: str,
    staged_pdf,
    *,
    copy_file_fn=shutil.copyfile,
    replace_fn=os.replace,
) -> Path:
    """Atomically publish a compiled PDF into the process-local preview cache."""

    destination = preview_pdf_path(
        preview_root,
        root,
        workspace,
        subtitle,
        create=True,
    )
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=".main-",
        suffix=".pdf.tmp",
        dir=destination.parent,
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        safe_preview_file(preview_root, temporary)
        copy_file_fn(Path(staged_pdf), temporary)
        safe_preview_file(preview_root, destination)
        replace_fn(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def count_preview_pages(
    preview_root,
    root,
    workspace,
    subtitle: str,
    *,
    page_count_fn=bounded_pdf_page_count,
) -> int:
    """Return zero for a missing PDF and otherwise inspect it with the bounded worker."""

    pdf_path = select_preview_pdf(preview_root, root, workspace, subtitle)
    if pdf_path is None:
        return 0
    return page_count_fn(pdf_path)


def _render_lock(path: Path) -> Lock:
    encoded = os.path.normcase(os.path.abspath(os.fspath(path))).encode("utf-8", errors="surrogatepass")
    index = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") % len(_RENDER_LOCKS)
    return _RENDER_LOCKS[index]


def _pdf_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_PDF_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_render_diagnostic(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-1000:]
    except OSError:
        return ""


def _render_failure(message, *, pages=None):
    result = {"status": "render_failed", "error": str(message)[:2000]}
    if pages is not None:
        result["pages"] = pages
    return result


def render_preview_page(
    preview_root,
    root,
    workspace,
    subtitle: str,
    page: int,
    *,
    page_count_fn=bounded_pdf_page_count,
    run_managed_fn=run_managed_to_files,
    replace_fn=os.replace,
):
    """Render one zero-based page into a content-validated process-local cache.

    The returned status is independent from HTTP so Flask remains a thin
    presentation adapter. A failed renderer never writes the final PNG.
    """

    pdf_path = select_preview_pdf(preview_root, root, workspace, subtitle)
    if pdf_path is None:
        return {"status": "not_compiled", "pages": 0}
    try:
        total = page_count_fn(pdf_path)
    except Exception as exc:  # The WebUI deliberately keeps malformed PDFs non-fatal.
        return {"status": "invalid_pdf", "error": str(exc)[:2000]}
    if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page >= total:
        return {"status": "page_out_of_range", "pages": total}

    pages_dir = preview_pages_dir(
        preview_root,
        root,
        workspace,
        subtitle,
        create=True,
    )
    # Include the source PDF identity in the final filename. This keeps page
    # publication a single atomic replace; a failed replace cannot leave a
    # newly rendered PNG paired with an old sidecar hash.
    lock_path = safe_preview_file(preview_root, pages_dir / f"page-{page + 1}")
    pdf_hash = None
    png_path = None

    with _render_lock(lock_path):
        try:
            pdf_hash = _pdf_content_hash(pdf_path)
        except OSError as exc:
            return _render_failure(exc, pages=total)
        png_path = safe_preview_file(
            preview_root,
            pages_dir / f"page-{page + 1}-{pdf_hash}.png",
        )
        if png_path.is_file():
            return {
                "status": "ok",
                "path": png_path,
                "pages": total,
                "cache": "hit",
                "pdf_sha256": pdf_hash,
            }

        render_token = secrets.token_hex(8)
        prefix = safe_preview_file(
            preview_root,
            pages_dir / f".page-{page + 1}-{render_token}",
        )
        staged_png = safe_preview_file(preview_root, prefix.with_suffix(".png"))
        stdout_path = safe_preview_file(preview_root, prefix.with_suffix(".stdout"))
        stderr_path = safe_preview_file(preview_root, prefix.with_suffix(".stderr"))
        try:
            rendered = run_managed_fn(
                [
                    "pdftoppm",
                    "-f", str(page + 1),
                    "-l", str(page + 1),
                    "-singlefile",
                    "-png",
                    "-r", "144",
                    str(pdf_path),
                    str(prefix),
                ],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                additional_output_paths=(staged_png,),
                timeout=_RENDER_TIMEOUT_SECONDS,
                memory_limit_mb=_RENDER_MEMORY_LIMIT_MB,
                output_limit_bytes=_RENDER_OUTPUT_LIMIT_BYTES,
                process_limit=_RENDER_PROCESS_LIMIT,
                cwd=Path(root),
            )
            if rendered.get("reason") != "completed" or rendered.get("returncode") != 0:
                detail = _read_render_diagnostic(stderr_path)
                return _render_failure(detail or rendered.get("message") or rendered.get("reason"), pages=total)
            if not staged_png.is_file():
                return _render_failure("renderer did not produce a PNG", pages=total)
            if not _is_regular_file(staged_png):
                return _render_failure("renderer produced an invalid PNG", pages=total)
            if staged_png.stat().st_size > _RENDER_OUTPUT_LIMIT_BYTES:
                return _render_failure("renderer produced an oversized PNG", pages=total)
            if _pdf_content_hash(pdf_path) != pdf_hash:
                return _render_failure("PDF changed during page rendering", pages=total)
            safe_preview_file(preview_root, png_path)
            replace_fn(staged_png, png_path)
            # Stale hashed pages are not observable through the API. Remove
            # them only after the new page has been published successfully.
            for stale in pages_dir.glob(f"page-{page + 1}-*.png"):
                if stale != png_path:
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError:
                        pass
        except (OSError, OutputBudgetError) as exc:
            return _render_failure(exc, pages=total)
        finally:
            for temporary in (staged_png, stdout_path, stderr_path):
                temporary.unlink(missing_ok=True)

        return {
            "status": "ok",
            "path": png_path,
            "pages": total,
            "cache": "rendered",
            "pdf_sha256": pdf_hash,
        }
