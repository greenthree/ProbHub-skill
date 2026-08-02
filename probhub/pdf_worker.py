"""Isolated pypdf worker. Invoke through probhub.pdf_processing only."""

import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .metadata import BOUNDARY_MARKER_PREFIX


REQUEST_LIMIT_BYTES = 2 * 1024 * 1024
BOUNDARY_MARKER_PATTERN = re.compile(
    rf"{re.escape(BOUNDARY_MARKER_PREFIX)}[0-9a-f]{{64}}"
)
LEGACY_HEADING_PATTERN = re.compile(r"题目\s+[A-Z]+\.\s*(.+)")


def _regular_file(path, *, label):
    path = Path(path)
    info = os.lstat(path)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    ):
        raise ValueError(f"{label} must not be a link")
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError(f"{label} must be a non-empty regular file")
    return path.resolve()


def _load_request(path):
    path = _regular_file(path, label="request")
    if path.stat().st_size > REQUEST_LIMIT_BYTES:
        raise ValueError("request is too large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    return payload


def _inspect(payload):
    input_path = _regular_file(payload.get("input", ""), label="PDF input")
    scan_text = payload.get("scan_text", False)
    if not isinstance(scan_text, bool):
        raise ValueError("scan_text must be boolean")
    markers = []
    legacy = []
    with input_path.open("rb") as stream:
        reader = PdfReader(stream)
        pages = len(reader.pages)
        if scan_text:
            for index, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                markers.extend(
                    {"marker": marker, "page": index + 1}
                    for marker in BOUNDARY_MARKER_PATTERN.findall(text)
                )
                for line in text.splitlines():
                    match = LEGACY_HEADING_PATTERN.search(line.strip())
                    if match:
                        legacy.append({"display_name": match.group(1).strip(), "page": index + 1})
                        break
    return {"ok": True, "pages": pages, "markers": markers, "legacy": legacy}


def _split(payload):
    input_path = _regular_file(payload.get("input", ""), label="PDF input")
    entries = payload.get("outputs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("outputs must be a non-empty list")
    normalized = []
    seen = set()
    with input_path.open("rb") as stream:
        reader = PdfReader(stream)
        total_pages = len(reader.pages)
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("split entry must be an object")
            output = Path(item.get("path", "")).resolve()
            start = item.get("start")
            end = item.get("end")
            if output == input_path or output in seen:
                raise ValueError("split outputs must be unique")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
                raise ValueError("split ranges must be integers")
            if start < 0 or end <= start or end > total_pages:
                raise ValueError("split range is outside the PDF")
            seen.add(output)
            normalized.append((output, start, end))

        prepared = []
        try:
            for output, start, end in normalized:
                output.parent.mkdir(parents=True, exist_ok=True)
                temporary = output.with_name(f".{output.name}.probhub-{uuid.uuid4().hex}.tmp")
                prepared.append((temporary, output, end - start))
                writer = PdfWriter()
                for index in range(start, end):
                    writer.add_page(reader.pages[index])
                with temporary.open("wb") as target:
                    writer.write(target)
            for temporary, output, _ in prepared:
                os.replace(temporary, output)
        finally:
            for temporary, _, _ in prepared:
                temporary.unlink(missing_ok=True)
    return {
        "ok": True,
        "pages": total_pages,
        "outputs": [
            {"path": str(output), "pages": pages}
            for _, output, pages in prepared
        ],
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in {"inspect", "split"}:
        print(json.dumps({"ok": False, "error": "usage: pdf_worker inspect|split request.json"}))
        return 2
    try:
        payload = _load_request(argv[1])
        response = _inspect(payload) if argv[0] == "inspect" else _split(payload)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        print(json.dumps({"ok": False, "error": detail}, ensure_ascii=True))
        return 2
    print(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
