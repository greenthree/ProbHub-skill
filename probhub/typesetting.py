import re
import tempfile
import uuid
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .errors import ProbHubError
from .metadata import write_typst_collection
from .process_control import run_managed_to_files


TYPST_TIMEOUT_SECONDS = 120
TYPST_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024


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

    generated = []
    lib_typ = main_typ.parent.parent / "lib.typ"
    if cover and lib_typ.is_file():
        generated_lib = lib_typ.with_name(f".probhub-lib-{token}.typ")
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
        generated_lib.write_text(lib_text, encoding="utf-8")
        generated.append(generated_lib)
        main_text = re.sub(
            r'(#import\s+")\.\./lib\.typ("\s*:)',
            rf'\g<1>../{generated_lib.name}\g<2>',
            main_text,
            count=1,
        )
    elif cover:
        for path in generated:
            path.unlink(missing_ok=True)
        raise ProbHubError(
            f"Typst cover config requires the shared library: {lib_typ}",
            code="typeset_failed",
        )

    generated_main = main_typ.with_name(f".probhub-main-{token}.typ")
    generated_main.write_text(main_text, encoding="utf-8")
    generated.append(generated_main)
    return generated_main, generated


def normalize_name(value):
    return "".join(str(value or "").split())


def problem_boundaries(pdf_path):
    reader = PdfReader(pdf_path)
    boundaries = []
    pattern = re.compile(r"题目\s+[A-Z]\.\s*(.+)")
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        for line in text.splitlines():
            match = pattern.search(line.strip())
            if match:
                boundaries.append({"display_name": match.group(1).strip(), "page": index + 1})
                break
    return boundaries


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
    boundaries = problem_boundaries(main_pdf)
    if not boundaries:
        raise ProbHubError("no problem headings found in compiled PDF")
    reader = PdfReader(main_pdf)
    outputs = {}
    for problem_dir, config in loaded_problems:
        problem_id = config["id"]
        if only_ids and problem_id not in only_ids:
            continue
        target = normalize_name(config.get("display_name") or config.get("name"))
        found = None
        for index, boundary in enumerate(boundaries):
            if normalize_name(boundary["display_name"]) == target:
                found = (boundary["page"], boundaries[index + 1]["page"] if index + 1 < len(boundaries) else len(reader.pages) + 1)
                break
        if not found:
            raise ProbHubError(f"problem heading not found in PDF: {config.get('display_name') or config.get('name')}")
        writer = PdfWriter()
        for page_number in range(found[0] - 1, found[1] - 1):
            writer.add_page(reader.pages[page_number])
        output = problem_dir / "problem.pdf"
        with output.open("wb") as stream:
            writer.write(stream)
        outputs[problem_id] = {"path": str(output), "pages": found[1] - found[0]}
    return outputs
