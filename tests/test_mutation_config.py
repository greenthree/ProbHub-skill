import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from probhub.io import read_yaml, write_yaml
from probhub.linting import lint_workspace
from probhub.mutation_config import (
    MAX_MUTATION_EXCLUSIONS,
    inspect_mutation_config,
)
from probhub.reporting import (
    build_workspace_report,
    render_markdown_report,
    render_text_report,
)
from probhub.workspace import load_workspace


FIXTURE_ROOT = Path(__file__).parent / "fixtures/workspaces/mutation"
VALID_ID = "cpp-token-v1:comparison-boundary:8:23:405c48a0063442d7"


class MutationConfigTests(unittest.TestCase):
    def base_config(self):
        return {
            "judge": {"type": "standard"},
            "mutation": {
                "schema_version": 1,
                "exclusions": [{"id": VALID_ID, "reason": "equivalent under n >= 0"}],
            },
        }

    def test_valid_config_is_normalized(self):
        config = self.base_config()
        config["mutation"]["exclusions"][0]["reason"] = "  equivalent under n >= 0  "

        report = inspect_mutation_config(config)

        self.assertTrue(report["valid"], report)
        self.assertTrue(report["configured"])
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["exclusions"], [{
            "id": VALID_ID,
            "reason": "equivalent under n >= 0",
        }])

    def test_invalid_shapes_have_stable_diagnostic_codes(self):
        cases = {}

        config = self.base_config()
        config["mutation"] = []
        cases["mutation_config_type"] = config

        config = self.base_config()
        config["mutation"].pop("schema_version")
        cases["mutation_schema_version"] = config

        config = self.base_config()
        config["mutation"]["extra"] = True
        cases["mutation_unknown_field"] = config

        config = self.base_config()
        config["mutation"]["exclusions"] = "bad"
        cases["mutation_exclusions_type"] = config

        config = self.base_config()
        config["mutation"]["exclusions"] = ["bad"]
        cases["mutation_exclusion_type"] = config

        config = self.base_config()
        config["mutation"]["exclusions"][0]["extra"] = True
        cases["mutation_exclusion_unknown_field"] = config

        config = self.base_config()
        config["mutation"]["exclusions"][0]["id"] = "not-an-id"
        cases["mutation_exclusion_id_invalid"] = config

        config = self.base_config()
        config["mutation"]["exclusions"][0]["reason"] = ""
        cases["mutation_exclusion_reason_required"] = config

        config = self.base_config()
        config["mutation"]["exclusions"][0]["reason"] = "x" * 1025
        cases["mutation_exclusion_reason_limit"] = config

        config = self.base_config()
        config["mutation"]["exclusions"].append(
            copy.deepcopy(config["mutation"]["exclusions"][0])
        )
        cases["mutation_exclusion_duplicate"] = config

        config = self.base_config()
        config["mutation"]["exclusions"] = [
            {"id": VALID_ID, "reason": "reason"}
            for _ in range(MAX_MUTATION_EXCLUSIONS + 1)
        ]
        cases["mutation_exclusions_limit"] = config

        config = self.base_config()
        config["judge"]["type"] = "custom"
        cases["mutation_standard_required"] = config

        for expected, candidate in cases.items():
            with self.subTest(expected=expected):
                report = inspect_mutation_config(candidate)
                self.assertFalse(report["valid"], report)
                self.assertIn(expected, {
                    item["code"] for item in report["diagnostics"]
                })

    def test_schema_version_rejects_bool_and_float(self):
        for value in (True, 1.0):
            with self.subTest(value=value):
                config = self.base_config()
                config["mutation"]["schema_version"] = value
                report = inspect_mutation_config(config)
                self.assertFalse(report["valid"], report)
                self.assertIn("mutation_schema_version", {
                    item["code"] for item in report["diagnostics"]
                })

    def test_lint_surfaces_schema_diagnostic_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE_ROOT, root)
            path = root / "M01/probhub.yaml"
            config = read_yaml(path)
            config["mutation"] = {
                "schema_version": 1,
                "exclusions": [
                    {"id": VALID_ID, "reason": "first"},
                    {"id": VALID_ID, "reason": "duplicate"},
                ],
            }
            write_yaml(path, config)
            _, workspace = load_workspace(root)

            result = lint_workspace(root, workspace)

            problem = result["problems"][0]
            self.assertFalse(result["ok"], result)
            self.assertIn("mutation_exclusion_duplicate", {
                item["code"] for item in problem["diagnostics"]
            })
            self.assertTrue(any(
                "[mutation_exclusion_duplicate]" in message
                for message in problem["errors"]
            ))

    def test_report_does_not_duplicate_invalid_config_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE_ROOT, root)
            path = root / "M01/probhub.yaml"
            config = read_yaml(path)
            config["mutation"] = {
                "schema_version": True,
                "unknown": "field",
            }
            write_yaml(path, config)
            _, workspace = load_workspace(root)

            report = build_workspace_report(root, workspace)

            codes = [item["code"] for item in report["problems"][0]["diagnostics"]]
            self.assertEqual(codes.count("mutation_schema_version"), 1)
            self.assertEqual(codes.count("mutation_unknown_field"), 1)
            self.assertEqual(report["summary"]["errors"], 2)
            self.assertEqual(
                render_markdown_report(report).count("mutation_schema_version"),
                1,
            )
            self.assertEqual(
                render_text_report(report).count("mutation_schema_version"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
