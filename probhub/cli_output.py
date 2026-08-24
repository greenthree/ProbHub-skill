"""Human-readable CLI summaries built from existing structured results."""

from __future__ import annotations

from .remediation import remediation_summary


_MAX_PROBLEMS = 5
_MAX_DIAGNOSTICS = 3


def _problem_items(problems):
    if isinstance(problems, dict):
        return list(problems.items())
    if isinstance(problems, list):
        items = []
        for index, item in enumerate(problems):
            problem_id = item.get("id") if isinstance(item, dict) else None
            items.append((str(problem_id or index + 1), item))
        return items
    return []


def _result_items(result):
    if not isinstance(result, dict):
        return [], None
    for key in ("problems", "judge", "packages", "pdfs", "tools"):
        items = _problem_items(result.get(key))
        if items:
            return items, key
    return [], None


def _status(item):
    if not isinstance(item, dict):
        return "RESULT"
    if item.get("state"):
        return str(item["state"]).upper()
    if item.get("status"):
        return str(item["status"]).upper()
    if item.get("ok") is True:
        return "OK"
    if item.get("ok") is False:
        return "FAILED"
    verification = item.get("verification")
    if isinstance(verification, dict) and verification.get("ok") is not None:
        return "OK" if verification["ok"] else "FAILED"
    if item.get("path"):
        return "OK"
    final = item.get("final")
    if isinstance(final, dict):
        value = final.get("status") or final.get("code")
        if value:
            return str(value).upper()
    return "RESULT"


def _diagnostic_text(item):
    fallback = []
    structured = []
    if not isinstance(item, dict):
        return fallback
    for value in item.get("errors", []) or []:
        fallback.append(("ERROR", str(value)))
    for value in item.get("warnings", []) or []:
        fallback.append(("WARN", str(value)))
    for diagnostic in item.get("diagnostics", []) or []:
        if not isinstance(diagnostic, dict):
            continue
        severity = str(diagnostic.get("severity", "warning")).upper()
        if severity not in {"ERROR", "WARNING", "WARN"}:
            continue
        label = "WARN" if severity == "WARNING" else severity
        code = diagnostic.get("code")
        message = diagnostic.get("message") or diagnostic.get("detail")
        if not message:
            continue
        location = diagnostic.get("path")
        if diagnostic.get("line"):
            location = f"{location or '<source>'}:{diagnostic['line']}"
        suffix = f" [{location}]" if location else ""
        prefix = f"{code}: " if code else ""
        remediation = remediation_summary(diagnostic.get("remediation"))
        remedy_suffix = f" Fix: {remediation}" if remediation else ""
        structured.append((label, f"{prefix}{message}{suffix}{remedy_suffix}"))
    def normalize(message):
        message = str(message)
        if message.startswith("[") and "] " in message:
            message = message.split("] ", 1)[1]
        if ": " in message and message.split(": ", 1)[0].replace("_", "").isalnum():
            message = message.split(": ", 1)[1]
        return message

    structured_messages = {normalize(message) for _, message in structured}
    return structured + [
        (label, message)
        for label, message in fallback
        if normalize(message) not in structured_messages
    ]


def _next_step(command, result):
    items, _ = _result_items(result)
    first_id = items[0][0] if items else "<ID>"
    if not isinstance(result, dict) or result.get("ok") is False:
        command_text = f"probhub {command} {first_id}" if command else "the command"
        return f"Fix the first error, then rerun `{command_text}`."
    if command in {"lint", "sample-check"}:
        return f"Run `probhub judge {first_id} --no-cache`."
    if command in {"judge", "judge-qa"}:
        return f"Run `probhub seal {first_id}` when all expectations pass."
    if command == "status":
        return f"Rebuild stale problems with `probhub build {first_id} --no-cache`."
    if command in {"checkpoint", "seal", "assemble"}:
        return "Inspect the isolated generation before formal build."
    if command in {"build", "typeset", "package"}:
        return f"Run `probhub status {first_id}` and verify the package."
    if command == "stress":
        return "Replay and fixate any counterexample before sealing."
    if command == "mutation":
        return "Review survivors manually; mutation is not a correctness proof."
    return "See the JSON output for complete details."


def render_human_result(result, command=None):
    """Render a bounded summary without changing the underlying result."""
    if not isinstance(result, dict):
        return f"ProbHub: {result}\n"

    ok = result.get("ok", True)
    lines = [f"ProbHub: {'OK' if ok else 'FAILED'}"]
    if result.get("code"):
        lines.append(f"Code: {result['code']}")
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    remediation = remediation_summary(result.get("remediation"))
    if remediation:
        lines.append(f"Fix: {remediation}")

    for label, message in _diagnostic_text(result)[:_MAX_DIAGNOSTICS]:
        lines.append(f"{label}: {message}")

    if result.get("state"):
        lines.append(f"State: {str(result['state']).upper()}")

    items, item_kind = _result_items(result)
    if items:
        label = "Problems" if item_kind in {"problems", "judge"} else item_kind.title()
        lines.append(f"{label}: {len(items)}")
        for problem_id, item in items[:_MAX_PROBLEMS]:
            detail = ""
            diagnostics = _diagnostic_text(item)
            if isinstance(item, dict) and item.get("reason"):
                detail = f" - {item['reason']}"
            lines.append(f"- {problem_id}: {_status(item)}{detail}")
            for label, message in diagnostics[:_MAX_DIAGNOSTICS]:
                lines.append(f"  {label}: {message}")
        if len(items) > _MAX_PROBLEMS:
            lines.append(f"- ... {len(items) - _MAX_PROBLEMS} more problem(s)")

    summary = result.get("summary")
    if isinstance(summary, dict) and summary:
        values = []
        for key in ("total", "passed", "failed", "killed", "survived", "secret_cases"):
            if key in summary:
                values.append(f"{key}={summary[key]}")
        if values:
            lines.append("Summary: " + ", ".join(values))

    if command:
        lines.append("Next: " + _next_step(command, result))
    return "\n".join(lines) + "\n"
