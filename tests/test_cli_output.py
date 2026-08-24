import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.cli import build_parser, main
from probhub.cli_output import render_human_result
from probhub.remediation import (
    attach_remediations,
    remediation_for_diagnostic,
    remediation_summary,
)


class CliOutputTests(unittest.TestCase):
    def test_format_text_is_explicit_and_keeps_json_compatibility(self):
        args = build_parser().parse_args(["--format", "text", "lint"])
        self.assertEqual(args.output_format, "text")
        args = build_parser().parse_args(["--json", "lint"])
        self.assertTrue(args.json_output)

    def test_global_format_json_overrides_the_report_renderer(self):
        output = io.StringIO()
        with (
            patch(
                "probhub.cli.command_report",
                return_value={"ok": True, "schema_version": 1, "problems": []},
            ),
            redirect_stdout(output),
        ):
            code = main(["--format", "json", "report"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)

    def test_summary_is_bounded_and_includes_actionable_diagnostic(self):
        result = {
            "ok": False,
            "problems": {
                "A": {
                    "ok": False,
                    "errors": ["source is invalid"],
                    "diagnostics": [{
                        "severity": "error",
                        "code": "statement_invalid",
                        "message": "fix the statement",
                        "path": "A/problem.md",
                        "line": 4,
                    }],
                },
                **{
                    f"P{i}": {"ok": True}
                    for i in range(6)
                },
            },
        }
        output = render_human_result(result, "lint")
        self.assertIn("ProbHub: FAILED", output)
        self.assertIn("statement_invalid: fix the statement [A/problem.md:4]", output)
        self.assertIn("... 2 more problem(s)", output)
        self.assertIn("Fix the first error", output)
        self.assertIn("probhub lint A", output)
        self.assertLessEqual(len(output.splitlines()), 20)

    def test_summary_uses_the_first_problem_id_in_next_command(self):
        output = render_human_result(
            {"ok": True, "problems": {"A": {"ok": True}}},
            "judge",
        )
        self.assertIn("probhub seal A", output)

    def test_summary_accepts_lint_style_problem_list(self):
        output = render_human_result(
            {"ok": True, "problems": [{"id": "L01", "ok": True}]},
            "lint",
        )
        self.assertIn("- L01: OK", output)
        self.assertIn("probhub judge L01", output)

    def test_summary_handles_build_and_doctor_sections(self):
        build = render_human_result(
            {"ok": True, "judge": {"L01": {"ok": True}}},
            "build",
        )
        self.assertIn("Problems: 1", build)
        self.assertIn("- L01: OK", build)
        doctor = render_human_result(
            {"ok": False, "tools": {"node": {"ok": False}}},
            "doctor",
        )
        self.assertIn("Tools: 1", doctor)
        self.assertIn("- node: FAILED", doctor)

    def test_text_mode_does_not_change_json_payload(self):
        payload = {"ok": True, "problems": {"A": {"ok": True}}}
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--format", "text", "lint"])
        # The command itself requires a workspace, so this checks the stable
        # structured error path rather than relying on a live filesystem.
        self.assertEqual(code, 1)
        self.assertIn("ProbHub: FAILED", output.getvalue())
        self.assertNotIn(json.dumps(payload), output.getvalue())

    def test_known_diagnostic_gets_bounded_actionable_remediation(self):
        diagnostic = {
            "code": "calibration_evidence_missing",
            "severity": "warning",
            "message": "evidence is missing",
        }
        enriched = attach_remediations([diagnostic], problem_id="L01")
        remediation = enriched[0]["remediation"]
        self.assertEqual(remediation["action_code"], "refresh_calibration_evidence")
        self.assertEqual(remediation["command"], ["probhub", "judge", "L01", "--no-cache"])
        self.assertFalse(remediation["manual_review_required"])
        self.assertIn("probhub judge L01 --no-cache", remediation_summary(remediation))
        self.assertEqual(build_parser().parse_args(remediation["command"][1:]).command, "judge")

    def test_unknown_remediation_is_ignored_and_paths_are_not_escaped(self):
        self.assertIsNone(remediation_summary({"action_code": "write_arbitrary_file"}))
        diagnostic = {
            "code": "constraint_mismatch",
            "severity": "warning",
            "message": "mismatch",
        }
        remediation = remediation_for_diagnostic(
            diagnostic,
            problem_id="L01",
            problem_dir="C:/workspace/L01",
            config={"judge": {"validator": "../../outside.cpp"}},
        )
        self.assertNotIn("target_path", remediation)
        self.assertTrue(remediation["manual_review_required"])

    def test_constraint_remediation_uses_problem_local_validator(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            validator = problem / "code/validator.cpp"
            validator.parent.mkdir()
            validator.write_text("int main() {}\n", encoding="utf-8")
            remediation = remediation_for_diagnostic(
                {"code": "constraint_statement_only"},
                problem_id="L01",
                problem_dir=problem,
                config={"judge": {"validator": "code/validator.cpp"}},
            )
        self.assertEqual(remediation["action_code"], "review_constraint_contract")
        self.assertEqual(remediation["target_path"], "code/validator.cpp")
        self.assertTrue(remediation["manual_review_required"])
        self.assertEqual(build_parser().parse_args(remediation["command"][1:]).command, "lint")

    def test_judge_qa_fixture_remediation_requires_manual_review(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            (problem / "probhub.yaml").write_text("schema_version: 1\n", encoding="utf-8")
            remediation = remediation_for_diagnostic(
                {"code": "judge_qa_fixture_path_outside"},
                problem_id="L01",
                problem_dir=problem,
            )
        self.assertEqual(remediation["action_code"], "repair_judge_qa_fixtures")
        self.assertEqual(remediation["target_path"], "probhub.yaml")
        self.assertTrue(remediation["manual_review_required"])
        self.assertEqual(build_parser().parse_args(remediation["command"][1:]).command, "lint")

    def test_judge_qa_evidence_remediation_uses_no_cache_command(self):
        remediation = remediation_for_diagnostic(
            {"code": "judge_qa_evidence_stale"},
            problem_id="L01",
        )
        self.assertEqual(remediation["action_code"], "refresh_judge_qa_evidence")
        self.assertEqual(
            remediation["command"],
            ["probhub", "judge-qa", "L01", "--no-cache"],
        )
        self.assertTrue(remediation["manual_review_required"])
        self.assertEqual(
            build_parser().parse_args(remediation["command"][1:]).command,
            "judge-qa",
        )

    def test_manifest_remediation_refreshes_seal_without_targeting_generated_file(self):
        remediation = remediation_for_diagnostic(
            {"code": "build_manifest_stale"},
            problem_id="L01",
        )
        self.assertEqual(remediation["action_code"], "refresh_sealed_revision")
        self.assertNotIn("target_path", remediation)
        args = build_parser().parse_args(remediation["command"][1:])
        self.assertEqual(args.command, "seal")
        self.assertTrue(args.no_cache)


if __name__ == "__main__":
    unittest.main()
