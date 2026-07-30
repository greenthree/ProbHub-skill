import shutil
import tempfile
import unittest
from pathlib import Path

from probhub.judging import judge_problem
from probhub.linting import lint_workspace, problem_status
from probhub.stressing import stress_problem
from probhub.workspace import load_workspace
from tests.fixture_support import (
    FIXTURE_ROOT,
    WORKSPACE_FIXTURES,
    copy_workspace_fixture,
    inject_fault,
    publish_current_status_artifacts,
)


class FixtureCatalogTests(unittest.TestCase):
    def test_workspaces_are_independent_lintable_schema_v1_projects(self):
        for name in WORKSPACE_FIXTURES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                fixture = copy_workspace_fixture(name, temp)
                root, workspace = load_workspace(fixture.root)
                result = lint_workspace(root, workspace)
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(result["problems"]), 1)
                self.assertEqual(result["problems"][0]["errors"], [])

    def test_committed_fixtures_contain_no_generated_artifacts(self):
        forbidden_names = {
            "build-manifest.json",
            "domjudge-problem.ini",
            "meta.json",
            "problem.pdf",
            "problem.yaml",
            "sandbox-cache-v1.json",
        }
        forbidden_suffixes = {".a", ".dll", ".exe", ".o", ".obj", ".so", ".zip"}
        for path in FIXTURE_ROOT.rglob("*"):
            if not path.is_file():
                continue
            self.assertNotIn(path.name, forbidden_names, path)
            self.assertNotIn(path.suffix.lower(), forbidden_suffixes, path)

    def test_copy_rejects_nonempty_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "keep.txt").write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be empty"):
                copy_workspace_fixture("standard", temp)


@unittest.skipUnless(shutil.which("g++"), "g++ is required for fixture integration tests")
class FixtureExecutionTests(unittest.TestCase):
    def run_judge_fixture(self, name):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = copy_workspace_fixture(name, temporary.name)
        result = judge_problem(fixture.root, fixture.problem, use_cache=False, timeout=120)
        return fixture, result

    def run_fault_fixture(self, base, fault):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = copy_workspace_fixture(base, temporary.name)
        program = inject_fault(fixture, fault)
        result = judge_problem(fixture.root, fixture.problem, use_cache=False, timeout=120)
        return fixture, program, result

    def test_standard_custom_float_and_interactive_judges(self):
        expected_types = {
            "standard": "standard",
            "custom": "custom",
            "float": "custom",
            "interactive": "interactive",
        }
        for name, judge_type in expected_types.items():
            with self.subTest(name=name):
                _, result = self.run_judge_fixture(name)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["final"]["code"], "all_expectations_met")
                cases = [event for event in result["events"] if event.get("type") == "case"]
                self.assertTrue(cases, result)
                self.assertEqual({case["judge_type"] for case in cases}, {judge_type})

    def test_stress_workspace_passes_deterministic_rounds(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = copy_workspace_fixture("stress", temp)
            result = stress_problem(
                fixture.root,
                fixture.problem,
                fixture.config(),
                rounds=5,
                master_seed=12345,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["rounds_completed"], 5)
            self.assertFalse((fixture.problem / ".probhub/stress").exists())

    def test_validator_rejection_is_infrastructure_failure(self):
        _, _, result = self.run_fault_fixture("standard", "validator")
        self.assertFalse(result["ok"], result)
        rejected = [
            event
            for event in result["events"]
            if event.get("type") == "validator" and not event.get("ok")
        ]
        self.assertEqual([event["case"] for event in rejected], ["secret/edge"])

    def test_checker_fail_is_not_counted_as_wrong_answer(self):
        _, _, result = self.run_fault_fixture("custom", "checker")
        self.assertFalse(result["ok"], result)
        failed = [
            event
            for event in result["events"]
            if event.get("type") == "case" and event.get("status") == "FAIL"
        ]
        self.assertTrue(failed, result)
        self.assertEqual(result["final"]["code"], "judge_failed")

    def test_output_limit_fault_is_structured_ole(self):
        _, program, result = self.run_fault_fixture("standard", "ole")
        self.assertTrue(result["ok"], result)
        cases = [
            event
            for event in result["events"]
            if event.get("type") == "case" and event.get("program") == program
        ]
        self.assertTrue(cases, result)
        self.assertEqual({event["status"] for event in cases}, {"OLE"})
        self.assertEqual({event["termination_reason"] for event in cases}, {"output_limit"})
        self.assertTrue(all(event["output_bytes"] > 1024 * 1024 for event in cases))

    def test_process_limit_fault_is_structured_re(self):
        _, program, result = self.run_fault_fixture("standard", "process-limit")
        self.assertTrue(result["ok"], result)
        cases = [
            event
            for event in result["events"]
            if event.get("type") == "case" and event.get("program") == program
        ]
        self.assertTrue(cases, result)
        self.assertEqual({event["status"] for event in cases}, {"RE"})
        self.assertTrue(
            all(event["termination_reason"] in {"completed", "process_limit"} for event in cases),
            cases,
        )

    def test_stale_artifact_fault_marks_package_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = copy_workspace_fixture("standard", temp)
            _, package = publish_current_status_artifacts(fixture)
            config = fixture.config()
            self.assertEqual(problem_status(fixture.problem, config)["state"], "current")
            package.write_bytes(package.read_bytes() + b" tampered")
            status = problem_status(fixture.problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["package_hash"])


if __name__ == "__main__":
    unittest.main()
