"""Bounded PDF inspection and extraction through an isolated worker."""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from .errors import ProbHubError
from .process_control import OutputBudgetError, run_managed_to_files


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PDF_TIMEOUT_SECONDS = 30
PDF_UI_TIMEOUT_SECONDS = 10
PDF_MEMORY_LIMIT_MB = 512
PDF_OUTPUT_LIMIT_BYTES = 1024 * 1024
PDF_PROCESS_LIMIT = 4
PDF_REQUEST_LIMIT_BYTES = 2 * 1024 * 1024


def _regular_file(path, *, label, require_nonempty=True):
    try:
        path = Path(path)
    except (TypeError, ValueError) as exc:
        raise ProbHubError(
            f"{label} must be a path",
            code="pdf_processing_failed",
        ) from exc
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProbHubError(f"{label} is unavailable: {path}: {exc}", code="pdf_processing_failed") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    ):
        raise ProbHubError(f"{label} must not be a link: {path}", code="pdf_processing_failed")
    if not stat.S_ISREG(info.st_mode):
        raise ProbHubError(f"{label} is not a regular file: {path}", code="pdf_processing_failed")
    if require_nonempty and info.st_size <= 0:
        raise ProbHubError(f"{label} is empty: {path}", code="pdf_processing_failed")
    return path.resolve()


def _worker_command(operation, request_path):
    return [sys.executable, "-m", "probhub.pdf_worker", operation, str(request_path)]


def _worker_error(result, stdout_path, stderr_path):
    reason = result.get("reason")
    messages = {
        "time_limit": "PDF processing timed out",
        "memory_limit": "PDF processing exceeded the memory limit",
        "output_limit": "PDF processing exceeded the diagnostic output limit",
        "process_limit": "PDF processing exceeded the process limit",
    }
    message = messages.get(reason)
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    if message is None and isinstance(payload, dict) and isinstance(payload.get("error"), str):
        message = "PDF processing failed: " + payload["error"][:2000]
    if message is None:
        detail = stderr.strip()[-2000:] or stdout.strip()[-2000:]
        message = result.get("message") or "PDF processing worker failed"
        if detail:
            message += ": " + detail
    return ProbHubError(message, code="pdf_processing_failed")


def _run_worker(operation, payload, *, timeout=None):
    timeout = PDF_TIMEOUT_SECONDS if timeout is None else float(timeout)
    with tempfile.TemporaryDirectory(prefix="probhub-pdf-") as temp:
        temp_dir = Path(temp)
        request_path = temp_dir / "request.json"
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > PDF_REQUEST_LIMIT_BYTES:
            raise ProbHubError("PDF processing request is too large", code="pdf_processing_failed")
        request_path.write_bytes(encoded)
        stdout_path = temp_dir / "stdout"
        stderr_path = temp_dir / "stderr"
        try:
            result = run_managed_to_files(
                _worker_command(operation, request_path),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=timeout,
                memory_limit_mb=PDF_MEMORY_LIMIT_MB,
                output_limit_bytes=PDF_OUTPUT_LIMIT_BYTES,
                process_limit=PDF_PROCESS_LIMIT,
                cwd=PACKAGE_ROOT,
            )
        except (OSError, OutputBudgetError) as exc:
            raise ProbHubError(
                f"PDF processing worker could not start: {exc}",
                code="pdf_processing_failed",
            ) from exc
        if result.get("reason") != "completed" or result.get("returncode") != 0:
            raise _worker_error(result, stdout_path, stderr_path)
        try:
            response = json.loads(stdout_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProbHubError(
                "PDF processing worker returned invalid JSON",
                code="pdf_processing_failed",
            ) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ProbHubError(
                "PDF processing worker returned an invalid result",
                code="pdf_processing_failed",
            )
        return response


def inspect_pdf(pdf_path, *, scan_text=False, timeout=None):
    pdf_path = _regular_file(pdf_path, label="PDF input")
    response = _run_worker(
        "inspect",
        {"input": str(pdf_path), "scan_text": bool(scan_text)},
        timeout=timeout,
    )
    pages = response.get("pages")
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
        raise ProbHubError("PDF worker returned an invalid page count", code="pdf_processing_failed")
    if scan_text:
        if not isinstance(response.get("markers"), list) or not isinstance(response.get("legacy"), list):
            raise ProbHubError("PDF worker returned invalid boundary data", code="pdf_processing_failed")
    return response


def pdf_page_count(pdf_path, *, timeout=PDF_UI_TIMEOUT_SECONDS):
    return inspect_pdf(pdf_path, timeout=timeout)["pages"]


def split_pdf(pdf_path, outputs, *, timeout=None):
    pdf_path = _regular_file(pdf_path, label="PDF input")
    if not isinstance(outputs, (list, tuple)):
        raise ProbHubError("PDF split outputs must be a list", code="pdf_processing_failed")
    normalized = []
    seen = set()
    for item in outputs:
        if not isinstance(item, dict):
            raise ProbHubError("PDF split entry must be an object", code="pdf_processing_failed")
        try:
            raw_output = os.fspath(item.get("path"))
        except TypeError as exc:
            raise ProbHubError(
                "PDF split output path must be a non-empty path",
                code="pdf_processing_failed",
            ) from exc
        if not isinstance(raw_output, str) or not raw_output:
            raise ProbHubError(
                "PDF split output path must be a non-empty path",
                code="pdf_processing_failed",
            )
        output = Path(raw_output).resolve()
        start = item.get("start")
        end = item.get("end")
        if output == pdf_path or output in seen:
            raise ProbHubError("PDF split outputs must be unique", code="pdf_processing_failed")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
            raise ProbHubError("PDF split ranges must be integers", code="pdf_processing_failed")
        seen.add(output)
        normalized.append({"path": str(output), "start": start, "end": end})
    if not normalized:
        return {"ok": True, "pages": inspect_pdf(pdf_path, timeout=timeout)["pages"], "outputs": []}
    response = _run_worker(
        "split",
        {"input": str(pdf_path), "outputs": normalized},
        timeout=timeout,
    )
    returned = response.get("outputs")
    if not isinstance(returned, list) or len(returned) != len(normalized):
        raise ProbHubError("PDF worker returned invalid split results", code="pdf_processing_failed")
    for item in normalized:
        _regular_file(item["path"], label="PDF split output")
    return response
