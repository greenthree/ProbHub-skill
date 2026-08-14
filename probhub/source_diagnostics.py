"""Small, high-confidence source checks for common testlib authoring mistakes.

These checks intentionally avoid pretending to be a C++ parser. They only report
patterns whose runtime meaning is unambiguous for the bundled testlib contract.
"""

from __future__ import annotations

import re
from pathlib import Path


_TESTLIB_INCLUDE = re.compile(r"^\s*#\s*include\s*[<\"]testlib\.h[>\"]", re.MULTILINE)
_REGISTER_GEN = re.compile(r"\bregisterGen\s*\(")
_READ_TOKEN_LABEL = re.compile(
    r"\b(?:std::)?string\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*"
    r"[^;\n]*?\.(?P<method>readToken|readWord)\s*\(\s*"
    r"\"(?P<pattern>[A-Za-z_]\w*)\"\s*\)"
)
_LINE_COMMENT = re.compile(r"//[^\r\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _without_comments(source):
    """Keep line breaks while removing comments from the conservative scan."""

    source = _BLOCK_COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), source)
    return _LINE_COMMENT.sub("", source)


def _line_number(source, offset):
    return source.count("\n", 0, offset) + 1


def diagnose_source(path, role, *, display_path=None):
    """Return stable diagnostics for a problem-local source file.

    ``role`` is one of ``generator``, ``stress_generator`` or ``validator``.
    The scan is deliberately narrow so a valid regex or a non-testlib program is
    not rejected merely because it contains a familiar word.
    """

    path = Path(path)
    reported_path = str(display_path) if display_path is not None else str(path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [{
            "code": "source_unreadable",
            "severity": "error",
            "role": role,
            "path": reported_path,
            "line": None,
            "message": f"{role} source could not be read: {exc}",
        }]

    scan = _without_comments(source)
    diagnostics = []
    if role in {"generator", "stress_generator"}:
        if _TESTLIB_INCLUDE.search(scan) and not _REGISTER_GEN.search(scan):
            diagnostics.append({
                "code": "generator_missing_registerGen",
                "severity": "error",
                "role": role,
                "path": reported_path,
                "line": _line_number(scan, _TESTLIB_INCLUDE.search(scan).start()),
                "message": (
                    f"{role} includes testlib.h but does not call registerGen(...); "
                    "testlib random generation and argument handling will fail at runtime"
                ),
                "hint": "call registerGen(argc, argv, 1) at the start of main",
            })

    if role == "validator":
        for match in _READ_TOKEN_LABEL.finditer(scan):
            variable = match.group("variable")
            pattern = match.group("pattern")
            method = match.group("method")
            if variable != pattern:
                continue
            diagnostics.append({
                "code": "validator_readToken_label_as_pattern",
                "severity": "error",
                "role": role,
                "path": reported_path,
                "line": _line_number(scan, match.start()),
                "message": (
                    f'{method}("{pattern}") treats "{pattern}" as a regex pattern, '
                    "not a variable label"
                ),
                "hint": (
                    f'use {method}("[a-z]+", "{pattern}") with the intended pattern, '
                    f"or {method}() when any token is allowed"
                ),
            })
    return diagnostics


def format_diagnostic(diagnostic):
    """Format one diagnostic for the existing lint error/warning arrays."""

    code = diagnostic.get("code", "source_diagnostic")
    path = diagnostic.get("path")
    line = diagnostic.get("line")
    location = ""
    if path:
        location = f" {path}"
        if line:
            location += f":{line}"
    hint = diagnostic.get("hint")
    message = f"[{code}]{location}: {diagnostic.get('message', '')}"
    if hint:
        message += f" Hint: {hint}."
    return message
