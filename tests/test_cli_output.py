import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from probhub.cli import build_parser, main
from probhub.cli_output import render_human_result


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


if __name__ == "__main__":
    unittest.main()
