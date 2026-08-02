"""Audit the pinned Python dependency closure with explicit exceptions only."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_release import run_bounded


DEFAULT_REQUIREMENTS = ROOT / "requirements.txt"
DEFAULT_EXCEPTIONS = ROOT / "security/python-audit-exceptions.json"
AUDIT_TIMEOUT_SECONDS = 180
AUDIT_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024


class DependencyAuditError(RuntimeError):
    pass


def load_exceptions(path, *, today=None):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyAuditError(f"cannot read dependency audit exceptions: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DependencyAuditError("dependency audit exceptions must use schema_version 1")
    entries = payload.get("exceptions")
    if not isinstance(entries, list):
        raise DependencyAuditError("dependency audit exceptions must be a list")

    today = today or date.today()
    identifiers = set()
    validated = []
    for index, entry in enumerate(entries):
        label = f"dependency audit exception {index + 1}"
        if not isinstance(entry, dict):
            raise DependencyAuditError(f"{label} must be an object")
        required = ("id", "package", "reason", "expires", "tracking_url")
        missing = [name for name in required if not isinstance(entry.get(name), str) or not entry[name].strip()]
        if missing:
            raise DependencyAuditError(f"{label} is missing: {', '.join(missing)}")
        identifier = entry["id"].strip()
        if identifier in identifiers:
            raise DependencyAuditError(f"duplicate dependency audit exception: {identifier}")
        identifiers.add(identifier)
        try:
            expires = date.fromisoformat(entry["expires"].strip())
        except ValueError as exc:
            raise DependencyAuditError(f"{label} has an invalid expires date") from exc
        if expires < today:
            raise DependencyAuditError(f"dependency audit exception expired: {identifier}")
        tracking_url = entry["tracking_url"].strip()
        if not tracking_url.startswith(("https://", "http://")):
            raise DependencyAuditError(f"{label} tracking_url must be HTTP(S)")
        validated.append({**entry, "id": identifier})
    return validated


def audit_command(requirements):
    return [
        sys.executable,
        "-m",
        "pip_audit",
        "--requirement",
        str(Path(requirements)),
        "--no-deps",
        "--disable-pip",
        "--strict",
        "--format",
        "json",
        "--desc",
        "off",
        "--aliases",
        "on",
        "--progress-spinner",
        "off",
        "--timeout",
        "30",
    ]


def evaluate_audit(payload, exceptions):
    dependencies = payload.get("dependencies") if isinstance(payload, dict) else None
    if not isinstance(dependencies, list):
        raise DependencyAuditError("Python dependency audit returned an unexpected schema")
    matched = set()
    unhandled = []
    vulnerability_count = 0
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
            raise DependencyAuditError("Python dependency audit returned an invalid dependency")
        package = dependency["name"].casefold()
        vulnerabilities = dependency.get("vulns")
        if not isinstance(vulnerabilities, list):
            raise DependencyAuditError("Python dependency audit returned invalid vulnerabilities")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not isinstance(vulnerability.get("id"), str):
                raise DependencyAuditError("Python dependency audit returned an invalid vulnerability")
            vulnerability_count += 1
            identifiers = {vulnerability["id"], *(vulnerability.get("aliases") or [])}
            accepted = None
            for entry in exceptions:
                if entry["id"] in identifiers and entry["package"].casefold() == package:
                    accepted = entry
                    break
            if accepted is None:
                fixes = ", ".join(vulnerability.get("fix_versions") or []) or "none published"
                unhandled.append(f"{dependency['name']} {vulnerability['id']} (fix: {fixes})")
            else:
                matched.add(accepted["id"])
    stale = sorted(entry["id"] for entry in exceptions if entry["id"] not in matched)
    if unhandled:
        raise DependencyAuditError(
            "unhandled Python dependency vulnerabilities: " + "; ".join(unhandled)
        )
    if stale:
        raise DependencyAuditError(
            "dependency audit exception no longer matches an applicable vulnerability: "
            + ", ".join(stale)
        )
    return len(dependencies), vulnerability_count


def run_audit(requirements=DEFAULT_REQUIREMENTS, exceptions_path=DEFAULT_EXCEPTIONS):
    requirements = Path(requirements)
    if not requirements.is_file():
        raise DependencyAuditError(f"requirements file is missing: {requirements}")
    exceptions = load_exceptions(exceptions_path)
    result = run_bounded(
        audit_command(requirements),
        cwd=ROOT,
        timeout=AUDIT_TIMEOUT_SECONDS,
        output_limit=AUDIT_OUTPUT_LIMIT_BYTES,
    )
    if result["reason"] != "completed" or result["returncode"] not in {0, 1}:
        detail = (result["stdout"] + "\n" + result["stderr"]).strip()[-12000:]
        raise DependencyAuditError(
            "Python dependency audit failed "
            f"({result['returncode']} / {result['reason']}): {detail or result.get('message') or 'no diagnostics'}"
        )
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        if result["returncode"]:
            detail = (result["stdout"] + "\n" + result["stderr"]).strip()[-12000:]
            raise DependencyAuditError(
                "Python dependency audit failed "
                f"({result['returncode']} / completed): {detail or 'invalid or empty JSON output'}"
            ) from exc
        raise DependencyAuditError("Python dependency audit returned invalid JSON") from exc
    dependencies, vulnerability_count = evaluate_audit(payload, exceptions)
    if result["returncode"] == 1 and vulnerability_count == 0:
        raise DependencyAuditError(
            "Python dependency audit exited with status 1 but reported no vulnerabilities"
        )
    return {
        "ok": True,
        "requirements": str(requirements),
        "exceptions": [entry["id"] for entry in exceptions],
        "dependencies": dependencies,
        "vulnerabilities": vulnerability_count,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    args = parser.parse_args(argv)
    try:
        payload = run_audit(args.requirements, args.exceptions)
    except DependencyAuditError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
