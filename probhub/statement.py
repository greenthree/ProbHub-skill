import re
from pathlib import Path

from .errors import ProbHubError

SECTION_ALIASES = {
    "题目描述": "description",
    "描述": "description",
    "输入格式": "input",
    "输入描述": "input",
    "输出格式": "output",
    "输出描述": "output",
    "提示": "notes",
    "说明": "notes",
    "注释": "notes",
}
REQUIRED_SECTIONS = ("description", "input", "output")

_ATX_HEADING = re.compile(
    r"^[ \t]{0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.*?)(?:[ \t]+#+[ \t]*)?$"
)
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_SAMPLE_IO_HEADINGS = (
    re.compile(
        r"^(?:样例|示例)\s*(?:[#：:]?\s*\d+\s*)?(?:输入|输出)"
        r"(?:\s*[#：:]?\s*\d+)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:输入|输出)\s*(?:样例|示例)(?:\s*[#：:]?\s*\d+)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^sample\s+(?:#?\s*\d+\s+)?(?:input|output)"
        r"(?:\s*#?\s*\d+)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:input|output)\s+sample(?:\s*#?\s*\d+)?$",
        re.IGNORECASE,
    ),
)


def iter_unfenced_lines(text):
    """Yield ``(line_number, line)`` pairs outside Markdown fenced code.

    This is deliberately a small CommonMark-compatible scanner rather than a
    Markdown parser.  It preserves source line numbers and only provides the
    boundary needed by statement lint and the heuristic constraint report.
    """

    fence_char = None
    fence_length = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        if fence_char is not None:
            stripped = line.lstrip(" \t")
            if (
                len(line) - len(stripped) <= 3
                and re.fullmatch(
                    rf"{re.escape(fence_char)}{{{fence_length},}}[ \t]*",
                    stripped,
                )
            ):
                fence_char = None
                fence_length = 0
            continue
        match = _FENCE_OPEN.match(line)
        if match:
            fence = match.group("fence")
            fence_char = fence[0]
            fence_length = len(fence)
            continue
        yield line_number, line


def _scan_headings(text):
    headings = []
    for line_number, line in iter_unfenced_lines(text):
        match = _ATX_HEADING.match(line)
        if not match:
            continue
        headings.append({
            "level": len(match.group("marks")),
            "title": match.group("title").strip(),
            "line": line_number,
            "raw": line,
        })
    return headings


def _is_sample_io_heading(heading):
    normalized = heading.strip().strip("`*")
    return any(pattern.fullmatch(normalized) for pattern in _SAMPLE_IO_HEADINGS)


def _structure_issue(path, code, message, *, line=None):
    issue = {
        "code": code,
        "severity": "error",
        "message": f"{path}: {message}",
    }
    if line is not None:
        issue["line"] = line
    return issue


def parse_statement(path, *, strict=True):
    path = Path(path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    headings = _scan_headings(text)
    h1_headings = [heading for heading in headings if heading["level"] == 1]
    title = h1_headings[0]["title"] if h1_headings else ""
    h2_headings = [heading for heading in headings if heading["level"] == 2]
    sections = {}
    unknown = []
    occurrences = []
    text_lines = text.splitlines()
    for index, heading_item in enumerate(h2_headings):
        heading = heading_item["title"]
        start_line = heading_item["line"] + 1
        end_line = (
            h2_headings[index + 1]["line"] - 1
            if index + 1 < len(h2_headings)
            else len(text_lines)
        )
        body = "\n".join(text_lines[start_line - 1:end_line]).strip()
        key = SECTION_ALIASES.get(heading)
        occurrence = {
            "heading": heading,
            "key": key,
            "line": heading_item["line"],
            "start_line": start_line,
            "end_line": end_line,
            "body": body,
        }
        occurrences.append(occurrence)
        if key:
            sections.setdefault(key, body)
        elif not _is_sample_io_heading(heading):
            unknown.append(heading)

    structure_errors = []
    if not h1_headings or not title:
        structure_errors.append(
            _structure_issue(path, "statement_missing_h1", "missing H1 title")
        )
    if len(h1_headings) > 1:
        structure_errors.append(
            _structure_issue(
                path,
                "statement_multiple_h1",
                "multiple H1 titles are not allowed",
                line=h1_headings[1]["line"],
            )
        )
    for key in REQUIRED_SECTIONS:
        matching = [item for item in occurrences if item["key"] == key]
        if not matching:
            structure_errors.append(
                _structure_issue(
                    path,
                    "statement_missing_section",
                    f"missing required statement section: {key}",
                )
            )
        elif not matching[0]["body"]:
            structure_errors.append(
                _structure_issue(
                    path,
                    "statement_empty_section",
                    f"required statement section is empty: {key}",
                    line=matching[0]["line"],
                )
            )
        if len(matching) > 1:
            structure_errors.append(
                _structure_issue(
                    path,
                    "statement_duplicate_section",
                    f"duplicate required statement section: {key}",
                    line=matching[1]["line"],
                )
            )

    first_positions = {
        key: next(
            (index for index, item in enumerate(occurrences) if item["key"] == key),
            None,
        )
        for key in REQUIRED_SECTIONS
    }
    if all(position is not None for position in first_positions.values()):
        actual = [first_positions[key] for key in REQUIRED_SECTIONS]
        if actual != sorted(actual):
            structure_errors.append(
                _structure_issue(
                    path,
                    "statement_section_order",
                    "required statement sections are out of order; expected "
                    "description, input, output",
                )
            )

    output_position = first_positions.get("output")
    notes_position = next(
        (index for index, item in enumerate(occurrences) if item["key"] == "notes"),
        None,
    )
    if (
        output_position is not None
        and notes_position is not None
        and notes_position < output_position
    ):
        notes_item = occurrences[notes_position]
        structure_errors.append(
            _structure_issue(
                path,
                "statement_notes_before_output",
                "notes/hints section must appear after the output section",
                line=notes_item["line"],
            )
        )

    for heading_item in headings:
        if heading_item["level"] >= 2 and _is_sample_io_heading(heading_item["title"]):
            structure_errors.append(
                _structure_issue(
                    path,
                    "statement_embedded_sample_section",
                    "sample input/output must come from data/sample, not Markdown "
                    f"heading '{heading_item['title']}'",
                    line=heading_item["line"],
                )
            )

    sections.setdefault("notes", "")
    result = {
        "title": title,
        "sections": sections,
        "unknown_headings": unknown,
        "raw": text,
        "headings": headings,
        "section_occurrences": occurrences,
        "structure": {
            "ok": not structure_errors,
            "errors": structure_errors,
        },
    }
    if strict and structure_errors:
        raise ProbHubError("; ".join(issue["message"] for issue in structure_errors))
    return result


def render_statement(name, statement):
    sections = statement.get("sections", statement)
    parts = [
        f"# {name}",
        "## 题目描述",
        sections.get("description", "").strip(),
        "## 输入格式",
        sections.get("input", "").strip(),
        "## 输出格式",
        sections.get("output", "").strip(),
    ]
    notes = sections.get("notes", "").strip()
    if notes:
        parts.extend(["## 提示", notes])
    return "\n\n".join(parts).rstrip() + "\n"
