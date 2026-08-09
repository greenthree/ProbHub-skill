import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_python_dependencies",
    ROOT / "scripts/audit_python_dependencies.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DependencyAuditTests(unittest.TestCase):
    def audit_result(self, *, returncode=0, reason="completed", stdout=None, stderr="", message=None):
        if stdout is None:
            stdout = json.dumps({"dependencies": []})
        return {
            "returncode": returncode,
            "reason": reason,
            "stdout": stdout,
            "stderr": stderr,
            "message": message,
        }

    def write_exceptions(self, root, entries):
        path = Path(root) / "exceptions.json"
        path.write_text(
            json.dumps({"schema_version": 1, "exceptions": entries}),
            encoding="utf-8",
        )
        return path

    def valid_entry(self, **changes):
        entry = {
            "id": "PYSEC-2099-1",
            "package": "example",
            "reason": "Upgrade is blocked by an upstream compatibility issue.",
            "expires": "2099-12-31",
            "tracking_url": "https://example.invalid/issues/1",
        }
        entry.update(changes)
        return entry

    def test_empty_checked_in_exception_file_is_valid(self):
        self.assertEqual(MODULE.load_exceptions(MODULE.DEFAULT_EXCEPTIONS), [])

    def test_exception_requires_reason_expiry_and_tracking_url(self):
        with tempfile.TemporaryDirectory() as temp:
            for changes in (
                {"reason": ""},
                {"expires": "not-a-date"},
                {"tracking_url": "issue-1"},
            ):
                with self.subTest(changes=changes):
                    path = self.write_exceptions(temp, [self.valid_entry(**changes)])
                    with self.assertRaises(MODULE.DependencyAuditError):
                        MODULE.load_exceptions(path, today=date(2099, 1, 1))

    def test_expired_and_duplicate_exceptions_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            expired = self.write_exceptions(
                temp,
                [self.valid_entry(expires="2020-01-01")],
            )
            with self.assertRaisesRegex(MODULE.DependencyAuditError, "expired"):
                MODULE.load_exceptions(expired, today=date(2020, 1, 2))

            duplicate = self.write_exceptions(
                temp,
                [self.valid_entry(), self.valid_entry(package="other")],
            )
            with self.assertRaisesRegex(MODULE.DependencyAuditError, "duplicate"):
                MODULE.load_exceptions(duplicate, today=date(2099, 1, 1))

    def test_audit_command_does_not_hide_vulnerabilities_from_evaluation(self):
        command = MODULE.audit_command(ROOT / "requirements.txt")
        self.assertNotIn("--ignore-vuln", command)
        self.assertIn("--strict", command)
        self.assertIn("--no-deps", command)
        self.assertIn("--disable-pip", command)

    def test_requirements_pin_the_complete_runtime_closure(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        packages = {
            line.split("==", 1)[0].casefold()
            for line in requirements
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(
            packages,
            {
                "blinker",
                "click",
                "colorama",
                "flask",
                "itsdangerous",
                "jinja2",
                "markupsafe",
                "pypdf",
                "pyyaml",
                "tree-sitter",
                "tree-sitter-cpp",
                "typing_extensions",
                "werkzeug",
            },
        )
        self.assertTrue(all("==" in line for line in requirements if line.strip()))
        self.assertIn(
            'typing_extensions==4.16.0; python_version < "3.11"',
            requirements,
        )

    def test_exception_must_match_vulnerability_and_package(self):
        payload = {
            "dependencies": [{
                "name": "Example",
                "version": "1.0",
                "vulns": [{
                    "id": "PYSEC-2099-1",
                    "aliases": ["GHSA-test-0000-0000"],
                    "fix_versions": ["2.0"],
                }],
            }],
        }
        self.assertEqual(
            MODULE.evaluate_audit(payload, [self.valid_entry()]),
            (1, 1),
        )
        with self.assertRaisesRegex(MODULE.DependencyAuditError, "unhandled"):
            MODULE.evaluate_audit(payload, [self.valid_entry(package="other")])

    def test_stale_exception_fails_closed(self):
        payload = {"dependencies": [{"name": "example", "version": "2.0", "vulns": []}]}
        with self.assertRaisesRegex(MODULE.DependencyAuditError, "no longer matches"):
            MODULE.evaluate_audit(payload, [self.valid_entry()])

    def test_run_audit_accepts_clean_result(self):
        with patch.object(MODULE, "run_bounded", return_value=self.audit_result()):
            result = MODULE.run_audit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["dependencies"], 0)
        self.assertEqual(result["vulnerabilities"], 0)

    def test_run_audit_fails_closed_on_process_failures(self):
        results = (
            self.audit_result(returncode=None, reason="time_limit", stdout="", message="timed out"),
            self.audit_result(returncode=None, reason="output_limit", stdout="", message="too much output"),
            self.audit_result(returncode=2, stdout="", stderr="tool failure"),
        )
        for result in results:
            with self.subTest(result=result):
                with (
                    patch.object(MODULE, "run_bounded", return_value=result),
                    self.assertRaisesRegex(MODULE.DependencyAuditError, "audit failed"),
                ):
                    MODULE.run_audit()

    def test_run_audit_reports_network_failure_with_invalid_json(self):
        result = self.audit_result(
            returncode=1,
            stdout="",
            stderr="advisory service unavailable",
        )
        with (
            patch.object(MODULE, "run_bounded", return_value=result),
            self.assertRaisesRegex(MODULE.DependencyAuditError, "advisory service unavailable"),
        ):
            MODULE.run_audit()

    def test_run_audit_rejects_malformed_success_json(self):
        result = self.audit_result(stdout="not-json")
        with (
            patch.object(MODULE, "run_bounded", return_value=result),
            self.assertRaisesRegex(MODULE.DependencyAuditError, "invalid JSON"),
        ):
            MODULE.run_audit()

    def test_run_audit_rejects_unhandled_vulnerability(self):
        payload = {
            "dependencies": [{
                "name": "example",
                "version": "1.0",
                "vulns": [{
                    "id": "PYSEC-2099-1",
                    "aliases": [],
                    "fix_versions": ["2.0"],
                }],
            }],
        }
        result = self.audit_result(returncode=1, stdout=json.dumps(payload))
        with (
            patch.object(MODULE, "run_bounded", return_value=result),
            self.assertRaisesRegex(MODULE.DependencyAuditError, "unhandled"),
        ):
            MODULE.run_audit()

    def test_run_audit_accepts_exit_one_for_reviewed_vulnerability(self):
        payload = {
            "dependencies": [{
                "name": "example",
                "version": "1.0",
                "vulns": [{
                    "id": "PYSEC-2099-1",
                    "aliases": [],
                    "fix_versions": [],
                }],
            }],
        }
        result = self.audit_result(returncode=1, stdout=json.dumps(payload))
        with tempfile.TemporaryDirectory() as temp:
            exceptions = self.write_exceptions(temp, [self.valid_entry()])
            with patch.object(MODULE, "run_bounded", return_value=result):
                audited = MODULE.run_audit(exceptions_path=exceptions)
        self.assertTrue(audited["ok"])
        self.assertEqual(audited["vulnerabilities"], 1)

    def test_run_audit_rejects_exit_one_without_reported_vulnerability(self):
        result = self.audit_result(returncode=1)
        with (
            patch.object(MODULE, "run_bounded", return_value=result),
            self.assertRaisesRegex(MODULE.DependencyAuditError, "reported no vulnerabilities"),
        ):
            MODULE.run_audit()


if __name__ == "__main__":
    unittest.main()
