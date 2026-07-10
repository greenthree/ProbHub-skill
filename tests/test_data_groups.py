import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from probhub.io import write_yaml
from probhub.linting import lint_workspace
from probhub.workspace import load_workspace

ROOT = Path(__file__).resolve().parents[1]
LOCAL_JUDGE = ROOT / "scripts" / "local_judge.py"
SPEC = importlib.util.spec_from_file_location("data_group_local_judge", LOCAL_JUDGE)
JUDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JUDGE)


class DataGroupExpectationUnitTests(unittest.TestCase):
    def setUp(self):
        self.testcases = [
            {"case": "sample/basic", "groups": []},
            {"case": "secret/overflow1", "groups": ["overflow"]},
            {"case": "secret/stress1", "groups": ["stress"]},
        ]
        self.groups = [
            {"name": "overflow", "role": "wrong-solution-killer", "patterns": ["secret/overflow*"], "targets": ["code/wrong_int.cpp"]},
            {"name": "stress", "role": "brute-killer", "patterns": ["secret/stress*"], "targets": ["code/brute.cpp"]},
            {"name": "ordinary", "role": None, "patterns": ["sample/*"], "targets": []},
        ]

    def result(self, case, status, groups=None):
        return {"case": case, "groups": groups or [], "status": status, "message": ""}

    def test_patterns_assign_groups(self):
        groups = JUDGE._data_groups({"data": {"groups": self.groups}})
        self.assertEqual(JUDGE._testcase_groups("secret/overflow1", "secret", "overflow1", groups), ["overflow"])
        prefix_group = [{"name": "edge", "patterns": [], "targets": [], "role": None}]
        self.assertEqual(JUDGE._testcase_groups("secret/edge-large", "secret", "edge-large", prefix_group), ["edge"])

    def test_wrong_must_match_status_inside_target_group(self):
        expected = JUDGE._normalize_expected("wrong", {"expected": {"status": "WA", "groups": ["overflow"]}})
        passing = [
            self.result("sample/basic", "AC"),
            self.result("secret/overflow1", "WA", ["overflow"]),
            self.result("secret/stress1", "AC", ["stress"]),
        ]
        outcome = JUDGE.evaluate_expectation("wrong", "code/wrong_int.cpp", expected, passing, self.testcases, self.groups)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["selected_cases"], ["secret/overflow1"])
        self.assertEqual(outcome["first_expected_match"]["case"], "secret/overflow1")
        self.assertIsNone(outcome["first_forbidden"])

        killed_outside_group = [
            self.result("sample/basic", "WA"),
            self.result("secret/overflow1", "AC", ["overflow"]),
            self.result("secret/stress1", "AC", ["stress"]),
        ]
        outcome = JUDGE.evaluate_expectation("wrong", "code/wrong_int.cpp", expected, killed_outside_group, self.testcases, self.groups)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["matched_cases"], [])

        wrong_status = [
            self.result("sample/basic", "AC"),
            self.result("secret/overflow1", "TLE", ["overflow"]),
            self.result("secret/stress1", "AC", ["stress"]),
        ]
        outcome = JUDGE.evaluate_expectation("wrong", "code/wrong_int.cpp", expected, wrong_status, self.testcases, self.groups)
        self.assertFalse(outcome["ok"])

    def test_untargeted_groups_do_not_narrow_expectation(self):
        expected = JUDGE._normalize_expected("wrong", None)
        selected, groups = JUDGE._expectation_cases("wrong", self.testcases, expected, self.groups, "code/wrong_other.cpp")
        self.assertEqual(groups, [])
        self.assertEqual([case["case"] for case in selected], [case["case"] for case in self.testcases])

    def test_brute_tle_cannot_hide_wa(self):
        expected = JUDGE._normalize_expected("brute", None)
        results = [
            self.result("sample/basic", "WA"),
            self.result("secret/overflow1", "AC", ["overflow"]),
            self.result("secret/stress1", "TLE", ["stress"]),
        ]
        outcome = JUDGE.evaluate_expectation("brute", "code/brute.cpp", expected, results, self.testcases, self.groups)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["first_forbidden"]["status"], "WA")
        self.assertEqual(outcome["first_expected_match"]["status"], "TLE")

    def test_accepted_always_checks_all_cases(self):
        expected = JUDGE._normalize_expected("std", {"expected": {"status": "AC", "groups": ["overflow"], "all": True}})
        results = [
            self.result("sample/basic", "WA"),
            self.result("secret/overflow1", "AC", ["overflow"]),
            self.result("secret/stress1", "AC", ["stress"]),
        ]
        outcome = JUDGE.evaluate_expectation("std", "code/std.cpp", expected, results, self.testcases, self.groups)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["groups"], [])
        self.assertEqual(len(outcome["selected_cases"]), 3)

    def test_legacy_string_solution_entries_remain_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            (problem / "code").mkdir()
            (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
            entries = JUDGE.discover_solution_entries(str(problem), {"solutions": {"accepted": ["code/std.cpp"]}})
            self.assertEqual(entries["std"][0]["expected"]["status"], ["AC"])
            self.assertTrue(entries["std"][0]["expected"]["all"])


class DataGroupLintTests(unittest.TestCase):
    def create_workspace(self, root):
        write_yaml(root / ".probhub/workspace.yaml", {"schema_version": 1, "problems": [{"id": "A", "directory": "A"}]})
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text("# Test\n\n## 题目描述\n\nD.\n\n## 输入格式\n\nI.\n\n## 输出格式\n\nO.\n", encoding="utf-8")
        for file in ("validator.cpp", "std.cpp", "wrong.cpp"):
            (problem / "code" / file).write_text("int main(){}\n", encoding="utf-8")
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "Test",
            "limits": {"time": 1, "memory": 256},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {
                "accepted": ["code/std.cpp"],
                "wrong": [{"file": "code/wrong.cpp", "expected": {"status": "WA", "groups": ["missing"]}}],
            },
            "data": {
                "sample_dir": "data/sample",
                "secret_dir": "data/secret",
                "groups": [
                    {"name": "duplicate", "patterns": ["sample/*"], "targets": ["code/missing.cpp"]},
                    {"name": "duplicate", "patterns": ["secret/*"]},
                ],
            },
        }
        write_yaml(problem / "probhub.yaml", config)
        return problem

    def test_lint_reports_unknown_duplicate_and_invalid_expectation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            errors = lint_workspace(root, workspace)["problems"][0]["errors"]
            self.assertIn("duplicate data group names: duplicate", errors)
            self.assertIn("unknown expected groups: missing", errors)
            self.assertIn("data group duplicate has unknown targets: code/missing.cpp", errors)

            config = yaml.safe_load((problem / "probhub.yaml").read_text(encoding="utf-8"))
            config["solutions"]["wrong"][0]["expected"]["forbid"] = "BOOM"
            write_yaml(problem / "probhub.yaml", config)
            errors = lint_workspace(root, workspace)["problems"][0]["errors"]
            self.assertIn("invalid expected forbid for code/wrong.cpp: BOOM", errors)


class SandboxUiEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui_spec = importlib.util.spec_from_file_location("data_group_ui", ROOT / "scripts" / "ui.py")
        cls.ui = importlib.util.module_from_spec(ui_spec)
        ui_spec.loader.exec_module(cls.ui)

    def test_group_and_expectation_events_are_preserved(self):
        result = self.ui._empty_sandbox_result({"limits": {"time": 1, "memory": 256}})
        self.ui._apply_sandbox_event(result, {
            "type": "groups",
            "groups": [{"name": "overflow", "patterns": ["secret/overflow*"], "targets": ["code/wrong.cpp"]}],
        })
        self.ui._apply_sandbox_event(result, {
            "type": "case",
            "kind": "wrong",
            "program": "code/wrong.cpp",
            "case": "secret/overflow1",
            "groups": ["overflow"],
            "status": "WA",
            "time": 0.01,
        })
        expectation = {
            "type": "expectation",
            "kind": "wrong",
            "program": "code/wrong.cpp",
            "expected_statuses": ["WA"],
            "forbidden_statuses": ["FAIL"],
            "groups": ["overflow"],
            "ok": True,
            "first_expected_match": {"case": "secret/overflow1", "status": "WA"},
        }
        self.ui._apply_sandbox_event(result, expectation)
        self.assertEqual(result["groups"][0]["name"], "overflow")
        self.assertEqual(result["cases"][0]["groups"], ["overflow"])
        self.assertTrue(result["expectations"]["code/wrong.cpp"]["ok"])
        self.assertIn("expectation:PASS", self.ui._sandbox_log_line(expectation))


