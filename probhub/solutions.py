import fnmatch
import math
import re
import stat
from collections.abc import Mapping
from itertools import combinations
from pathlib import Path, PurePosixPath, PureWindowsPath


SOLUTION_KINDS = {
    "accepted": "std",
    "brute": "brute",
    "wrong": "wrong",
}
INDEPENDENCE_BASES = {"algorithm", "key_implementation"}
HIGH_DIFFICULTY_THRESHOLD = 4
EXPECTED_STATUSES = {"AC", "WA", "TLE", "MLE", "OLE", "RE", "FAIL"}


def expected_defaults(kind):
    if kind == "std":
        return {
            "status": ["AC"],
            "all": True,
            "forbid": ["WA", "TLE", "MLE", "OLE", "RE", "FAIL"],
            "groups": [],
        }
    if kind == "brute":
        return {
            "status": ["TLE", "MLE", "OLE"],
            "all": False,
            "forbid": ["WA", "FAIL"],
            "groups": [],
        }
    return {
        "status": ["WA", "TLE", "MLE", "OLE", "RE"],
        "all": False,
        "forbid": ["FAIL"],
        "groups": [],
    }


def _list_value(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def _string_list(value, *, uppercase=False):
    result = []
    for item in _list_value(value):
        text = str(item).strip()
        if text:
            result.append(text.upper() if uppercase else text)
    return result


def normalize_expected(kind, entry):
    expected = expected_defaults(kind)
    configured = entry.get("expected") if isinstance(entry, dict) else None
    if isinstance(configured, dict):
        expected.update(configured)
    expected["status"] = _string_list(expected.get("status"), uppercase=True)
    expected["forbid"] = _string_list(expected.get("forbid"), uppercase=True)
    expected["groups"] = _string_list(expected.get("groups"))
    # Preserve an invalid configured value for diagnostics instead of silently
    # turning values such as the string "false" into True.
    return expected


def normalize_run_on(entry):
    if not isinstance(entry, dict) or "run_on" not in entry:
        return None
    return _string_list(entry.get("run_on"))


def run_on_schema_error(entry):
    if not isinstance(entry, dict) or "run_on" not in entry:
        return None
    value = entry.get("run_on")
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        return "run_on must be a non-empty group name or list"
    if any(not isinstance(item, str) or not item.strip() for item in values):
        return "run_on entries must be non-empty strings"
    normalized = [item.strip() for item in values]
    if len(normalized) != len(set(normalized)):
        return "run_on group names must be unique"
    return None


def _solution_file(entry):
    value = entry.get("file") if isinstance(entry, dict) else entry
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().replace("\\", "/")


def normalize_solution_entries(config):
    result = {"std": [], "brute": [], "wrong": []}
    configured = config.get("solutions") or {}
    if not isinstance(configured, dict):
        return result
    for config_kind, kind in SOLUTION_KINDS.items():
        raw_entries = configured.get(config_kind) or []
        if not isinstance(raw_entries, list):
            raw_entries = [raw_entries]
        for index, raw in enumerate(raw_entries):
            raw_expected = raw.get("expected") if isinstance(raw, dict) else None
            expected_all_error = None
            if (
                isinstance(raw_expected, dict)
                and "all" in raw_expected
                and not isinstance(raw_expected.get("all"), bool)
            ):
                expected_all_error = "expected.all must be boolean"
            result[kind].append({
                "kind": kind,
                "config_kind": config_kind,
                "index": index,
                "file": _solution_file(raw),
                "expected": normalize_expected(kind, raw),
                "run_on": normalize_run_on(raw),
                "run_on_error": run_on_schema_error(raw),
                "expected_groups_configured": bool(
                    isinstance(raw_expected, dict)
                    and _string_list(raw_expected.get("groups"))
                ),
                "expected_all_error": expected_all_error,
                "independence": raw.get("independence") if isinstance(raw, dict) else None,
                "raw": raw,
            })
    return result


def configured_solution_entries(config):
    """Return normalized configured solutions through the stable public API."""
    return normalize_solution_entries(config)


def _expected_schema_issues(entry, group_names):
    raw = entry.get("raw")
    if not isinstance(raw, dict) or "expected" not in raw:
        return []

    program = entry.get("file") or f"{entry.get('config_kind')}[{entry.get('index')}]"
    expected = raw.get("expected")
    if not isinstance(expected, Mapping):
        return [{
            "code": "invalid_solution_expected",
            "field": "expected",
            "message": f"solutions.{entry.get('config_kind')}.expected must be a mapping",
            "value": expected,
        }]

    issues = []
    if "status" not in expected:
        issues.append({
            "code": "invalid_solution_expected",
            "field": "status",
            "message": f"expected.status is required for {program}",
            "reason": "required",
        })

    for field in ("status", "forbid"):
        if field not in expected:
            continue
        configured = expected.get(field)
        if isinstance(configured, str):
            values = [configured]
        elif isinstance(configured, list):
            values = configured
        else:
            issues.append({
                "code": "invalid_solution_expected",
                "field": field,
                "message": f"expected.{field} must be a status or list for {program}",
                "value": configured,
                "reason": "type",
            })
            continue
        if field == "status" and (
            not values
            or any(isinstance(value, str) and not value.strip() for value in values)
        ):
            issues.append({
                "code": "invalid_solution_expected",
                "field": field,
                "message": f"expected.status must not be empty for {program}",
                "reason": "empty",
            })
            continue
        if any(not isinstance(value, str) or not value.strip() for value in values):
            issues.append({
                "code": "invalid_solution_expected",
                "field": field,
                "message": f"expected.{field} must be a status or list for {program}",
                "value": values,
                "reason": "entry_type",
            })
            continue
        invalid = sorted({value.strip().upper() for value in values} - EXPECTED_STATUSES)
        if invalid:
            issues.append({
                "code": "invalid_solution_expected",
                "field": field,
                "message": f"invalid expected {field} for {program}: {', '.join(invalid)}",
                "invalid": invalid,
                "reason": "status",
            })

    if "groups" in expected:
        configured = expected.get("groups")
        if isinstance(configured, str):
            groups = [configured]
        elif isinstance(configured, list):
            groups = configured
        else:
            groups = None
        if groups is None or any(
            not isinstance(group, str) or not group.strip()
            for group in groups
        ):
            issues.append({
                "code": "invalid_solution_expected",
                "field": "groups",
                "message": f"expected.groups must be a name or list for {program}",
                "value": configured,
                "reason": "type",
            })
        else:
            normalized_groups = [group.strip() for group in groups]
            unknown = sorted(set(normalized_groups) - group_names)
            if unknown:
                issues.append({
                    "code": "invalid_solution_expected",
                    "field": "groups",
                    "message": "unknown expected groups: " + ", ".join(unknown),
                    "groups": unknown,
                    "reason": "unknown",
                })

    if "all" in expected and not isinstance(expected.get("all"), bool):
        issues.append({
            "code": "invalid_expected_all",
            "field": "all",
            "message": f"expected.all must be boolean for {program}",
            "value": expected.get("all"),
            "reason": "type",
        })
    return issues


def normalize_data_groups(config):
    raw_groups = ((config.get("data") or {}).get("groups") or [])
    if not isinstance(raw_groups, list):
        return []
    result = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        patterns = _string_list(raw.get("patterns") or raw.get("cases") or [])
        targets = [item.replace("\\", "/") for item in _string_list(raw.get("targets") or [])]
        result.append({
            "name": name,
            "role": raw.get("role"),
            "patterns": patterns,
            "targets": targets,
        })
    return result


def case_group_names(case_name, suite, base_name, groups):
    case_name = str(case_name).replace("\\", "/")
    suite = str(suite).replace("\\", "/")
    base_name = str(base_name).replace("\\", "/")
    values = (case_name, base_name, f"{suite}/{base_name}")
    matched = []
    for group in groups:
        patterns = group.get("patterns") or [
            f"{group['name']}*",
            f"*/{group['name']}*",
        ]
        if any(
            fnmatch.fnmatchcase(value, str(pattern).replace("\\", "/"))
            for value in values
            for pattern in patterns
        ):
            matched.append(group["name"])
    return matched


def targeted_group_names(groups, program_name):
    program_name = str(program_name or "").replace("\\", "/")
    return [
        group["name"]
        for group in groups
        if program_name in (group.get("targets") or [])
    ]


def select_target_cases(entry, testcases, groups):
    selected_groups = targeted_group_names(groups, entry.get("file"))
    if not selected_groups:
        return [], []
    wanted = set(selected_groups)
    return [
        testcase
        for testcase in testcases
        if wanted.intersection(testcase.get("groups") or [])
    ], selected_groups


def select_run_cases(entry, testcases):
    run_on = entry.get("run_on")
    if run_on is None:
        return list(testcases), []
    names = set(run_on)
    executed, skipped = [], []
    for testcase in testcases:
        if testcase.get("suite") == "sample" or names.intersection(testcase.get("groups") or []):
            executed.append(testcase)
        else:
            skipped.append(testcase)
    return executed, skipped


def select_expectation_cases(kind, entry, testcases, groups):
    expected = entry.get("expected") or expected_defaults(kind)
    # A full-domain accepted solution remains responsible for every testcase,
    # preserving the historical invariant. Only an explicitly partial
    # secondary accepted (run_on is present) may scope its expectation.
    if kind == "std" and entry.get("run_on") is None:
        return list(testcases), []
    selected_groups = list(expected.get("groups") or [])
    if not selected_groups and kind != "std":
        selected_groups = targeted_group_names(groups, entry.get("file"))
    if selected_groups:
        wanted = set(selected_groups)
        selected = [
            testcase
            for testcase in testcases
            if wanted.intersection(testcase.get("groups") or [])
        ]
        return selected, selected_groups
    return list(testcases), []


def _problem_testcases(problem_dir, config, groups):
    problem_dir = Path(problem_dir)
    data = config.get("data") or {}
    result = []
    for suite, default in (("sample", "data/sample"), ("secret", "data/secret")):
        directory = problem_dir / data.get(f"{suite}_dir", default)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.in"), key=lambda item: item.name):
            case_name = f"{suite}/{path.stem}"
            result.append({
                "suite": suite,
                "name": path.stem,
                "case": case_name,
                "groups": case_group_names(case_name, suite, path.stem, groups),
            })
    return result


def _diagnostic(diagnostics, errors, warnings, code, severity, message, **payload):
    item = _json_safe({"code": code, "severity": severity, "message": message, **payload})
    diagnostics.append(item)
    rendered = f"[{code}] {message}"
    (errors if severity == "error" else warnings).append(rendered)


def _json_safe(value, active=None):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)

    active = set() if active is None else active
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            return "<recursive>"
        active.add(marker)
        try:
            return {
                str(key): _json_safe(item, active)
                for key, item in value.items()
            }
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in active:
            return "<recursive>"
        active.add(marker)
        try:
            return [_json_safe(item, active) for item in value]
        finally:
            active.remove(marker)
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _path_is_link_like(path):
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse and attributes & reparse)
    except OSError:
        return False


