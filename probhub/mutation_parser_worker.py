"""Isolated Tree-sitter worker for C++ mutation syntax locations.

This module is the only place in the Core package that imports the native
Tree-sitter bindings.  The parent process communicates with it through a
small, versioned JSON request/response protocol and runs it under the shared
process controller.
"""

from __future__ import annotations

import bisect
import hashlib
from importlib import metadata
import json
import os
import re
import stat
import sys
from array import array
from pathlib import Path

from .mutation_syntax import (
    MUTATION_LOCATOR_VERSION,
    MUTATION_PARSER_DIAGNOSTIC_BYTES,
    MUTATION_PARSER_MAX_LOCATION_ENTRIES,
    MUTATION_PARSER_PROTOCOL_VERSION,
    MUTATION_PARSER_REQUEST_LIMIT_BYTES,
    MUTATION_PARSER_RESPONSE_LIMIT_BYTES,
    MUTATION_PARSER_SOURCE_LIMIT_BYTES,
    TREE_SITTER_CPP_VERSION,
    TREE_SITTER_VERSION,
)


MAX_SOURCE_BYTES = MUTATION_PARSER_SOURCE_LIMIT_BYTES
MAX_REQUEST_BYTES = MUTATION_PARSER_REQUEST_LIMIT_BYTES
MAX_RESPONSE_BYTES = MUTATION_PARSER_RESPONSE_LIMIT_BYTES
MAX_LOCATION_ENTRIES = MUTATION_PARSER_MAX_LOCATION_ENTRIES
MAX_ERROR_BYTES = MUTATION_PARSER_DIAGNOSTIC_BYTES

_EXECUTABLE_OWNER_TYPES = frozenset({"function_definition", "lambda_expression"})
_BLOCKED_CONTEXT_TYPES = frozenset({
    "alignof_expression",
    "attribute_declaration",
    "attribute_specifier",
    "concept_definition",
    "decltype",
    "noexcept",
    "requires_clause",
    "requires_expression",
    "sizeof_expression",
    "static_assert_declaration",
    "template_argument_list",
    "template_parameter_list",
    "typeid_expression",
})
_BLOCKED_KEYWORD_CALLS = frozenset({"noexcept", "typeid"})


def _bounded(value, limit=MAX_ERROR_BYTES):
    return str(value or "").encode("utf-8", errors="replace")[:limit].decode(
        "utf-8", errors="ignore"
    )


def _emit(payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) + 1 > MAX_RESPONSE_BYTES:
        payload = _error("mutation_parser_protocol_error", "worker response is too large")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _error(code, message):
    return {
        "protocol_version": MUTATION_PARSER_PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": str(code), "message": _bounded(message)},
    }