@unittest.skipUnless(shutil.which("g++"), "g++ is required for data-group integration tests")
class DataGroupIntegrationTests(unittest.TestCase):
    def create_problem(self, root):
        problem = root / "A"
        code = problem / "code"
        sample = problem / "data/sample"
        secret = problem / "data/secret"
        code.mkdir(parents=True)
        sample.mkdir(parents=True)
        secret.mkdir(parents=True)
        (code / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (code / "std.cpp").write_text(textwrap.dedent(r"""
            #include <iostream>
            int main(){ long long x; std::cin >> x; std::cout << x << '\n'; }
        """), encoding="utf-8")
        (code / "wrong.cpp").write_text(textwrap.dedent(r"""
            #include <iostream>
            int main(){ long long x; std::cin >> x; std::cout << (x == 2 ? 0 : x) << '\n'; }
        """), encoding="utf-8")
        (sample / "basic.in").write_text("1\n", encoding="utf-8")
        (sample / "basic.ans").write_text("1\n", encoding="utf-8")
        (secret / "overflow1.in").write_text("2\n", encoding="utf-8")
        (secret / "overflow1.ans").write_text("2\n", encoding="utf-8")
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "Groups",
            "limits": {"time": 2, "memory": 256},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {
                "accepted": ["code/std.cpp"],
                "brute": [],
                "wrong": [{"file": "code/wrong.cpp", "expected": {"status": "WA", "groups": ["overflow"]}}],
            },
            "data": {
                "sample_dir": "data/sample",
                "secret_dir": "data/secret",
                "groups": [{"name": "overflow", "role": "wrong-solution-killer", "patterns": ["secret/overflow*"], "targets": ["code/wrong.cpp"]}],
            },
        }
        (problem / "probhub.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return problem

    def run_judge(self, problem, no_cache=False):
        args = [sys.executable, str(LOCAL_JUDGE), str(problem), "--jsonl"]
        if no_cache:
            args.append("--no-cache")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, events

    def test_events_and_expectation_only_changes_reuse_case_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.create_problem(Path(temp))
            first, events = self.run_judge(problem, no_cache=True)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            groups_event = next(event for event in events if event.get("type") == "groups")
            self.assertEqual(groups_event["groups"][0]["name"], "overflow")
            overflow_case = next(event for event in events if event.get("type") == "case" and event.get("program") == "code/wrong.cpp" and event.get("case") == "secret/overflow1")
            self.assertEqual(overflow_case["groups"], ["overflow"])
            expectation = next(event for event in events if event.get("type") == "expectation" and event.get("program") == "code/wrong.cpp")
            self.assertTrue(expectation["ok"])
            self.assertEqual(expectation["first_expected_match"]["status"], "WA")

            config_path = problem / "probhub.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["solutions"]["wrong"][0]["expected"]["status"] = "TLE"
            config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
            second, events = self.run_judge(problem)
            self.assertNotEqual(second.returncode, 0)
            wrong_cases = [event for event in events if event.get("type") == "case" and event.get("program") == "code/wrong.cpp"]
            self.assertTrue(wrong_cases and all(event["cached"] for event in wrong_cases))
            expectation = next(event for event in events if event.get("type") == "expectation" and event.get("program") == "code/wrong.cpp")
            self.assertFalse(expectation["ok"])
            self.assertEqual(events[-1]["code"], "expectation_not_met")


if __name__ == "__main__":
    unittest.main()
