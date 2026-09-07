import unittest
import tempfile
import json
from pathlib import Path

from probhub.constraints import (
    CONSTRAINTS_PROBLEM_SCHEMA_VERSION,
    INT64_MAX,
    INT64_MIN,
    inspect_constraints,
)
from probhub.io import write_yaml
from probhub.workspace import load_problem


class ConstraintsSchemaTests(unittest.TestCase):
    def test_absent_constraints_are_disabled(self):
        report = inspect_constraints({})
        self.assertEqual(report["valid"], True)
        self.assertEqual(report["enabled"], False)
        self.assertIsNone(report["fingerprint"])

    def test_schema_v1_is_fail_closed(self):
        report = inspect_constraints({
            "constraints": {"version": 1, "values": {"n_max": 10}},
        })
        self.assertFalse(report["valid"])
        self.assertIn("constraints_requires_problem_schema_v2", {
            item["code"] for item in report["diagnostics"]
        })

    def test_schema_v2_normalizes_values_and_fingerprints(self):
        config = {
            "constraints": {
                "version": 1,
                "values": {"z_max": INT64_MAX, "n_min": INT64_MIN, "zero": 0},
            },
        }
        report = inspect_constraints(config, problem_schema_version=CONSTRAINTS_PROBLEM_SCHEMA_VERSION)
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["enabled"])
        self.assertEqual(list(report["values"]), ["n_min", "z_max", "zero"])
        self.assertRegex(report["fingerprint"], r"^[0-9a-f]{64}$")

    def test_invalid_fields_values_and_reserved_names_are_stable(self):
        report = inspect_constraints({
            "constraints": {
                "version": 1,
                "extra": True,
                "values": {
                    "BadName": 1,
                    "class": 1,
                    "ok": True,
                    "too_big": INT64_MAX + 1,
                },
            },
        }, problem_schema_version=2)
        self.assertFalse(report["valid"])
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertTrue({
            "constraints_unknown_field",
            "constraints_key_invalid",
            "constraints_key_reserved",
            "constraints_value_invalid",
        } <= codes)

    def test_invalid_unicode_key_returns_structured_json_safe_diagnostic(self):
        report = inspect_constraints({
            "constraints": {
                "version": 1,
                "values": {"a\ud800": 1},
            },
        }, problem_schema_version=2)
        self.assertFalse(report["valid"])
        self.assertIn(
            "constraints_key_invalid",
            {item["code"] for item in report["diagnostics"]},
        )
        json.dumps(report, ensure_ascii=False)

    def test_problem_schema_v1_rejects_constraints_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = root / "A"
            problem.mkdir()
            write_yaml(problem / "probhub.yaml", {
                "schema_version": 1,
                "id": "A",
                "constraints": {"version": 1, "values": {"n_max": 10}},
            })
            with self.assertRaisesRegex(Exception, "constraints requires problem schema_version 2"):
                load_problem(root, {"id": "A", "directory": "A"})


if __name__ == "__main__":
    unittest.main()