def _regular_source(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"source file is unavailable: {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    ):
        raise ValueError("source file must not be a link")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("source file is not a regular file")
    if info.st_size > MAX_SOURCE_BYTES:
        raise ValueError("source file is too large")
    return Path(path).read_bytes()


def _request(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request is too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "protocol_version", "operation", "source_path", "source_sha256"
    }:
        raise ValueError("request schema is invalid")
    if (
        isinstance(payload["protocol_version"], bool)
        or payload["protocol_version"] != MUTATION_PARSER_PROTOCOL_VERSION
    ):
        raise ValueError("request protocol version is unsupported")
    if payload["operation"] != "locate-cpp-v1":
        raise ValueError("request operation is unsupported")
    if (
        not isinstance(payload["source_path"], str)
        or not payload["source_path"]
        or not Path(payload["source_path"]).is_absolute()
    ):
        raise ValueError("request source path is invalid")
    if (
        not isinstance(payload["source_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["source_sha256"]) is None
    ):
        raise ValueError("request source hash is invalid")
    return payload


class _SourceOffsets:
    def __init__(self, source):
        self.source = source
        self.ascii = source.isascii()
        if self.ascii:
            self.byte_offsets = None
            return
        self.byte_offsets = array("Q", [0])
        total = 0
        for character in source:
            total += len(character.encode("utf-8"))
            self.byte_offsets.append(total)

    def character_offset(self, byte_offset):
        if self.ascii:
            if not 0 <= byte_offset <= len(self.source):
                raise ValueError("parser returned an out-of-range source location")
            return byte_offset
        index = bisect.bisect_left(self.byte_offsets, byte_offset)
        if index >= len(self.byte_offsets) or self.byte_offsets[index] != byte_offset:
            raise ValueError("parser returned a location inside a UTF-8 character")
        return index

    def span(self, node):
        start = self.character_offset(node.start_byte)
        end = self.character_offset(node.end_byte)
        if not 0 <= start <= end <= len(self.source):
            raise ValueError("parser returned an out-of-range source location")
        return {
            "start": start,
            "end": end,
            "text": self.source[start:end],
            "kind": node.type,
        }


def _parse_cpp(source_bytes):
    try:
        from tree_sitter import Parser
        from tree_sitter import Language
        import tree_sitter_cpp

        language = Language(tree_sitter_cpp.language())
        tree = Parser(language).parse(source_bytes)
    except ImportError as exc:
        raise RuntimeError(f"mutation parser dependencies are unavailable: {exc}") from exc
    except (TypeError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"C++ mutation parser failed: {exc}") from exc
    if tree is None or tree.root_node is None:
        raise RuntimeError("C++ mutation parser returned no syntax tree")
    if tree.root_node.has_error:
        error = _first_syntax_error(tree.root_node)
        point = error.start_point if error is not None else tree.root_node.start_point
        raise SyntaxError(
            "accepted C++ source contains syntax the mutation parser cannot locate safely "
            f"at line {point.row + 1}, byte column {point.column + 1}"
        )
    return tree


def _first_syntax_error(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR" or current.is_missing:
            return current
        stack.extend(reversed(current.children))
    return None


def _iter_nodes(root):
    stack = [(root, ())]
    while stack:
        node, ancestors = stack.pop()
        if node.type.startswith("preproc_"):
            continue
        yield node, ancestors
        child_ancestors = (*ancestors, node)
        stack.extend((child, child_ancestors) for child in reversed(node.children))


def _inside_executable_body(ancestors):
    owner_index = -1
    for index, ancestor in enumerate(ancestors):
        if ancestor.type in _EXECUTABLE_OWNER_TYPES:
            owner_index = index
    return owner_index >= 0 and any(
        ancestor.type == "compound_statement"
        for ancestor in ancestors[owner_index + 1:]
    )


def _inside_blocked_context(ancestors, offsets):
    for ancestor in ancestors:
        if ancestor.type != "call_expression":
            continue
        function = ancestor.child_by_field_name("function")
        if function is not None and offsets.span(function)["text"] in _BLOCKED_KEYWORD_CALLS:
            return True
    return any(
        ancestor.type in _BLOCKED_CONTEXT_TYPES
        or ancestor.type.startswith("preproc_")
        for ancestor in ancestors
    )


def _inside_constexpr_condition(node, ancestors):
    for ancestor in reversed(ancestors):
        if ancestor.type != "if_statement":
            continue
        if not any(child.type in {"constexpr", "consteval"} for child in ancestor.children):
            continue
        condition = ancestor.child_by_field_name("condition")
        if (
            condition is not None
            and condition.start_byte <= node.start_byte
            and node.end_byte <= condition.end_byte
        ):
            return True
    return False


def _inside_case_label(node, ancestors):
    for ancestor in reversed(ancestors):
        if ancestor.type != "case_statement":
            continue
        value = ancestor.child_by_field_name("value")
        return (
            value is not None
            and value.start_byte <= node.start_byte
            and node.end_byte <= value.end_byte
        )
    return False


def _eligible_expression(node, ancestors, offsets):
    return (
        _inside_executable_body(ancestors)
        and not _inside_blocked_context(ancestors, offsets)
        and not _inside_constexpr_condition(node, ancestors)
        and not _inside_case_label(node, ancestors)
    )


def _locate(source):
    source_bytes = source.encode("utf-8")
    tree = _parse_cpp(source_bytes)
    offsets = _SourceOffsets(source)
    comparisons = []
    unary_not = []
    conditions = []
    for node, ancestors in _iter_nodes(tree.root_node):
        if node.type == "binary_expression" and _eligible_expression(node, ancestors, offsets):
            operator = node.child_by_field_name("operator")
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if operator is not None and left is not None and right is not None:
                comparisons.append({
                    "operator": offsets.span(operator),
                    "left": offsets.span(left),
                    "right": offsets.span(right),
                })
        elif node.type == "unary_expression" and _eligible_expression(node, ancestors, offsets):
            operator = node.child_by_field_name("operator")
            if operator is not None:
                operator_span = offsets.span(operator)
                if operator_span["text"] == "!":
                    unary_not.append(operator_span)
        elif node.type in {"if_statement", "while_statement", "do_statement"}:
            if (
                not _inside_executable_body(ancestors)
                or _inside_blocked_context(ancestors, offsets)
            ):
                continue
            if node.type == "if_statement" and any(
                child.type in {"constexpr", "consteval"} for child in node.children
            ):
                continue
            condition = node.child_by_field_name("condition")
            if condition is None or condition.type not in {
                "condition_clause", "parenthesized_expression"
            }:
                continue
            if condition.child_by_field_name("initializer") is not None:
                continue
            value = condition.child_by_field_name("value")
            if (
                condition.type == "condition_clause"
                and (value is None or value.type in {"declaration", "condition_declaration"})
            ):
                continue
            keyword = "if" if node.type == "if_statement" else "while"
            conditions.append({"keyword": keyword, "condition": offsets.span(condition)})
    if any(len(items) > MAX_LOCATION_ENTRIES for items in (comparisons, unary_not, conditions)):
        raise OverflowError("mutation parser returned too many syntax locations")
    comparisons.sort(key=lambda item: (
        item["operator"]["start"],
        item["operator"]["end"],
        item["left"]["start"],
        item["right"]["start"],
    ))
    unary_not.sort(key=lambda item: (item["start"], item["end"]))
    conditions.sort(key=lambda item: (
        item["condition"]["start"],
        item["condition"]["end"],
        item["keyword"],
    ))
    return comparisons, unary_not, conditions


def _run(request_path):
    request = _request(request_path)
    try:
        installed_tree_sitter = metadata.version("tree-sitter")
        installed_tree_sitter_cpp = metadata.version("tree-sitter-cpp")
    except metadata.PackageNotFoundError as exc:
        return _error("mutation_parser_unavailable", str(exc))
    if (
        installed_tree_sitter != TREE_SITTER_VERSION
        or installed_tree_sitter_cpp != TREE_SITTER_CPP_VERSION
    ):
        return _error(
            "mutation_parser_unavailable",
            "tree-sitter native bindings do not match the pinned runtime",
        )
    source_bytes = _regular_source(request["source_path"])
    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != request["source_sha256"]:
        return _error("mutation_parser_input_changed", "source hash changed before parsing")
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _error("mutation_source_invalid", "C++ source is not valid UTF-8 text")
    try:
        comparisons, unary_not, conditions = _locate(source)
    except SyntaxError as exc:
        return _error("mutation_syntax_invalid", str(exc))
    except OverflowError as exc:
        return _error("mutation_parser_protocol_error", str(exc))
    except ValueError as exc:
        return _error("mutation_location_invalid", str(exc))
    except RuntimeError as exc:
        message = str(exc)
        code = (
            "mutation_parser_unavailable"
            if "dependencies are unavailable" in message
            else "mutation_parser_failed"
        )
        return _error(code, message)
    return {
        "protocol_version": MUTATION_PARSER_PROTOCOL_VERSION,
        "ok": True,
        "operation": "locate-cpp-v1",
        "locator_version": MUTATION_LOCATOR_VERSION,
        "tree_sitter_version": TREE_SITTER_VERSION,
        "tree_sitter_cpp_version": TREE_SITTER_CPP_VERSION,
        "source_sha256": digest,
        "locations": {
            "comparisons": comparisons,
            "unary_not": unary_not,
            "conditions": conditions,
        },
    }


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        _emit(_error("mutation_parser_protocol_error", "expected one request path"))
        return 2
    try:
        response = _run(args[0])
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        response = _error("mutation_parser_protocol_error", str(exc))
    except MemoryError:
        response = _error("mutation_parser_memory_limit", "mutation parser exceeded its memory limit")
    except BaseException as exc:  # native/parser failures must be structured when possible
        response = _error("mutation_parser_failed", str(exc))
    _emit(response)
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
