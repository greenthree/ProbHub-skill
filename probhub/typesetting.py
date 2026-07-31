import re
import tempfile
import uuid
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .errors import ProbHubError
from .metadata import (
    BOUNDARY_MARKER_PREFIX,
    normalize_display_name,
    problem_boundary_marker,
    write_typst_collection,
)
from .process_control import run_managed_to_files


TYPST_TIMEOUT_SECONDS = 120
TYPST_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
TYPST_TEMP_SOURCE_PREFIX = ".probhub-"
BOUNDARY_MARKER_PATTERN = re.compile(
    rf"{re.escape(BOUNDARY_MARKER_PREFIX)}[0-9a-f]{{64}}"
)
LEGACY_HEADING_PATTERN = re.compile(r"题目\s+[A-Z]+\.\s*(.+)")
TYPST_BOUNDARY_FIELD_PATTERN = re.compile(
    r"(?:\.\s*boundary_marker\b|at\(\s*[\"']boundary_marker[\"']\s*\))"
)
TYPST_LOCAL_IMPORT_PATTERN = re.compile(
    r"#(?:import|include)\s+[\"']([^\"']+\.typ)[\"']"
)


def is_temporary_typst_source(path):
    path = Path(path)
    return path.name.startswith(TYPST_TEMP_SOURCE_PREFIX) and path.suffix.lower() == ".typ"


