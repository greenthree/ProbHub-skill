import re
import subprocess
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .errors import ProbHubError
from .metadata import write_typst_collection


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
    command = ["typst", "compile", "--root", "."]
    creation_timestamp = typst.get("creation_timestamp")
    if creation_timestamp is not None:
        command.extend(["--creation-timestamp", str(int(creation_timestamp))])
    command.extend([str(main_typ.relative_to(root)), str(main_pdf.relative_to(root))])
    proc = subprocess.run(
        command,
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode:
        raise ProbHubError("Typst compilation failed")
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
