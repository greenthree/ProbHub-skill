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


def parse_statement(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    title = title_match.group(1).strip() if title_match else ""
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    sections = {}
    unknown = []
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        key = SECTION_ALIASES.get(heading)
        if key:
            sections[key] = body
        elif not heading.startswith("样例"):
            unknown.append(heading)
    missing = [key for key in REQUIRED_SECTIONS if not sections.get(key)]
    if missing:
        raise ProbHubError(f"{path}: missing statement sections: {', '.join(missing)}")
    sections.setdefault("notes", "")
    return {"title": title, "sections": sections, "unknown_headings": unknown, "raw": text}


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