def _configured_source_path(problem_dir, program):
    if not isinstance(program, str) or not program or "\x00" in program:
        return None, "invalid", "path must be a non-empty relative path"

    normalized = program.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        return None, "invalid", "absolute paths are forbidden"
    if ".." in posix_path.parts:
        return None, "invalid", "parent traversal is forbidden"

    parts = [part for part in posix_path.parts if part not in {"", "."}]
    if not parts:
        return None, "invalid", "path must name a file"
    candidate = problem_dir.joinpath(*parts)
    current = problem_dir
    for part in parts:
        current = current / part
        if _path_is_link_like(current):
            return None, "invalid", "symbolic links and junctions are forbidden"

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, "missing", "file does not exist"
    except (OSError, RuntimeError, ValueError) as exc:
        return None, "invalid", f"path cannot be resolved: {exc}"

    try:
        resolved.relative_to(problem_dir)
    except ValueError:
        return None, "invalid", "resolved path leaves the problem directory"
    try:
        mode = candidate.lstat().st_mode
    except OSError as exc:
        return None, "invalid", f"file cannot be inspected: {exc}"
    if not stat.S_ISREG(mode):
        return None, "invalid", "path must name a regular file"
    return resolved, None, None


def resolve_solution_source(problem_dir, program):
    """Resolve one configured solution through the shared strict path fence."""
    return _configured_source_path(Path(problem_dir).resolve(), program)


