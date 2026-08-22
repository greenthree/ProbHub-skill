import tempfile
import unittest
from pathlib import Path

from probhub.judge_planning import (
    build_solution_calibration,
    collect_testcases,
    evaluate_expectation,
    solution_execution_plan,
    status_only_resource_kill,
)


class JudgePlanningCoreTests(unittest.TestCase):
    def test_collect_testcases_is_stable_and_assigns_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            sample = problem / "data/sample"
            secret = problem / "data/secret"
            sample.mkdir(parents=True)
            secret.mkdir(parents=True)
            (sample / "b.in").write_text("", encoding="utf-8")
            (sample / "a.in").write_text("", encoding="utf-8")
            (secret / "large.in").write_text("", encoding="utf-8")
            config = {
                "data": {
                    "groups": [{"name": "large", "patterns": ["secret/large*"], "targets": []}]
                }
            }
            cases = collect_testcases(problem, config)
            self.assertEqual([case["case"] for case in cases], ["sample/a", "sample/b", "secret/large"])
            self.assertEqual(cases[-1]["groups"], ["large"])

    def test_expectation_requires_selected_cases_and_forbids_bad_status(self):
        cases = [
            {"case": "sample/a", "groups": []},
            {"case": "secret/large", "groups": ["large"]},
        ]
        expected = {"status": ["WA"], "forbid": ["FAIL"], "all": True}
        result = evaluate_expectation(
            "wrong", "code/wrong.cpp", expected,
            [{"case": "sample/a", "status": "WA"}, {"case": "secret/large", "status": "AC"}],
            cases, [{"name": "large", "patterns": [], "targets": []}],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["matched_cases"], ["sample/a"])

    def test_execution_plan_reports_unknown_group_and_target_gap(self):
        cases = [{"case": "sample/a", "groups": []}]
        entries = {"std": [], "brute": [], "wrong": [{
            "file": "code/wrong.cpp", "expected": {"status": ["WA"]},
            "run_on": ["missing"], "run_on_error": None, "index": 0,
        }]}
        plans, errors = solution_execution_plan(entries, cases, [])
        self.assertFalse(plans["wrong"][0]["coverage_ok"])
        self.assertEqual(errors[0]["result_code"], "solution_run_on_unknown_group")

    def test_resource_and_calibration_summaries_are_bounded(self):
        mle = status_only_resource_kill(
            "MLE",
            {"case": "secret/memory", "groups": [], "memory": 300,
             "memory_enforced": True, "termination_reason": "memory_limit"},
            {"MLE": 256, "OLE": 64},
        )
        self.assertEqual(mle["evidence_kind"], "lower_bound")
        summary = build_solution_calibration(
            "std",
            [{"case": "sample/a", "time": 0.2, "cached": False}],
            1.0,
            {"accepted_time_multiplier": 3.0},
            [],
        )
        self.assertEqual(summary["max_time_case"], "sample/a")
        self.assertEqual(summary["accepted_check"]["ok"], True)


if __name__ == "__main__":
    unittest.main()
