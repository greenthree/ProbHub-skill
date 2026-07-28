"""Bounded, evidence-preserving statement/Validator constraint comparison.

This module intentionally recognizes a small set of direct literal forms.  It
is a lint-time review aid, not a C++ parser or a proof that the statement and
Validator are semantically equivalent.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re

from .statement import iter_unfenced_lines


_COMPARISON_SPLIT = re.compile(r"\s*(<=|>=|==|=|<|>)\s*")
_CPP_NUMBER = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:[uUlLfF]+)?$"
)
_PLAIN_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_POWER_OF_TEN = re.compile(
    r"^(?P<sign>[+-]?)"
    r"(?:(?P<coefficient>\d+(?:\.\d+)?)\s*(?:\\times|\\cdot|×|·|\*)\s*)?"
    r"10\s*\^\s*\{?\s*(?P<exponent>[+-]?\d+)\s*\}?$"
)
_READ_CALLS = {
    "readInt": "integer",
    "readLong": "integer",
    "readDouble": "real",
    "readStrictDouble": "real",
    "readReal": "real",
    "readStrictReal": "real",
}
_CPP_RAW_STRING_OPEN = re.compile(
    r'(?:u8|u|U|L)?R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\('
)


def _decimal_text(value):
    if value == value.to_integral_value():
        return str(int(value))
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _json_number(value):
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _parse_statement_number(raw):
    token = raw.strip()
    token = token.replace("\u2212", "-").replace("\u00a0", " ")
    token = token.strip("()[]")
    power = _POWER_OF_TEN.fullmatch(token)
    try:
        if power:
            coefficient = Decimal(power.group("coefficient") or "1")
            if power.group("sign") == "-":
                coefficient = -coefficient
            value = coefficient * (Decimal(10) ** int(power.group("exponent")))
            return value
        if _PLAIN_NUMBER.fullmatch(token):
            return Decimal(token)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return None


def _parse_cpp_number(raw):
    token = raw.strip()
    if not _CPP_NUMBER.fullmatch(token):
        return None
    token = re.sub(r"[uUlLfF]+$", "", token)
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _strip_balanced_outer_parentheses(value):
    value = value.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        balanced = True
        for index, character in enumerate(value):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    balanced = False
                    break
            if depth < 0:
                balanced = False
                break
        if not balanced or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _strip_cpp_casts(value):
    value = _strip_balanced_outer_parentheses(value)
    while True:
        match = re.fullmatch(r"static_cast\s*<[^>]+>\s*\((.*)\)", value, re.DOTALL)
        if not match:
            return value
        value = _strip_balanced_outer_parentheses(match.group(1))


def _canonical_subject(raw):
    value = raw.strip().strip("$`*")
    value = value.replace("\\left", "").replace("\\right", "")
    value = value.replace("\\_", "_")
    value = value.replace("\u2212", "-")
    value = re.sub(r"\\(?:mathrm|text|operatorname)\s*\{([^{}]+)\}", r"\1", value)
    value = _strip_balanced_outer_parentheses(value)
    length_match = re.fullmatch(r"\|\s*([^|]+?)\s*\|", value)
    if not length_match:
        length_match = re.fullmatch(
            r"\\(?:lvert|left\|)\s*(.+?)\s*\\(?:rvert|right\|)",
            value,
        )
    if length_match:
        inner = _canonical_subject(length_match.group(1))
        return f"len:{inner}" if inner else ""
    size_match = re.fullmatch(
        r"([A-Za-z_]\w*)\s*\.\s*(?:size|length)\s*\(\s*\)",
        value,
    )
    if size_match:
        return f"len:{size_match.group(1).casefold()}"
    std_size_match = re.fullmatch(r"std::size\s*\(\s*([A-Za-z_]\w*)\s*\)", value)
    if std_size_match:
        return f"len:{std_size_match.group(1).casefold()}"
    value = re.sub(r"\{([^{}]+)\}", r"\1", value)
    value = re.sub(r"\s+", "", value)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?", value):
        return value.casefold()
    return ""


def _statement_subjects(raw):
    value = raw.strip()
    pieces = re.split(r"\s*[,，]\s*", value)
    subjects = []
    for piece in pieces:
        canonical = _canonical_subject(piece)
        if not canonical:
            return []
        subjects.append((piece.strip(), canonical))
    return subjects


def _normalize_statement_expression(expression):
    replacements = (
        (r"\\leq?", "<="),
        (r"\\geq?", ">="),
        ("≤", "<="),
        ("≥", ">="),
    )
    value = expression
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return value.replace("\\left", "").replace("\\right", "").strip()


def _parse_statement_operand(raw):
    raw = raw.strip().strip("，。；;:")
    number = _parse_statement_number(raw)
    if number is not None:
        return {"type": "number", "raw": raw, "number": number}
    subjects = _statement_subjects(raw)
    if subjects:
        return {"type": "subjects", "raw": raw, "subjects": subjects}
    return {"type": "unknown", "raw": raw}


def _operator_for_subject(operator, subject_on_left):
    if subject_on_left:
        return operator
    return {
        "<": ">",
        "<=": ">=",
        ">": "<",
        ">=": "<=",
        "=": "==",
        "==": "==",
    }[operator]


def _bound_for_operator(operator):
    if operator in {"<", "<="}:
        return "upper"
    if operator in {">", ">="}:
        return "lower"
    return "exact"


def _constraint_item(
    *,
    source,
    subject,
    subject_normalized,
    operator,
    boundary,
    path,
    line,
    raw,
    kind,
    origin,
):
    bound = _bound_for_operator(operator)
    item = {
        "source": source,
        "subject": subject,
        "subject_normalized": subject_normalized,
        "bound": bound,
        "operator": operator,
        "inclusive": operator in {"<=", ">=", "=="},
        "dynamic": boundary["type"] != "number",
        "direct": boundary["type"] == "number",
        "kind": kind,
        "origin": origin,
        "path": str(path),
        "line": int(line),
        "raw": raw.strip(),
    }
    if boundary["type"] == "number":
        number = boundary["number"]
        number_text = _decimal_text(number)
        item.update({
            "value": _json_number(number),
            "value_normalized": number_text,
            "normalized": f"{subject_normalized} {operator} {number_text}",
        })
    else:
        expression = boundary.get("raw", "").strip()
        expression_subjects = boundary.get("subjects") or []
        expression_normalized = (
            ",".join(item[1] for item in expression_subjects)
            if expression_subjects
            else re.sub(r"\s+", "", expression).casefold()
        )
        item.update({
            "expression": expression,
            "expression_normalized": expression_normalized,
            "normalized": f"{subject_normalized} {operator} {expression_normalized}",
        })
    return item


def _constraints_for_relation(
    left,
    operator,
    right,
    *,
    path,
    line,
    raw,
    origin,
    kind="unknown",
    preferred_subject=None,
):
    if preferred_subject == "left":
        subject_operand, boundary, subject_on_left = left, right, True
    elif preferred_subject == "right":
        subject_operand, boundary, subject_on_left = right, left, False
    elif left["type"] == "subjects" and right["type"] != "unknown":
        subject_operand, boundary, subject_on_left = left, right, True
    elif right["type"] == "subjects" and left["type"] == "number":
        subject_operand, boundary, subject_on_left = right, left, False
    else:
        return []
    if subject_operand["type"] != "subjects" or boundary["type"] == "unknown":
        return []
    normalized_operator = _operator_for_subject(operator, subject_on_left)
    return [
        _constraint_item(
            source="statement" if origin == "input_format" else "validator",
            subject=subject,
            subject_normalized=canonical,
            operator=normalized_operator,
            boundary=boundary,
            path=path,
            line=line,
            raw=raw,
            kind=kind,
            origin=origin,
        )
        for subject, canonical in subject_operand["subjects"]
    ]


def _comparison_constraints(
    expression,
    *,
    path,
    line,
    origin,
    operand_parser,
    kind="unknown",
):
    parts = _COMPARISON_SPLIT.split(expression)
    if len(parts) < 3:
        return []
    operands = [operand_parser(parts[index]) for index in range(0, len(parts), 2)]
    operators = ["==" if value == "=" else value for value in parts[1::2]]
    if any(operand["type"] == "unknown" for operand in operands):
        return []

    constraints = []
    consumed = set()
    for index in range(1, len(operands) - 1):
        if operands[index]["type"] != "subjects":
            continue
        constraints.extend(_constraints_for_relation(
            operands[index - 1],
            operators[index - 1],
            operands[index],
            path=path,
            line=line,
            raw=expression,
            origin=origin,
            kind=kind,
            preferred_subject="right",
        ))
        constraints.extend(_constraints_for_relation(
            operands[index],
            operators[index],
            operands[index + 1],
            path=path,
            line=line,
            raw=expression,
            origin=origin,
            kind=kind,
            preferred_subject="left",
        ))
        consumed.update({index - 1, index})

    for index, operator in enumerate(operators):
        if index in consumed:
            continue
        constraints.extend(_constraints_for_relation(
            operands[index],
            operator,
            operands[index + 1],
            path=path,
            line=line,
            raw=expression,
            origin=origin,
            kind=kind,
        ))
    return constraints


def _inline_math_expressions(line):
    expressions = [match.group(1) for match in re.finditer(r"\$\$?(.+?)\$\$?", line)]
    if expressions:
        return expressions
    return [line] if re.search(r"(?:<=|>=|<|>|≤|≥|\\le|\\ge)", line) else []


def extract_statement_constraints(parsed, path):
    input_sections = [
        item for item in parsed.get("section_occurrences", []) if item.get("key") == "input"
    ]
    if not input_sections:
        return []
    section = input_sections[0]
    start_line = section["start_line"]
    end_line = section["end_line"]
    constraints = []
    for line_number, line in iter_unfenced_lines(parsed["raw"]):
        if line_number < start_line or line_number > end_line:
            continue
        for expression in _inline_math_expressions(line):
            normalized = _normalize_statement_expression(expression)
            constraints.extend(_comparison_constraints(
                normalized,
                path=path,
                line=line_number,
                origin="input_format",
                operand_parser=_parse_statement_operand,
            ))
    return _deduplicate_constraints(constraints)


def _strip_cpp_comments(text):
    output = list(text)
    index = 0
    state = "code"
    while index < len(text):
        character = text[index]
        next_character = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            raw_match = _CPP_RAW_STRING_OPEN.match(text, index)
            if raw_match:
                marker = ")" + raw_match.group("delimiter") + '"'
                close = text.find(marker, raw_match.end())
                index = len(text) if close < 0 else close + len(marker)
                continue
            if character == "/" and next_character == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if character == "/" and next_character == "*":
                output[index] = output[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if character == '"':
                state = "string"
            elif character == "'":
                state = "character"
        elif state == "line_comment":
            if character == "\n":
                state = "code"
            else:
                output[index] = " "
        elif state == "block_comment":
            if character == "*" and next_character == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "code"
                continue
            if character != "\n":
                output[index] = " "
        elif state in {"string", "character"}:
            delimiter = '"' if state == "string" else "'"
            if character == "\\":
                index += 2
                continue
            if character == delimiter:
                state = "code"
        index += 1
    return "".join(output)


def _mask_cpp_strings(text):
    """Blank C++ string/character literals while preserving offsets and lines."""

    output = list(text)

    def blank(start, end):
        for position in range(start, end):
            if output[position] not in {"\n", "\r"}:
                output[position] = " "

    cursor = 0
    while True:
        match = _CPP_RAW_STRING_OPEN.search(text, cursor)
        if not match:
            break
        marker = ")" + match.group("delimiter") + '"'
        close = text.find(marker, match.end())
        end = len(text) if close < 0 else close + len(marker)
        blank(match.start(), end)
        cursor = max(end, match.end())

    index = 0
    while index < len(text):
        if output[index] == " ":
            index += 1
            continue
        quote = text[index]
        if quote not in {'"', "'"}:
            index += 1
            continue
        start = index
        index += 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                index += 1
                break
            index += 1
        blank(start, min(index, len(text)))
    return "".join(output)


def _call_arguments(text, open_parenthesis):
    arguments = []
    current_start = open_parenthesis + 1
    depth = 1
    index = current_start
    state = "code"
    while index < len(text):
        character = text[index]
        if state == "code":
            if character == '"':
                state = "string"
            elif character == "'":
                state = "character"
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    arguments.append(text[current_start:index].strip())
                    return arguments, index
            elif character == "," and depth == 1:
                arguments.append(text[current_start:index].strip())
                current_start = index + 1
        else:
            delimiter = '"' if state == "string" else "'"
            if character == "\\":
                index += 2
                continue
            if character == delimiter:
                state = "code"
        index += 1
    return None, None


def _iter_named_calls(text, names, *, receiver=None):
    name_pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    if receiver is None:
        pattern = re.compile(
            rf"(?<![\w.])(?P<name>{name_pattern})\s*\("
        )
    else:
        pattern = re.compile(
            rf"(?<![\w.]){re.escape(receiver)}\s*\.\s*"
            rf"(?P<name>{name_pattern})\s*\("
        )
    scan_text = _mask_cpp_strings(text)
    for match in pattern.finditer(scan_text):
        open_parenthesis = scan_text.find("(", match.start(), match.end())
        arguments, end = _call_arguments(text, open_parenthesis)
        if arguments is not None:
            yield match, match.group("name"), arguments, end


def _cpp_string_literal(raw):
    match = re.fullmatch(r'"((?:\\.|[^"\\])*)"', raw.strip(), re.DOTALL)
    if not match:
        return None
    return re.sub(r"\\([\\\"])", r"\1", match.group(1))


def _assigned_variable(text, call_start):
    boundary = max(text.rfind(";", 0, call_start), text.rfind("\n", 0, call_start)) + 1
    prefix = text[boundary:call_start]
    match = re.search(r"\b([A-Za-z_]\w*)\s*=\s*$", prefix)
    return match.group(1) if match else None


def _validator_subject(raw, aliases):
    value = _strip_cpp_casts(raw.strip())
    canonical = _canonical_subject(value)
    if not canonical:
        return []
    alias = aliases.get(canonical)
    if alias:
        return [(alias["subject"], alias["canonical"], alias.get("kind", "unknown"))]
    display = value.strip()
    kind = "integer" if canonical.startswith("len:") else "unknown"
    return [(display, canonical, kind)]


def _parse_validator_operand(raw, aliases):
    value = _strip_cpp_casts(raw.strip())
    number = _parse_cpp_number(value)
    if number is not None:
        return {"type": "number", "raw": raw.strip(), "number": number}
    subjects = _validator_subject(value, aliases)
    if subjects:
        return {
            "type": "subjects",
            "raw": raw.strip(),
            "subjects": [(subject, canonical) for subject, canonical, _kind in subjects],
            "subject_kinds": {canonical: kind for _subject, canonical, kind in subjects},
        }
    return {"type": "unknown", "raw": raw.strip()}


def _raw_source(text, start, end):
    return text[start:end + 1].strip()


def extract_validator_constraints(text, path, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else []
    stripped = _strip_cpp_comments(text)
    aliases = {}
    constraints = []
    read_calls = list(_iter_named_calls(stripped, _READ_CALLS, receiver="inf"))
    for match, method, arguments, end in read_calls:
        if len(arguments) < 2:
            continue
        assigned = _assigned_variable(stripped, match.start())
        label = _cpp_string_literal(arguments[2]) if len(arguments) >= 3 else None
        subject = label or assigned
        if not subject:
            continue
        canonical = _canonical_subject(subject)
        if not canonical:
            continue
        kind = _READ_CALLS[method]
        line = stripped.count("\n", 0, match.start()) + 1
        raw = _raw_source(text, match.start(), end)
        if assigned:
            aliases[assigned.casefold()] = {
                "subject": subject,
                "canonical": canonical,
                "kind": kind,
            }
        for argument, operator, bound in (
            (arguments[0], ">=", "lower"),
            (arguments[1], "<=", "upper"),
        ):
            number = _parse_cpp_number(argument)
            if number is not None:
                boundary = {"type": "number", "raw": argument, "number": number}
            else:
                dynamic_subjects = _validator_subject(argument, aliases)
                boundary = {
                    "type": "subjects" if dynamic_subjects else "dynamic",
                    "raw": argument,
                    "subjects": [
                        (display, normalized)
                        for display, normalized, _dynamic_kind in dynamic_subjects
                    ],
                }
            item = _constraint_item(
                source="validator",
                subject=subject,
                subject_normalized=canonical,
                operator=operator,
                boundary=boundary,
                path=path,
                line=line,
                raw=raw,
                kind=kind,
                origin=method,
            )
            item["bound"] = bound
            constraints.append(item)

    for match, _method, arguments, end in _iter_named_calls(stripped, {"ensuref"}):
        if not arguments:
            continue
        condition = re.sub(
            r"static_cast\s*<[^>]+>\s*\(\s*"
            r"([A-Za-z_]\w*(?:\s*\.\s*(?:size|length)\s*\(\s*\))?)\s*\)",
            r"\1",
            arguments[0],
        )
        line = stripped.count("\n", 0, match.start()) + 1
        raw = _raw_source(text, match.start(), end)
        if "||" in condition:
            diagnostics.append({
                "code": "constraint_validator_expression_unsupported",
                "severity": "info",
                "message": (
                    "Validator ensuref expression contains disjunction; it was retained "
                    "for manual review instead of being treated as a hard constraint"
                ),
                "path": str(path),
                "line": line,
                "raw": raw,
            })
            continue
        for clause in condition.split("&&"):
            clause = _strip_balanced_outer_parentheses(clause.strip())
            operand_parser = lambda value: _parse_validator_operand(value, aliases)
            found = _comparison_constraints(
                clause,
                path=path,
                line=line,
                origin="ensuref",
                operand_parser=operand_parser,
            )
            for item in found:
                subject_kind = "unknown"
                parsed_operand = _parse_validator_operand(item["subject"], aliases)
                if parsed_operand.get("subject_kinds"):
                    subject_kind = next(iter(parsed_operand["subject_kinds"].values()))
                item["kind"] = subject_kind
                item["raw"] = raw
            constraints.extend(found)
    return _deduplicate_constraints(constraints)


def _deduplicate_constraints(constraints):
    result = []
    seen = set()
    for item in constraints:
        key = (
            item["source"],
            item["subject_normalized"],
            item["bound"],
            item["normalized"],
            item["path"],
            item["line"],
            item["origin"],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            item["subject_normalized"],
            item["bound"],
            item["path"],
            item["line"],
            item["normalized"],
        ),
    )


def _known_numeric_domain(statement_item, validator_item):
    kinds = {statement_item.get("kind"), validator_item.get("kind")}
    if "real" in kinds:
        return "real"
    if "integer" in kinds:
        return "integer"
    return None


def _integer_semantics(statement_item, validator_item):
    return _known_numeric_domain(statement_item, validator_item) == "integer"


def _semantic_number(item, integer_semantics):
    value = Decimal(item["value_normalized"])
    if integer_semantics and item["bound"] == "upper" and item["operator"] == "<":
        return value - 1, True
    if integer_semantics and item["bound"] == "lower" and item["operator"] == ">":
        return value + 1, True
    return value, item["inclusive"]


def _numeric_equivalent(statement_item, validator_item, integer_semantics):
    statement_number, statement_inclusive = _semantic_number(
        statement_item, integer_semantics
    )
    validator_number, validator_inclusive = _semantic_number(
        validator_item, integer_semantics
    )
    return (
        statement_number == validator_number
        and statement_inclusive == validator_inclusive
    )


def _equivalent(statement_item, validator_item):
    if statement_item["dynamic"] or validator_item["dynamic"]:
        return (
            statement_item["dynamic"]
            and validator_item["dynamic"]
            and statement_item.get("expression_normalized")
            == validator_item.get("expression_normalized")
            and statement_item["operator"] == validator_item["operator"]
        )
    domain = _known_numeric_domain(statement_item, validator_item)
    if domain == "integer":
        return _numeric_equivalent(statement_item, validator_item, True)
    if domain == "real":
        return _numeric_equivalent(statement_item, validator_item, False)
    # Unknown domains are considered equivalent only when both integer and
    # real semantics agree. This prevents a heuristic from silently choosing
    # the interpretation that happens to match.
    return (
        _numeric_equivalent(statement_item, validator_item, True)
        and _numeric_equivalent(statement_item, validator_item, False)
    )


def _definite_numeric_mismatch(statement_item, validator_item):
    domain = _known_numeric_domain(statement_item, validator_item)
    if domain == "integer":
        return not _numeric_equivalent(statement_item, validator_item, True)
    if domain == "real":
        return not _numeric_equivalent(statement_item, validator_item, False)
    return (
        not _numeric_equivalent(statement_item, validator_item, True)
        and not _numeric_equivalent(statement_item, validator_item, False)
    )


def _semantic_normalized(item, counterpart):
    if item["dynamic"]:
        return item["normalized"]
    integer_semantics = _integer_semantics(item, counterpart)
    number, inclusive = _semantic_number(item, integer_semantics)
    operator = item["operator"]
    if integer_semantics and item["bound"] == "upper" and operator == "<":
        operator = "<="
    elif integer_semantics and item["bound"] == "lower" and operator == ">":
        operator = ">="
    elif not inclusive and operator in {"<", ">"}:
        operator = item["operator"]
    return f"{item['subject_normalized']} {operator} {_decimal_text(number)}"


def _unmatched_copy(item, reason):
    return {**item, "reconciliation": reason}


def reconcile_constraints(statement_constraints, validator_constraints):
    statement_groups = defaultdict(list)
    validator_groups = defaultdict(list)
    for item in statement_constraints:
        statement_groups[(item["subject_normalized"], item["bound"])].append(item)
    for item in validator_constraints:
        validator_groups[(item["subject_normalized"], item["bound"])].append(item)

    matched = []
    statement_only = []
    validator_only = []
    diagnostics = []
    high_confidence_mismatches = 0
    all_keys = sorted(set(statement_groups) | set(validator_groups))
    for key in all_keys:
        statement_items = list(statement_groups.get(key, []))
        validator_items = list(validator_groups.get(key, []))
        used_validator = set()
        remaining_statement = []
        for statement_item in statement_items:
            match_index = next(
                (
                    index
                    for index, validator_item in enumerate(validator_items)
                    if index not in used_validator
                    and _equivalent(statement_item, validator_item)
                ),
                None,
            )
            if match_index is None:
                remaining_statement.append(statement_item)
                continue
            used_validator.add(match_index)
            validator_item = validator_items[match_index]
            matched.append({
                "subject": statement_item["subject"],
                "subject_normalized": key[0],
                "bound": key[1],
                "confidence": "high" if not statement_item["dynamic"] else "medium",
                "dynamic": statement_item["dynamic"] or validator_item["dynamic"],
                "normalized": _semantic_normalized(statement_item, validator_item),
                "statement": statement_item,
                "validator": validator_item,
            })
        remaining_validator = [
            item for index, item in enumerate(validator_items) if index not in used_validator
        ]

        high_mismatch = (
            len(statement_items) == 1
            and len(validator_items) == 1
            and len(remaining_statement) == 1
            and len(remaining_validator) == 1
            and remaining_statement[0]["direct"]
            and remaining_validator[0]["direct"]
            and _definite_numeric_mismatch(
                remaining_statement[0], remaining_validator[0]
            )
        )
        if high_mismatch:
            statement_item = remaining_statement[0]
            validator_item = remaining_validator[0]
            high_confidence_mismatches += 1
            diagnostics.append({
                "code": "constraint_mismatch",
                "severity": "warning",
                "message": (
                    f"statement/Validator constraint mismatch for {key[0]} {key[1]}: "
                    f"{statement_item['normalized']} vs {validator_item['normalized']}"
                ),
                "subject": key[0],
                "bound": key[1],
                "confidence": "high",
                "statement": statement_item,
                "validator": validator_item,
            })
            statement_only.append(_unmatched_copy(statement_item, "numeric_mismatch"))
            validator_only.append(_unmatched_copy(validator_item, "numeric_mismatch"))
            continue

        opposite_exists = bool(statement_items and validator_items)
        statement_reason = "ambiguous_or_nonliteral" if opposite_exists else "no_validator_match"
        validator_reason = "ambiguous_or_nonliteral" if opposite_exists else "no_statement_match"
        statement_only.extend(
            _unmatched_copy(item, statement_reason) for item in remaining_statement
        )
        validator_only.extend(
            _unmatched_copy(item, validator_reason) for item in remaining_validator
        )

    for item in statement_only:
        if item["reconciliation"] == "numeric_mismatch":
            continue
        diagnostics.append({
            "code": "constraint_statement_only",
            "severity": "info",
            "message": f"statement-only constraint: {item['normalized']}",
            "subject": item["subject_normalized"],
            "bound": item["bound"],
            "evidence": item,
        })
    for item in validator_only:
        if item["reconciliation"] == "numeric_mismatch":
            continue
        diagnostics.append({
            "code": "constraint_validator_only",
            "severity": "info",
            "message": f"Validator-only constraint: {item['normalized']}",
            "subject": item["subject_normalized"],
            "bound": item["bound"],
            "evidence": item,
        })

    dynamic_count = sum(
        item["dynamic"] for item in [*statement_constraints, *validator_constraints]
    )
    if dynamic_count:
        diagnostics.append({
            "code": "constraint_dynamic_bounds",
            "severity": "info",
            "message": (
                f"{dynamic_count} dynamic bound(s) were retained as partial evidence; "
                "no semantic mismatch warning is inferred from them"
            ),
        })
    if matched and not statement_only and not validator_only and not dynamic_count:
        confidence = "high"
    elif matched:
        confidence = "medium"
    else:
        confidence = "low"
    no_evidence = not statement_constraints and not validator_constraints
    if no_evidence:
        diagnostics.append({
            "code": "constraint_no_evidence",
            "severity": "info",
            "message": (
                "no directly comparable statement/Validator constraints were found; "
                "manual review is still required"
            ),
        })
    requires_review = bool(
        statement_only or validator_only or dynamic_count or no_evidence
    )
    return {
        "analysis_state": "partial",
        "scope": (
            "Heuristic direct-literal comparison for the input section and selected "
            "testlib read*/ensuref forms; this is not full C++ semantic analysis."
        ),
        "matched": matched,
        "statement_only": statement_only,
        "validator_only": validator_only,
        "confidence": confidence,
        "partial": True,
        "requires_review": requires_review,
        "diagnostics": diagnostics,
        "statement_constraints": list(statement_constraints),
        "validator_constraints": list(validator_constraints),
        "summary": {
            "matched": len(matched),
            "statement_only": len(statement_only),
            "validator_only": len(validator_only),
            "dynamic": dynamic_count,
            "high_confidence_mismatches": high_confidence_mismatches,
        },
    }


def analyze_constraint_consistency(parsed, statement_path, validator_text, validator_path):
    statement_constraints = extract_statement_constraints(parsed, statement_path)
    extraction_diagnostics = []
    validator_constraints = extract_validator_constraints(
        validator_text,
        validator_path,
        diagnostics=extraction_diagnostics,
    )
    result = reconcile_constraints(statement_constraints, validator_constraints)
    result["diagnostics"] = [*extraction_diagnostics, *result["diagnostics"]]
    if extraction_diagnostics:
        result["requires_review"] = True
    return result