_RAW_STRING_START = re.compile(
    r'(?:u8|u|U|L)?R"(?P<delimiter>[^ ()\\\t\r\n\v\f]{0,16})\('
)
_DIRECTIVE = re.compile(r"^\s*#\s*([A-Za-z_]\w*)\b(.*)$")
_INTEGER_LITERAL = re.compile(
    r"(?P<number>0[xX][0-9A-Fa-f]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)"
    r"[uUlL]*"
)


def _mask_cpp_non_code(source_text):
    masked = []
    length = len(source_text)
    index = 0
    while index < length:
        char = source_text[index]
        following = source_text[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            masked.extend((" ", " "))
            index += 2
            while index < length and source_text[index] not in "\r\n":
                masked.append(" ")
                index += 1
            continue
        if char == "/" and following == "*":
            masked.extend((" ", " "))
            index += 2
            while index < length:
                if source_text[index] == "*" and index + 1 < length and source_text[index + 1] == "/":
                    masked.extend((" ", " "))
                    index += 2
                    break
                masked.append(source_text[index] if source_text[index] in "\r\n" else " ")
                index += 1
            continue

        raw_match = None
        if not (index and (source_text[index - 1].isalnum() or source_text[index - 1] == "_")):
            raw_match = _RAW_STRING_START.match(source_text, index)
        if raw_match is not None:
            terminator = ")" + raw_match.group("delimiter") + '"'
            end = source_text.find(terminator, raw_match.end())
            end = length if end < 0 else end + len(terminator)
            while index < end:
                masked.append(source_text[index] if source_text[index] in "\r\n" else " ")
                index += 1
            continue

        if char in {'"', "'"}:
            quote = char
            masked.append(" ")
            index += 1
            while index < length:
                char = source_text[index]
                if char == "\\" and index + 1 < length:
                    masked.append(" ")
                    index += 1
                    escaped = source_text[index]
                    masked.append(escaped if escaped in "\r\n" else " ")
                    index += 1
                    continue
                masked.append(char if char in "\r\n" else " ")
                index += 1
                if char == quote or char in "\r\n":
                    break
            continue

        masked.append(char)
        index += 1
    return "".join(masked)


def _strip_wrapping_parentheses(expression):
    value = expression.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        closes_at_end = False
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = index == len(value) - 1
                    break
        if not closes_at_end:
            break
        value = value[1:-1].strip()
    return value


def _literal_preprocessor_condition(expression):
    value = re.sub(r"\s+", "", _strip_wrapping_parentheses(expression))
    match = _INTEGER_LITERAL.fullmatch(value)
    if match is None:
        return None
    number = match.group("number")
    if number.lower().startswith("0x"):
        parsed = int(number, 16)
    elif number.lower().startswith("0b"):
        parsed = int(number, 2)
    elif len(number) > 1 and number.startswith("0"):
        parsed = int(number, 8)
    else:
        parsed = int(number, 10)
    return bool(parsed)


def _parse_include_target(source_line, keyword_end):
    tail = source_line[keyword_end:]
    index = 0
    while index < len(tail):
        if tail[index].isspace():
            index += 1
            continue
        if tail.startswith("/*", index):
            closing = tail.find("*/", index + 2)
            if closing < 0:
                return None
            index = closing + 2
            continue
        break
    if index >= len(tail) or tail[index] not in {'"', "<"}:
        return None
    closing = '"' if tail[index] == '"' else ">"
    end = tail.find(closing, index + 1)
    if end < 0:
        return None
    target = tail[index + 1:end].strip()
    return target or None


def _include_targets(source_text):
    masked_lines = _mask_cpp_non_code(source_text).splitlines()
    source_lines = source_text.splitlines()
    active = True
    conditionals = []
    result = []
    for index, masked_line in enumerate(masked_lines):
        match = _DIRECTIVE.match(masked_line)
        if match is None:
            continue
        directive = match.group(1).lower()
        expression = match.group(2)
        if directive in {"if", "ifdef", "ifndef"}:
            condition = (
                _literal_preprocessor_condition(expression)
                if directive == "if"
                else None
            )
            conditionals.append({
                "parent_active": active,
                "later_possible": condition is not True,
            })
            active = active and condition is not False
            continue
        if directive == "elif":
            if conditionals:
                frame = conditionals[-1]
                condition = _literal_preprocessor_condition(expression)
                active = (
                    frame["parent_active"]
                    and frame["later_possible"]
                    and condition is not False
                )
                if condition is True:
                    frame["later_possible"] = False
            continue
        if directive == "else":
            if conditionals:
                frame = conditionals[-1]
                active = frame["parent_active"] and frame["later_possible"]
                frame["later_possible"] = False
            continue
        if directive == "endif":
            if conditionals:
                active = conditionals.pop()["parent_active"]
            continue
        if directive != "include" or not active or index >= len(source_lines):
            continue
        target = _parse_include_target(source_lines[index], match.end(1))
        if target:
            result.append(target)
    return result


def _resolved_include(problem_dir, source_path, include):
    candidates = (source_path.parent / include, problem_dir / include)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _is_full_range_ac(kind, entry, testcases, groups):
    expected = entry.get("expected") or expected_defaults(kind)
    if entry.get("run_on") is not None:
        return False
    if set(expected.get("status") or []) != {"AC"} or expected.get("all") is not True:
        return False
    selected, _ = select_expectation_cases(kind, entry, testcases, groups)
    selected_names = [case.get("case") for case in selected]
    testcase_names = [case.get("case") for case in testcases]
    return len(selected_names) == len(testcase_names) and set(selected_names) == set(testcase_names)


def analyze_solution_verification(problem_dir, config):
    problem_dir = Path(problem_dir).resolve()
    diagnostics, errors, warnings = [], [], []
    difficulty = config.get("difficulty")
    difficulty_valid = difficulty is None or (
        isinstance(difficulty, int)
        and not isinstance(difficulty, bool)
        and 0 <= difficulty <= 5
    )
    if not difficulty_valid:
        _diagnostic(
            diagnostics,
            errors,
            warnings,
            "invalid_difficulty",
            "error",
            "difficulty must be an integer from 0 to 5",
            value=difficulty,
        )
    high = difficulty_valid and difficulty is not None and difficulty >= HIGH_DIFFICULTY_THRESHOLD

    entries = normalize_solution_entries(config)
    groups = normalize_data_groups(config)
    group_names = {group["name"] for group in groups}
    testcases = _problem_testcases(problem_dir, config, groups)
    group_case_counts = {
        name: sum(name in (case.get("groups") or []) for case in testcases)
        for name in group_names
    }

    unique_entries = {}
    for kind, kind_entries in entries.items():
        seen = {}
        unique = []
        for entry in kind_entries:
            program = entry.get("file")
            if not program:
                continue
            folded = program.casefold()
            if folded in seen:
                reference = seen[folded]["file"]
                if kind == "std":
                    code = "duplicate_accepted_solution"
                    message = f"accepted solution is registered more than once: {program}"
                else:
                    code = "duplicate_solution_entry"
                    message = (
                        f"solutions.{entry.get('config_kind')} entry is registered more "
                        f"than once: {program}"
                    )
                _diagnostic(
                    diagnostics, errors, warnings,
                    code, "error", message,
                    kind=kind,
                    config_kind=entry.get("config_kind"),
                    program=program,
                    reference=reference,
                )
                continue
            seen[folded] = entry
            unique.append(entry)
        unique_entries[kind] = unique

    configured_sources = {}
    valid_expected_entries = set()
    for kind, kind_entries in entries.items():
        for entry in kind_entries:
            program = entry.get("file")
            config_kind = entry.get("config_kind")
            if not program:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "solution_file_missing", "error",
                    f"solutions.{config_kind} entry must declare a non-empty file",
                    kind=kind,
                    config_kind=config_kind,
                    index=entry.get("index"),
                )
                continue
            expected_issues = _expected_schema_issues(entry, group_names)
            if not expected_issues:
                valid_expected_entries.add((kind, entry.get("index")))
            for issue in expected_issues:
                payload = {
                    key: value
                    for key, value in issue.items()
                    if key not in {"code", "message"}
                }
                _diagnostic(
                    diagnostics, errors, warnings,
                    issue["code"], "error", issue["message"],
                    kind=kind,
                    config_kind=config_kind,
                    program=program,
                    **payload,
                )
            source_path, path_state, reason = _configured_source_path(problem_dir, program)
            if source_path is None:
                if path_state == "missing":
                    path_code = "accepted_missing" if kind == "std" else "solution_missing"
                    message = f"solution source file not found: {program}"
                else:
                    path_code = "solution_source_invalid"
                    message = (
                        "solution source must be a regular non-link file inside the problem "
                        f"directory: {program} ({reason})"
                    )
                _diagnostic(
                    diagnostics, errors, warnings,
                    path_code, "error", message,
                    kind=kind,
                    config_kind=config_kind,
                    program=program,
                    reason=reason,
                )
                continue
            try:
                source_bytes = source_path.read_bytes()
            except OSError as exc:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "solution_source_unreadable", "error",
                    f"solution source cannot be read: {program}",
                    kind=kind,
                    config_kind=config_kind,
                    program=program,
                    reason=str(exc),
                )
                continue
            configured_sources[(kind, entry.get("index"))] = {
                "entry": entry,
                "path": source_path,
                "bytes": source_bytes,
                "text": source_bytes.decode("utf-8", errors="replace"),
            }

    accepted = entries["std"]
    unique_accepted = unique_entries["std"]
    accepted_sources = {}
    for entry in unique_accepted:
        folded = entry["file"].casefold()
        source = configured_sources.get(("std", entry.get("index")))
        if source is not None:
            accepted_sources[folded] = source

    for left, right in combinations(accepted_sources.values(), 2):
        if left["bytes"] != right["bytes"]:
            continue
        program = right["entry"]["file"]
        reference = left["entry"]["file"]
        _diagnostic(
            diagnostics, errors, warnings,
            "independent_accepted_identical", "error",
            f"accepted solutions have identical bytes: {program} and {reference}",
            program=program,
            reference=reference,
        )

    accepted_by_resolved_path = {}
    for source in accepted_sources.values():
        accepted_by_resolved_path.setdefault(source["path"], []).append(source)
    reported_includes = set()
    for source in accepted_sources.values():
        program = source["entry"]["file"]
        for include in _include_targets(source["text"]):
            resolved = _resolved_include(problem_dir, source["path"], include)
            for reference_source in accepted_by_resolved_path.get(resolved, []):
                if reference_source is source:
                    continue
                reference = reference_source["entry"]["file"]
                key = (program.casefold(), reference.casefold())
                if key in reported_includes:
                    continue
                reported_includes.add(key)
                _diagnostic(
                    diagnostics, errors, warnings,
                    "independent_accepted_includes_reference", "error",
                    f"accepted solution directly includes another accepted: {program} includes {reference}",
                    program=program,
                    reference=reference,
                )

    for kind, kind_entries in entries.items():
        for entry in kind_entries:
            raw = entry.get("raw")
            program = entry.get("file") or f"{entry.get('config_kind')}[{entry.get('index')}]"
            if not isinstance(raw, dict) or "run_on" not in raw:
                continue
            normalized = entry.get("run_on") or []
            schema_error = entry.get("run_on_error")
            if schema_error:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "invalid_solution_run_on", "error",
                    f"run_on for {program} is invalid: {schema_error}",
                    program=program,
                )
                continue
            if kind == "std" and entry.get("index") == 0:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "primary_accepted_run_on_forbidden", "error",
                    f"first accepted solution must run on all cases: {program}",
                    program=program,
                )
            if kind == "std" and entry.get("index", 0) > 0:
                expected_raw = raw.get("expected")
                expected_groups = (
                    expected_raw.get("groups")
                    if isinstance(expected_raw, dict)
                    else None
                )
                if not _string_list(expected_groups):
                    _diagnostic(
                        diagnostics, errors, warnings,
                        "partial_accepted_expected_groups_required", "error",
                        f"accepted solution with run_on must explicitly declare expected.groups: {program}",
                        program=program,
                    )
            unknown = sorted(set(normalized) - group_names)
            if unknown:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "solution_run_on_unknown_group", "error",
                    f"run_on for {program} references unknown groups: {', '.join(unknown)}",
                    program=program,
                    groups=unknown,
                )
            empty = sorted(name for name in normalized if group_case_counts.get(name, 0) == 0)
            if empty and not unknown:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "solution_run_on_has_no_cases", "error",
                    f"run_on for {program} references groups with no matching cases: {', '.join(empty)}",
                    program=program,
                    groups=empty,
                )
            if unknown or empty:
                continue
            executed, _ = select_run_cases(entry, testcases)
            selected, _ = select_expectation_cases(kind, entry, testcases, groups)
            targeted, target_groups = select_target_cases(entry, testcases, groups)
            executed_names = {case["case"] for case in executed}
            missing = [case["case"] for case in selected if case["case"] not in executed_names]
            if missing:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "solution_run_on_expectation_gap", "error",
                    f"run_on for {program} skips {len(missing)} case(s) selected by its expectation",
                    program=program,
                    missing_count=len(missing),
                    missing_cases=missing[:20],
                )
            selected_names = {case["case"] for case in selected}
            missing_targets = [
                case["case"]
                for case in targeted
                if case["case"] not in executed_names
                and case["case"] not in selected_names
            ]
            if missing_targets:
                _diagnostic(
                    diagnostics, errors, warnings,
                    "solution_run_on_target_gap", "error",
                    f"run_on for {program} skips {len(missing_targets)} case(s) required by data.groups.targets",
                    program=program,
                    target_groups=target_groups,
                    missing_count=len(missing_targets),
                    missing_cases=missing_targets[:20],
                )

    claims = []
    accepted_by_path = {
        entry["file"].casefold(): entry
        for entry in unique_accepted
        if entry.get("file")
    }
    for entry in unique_accepted[1:]:
        program = entry["file"]
        claim = entry.get("independence")
        if claim is None:
            _diagnostic(
                diagnostics, errors, warnings,
                "independent_accepted_unverified", "warning",
                f"accepted solution has no independence declaration: {program}",
                program=program,
            )
            continue
        if not isinstance(claim, dict):
            _diagnostic(
                diagnostics, errors, warnings,
                "invalid_independence_declaration", "error",
                f"independence for {program} must be a mapping",
                program=program,
            )
            continue
        raw_reference = claim.get("from")
        raw_basis = claim.get("basis")
        raw_note = claim.get("note")
        fields_are_strings = all(
            isinstance(value, str)
            for value in (raw_reference, raw_basis, raw_note)
        )
        reference = (
            raw_reference.strip().replace("\\", "/")
            if fields_are_strings
            else ""
        )
        basis = raw_basis.strip() if fields_are_strings else ""
        note = raw_note.strip() if fields_are_strings else ""
        reference_entry = accepted_by_path.get(reference.casefold()) if reference else None
        valid = bool(
            fields_are_strings
            and reference_entry is not None
            and reference.casefold() != program.casefold()
            and basis in INDEPENDENCE_BASES
            and note
        )
        if not valid:
            _diagnostic(
                diagnostics, errors, warnings,
                "invalid_independence_declaration", "error",
                f"independence for {program} requires string from/basis/note fields, "
                "another accepted in from, basis algorithm/key_implementation, and a non-empty note",
                program=program,
            )
            continue
        canonical_reference = reference_entry["file"]
        claim_result = {
            "program": program,
            "from": canonical_reference,
            "basis": basis,
            "note": note,
            "verified": False,
        }
        claims.append(claim_result)

    full_range_accepted = [
        entry for entry in unique_accepted
        if entry["file"].casefold() in accepted_sources
        and ("std", entry.get("index")) in valid_expected_entries
        and _is_full_range_ac("std", entry, testcases, groups)
    ]
    full_range_brute = [
        entry for entry in unique_entries["brute"]
        if ("brute", entry.get("index")) in configured_sources
        and ("brute", entry.get("index")) in valid_expected_entries
        and _is_full_range_ac("brute", entry, testcases, groups)
    ]
    if high and len(full_range_accepted) <= 1 and not full_range_brute:
        _diagnostic(
            diagnostics, errors, warnings,
            "single_accepted_high_difficulty", "warning",
            "high-difficulty problem has only one full-range accepted solution and no full-range AC reference",
            difficulty=difficulty,
            full_range_accepted_count=len(full_range_accepted),
        )

    return _json_safe({
        "analysis_state": "declaration_and_static_checks",
        "verified": False,
        "difficulty": {
            "value": difficulty,
            "high_threshold": HIGH_DIFFICULTY_THRESHOLD,
            "is_high": bool(high),
        },
        "accepted": {
            "declared_count": len(accepted),
            "unique_count": len(unique_accepted),
            "full_range_count": len(full_range_accepted),
            "primary": unique_accepted[0]["file"] if unique_accepted else None,
            "independence_claims": claims,
        },
        "brute_coverage": {
            "state": "full" if full_range_brute else ("partial" if entries["brute"] else "missing"),
            "full_range_programs": [entry.get("file") for entry in full_range_brute],
        },
        "declared_count": len(accepted),
        "full_range_count": len(full_range_accepted),
        "requires_review": bool(claims or warnings or errors),
        "diagnostics": diagnostics,
        "errors": errors,
        "warnings": warnings,
    })
