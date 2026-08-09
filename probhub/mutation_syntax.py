"""Strict C++ syntax locations used by mutation planning."""

from __future__ import annotations

import bisect
from array import array
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata

from .errors import ProbHubError


MUTATION_LOCATOR_VERSION = "tree-sitter-cpp-v1"
TREE_SITTER_VERSION = "0.26.0"
TREE_SITTER_CPP_VERSION = "0.23.4"
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


@dataclass(frozen=True)
class CppSyntaxSpan:
    start: int
    end: int
    text: str
    kind: str


@dataclass(frozen=True)
class CppComparisonLocation:
    operator: CppSyntaxSpan
    left: CppSyntaxSpan
    right: CppSyntaxSpan


@dataclass(frozen=True)
class CppConditionLocation:
    keyword: str
    condition: CppSyntaxSpan


@dataclass(frozen=True)
class CppSyntaxLocations:
    comparisons: tuple[CppComparisonLocation, ...]
    unary_not: tuple[CppSyntaxSpan, ...]
    conditions: tuple[CppConditionLocation, ...]


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
                raise ProbHubError(
                    "C++ parser returned an out-of-range source location",
                    code="mutation_location_invalid",
                )
            return byte_offset
        index = bisect.bisect_left(self.byte_offsets, byte_offset)
        if index >= len(self.byte_offsets) or self.byte_offsets[index] != byte_offset:
            raise ProbHubError(
                "C++ parser returned a location inside a UTF-8 character",
                code="mutation_location_invalid",
            )
        return index

    def span(self, node):
        start = self.character_offset(node.start_byte)
        end = self.character_offset(node.end_byte)
        if not 0 <= start <= end <= len(self.source):
            raise ProbHubError(
                "C++ parser returned an out-of-range source location",
                code="mutation_location_invalid",
            )
        return CppSyntaxSpan(start, end, self.source[start:end], node.type)


def mutation_parser_identity():
    try:
        tree_sitter_version = metadata.version("tree-sitter")
        tree_sitter_cpp_version = metadata.version("tree-sitter-cpp")
    except metadata.PackageNotFoundError as exc:
        raise ProbHubError(
            "mutation syntax planning requires tree-sitter and tree-sitter-cpp",
            code="mutation_parser_unavailable",
        ) from exc
    if (
        tree_sitter_version != TREE_SITTER_VERSION
        or tree_sitter_cpp_version != TREE_SITTER_CPP_VERSION
    ):
        raise ProbHubError(
            "mutation syntax parser versions do not match the pinned runtime "
            f"(tree-sitter {TREE_SITTER_VERSION}, tree-sitter-cpp {TREE_SITTER_CPP_VERSION})",
            code="mutation_parser_unavailable",
        )
    return {
        "locator_version": MUTATION_LOCATOR_VERSION,
        "tree_sitter_version": tree_sitter_version,
        "tree_sitter_cpp_version": tree_sitter_cpp_version,
    }


@lru_cache(maxsize=1)
def _cpp_language():
    mutation_parser_identity()
    try:
        from tree_sitter import Language
        import tree_sitter_cpp

        return Language(tree_sitter_cpp.language())
    except (ImportError, TypeError, ValueError, RuntimeError) as exc:
        raise ProbHubError(
            f"cannot initialize the C++ mutation parser: {exc}",
            code="mutation_parser_unavailable",
        ) from exc


def _parse_cpp(source_bytes):
    try:
        from tree_sitter import Parser

        tree = Parser(_cpp_language()).parse(source_bytes)
    except ProbHubError:
        raise
    except (ImportError, TypeError, ValueError, RuntimeError) as exc:
        raise ProbHubError(
            f"C++ mutation parser failed: {exc}",
            code="mutation_parser_failed",
        ) from exc
    if tree is None or tree.root_node is None:
        raise ProbHubError(
            "C++ mutation parser returned no syntax tree",
            code="mutation_parser_failed",
        )
    if tree.root_node.has_error:
        error = _first_syntax_error(tree.root_node)
        point = error.start_point if error is not None else tree.root_node.start_point
        raise ProbHubError(
            "accepted C++ source contains syntax the mutation parser cannot locate safely "
            f"at line {point.row + 1}, byte column {point.column + 1}",
            code="mutation_syntax_invalid",
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
        if function is not None and offsets.span(function).text in _BLOCKED_KEYWORD_CALLS:
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


def locate_cpp_mutation_syntax(source):
    """Return conservative AST-backed locations for the existing operators."""
    if not isinstance(source, str):
        raise ProbHubError("C++ source must be text", code="mutation_source_invalid")
    try:
        source_bytes = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProbHubError(
            "C++ source is not valid UTF-8 text",
            code="mutation_source_invalid",
        ) from exc

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
                comparisons.append(CppComparisonLocation(
                    offsets.span(operator),
                    offsets.span(left),
                    offsets.span(right),
                ))
        elif node.type == "unary_expression" and _eligible_expression(node, ancestors, offsets):
            operator = node.child_by_field_name("operator")
            if operator is not None:
                operator_span = offsets.span(operator)
                if operator_span.text == "!":
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
            conditions.append(CppConditionLocation(keyword, offsets.span(condition)))

    return CppSyntaxLocations(
        tuple(comparisons),
        tuple(unary_not),
        tuple(conditions),
    )
