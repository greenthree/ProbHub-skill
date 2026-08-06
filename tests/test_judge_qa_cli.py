import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from probhub.cli import main as cli_main
from tests.fixture_support import copy_workspace_fixture


class JudgeQACliTests(unittest.TestCase):
    def copy_fixture(self, name="checker-qa"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return copy_workspace_fixture(name, temporary.name)

    def invoke(self, root, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(["--workspace", str(root), "--json", *arguments])
        return code, json.loads(output.getvalue())

    def test_no_cache_is_forwarded_without_changing_result_shape(self):
        fixture = self.copy_fixture()
        result = {
            "ok": True,
            "applicable": True,
            "status": "passed",
            "code": "judge_qa_passed",
            "cache": {
                "mode": "refresh",
                "compile_hits": 0,
                "compile_misses": 2,
            },
        }
        with patch("probhub.cli.judge_qa_problem", return_value=result) as execute:
            code, payload = self.invoke(
                fixture.root,
                "judge-qa",
                "F06",
                "--no-cache",
            )

        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["problems"]["F06"]["status"], "passed")
        self.assertFalse(execute.call_args.kwargs["use_cache"])

    def test_not_configured_is_successful_but_not_reported_as_passed(self):
        fixture = self.copy_fixture("custom")

        code, payload = self.invoke(fixture.root, "judge-qa")

        self.assertEqual(code, 0, payload)
        result = next(iter(payload["problems"].values()))
        self.assertTrue(result["ok"])
        self.assertFalse(result["applicable"])
        self.assertEqual(result["status"], "not-configured")

    def test_failure_states_return_nonzero(self):
        fixture = self.copy_fixture()
        for status in (
            "expectation-failed",
            "infrastructure-failed",
            "cancelled",
        ):
            with self.subTest(status=status), patch(
                "probhub.cli.judge_qa_problem",
                return_value={"ok": False, "applicable": True, "status": status},
            ):
                code, payload = self.invoke(fixture.root, "judge-qa", "F06")
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["problems"]["F06"]["status"], status)


if __name__ == "__main__":
    unittest.main()
