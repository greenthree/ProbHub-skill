import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.building import _publish_transaction, build_workspace
from probhub.cli import main
from probhub.errors import ProbHubError
from probhub.generations import assemble_exam_generation
from probhub.judging import judge_problem
from probhub.process_control import ProcessCancelled, check_cancellation
from probhub.stressing import stress_problem
from tests.fixture_support import copy_workspace_fixture


class CancellationContractTests(unittest.TestCase):
    def test_shared_check_uses_the_cancelled_exception_and_code(self):
        with self.assertRaises(ProcessCancelled) as raised:
            check_cancellation(lambda: True)
        self.assertEqual(raised.exception.code, "cancelled")

    def test_cli_maps_cancelled_operations_to_stable_structured_result(self):
        output = io.StringIO()
        with patch("probhub.cli.command_lint", side_effect=ProcessCancelled("stop now")), redirect_stdout(output):
            code = main(["--json", "lint"])
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload, {
            "ok": False,
            "error": "stop now",
            "code": "cancelled",
        })

    def test_build_cancel_before_publication_keeps_existing_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "artifact.txt"
            target.write_text("known-good", encoding="utf-8")
            source = root / "staged.txt"
            source.write_text("new", encoding="utf-8")

            with self.assertRaises(ProbHubError) as raised:
                _publish_transaction(
                    root,
                    "cancel-before-publish",
                    [(source, target)],
                    cancel_check=lambda: True,
                )

            self.assertEqual(raised.exception.code, "cancelled")
            self.assertEqual(target.read_text(encoding="utf-8"), "known-good")
            self.assertFalse(any(root.glob(".probhub/build-publish-*")))

    def test_cleanup_failure_overrides_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "artifact.txt"
            target.write_text("known-good", encoding="utf-8")
            source = root / "staged.txt"
            source.write_text("new", encoding="utf-8")

            with patch("probhub.building.shutil.rmtree", side_effect=OSError("locked")):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_transaction(
                        root,
                        "cancel-cleanup",
                        [(source, target)],
                        cancel_check=lambda: True,
                    )

            self.assertEqual(raised.exception.code, "publish_cleanup_failed")
            self.assertEqual(target.read_text(encoding="utf-8"), "known-good")

    def test_cancellation_during_replacements_rolls_back_previous_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("old-first", encoding="utf-8")
            second.write_text("old-second", encoding="utf-8")
            staged_first = root / "staged-first.txt"
            staged_second = root / "staged-second.txt"
            staged_first.write_text("new-first", encoding="utf-8")
            staged_second.write_text("new-second", encoding="utf-8")
            calls = iter((False, False, True))

            with self.assertRaises(ProbHubError) as raised:
                _publish_transaction(
                    root,
                    "cancel-during-publish",
                    [(staged_first, first), (staged_second, second)],
                    cancel_check=lambda: next(calls, True),
                )

            self.assertEqual(raised.exception.code, "cancelled")
            self.assertEqual(first.read_text(encoding="utf-8"), "old-first")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second")

    def test_cancel_requested_after_commit_does_not_change_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "artifact.txt"
            target.write_text("old", encoding="utf-8")
            source = root / "staged.txt"
            source.write_text("new", encoding="utf-8")
            calls = iter((False, False, True))

            _publish_transaction(
                root,
                "commit-boundary",
                [(source, target)],
                cancel_check=lambda: next(calls, True),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_build_api_maps_boundary_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ProbHubError) as raised:
                build_workspace(
                    Path(temp),
                    {},
                    [],
                    cancel_check=lambda: True,
                )
        self.assertEqual(raised.exception.code, "cancelled")

    def test_judge_api_keeps_cancellation_in_the_final_result(self):
        with patch(
            "probhub.judging._invoke_local_judge",
            side_effect=ProcessCancelled("judge stopped"),
        ):
            result = judge_problem(".", ".", use_cache=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["final"]["status"], "cancelled")
        self.assertEqual(result["final"]["code"], "cancelled")

    def test_stress_api_returns_structured_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = copy_workspace_fixture("stress", temp)
            result = stress_problem(
                fixture.root,
                fixture.problem,
                fixture.config(),
                rounds=1,
                cancel_check=lambda: True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["code"], "cancelled")

    def test_generation_api_cancels_before_writing_current_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ProbHubError) as raised:
                assemble_exam_generation(Path(temp), cancel_check=lambda: True)
        self.assertEqual(raised.exception.code, "cancelled")


if __name__ == "__main__":
    unittest.main()
