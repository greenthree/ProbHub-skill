"""Schema validation for problem-level mutation exclusions."""

from __future__ import annotations

import re

from .errors import ProbHubError


MUTATION_CONFIG_SCHEMA_VERSION = 1
MUTATION_OPERATOR_VERSION = "cpp-token-v1"
MUTATION_OPERATORS = (
    "comparison-boundary",
    "boolean-negation",
    "integer-boundary",
)
MAX_MUTATION_EXCLUSIONS = 256
MAX_MUTATION_EXCLUSION_ID_BYTES = 256
MAX_MUTATION_EXCLUSION_REASON_BYTES = 1024

_MUTATION_CONFIG_FIELDS = frozenset({"schema_version", "exclusions"})
_MUTATION_EXCLUSION_FIELDS = frozenset({"id", "reason"})
_MUTATION_ID_RE = re.compile(
    rf"^{re.escape(MUTATION_OPERATOR_VERSION)}:"
    rf"(?:{'|'.join(re.escape(item) for item in MUTATION_OPERATORS)}):"
    r"[1-9][0-9]*:[1-9][0-9]*:[0-9a-f]{16}$"
)


def _diagnostic(code, message, path):
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "path": path,
    }


def inspect_mutation_config(config):
    """Return normalized exclusions and stable diagnostics without raising."""
    configured = isinstance(config, dict) and "mutation" in config
    report = {
        "configured": configured,
        "schema_version": None,
        "exclusions": [],
        "diagnostics": [],
        "valid": True,
    }
    if not configured:
        return report

    value = config.get("mutation")
    if not isinstance(value, dict):
        report["diagnostics"].append(_diagnostic(
            "mutation_config_type",
            "mutation must be a mapping",
            "mutation",
        ))
        report["valid"] = False
        return report

    unknown_fields = sorted(set(value) - _MUTATION_CONFIG_FIELDS)
    for field in unknown_fields:
        report["diagnostics"].append(_diagnostic(
            "mutation_unknown_field",
            f"unknown mutation field: {field}",
            f"mutation.{field}",
        ))

    schema_version = value.get("schema_version")
    report["schema_version"] = schema_version
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MUTATION_CONFIG_SCHEMA_VERSION
    ):
        report["diagnostics"].append(_diagnostic(
            "mutation_schema_version",
            f"mutation.schema_version must be {MUTATION_CONFIG_SCHEMA_VERSION}",
            "mutation.schema_version",
        ))

    exclusions = value.get("exclusions", [])
    if not isinstance(exclusions, list):
        report["diagnostics"].append(_diagnostic(
            "mutation_exclusions_type",
            "mutation.exclusions must be a list",
            "mutation.exclusions",
        ))
        report["valid"] = False
        return report
    if len(exclusions) > MAX_MUTATION_EXCLUSIONS:
        report["diagnostics"].append(_diagnostic(
            "mutation_exclusions_limit",
            f"mutation.exclusions must contain at most {MAX_MUTATION_EXCLUSIONS} entries",
            "mutation.exclusions",
        ))

    seen = {}
    for index, item in enumerate(exclusions[:MAX_MUTATION_EXCLUSIONS]):
        path = f"mutation.exclusions[{index}]"
        if not isinstance(item, dict):
            report["diagnostics"].append(_diagnostic(
                "mutation_exclusion_type",
                "mutation exclusion entries must be mappings",
                path,
            ))
            continue

        unknown_item_fields = sorted(set(item) - _MUTATION_EXCLUSION_FIELDS)
        for field in unknown_item_fields:
            report["diagnostics"].append(_diagnostic(
                "mutation_exclusion_unknown_field",
                f"unknown mutation exclusion field: {field}",
                f"{path}.{field}",
            ))

        raw_id = item.get("id")
        mutation_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not mutation_id:
            report["diagnostics"].append(_diagnostic(
                "mutation_exclusion_id_required",
                "mutation exclusion id must be a non-empty string",
                f"{path}.id",
            ))
        elif (
            len(mutation_id.encode("utf-8")) > MAX_MUTATION_EXCLUSION_ID_BYTES
            or _MUTATION_ID_RE.fullmatch(mutation_id) is None
        ):
            report["diagnostics"].append(_diagnostic(
                "mutation_exclusion_id_invalid",
                "mutation exclusion id must be a current stable mutation ID",
                f"{path}.id",
            ))

        raw_reason = item.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if not reason:
            report["diagnostics"].append(_diagnostic(
                "mutation_exclusion_reason_required",
                "mutation exclusion reason must be a non-empty string",
                f"{path}.reason",
            ))
        elif len(reason.encode("utf-8")) > MAX_MUTATION_EXCLUSION_REASON_BYTES:
            report["diagnostics"].append(_diagnostic(
                "mutation_exclusion_reason_limit",
                f"mutation exclusion reason must not exceed {MAX_MUTATION_EXCLUSION_REASON_BYTES} bytes",
                f"{path}.reason",
            ))

        if mutation_id:
            previous = seen.get(mutation_id)
            if previous is not None:
                report["diagnostics"].append(_diagnostic(
                    "mutation_exclusion_duplicate",
                    f"duplicate mutation exclusion id: {mutation_id}",
                    f"{path}.id",
                ))
            else:
                seen[mutation_id] = index

        if (
            mutation_id
            and reason
            and len(mutation_id.encode("utf-8")) <= MAX_MUTATION_EXCLUSION_ID_BYTES
            and len(reason.encode("utf-8")) <= MAX_MUTATION_EXCLUSION_REASON_BYTES
            and _MUTATION_ID_RE.fullmatch(mutation_id) is not None
            and not unknown_item_fields
            and seen.get(mutation_id) == index
        ):
            report["exclusions"].append({"id": mutation_id, "reason": reason})

    judge_type = str(((config.get("judge") or {}).get("type", "standard"))).strip().lower()
    if judge_type == "checker":
        judge_type = "custom"
    if judge_type not in {"", "standard"}:
        report["diagnostics"].append(_diagnostic(
            "mutation_standard_required",
            "mutation exclusions are only valid for judge.type: standard",
            "mutation",
        ))

    report["valid"] = not report["diagnostics"]
    return report


def load_mutation_exclusions(config):
    report = inspect_mutation_config(config)
    if not report["valid"]:
        first = report["diagnostics"][0]
        raise ProbHubError(
            f"invalid mutation configuration: [{first['code']}] {first['message']}",
            code="mutation_config_invalid",
        )
    return list(report["exclusions"])


def normalize_mutation_exclusions(exclusions):
    if exclusions is None:
        return []
    report = inspect_mutation_config({
        "judge": {"type": "standard"},
        "mutation": {
            "schema_version": MUTATION_CONFIG_SCHEMA_VERSION,
            "exclusions": exclusions,
        },
    })
    if not report["valid"]:
        first = report["diagnostics"][0]
        raise ProbHubError(first["message"], code="mutation_config_invalid")
    return list(report["exclusions"])
