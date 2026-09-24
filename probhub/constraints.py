"""Fail-closed validation for the opt-in constraints schema.

This module only validates and canonicalizes the future problem Schema v2
block. Execution paths do not consume it yet; Schema v1 problems containing
the block are rejected by lint so a partially wired single source cannot be
used accidentally.
"""

from __future__ import annotations

import hashlib
import json
import re


CONSTRAINTS_SCHEMA_VERSION = 1
CONSTRAINTS_PROBLEM_SCHEMA_VERSION = 2
MAX_CONSTRAINT_VALUES = 256
MAX_CONSTRAINT_KEY_BYTES = 64
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

_KEY_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_CPP_KEYWORDS = frozenset({
    "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand", "bitor",
    "bool", "break", "case", "catch", "char", "char8_t", "char16_t", "char32_t",
    "class", "compl", "concept", "const", "consteval", "constexpr", "constinit",
    "const_cast", "continue", "co_await", "co_return", "co_yield", "decltype",
    "default", "delete", "do", "double", "dynamic_cast", "else", "enum", "explicit",
    "export", "extern", "false", "float", "for", "friend", "goto", "if", "inline",
    "int", "long", "mutable", "namespace", "new", "noexcept", "not", "not_eq",
    "nullptr", "operator", "or", "or_eq", "private", "protected", "public", "register",
    "reinterpret_cast", "requires", "return", "short", "signed", "sizeof", "static",
    "static_assert", "static_cast", "struct", "switch", "template", "this", "thread_local",
    "throw", "true", "try", "typedef", "typeid", "typename", "union", "unsigned", "using",
    "virtual", "void", "volatile", "wchar_t", "while", "xor", "xor_eq",
})


def _diagnostic(code, message, path, **details):
    return {"code": code, "severity": "error", "message": message, "path": path, **details}


def _invalid(report, code, message, path, **details):
    report["diagnostics"].append(_diagnostic(code, message, path, **details))
    report["valid"] = False


def _display_key(value):
    """Return a JSON-safe diagnostic spelling for an arbitrary YAML key."""
    if not isinstance(value, str):
        return repr(value)
    return value.encode("utf-8", "backslashreplace").decode("ascii")


def _canonical_values(values):
    payload = {
        "protocol": "constraints-v1",
        "values": [[key, values[key]] for key in sorted(values)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inspect_constraints(config, *, problem_schema_version=1):
    """Validate and canonicalize a constraints block without side effects."""
    configured = isinstance(config, dict) and "constraints" in config
    report = {
        "configured": configured,
        "enabled": False,
        "schema_version": None,
        "values": {},
        "fingerprint": None,
        "diagnostics": [],
        "valid": True,
    }
    if not configured:
        return report

    if problem_schema_version != CONSTRAINTS_PROBLEM_SCHEMA_VERSION:
        _invalid(
            report,
            "constraints_requires_problem_schema_v2",
            "constraints requires problem schema_version 2; execution remains fail-closed",
            "constraints",
            expected_schema=CONSTRAINTS_PROBLEM_SCHEMA_VERSION,
            actual_schema=problem_schema_version,
        )

    value = config.get("constraints")
    if not isinstance(value, dict):
        _invalid(report, "constraints_type", "constraints must be a mapping", "constraints")
        return report

    unknown = sorted(set(value) - {"version", "values"}, key=str)
    for key in unknown:
        _invalid(report, "constraints_unknown_field", f"unknown constraints field: {key}", f"constraints.{key}")

    version = value.get("version")
    report["schema_version"] = version
    if type(version) is not int or version != CONSTRAINTS_SCHEMA_VERSION:
        _invalid(
            report,
            "constraints_unsupported_version",
            f"constraints.version must be {CONSTRAINTS_SCHEMA_VERSION}",
            "constraints.version",
            actual=version,
        )

    values = value.get("values")
    if not isinstance(values, dict):
        _invalid(report, "constraints_values_type", "constraints.values must be a mapping", "constraints.values")
        return report
    if not values:
        _invalid(report, "constraints_values_empty", "constraints.values must not be empty", "constraints.values")
    if len(values) > MAX_CONSTRAINT_VALUES:
        _invalid(
            report,
            "constraints_values_limit",
            f"constraints.values must contain at most {MAX_CONSTRAINT_VALUES} entries",
            "constraints.values",
        )

    normalized = {}
    for key, raw_value in values.items():
        path = f"constraints.values.{_display_key(key)}"
        if not isinstance(key, str) or not key or not key.isascii():
            _invalid(report, "constraints_key_invalid", "constraint key must be ASCII lower snake case", path)
            continue
        if len(key) > MAX_CONSTRAINT_KEY_BYTES or _KEY_RE.fullmatch(key) is None:
            _invalid(report, "constraints_key_invalid", "constraint key must be ASCII lower snake case", path)
            continue
        if key in _CPP_KEYWORDS:
            _invalid(report, "constraints_key_reserved", f"constraint key is a C++ keyword: {key}", path)
            continue
        if type(raw_value) is not int or not INT64_MIN <= raw_value <= INT64_MAX:
            _invalid(
                report,
                "constraints_value_invalid",
                "constraint value must be a signed 64-bit integer",
                path,
                value=raw_value,
            )
            continue
        normalized[key] = raw_value

    report["values"] = dict(sorted(normalized.items()))
    if report["valid"] and version == CONSTRAINTS_SCHEMA_VERSION:
        report["enabled"] = problem_schema_version == CONSTRAINTS_PROBLEM_SCHEMA_VERSION
        report["fingerprint"] = _canonical_values(report["values"])
    return report