def _typst_string(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _configured_typst_sources(root, workspace, main_typ):
    contest = workspace.get("contest") or {}
    cover = ((workspace.get("typst") or {}).get("cover") or {})
    if not contest and not cover:
        return main_typ, []

    token = uuid.uuid4().hex
    main_text = main_typ.read_text(encoding="utf-8")
    for key in ("title", "subtitle", "author", "date"):
        if key in contest:
            value = _typst_string(contest[key])
            main_text = re.sub(rf'({key}:\s*)"[^"]*"', rf'\g<1>"{value}"', main_text, count=1)

    lib_typ = main_typ.parent.parent / "lib.typ"
    if cover and not lib_typ.is_file():
        raise ProbHubError(
            f"Typst cover config requires the shared library: {lib_typ}",
            code="typeset_failed",
        )

    generated_lib = None
    lib_text = None
    if cover:
        generated_lib = lib_typ.with_name(f"{TYPST_TEMP_SOURCE_PREFIX}lib-{token}.typ")
        lib_text = lib_typ.read_text(encoding="utf-8")
        logo = _typst_string(cover.get("logo", "usts.png"))
        width = str(cover.get("logo_width", "9cm")).strip() or "9cm"
        above = str(cover.get("logo_space_above", "0em")).strip() or "0em"
        below = str(cover.get("logo_space_below", "0em")).strip() or "0em"
        lib_text = re.sub(
            r'image\("[^"]+"\s*(?:,\s*width:\s*[^,)]+)?',
            lambda _: f'image("{logo}", width: {width}',
            lib_text,
            count=1,
        )
        lib_text = re.sub(
            r'v\([^)]+\)(\s*(?://[^\n]*)?\s*\n\s*(?://[^\n]*\n\s*)?align\(center,\s*image\()',
            lambda match: f'v({above}){match.group(1)}',
            lib_text,
            count=1,
        )
        lib_text = re.sub(
            r'(align\(center,\s*image\([^)]+\)\)\s*\n\s*)v\([^)]+\)',
            lambda match: f'{match.group(1)}v({below})',
            lib_text,
            count=1,
        )
        main_text = re.sub(
            r'(#import\s+")\.\./lib\.typ("\s*:)',
            rf'\g<1>../{generated_lib.name}\g<2>',
            main_text,
            count=1,
        )

    generated_main = main_typ.with_name(f"{TYPST_TEMP_SOURCE_PREFIX}main-{token}.typ")
    generated = []
    try:
        if generated_lib is not None:
            generated.append(generated_lib)
            generated_lib.write_text(lib_text, encoding="utf-8")
        generated.append(generated_main)
        generated_main.write_text(main_text, encoding="utf-8")
    except BaseException:
        for path in generated:
            path.unlink(missing_ok=True)
        raise
    return generated_main, generated


def normalize_name(value):
    return normalize_display_name(value)


def typst_boundary_protocol_supported(root, workspace):
    typst_dir = Path(root) / (
        (workspace.get("typst") or {}).get("directory", "typst-statement/正式赛")
    )
    main_typ = typst_dir / "main.typ"
    if not main_typ.is_file():
        return False
    pending = [main_typ.resolve()]
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited or is_temporary_typst_source(path):
            continue
        visited.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if TYPST_BOUNDARY_FIELD_PATTERN.search(text):
            return True
        for relative in TYPST_LOCAL_IMPORT_PATTERN.findall(text):
            child = (path.parent / relative).resolve()
            if child.is_file() and child not in visited:
                pending.append(child)
    return False


def _boundary_error(message):
    raise ProbHubError(message, code="pdf_boundary_invalid")


def _expected_boundaries(loaded_problems):
    return [
        {
            "id": config["id"],
            "display_name": config.get("display_name") or config.get("name"),
            "marker": problem_boundary_marker(config["id"]),
        }
        for _, config in loaded_problems
    ]


def _validate_marker_boundaries(boundaries, expected):
    expected_by_marker = {item["marker"]: item for item in expected}
    unknown = sorted({item["marker"] for item in boundaries} - set(expected_by_marker))
    if unknown:
        _boundary_error("compiled PDF contains unknown problem boundary marker(s)")

    seen_markers = [item["marker"] for item in boundaries]
    duplicates = sorted({marker for marker in seen_markers if seen_markers.count(marker) > 1})
    if duplicates:
        duplicate_ids = [expected_by_marker[item]["id"] for item in duplicates]
        _boundary_error(
            "compiled PDF contains duplicate problem boundary marker(s): "
            + ", ".join(duplicate_ids)
        )

    expected_markers = [item["marker"] for item in expected]
    if seen_markers != expected_markers:
        seen_ids = [
            expected_by_marker[item]["id"]
            for item in seen_markers
            if item in expected_by_marker
        ]
        expected_ids = [item["id"] for item in expected]
        _boundary_error(
            "compiled PDF problem boundaries are missing or out of order "
            f"(expected: {', '.join(expected_ids)}; found: {', '.join(seen_ids) or 'none'})"
        )

    pages = [item["page"] for item in boundaries]
    if any(current <= previous for previous, current in zip(pages, pages[1:])):
        _boundary_error("compiled PDF problem boundary pages are not strictly increasing")

    return [
        {
            "id": expected_by_marker[item["marker"]]["id"],
            "marker": item["marker"],
            "page": item["page"],
        }
        for item in boundaries
    ]


def _validate_legacy_boundaries(boundaries, expected):
    by_name = {}
    for item in expected:
        by_name.setdefault(normalize_name(item["display_name"]), []).append(item)
    collisions = [items for items in by_name.values() if len(items) > 1]
    if collisions:
        details = [
            "/".join(item["id"] for item in items)
            for items in collisions
        ]
        _boundary_error(
            "legacy Typst boundary detection cannot distinguish normalized duplicate "
            "problem names: " + ", ".join(details)
        )

    if len(boundaries) != len(expected):
        _boundary_error(
            "legacy Typst boundary detection found an unexpected number of problem headings "
            f"(expected {len(expected)}, found {len(boundaries)})"
        )

    validated = []
    for boundary, expected_item in zip(boundaries, expected):
        if normalize_name(boundary["display_name"]) != normalize_name(
            expected_item["display_name"]
        ):
            _boundary_error(
                "legacy Typst problem headings are missing or out of order "
                f"at {expected_item['id']}"
            )
        validated.append({
            "id": expected_item["id"],
            "display_name": boundary["display_name"],
            "page": boundary["page"],
        })

    pages = [item["page"] for item in validated]
    if any(current <= previous for previous, current in zip(pages, pages[1:])):
        _boundary_error("legacy Typst problem boundary pages are not strictly increasing")
    return validated


def problem_boundaries(pdf_path, loaded_problems=None):
    reader = PdfReader(pdf_path)
    markers = []
    legacy = []
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
    if markers:
        if loaded_problems is None:
            return markers
        return _validate_marker_boundaries(markers, _expected_boundaries(loaded_problems))
    if loaded_problems is None:
        return legacy
    return _validate_legacy_boundaries(legacy, _expected_boundaries(loaded_problems))


def compile_collection(root, workspace, loaded_problems):
    typst = workspace.get("typst") or {}
    typst_dir, problems = write_typst_collection(root, workspace, loaded_problems)
    main_typ = typst_dir / "main.typ"
    main_pdf = typst_dir / "main.pdf"
    if not main_typ.is_file():
        raise ProbHubError(f"Typst entry not found: {main_typ}")
    compile_main, generated = _configured_typst_sources(root, workspace, main_typ)
    try:
        command = ["typst", "compile", "--root", "."]
        creation_timestamp = typst.get("creation_timestamp")
        if creation_timestamp is not None:
            command.extend(["--creation-timestamp", str(int(creation_timestamp))])
        command.extend([str(compile_main.relative_to(root)), str(main_pdf.relative_to(root))])
        with tempfile.TemporaryDirectory(prefix="probhub-typst-") as temp:
            stdout_path = Path(temp) / "stdout"
            stderr_path = Path(temp) / "stderr"
            result = run_managed_to_files(
                command,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=TYPST_TIMEOUT_SECONDS,
                memory_limit_mb=2048,
                output_limit_bytes=TYPST_OUTPUT_LIMIT_BYTES,
                process_limit=32,
                cwd=root,
            )
            if result["reason"] != "completed" or result["returncode"] != 0:
                detail = stderr_path.read_text(encoding="utf-8", errors="replace")
                if not detail.strip():
                    detail = stdout_path.read_text(encoding="utf-8", errors="replace")
                message = result.get("message") or "Typst compilation failed"
                if detail.strip():
                    message += ": " + detail.strip()[-4000:]
                raise ProbHubError(message, code="typeset_failed")
    finally:
        for path in generated:
            path.unlink(missing_ok=True)
    return typst_dir, main_pdf, problems


def extract_problem_pdfs(main_pdf, loaded_problems, only_ids=None):
    boundaries = problem_boundaries(main_pdf, loaded_problems)
    if not boundaries:
        raise ProbHubError("no problem headings found in compiled PDF")
    reader = PdfReader(main_pdf)
    outputs = {}
    boundary_indexes = {item["id"]: index for index, item in enumerate(boundaries)}
    for problem_dir, config in loaded_problems:
        problem_id = config["id"]
        if only_ids and problem_id not in only_ids:
            continue
        index = boundary_indexes.get(problem_id)
        if index is None:
            _boundary_error(f"problem boundary not found in PDF: {problem_id}")
        found = (
            boundaries[index]["page"],
            boundaries[index + 1]["page"]
            if index + 1 < len(boundaries)
            else len(reader.pages) + 1,
        )
        writer = PdfWriter()
        for page_number in range(found[0] - 1, found[1] - 1):
            writer.add_page(reader.pages[page_number])
        output = problem_dir / "problem.pdf"
        with output.open("wb") as stream:
            writer.write(stream)
        outputs[problem_id] = {"path": str(output), "pages": found[1] - found[0]}
    return outputs
