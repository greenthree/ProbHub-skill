import tempfile
import unittest
from pathlib import Path

from probhub.judge_execution import (
    compare_sample_answer,
    run_custom_testcase,
    run_interactive_testcase,
    run_program_to_file,
    run_standard_testcase,
    run_testcase,
    run_validator,
)
from probhub.process_control import OutputBudgetError


class JudgeExecutionCoreTests(unittest.TestCase):
    def test_program_resource_failures_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            output = str(Path(temp) / "output")

            def fail(*_args, **_kwargs):
                raise OutputBudgetError("budget unavailable")

            status, _, _, _, message, details = run_program_to_file(
                "solution", "input", output, run_managed_fn=fail
            )
            self.assertEqual(status, "FAIL")
            self.assertIn("budget unavailable", message)
            self.assertEqual(details["termination_reason"], "output_control_error")

    def test_standard_and_sample_comparison_are_core_owned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            answer = root / "answer"
            output.write_bytes(b"1\r\n")
            answer.write_bytes(b"1\n")
            self.assertTrue(compare_sample_answer(output, answer)["matches"])

            def run_program(*_args, **_kwargs):
                return "AC", 0.01, 1, True, "", {"output_bytes": 2}

            result = run_standard_testcase(
                "solution", "input", answer, run_program_fn=run_program,
                output_path_fn=lambda _path: str(output), cleanup_fn=lambda _path: None,
            )
            self.assertEqual(result[0], "AC")

    def test_custom_and_interactive_execution_keep_structured_details(self):
        def run_program(*_args, **_kwargs):
            return "AC", 0.01, 1, True, "", {"output_bytes": 0}

        def checker(*_args, **_kwargs):
            return {
                "verdict": "WA",
                "execution_status": "completed",
                "failure_kind": "wrong_answer",
                "actor": "contestant",
                "termination_reason": "completed",
                "cleanup": {"clean": True},
                "message": "wrong",
            }

        custom = run_custom_testcase(
            "solution", "checker", "input", "answer", run_program_fn=run_program,
            checker_fn=checker, output_path_fn=lambda _path: "output",
            cleanup_fn=lambda _path: None,
        )
        self.assertEqual(custom[0], "WA")
        self.assertEqual(custom[5]["actor"], "contestant")

        def interactive(*_args, **_kwargs):
            return {"status": "AC", "time": 0.02, "memory": 2, "memory_enforced": True}

        interactive_result = run_interactive_testcase(
            "solution", "interactor", "input", "answer", interactive_fn=interactive
        )
        self.assertEqual(interactive_result[0], "AC")
        self.assertEqual(interactive_result[5]["resources"], None)

    def test_dispatch_and_validator_use_injected_runtime(self):
        standard = lambda *args, **kwargs: ("AC", 0.0, 0, True, "", {})
        custom = lambda *args, **kwargs: ("WA", 0.0, 0, True, "", {})
        interactive = lambda *args, **kwargs: ("AC", 0.0, 0, True, "", {})
        self.assertEqual(
            run_testcase("s", "i", "a", judge_type="custom", custom_fn=custom)[0], "WA"
        )
        self.assertEqual(
            run_testcase("s", "i", "a", judge_type="interactive", interactive_fn=interactive)[0], "AC"
        )
        self.assertEqual(
            run_testcase("s", "i", "a", standard_fn=standard)[0], "AC"
        )

        def validator(*_args, **_kwargs):
            return {"reason": "completed", "returncode": 0, "message": ""}

        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input"
            input_path.write_bytes(b"1\r\n")
            self.assertEqual(run_validator("validator", input_path, run_managed_fn=validator), (True, ""))


if __name__ == "__main__":
    unittest.main()
