"""Strict C++ mutation locations through an isolated native parser worker."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from .errors import ProbHubError
from .process_control import OutputBudgetError, ProcessCancelled, run_managed_to_files


MUTATION_LOCATOR_VERSION = "tree-sitter-cpp-v1"
TREE_SITTER_VERSION = "0.26.0"
TREE_SITTER_CPP_VERSION = "0.23.4"
MUTATION_PARSER_PROTOCOL_VERSION = 1
MUTATION_PARSER_OPERATION = "locate-cpp-v1"
MUTATION_PARSER_TIMEOUT_SECONDS = 30.0
MUTATION_PARSER_MEMORY_LIMIT_MB = 512
MUTATION_PARSER_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
MUTATION_PARSER_PROCESS_LIMIT = 8
MUTATION_PARSER_REQUEST_LIMIT_BYTES = 8 * 1024 * 1024
MUTATION_PARSER_RESPONSE_LIMIT_BYTES = 4 * 1024 * 1024
MUTATION_PARSER_SOURCE_LIMIT_BYTES = 8 * 1024 * 1024
MUTATION_PARSER_MAX_LOCATION_ENTRIES = 65536
MUTATION_PARSER_DIAGNOSTIC_BYTES = 4096
_WORKER_ERROR_CODES = frozenset({
    "mutation_source_invalid",
    "mutation_syntax_invalid",
    "mutation_parser_unavailable",
    "mutation_parser_failed",
    "mutation_parser_memory_limit",
    "mutation_parser_protocol_error",
    "mutation_parser_input_changed",
    "mutation_location_invalid",
})
_WORKER_BINARY_KINDS = frozenset({
    "<", "<=", ">", ">=", "==", "!=", "<=>", "+", "-", "*", "/", "%",
    "&&", "||", "&", "|", "^", "<<", ">>", "+=", "-=", "*=", "/=", "%=",
    "&=", "|=", "^=", "<<=", ">>=", ",",
})
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_WORKER_BOOTSTRAP = (
    "import runpy,sys; "
    "package_root,request_path=sys.argv[1:3]; "
    "sys.path.insert(0, package_root); "
    "sys.argv=['probhub.mutation_parser_worker', request_path]; "
    "runpy.run_module('probhub.mutation_parser_worker', run_name='__main__')"
)


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


def mutation_parser_identity():
    """Return the pinned parser identity without importing native bindings."""
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


def _bounded(value, limit=MUTATION_PARSER_DIAGNOSTIC_BYTES):
    return str(value or "").encode("utf-8", errors="replace")[:limit].decode(
        "utf-8", errors="ignore"
    )


def _worker_command(request_path):
    return [
        sys.executable,
        "-c",
        _WORKER_BOOTSTRAP,
        str(_PACKAGE_ROOT),
        str(request_path),
    ]


def _worker_environment():
    env = os.environ.copy()
    for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(variable, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _regular_file_size(path, *, label):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProbHubError(
            f"{label} is unavailable: {path}: {exc}",
            code="mutation_parser_protocol_error",
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    ):
        raise ProbHubError(f"{label} must not be a link", code="mutation_parser_protocol_error")
    if not stat.S_ISREG(info.st_mode):
        raise ProbHubError(f"{label} is not a regular file", code="mutation_parser_protocol_error")
    return int(info.st_size)


def _read_output(path):
    size = _regular_file_size(path, label="mutation parser response")
    if size <= 0 or size > MUTATION_PARSER_RESPONSE_LIMIT_BYTES:
        raise ProbHubError(
            "mutation parser response is missing, empty, or too large",
            code="mutation_parser_protocol_error",
        )
    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbHubError(
            "mutation parser returned invalid JSON",
            code="mutation_parser_protocol_error",
        ) from exc
    if not isinstance(payload, dict):
        raise ProbHubError(
            "mutation parser returned a non-object response",
            code="mutation_parser_protocol_error",
        )
    return payload


def _validate_error(payload):
    if set(payload) != {"protocol_version", "ok", "error"}:
        raise ProbHubError(
            "mutation parser error response has an invalid schema",
            code="mutation_parser_protocol_error",
        )
    if (
        isinstance(payload.get("protocol_version"), bool)
        or payload.get("protocol_version") != MUTATION_PARSER_PROTOCOL_VERSION
        or payload.get("ok") is not False
    ):
        raise ProbHubError(
            "mutation parser error response has an invalid protocol version",
            code="mutation_parser_protocol_error",
        )
    error = payload.get("error")
    if (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error["code"], str)
        or not isinstance(error["message"], str)
        or error["code"] not in _WORKER_ERROR_CODES
        or len(error["code"].encode("utf-8", errors="replace")) > 128
        or len(error["message"].encode("utf-8", errors="replace")) > MUTATION_PARSER_DIAGNOSTIC_BYTES
    ):
        raise ProbHubError(
            "mutation parser error response has an invalid error object",
            code="mutation_parser_protocol_error",
        )
    raise ProbHubError(error["message"], code=error["code"])


def _validate_span(payload, source):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"start", "end", "text", "kind"}
        or isinstance(payload["start"], bool)
        or not isinstance(payload["start"], int)
        or isinstance(payload["end"], bool)
        or not isinstance(payload["end"], int)
        or not isinstance(payload["text"], str)
        or not isinstance(payload["kind"], str)
        or not payload["kind"]
    ):
        raise ProbHubError("mutation parser returned an invalid span", code="mutation_parser_protocol_error")
    start = payload["start"]
    end = payload["end"]
    if not 0 <= start <= end <= len(source) or source[start:end] != payload["text"]:
        raise ProbHubError("mutation parser returned an out-of-range span", code="mutation_parser_protocol_error")
    if len(payload["kind"].encode("utf-8", errors="replace")) > 128:
        raise ProbHubError("mutation parser returned an oversized span kind", code="mutation_parser_protocol_error")
    return CppSyntaxSpan(start, end, payload["text"], payload["kind"])


def _validate_locations(payload, source, *, control_check=None):
    if not isinstance(payload, dict) or set(payload) != {"comparisons", "unary_not", "conditions"}:
        raise ProbHubError("mutation parser returned invalid locations", code="mutation_parser_protocol_error")
    comparisons = payload["comparisons"]
    unary_not = payload["unary_not"]
    conditions = payload["conditions"]
    if any(
        not isinstance(items, list) or len(items) > MUTATION_PARSER_MAX_LOCATION_ENTRIES
        for items in (comparisons, unary_not, conditions)
    ):
        raise ProbHubError("mutation parser returned too many locations", code="mutation_parser_protocol_error")
    normalized_comparisons = []
    seen_comparisons = set()
    for index, item in enumerate(comparisons):
        if control_check is not None and index % 128 == 0:
            control_check()
        if not isinstance(item, dict) or set(item) != {"operator", "left", "right"}:
            raise ProbHubError("mutation parser returned an invalid comparison", code="mutation_parser_protocol_error")
        operator = _validate_span(item["operator"], source)
        left = _validate_span(item["left"], source)
        right = _validate_span(item["right"], source)
        if operator.kind != operator.text or operator.text not in _WORKER_BINARY_KINDS:
            raise ProbHubError("mutation parser returned an invalid binary operator", code="mutation_parser_protocol_error")
        if not left.end <= operator.start <= operator.end <= right.start:
            raise ProbHubError("mutation parser returned invalid comparison layout", code="mutation_parser_protocol_error")
        identity = (
            operator.start, operator.end, operator.text,
            left.start, left.end, right.start, right.end,
        )
        if identity in seen_comparisons:
            raise ProbHubError("mutation parser returned duplicate comparisons", code="mutation_parser_protocol_error")
        seen_comparisons.add(identity)
        normalized_comparisons.append(CppComparisonLocation(operator, left, right))
    normalized_comparisons.sort(key=lambda item: (item.operator.start, item.operator.end, item.left.start, item.right.start))
    normalized_unary = []
    seen_unary = set()
    for index, item in enumerate(unary_not):
        if control_check is not None and index % 128 == 0:
            control_check()
        span = _validate_span(item, source)
        if span.text != "!" or span.kind != "!":
            raise ProbHubError("mutation parser returned an invalid unary negation", code="mutation_parser_protocol_error")
        identity = (span.start, span.end)
        if identity in seen_unary:
            raise ProbHubError("mutation parser returned duplicate unary negations", code="mutation_parser_protocol_error")
        seen_unary.add(identity)
        normalized_unary.append(span)
    normalized_unary.sort(key=lambda item: (item.start, item.end))
    normalized_conditions = []
    seen_conditions = set()
    for index, item in enumerate(conditions):
        if control_check is not None and index % 128 == 0:
            control_check()
        if not isinstance(item, dict) or set(item) != {"keyword", "condition"}:
            raise ProbHubError("mutation parser returned an invalid condition", code="mutation_parser_protocol_error")
        if item["keyword"] not in {"if", "while"}:
            raise ProbHubError("mutation parser returned an invalid condition keyword", code="mutation_parser_protocol_error")
        condition = _validate_span(item["condition"], source)
        if condition.kind not in {"condition_clause", "parenthesized_expression"}:
            raise ProbHubError("mutation parser returned an invalid condition span", code="mutation_parser_protocol_error")
        if not condition.text.startswith("(") or not condition.text.endswith(")"):
            raise ProbHubError("mutation parser returned an unparenthesized condition", code="mutation_parser_protocol_error")
        identity = (item["keyword"], condition.start, condition.end)
        if identity in seen_conditions:
            raise ProbHubError("mutation parser returned duplicate conditions", code="mutation_parser_protocol_error")
        seen_conditions.add(identity)
        normalized_conditions.append(CppConditionLocation(
            item["keyword"], condition
        ))
    normalized_conditions.sort(key=lambda item: (item.condition.start, item.condition.end, item.keyword))
    if control_check is not None:
        control_check()
    return CppSyntaxLocations(
        tuple(normalized_comparisons),
        normalized_unary,
        tuple(normalized_conditions),
    )


def _worker_diagnostic(stdout_path, stderr_path):
    parts = []
    for path in (stderr_path, stdout_path):
        try:
            raw = Path(path).read_bytes()[:MUTATION_PARSER_DIAGNOSTIC_BYTES]
        except OSError:
            continue
        if raw:
            parts.append(raw.decode("utf-8", errors="replace").strip())
    return _bounded("\n".join(part for part in parts if part))


def _raise_execution_failure(result, stdout_path, stderr_path):
    reason = result.get("reason")
    mapped = {
        "time_limit": ("mutation_parser_timeout", "mutation parser timed out"),
        "memory_limit": ("mutation_parser_memory_limit", "mutation parser exceeded its memory limit"),
        "output_limit": ("mutation_parser_output_limit", "mutation parser exceeded its output limit"),
        "process_limit": ("mutation_parser_process_limit", "mutation parser exceeded its process limit"),
        "cancelled": ("mutation_parser_cancelled", "mutation parser was cancelled"),
    }
    if reason in mapped:
        code, message = mapped[reason]
        raise ProbHubError(message, code=code)
    detail = _worker_diagnostic(stdout_path, stderr_path)
    message = "mutation parser process failed"
    if detail:
        message += ": " + detail
    raise ProbHubError(message, code="mutation_parser_crashed")


def _parser_control_check(cancel_check, deadline):
    if cancel_check is not None:
        try:
            requested = bool(cancel_check())
        except ProbHubError:
            raise
        except Exception as exc:
            raise ProbHubError(
                f"mutation parser cancellation check failed: {exc}",
                code="mutation_parser_cancel_check_failed",
            ) from exc
        if requested:
            raise ProbHubError("mutation parser was cancelled", code="mutation_parser_cancelled")
    if deadline is not None and time.monotonic() >= float(deadline):
        raise ProbHubError("mutation parser deadline exceeded", code="mutation_parser_timeout")


def _run_parser_worker(source, *, timeout=None, cancel_check=None, deadline=None):
    if not isinstance(source, str):
        raise ProbHubError("C++ source must be text", code="mutation_source_invalid")
    try:
        source_bytes = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProbHubError("C++ source is not valid UTF-8 text", code="mutation_source_invalid") from exc
    if len(source_bytes) > MUTATION_PARSER_SOURCE_LIMIT_BYTES:
        raise ProbHubError("C++ source is too large for mutation parsing", code="mutation_parser_protocol_error")
    mutation_parser_identity()
    try:
        timeout = MUTATION_PARSER_TIMEOUT_SECONDS if timeout is None else float(timeout)
    except (TypeError, ValueError) as exc:
        raise ProbHubError("mutation parser timeout must be positive", code="mutation_parser_timeout_invalid") from exc
    if timeout <= 0:
        raise ProbHubError("mutation parser timeout must be positive", code="mutation_parser_timeout_invalid")
    managed_cancel_check = None
    if cancel_check is not None or deadline is not None:
        def managed_cancel_check():
            _parser_control_check(cancel_check, deadline)
            return False
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-mutation-parser-") as temp:
            temp_dir = Path(temp)
            source_path = temp_dir / "source.cpp"
            request_path = temp_dir / "request.json"
            stdout_path = temp_dir / "stdout"
            stderr_path = temp_dir / "stderr"
            source_path.write_bytes(source_bytes)
            request = {
                "protocol_version": MUTATION_PARSER_PROTOCOL_VERSION,
                "operation": MUTATION_PARSER_OPERATION,
                "source_path": str(source_path),
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            }
            encoded_request = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded_request) > MUTATION_PARSER_REQUEST_LIMIT_BYTES:
                raise ProbHubError("mutation parser request is too large", code="mutation_parser_protocol_error")
            request_path.write_bytes(encoded_request)
            try:
                result = run_managed_to_files(
                    _worker_command(request_path),
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout=timeout,
                    memory_limit_mb=MUTATION_PARSER_MEMORY_LIMIT_MB,
                    output_limit_bytes=MUTATION_PARSER_OUTPUT_LIMIT_BYTES,
                    process_limit=MUTATION_PARSER_PROCESS_LIMIT,
                    cwd=temp_dir,
                    env=_worker_environment(),
                    cancel_check=managed_cancel_check,
                )
            except ProcessCancelled as exc:
                raise ProbHubError("mutation parser was cancelled", code="mutation_parser_cancelled") from exc
            except OutputBudgetError as exc:
                raise ProbHubError(str(exc), code="mutation_parser_output_limit") from exc
            except (OSError, ValueError) as exc:
                raise ProbHubError(f"mutation parser could not start: {exc}", code="mutation_parser_start_failed") from exc

            if result.get("reason") != "completed":
                _raise_execution_failure(result, stdout_path, stderr_path)
            if result.get("returncode") != 0:
                # A valid structured parser error takes precedence over the
                # native process exit code. Crashes and malformed output remain distinct.
                try:
                    payload = _read_output(stdout_path)
                except ProbHubError:
                    payload = None
                if isinstance(payload, dict) and payload.get("ok") is False:
                    _validate_error(payload)
                _raise_execution_failure(result, stdout_path, stderr_path)

            _parser_control_check(cancel_check, deadline)
            payload = _read_output(stdout_path)
            if payload.get("ok") is not True:
                _validate_error(payload)
            if set(payload) != {
                "protocol_version", "ok", "operation", "locator_version",
                "tree_sitter_version", "tree_sitter_cpp_version", "source_sha256", "locations",
            }:
                raise ProbHubError("mutation parser response has an invalid schema", code="mutation_parser_protocol_error")
            identity = mutation_parser_identity()
            if (
                isinstance(payload["protocol_version"], bool)
                or payload["protocol_version"] != MUTATION_PARSER_PROTOCOL_VERSION
                or payload["operation"] != MUTATION_PARSER_OPERATION
                or payload["locator_version"] != identity["locator_version"]
                or payload["tree_sitter_version"] != identity["tree_sitter_version"]
                or payload["tree_sitter_cpp_version"] != identity["tree_sitter_cpp_version"]
                or payload["source_sha256"] != hashlib.sha256(source_bytes).hexdigest()
            ):
                raise ProbHubError("mutation parser response identity mismatch", code="mutation_parser_protocol_error")
            return _validate_locations(
                payload["locations"], source,
                control_check=lambda: _parser_control_check(cancel_check, deadline),
            )
    except ProbHubError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ProbHubError(
            f"mutation parser could not prepare its temporary request: {exc}",
            code="mutation_parser_start_failed",
        ) from exc


def locate_cpp_mutation_syntax(source, *, timeout=None, cancel_check=None, deadline=None):
    """Return conservative AST-backed locations from the isolated worker."""
    return _run_parser_worker(source, timeout=timeout, cancel_check=cancel_check, deadline=deadline)
