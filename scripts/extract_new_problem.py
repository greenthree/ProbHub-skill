# -*- coding: utf-8 -*-
import sys
import os
import json
import subprocess
from pypdf import PdfReader, PdfWriter

BOILERPLATE_PAGES = 2  # cover + blank page

def get_page_count(pdf_path):
    """Return page count of a PDF, or 0 if file doesn't exist."""
    if not os.path.exists(pdf_path):
        return 0
    return len(PdfReader(pdf_path).pages)

def extract_text_from_page(reader, page_idx):
    """Extract text from a single PDF page."""
    try:
        text = reader.pages[page_idx].extract_text()
        return text if text else ""
    except Exception:
        return ""

def find_problem_boundaries(pdf_path):
    """
    Scan all pages in the PDF and return a list of {display_name, page} dicts
    for each problem found, ordered by page number.
    Problems are identified by the heading pattern "题目 X. NAME".
    """
    reader = PdfReader(pdf_path)
    found = []

    for i in range(len(reader.pages)):
        text = extract_text_from_page(reader, i)
        if not text:
            continue
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith("题目 ") and ". " in line:
                dot_idx = line.index(". ")
                display_name = line[dot_idx + 2:].strip()
                if display_name:
                    found.append({"display_name": display_name, "page": i + 1})
                    break

    return found

def is_last_problem(typst_dir, target_name):
    """Check if target problem is the last one in problems.json."""
    problems_json = os.path.join(typst_dir, "problems.json")
    if not os.path.exists(problems_json):
        return True
    try:
        with open(problems_json, "r", encoding="utf-8") as f:
            problems = json.load(f)
    except Exception:
        return True
    if not isinstance(problems, list) or len(problems) == 0:
        return True
    # Get display_name of the last problem
    last = problems[-1]
    last_name = last.get("problem", {}).get("display_name", "")
    return last_name == target_name

def extract_by_page_count(main_pdf_path, output_pdf_path, prev_pages):
    """Extract the last X pages using page count difference (for new problems).
    Returns True on success, False if fallback needed (e.g. x <= 0)."""
    new_pages = get_page_count(main_pdf_path)
    print(f"[*] Page count method: before={prev_pages}, after={new_pages}")

    if prev_pages == 0:
        x = new_pages - BOILERPLATE_PAGES
        print(f"[*] First problem — total {new_pages} pages, boilerplate {BOILERPLATE_PAGES}, x = {x}")
    else:
        x = new_pages - prev_pages
        print(f"[*] Appended problem — x = {x}")

    if x <= 0:
        print("[*] Page count unchanged, falling back to text-scan mode...")
        return False

    reader = PdfReader(main_pdf_path)
    writer = PdfWriter()
    for i in range(new_pages - x, new_pages):
        writer.add_page(reader.pages[i])

    os.makedirs(os.path.dirname(output_pdf_path) or ".", exist_ok=True)
    with open(output_pdf_path, "wb") as f:
        writer.write(f)

    print(f"[+] Extracted last {x} pages -> {output_pdf_path}")
    return True

def extract_by_text_scan(main_pdf_path, output_pdf_path, target_name):
    """Extract using PDF text scanning (for mid-sequence edits)."""
    print(f"[*] Scanning PDF for problem boundaries...")
    boundaries = find_problem_boundaries(main_pdf_path)

    if not boundaries:
        print("[-] No problem headings found in PDF. Check lib.typ rendering.")
        sys.exit(1)

    print(f"[*] Found {len(boundaries)} problem(s):")
    for b in boundaries:
        print(f"    - \"{b['display_name']}\" starts at page {b['page']}")

    start_page = -1
    end_page = -1

    for i, b in enumerate(boundaries):
        if b["display_name"] == target_name:
            start_page = b["page"]
            if i + 1 < len(boundaries):
                end_page = boundaries[i + 1]["page"]
            break

    if start_page == -1:
        print(f"[-] Target problem '{target_name}' not found in PDF headings.")
        sys.exit(1)

    reader = PdfReader(main_pdf_path)
    total_pages = len(reader.pages)
    if end_page == -1:
        end_page = total_pages + 1

    writer = PdfWriter()
    for page_num in range(start_page - 1, end_page - 1):
        writer.add_page(reader.pages[page_num])

    os.makedirs(os.path.dirname(output_pdf_path) or ".", exist_ok=True)
    with open(output_pdf_path, "wb") as f:
        writer.write(f)

    page_count = end_page - start_page
    print(f"[+] Extracted! ({page_count} pages, P{start_page} - P{end_page - 1}) -> {output_pdf_path}")

def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/extract_new_problem.py <typst_dir> <problem_dir>")
        sys.exit(1)

    typst_dir = sys.argv[1]
    problem_dir = sys.argv[2]

    main_typ_path = os.path.join(typst_dir, "main.typ")
    main_pdf_path = os.path.join(typst_dir, "main.pdf")
    output_pdf_path = os.path.join(problem_dir, "problem.pdf")

    # 1. Read target problem name
    meta_path = os.path.join(problem_dir, "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            target_name = json.load(f)["problem"]["display_name"]
    except Exception as e:
        print(f"[-] Cannot read meta.json: {e}")
        sys.exit(1)

    # 2. Decide mode: last problem = new → page count; otherwise → text scan
    last = is_last_problem(typst_dir, target_name)

    if last:
        print("[*] Target is the last problem — using page-count mode.")
        prev_pages = get_page_count(main_pdf_path)
    else:
        print("[*] Target is NOT the last problem — using text-scan mode.")

    # 3. Compile full PDF
    print(f"[*] Compiling full PDF: {main_typ_path} ...")
    try:
        subprocess.run(
            ["typst", "compile", "--root", ".", main_typ_path, main_pdf_path],
            check=True, encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print("[-] Fatal: Typst compilation failed!")
        sys.exit(1)

    # 4. Extract
    if last:
        ok = extract_by_page_count(main_pdf_path, output_pdf_path, prev_pages)
        if not ok:
            extract_by_text_scan(main_pdf_path, output_pdf_path, target_name)
    else:
        extract_by_text_scan(main_pdf_path, output_pdf_path, target_name)

if __name__ == "__main__":
    main()
