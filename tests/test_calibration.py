import importlib.util
import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probhub.calibration import (
    CALIBRATION_SCHEMA_VERSION,
    CALIBRATION_STRATEGY_VERSION,
    EVIDENCE_FILENAME,
    MEASUREMENT_NOTE,
    SANDBOX_CACHE_SCHEMA_VERSION,
    build_judge_evidence,
    evaluate_calibration,
    read_calibration_evidence,
    validate_calibration_config,
    write_calibration_evidence,
)
from probhub.building import _publish_calibration_evidence
from probhub.cli import _ensure_local_gitignore
from probhub.io import write_yaml
from probhub.linting import lint_problem, problem_status


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "calibration_local_judge", ROOT / "scripts" / "local_judge.py"
)
LOCAL_JUDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCAL_JUDGE)


def _evidence(*, source_hash="source", data_hash="data", headroom=3.0, tle_ratio=1.5):
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "strategy_version": CALIBRATION_STRATEGY_VERSION,
        "sandbox_cache_schema_version": SANDBOX_CACHE_SCHEMA_VERSION,
        "source_hash": source_hash,
        "data_hash": data_hash,
        "measurement": {
            "platform": platform.system(),
            "machine": platform.machine(),
            "target_guarantee": False,
            "note": MEASUREMENT_NOTE,
        },
        "solutions": [
            {
                "kind": "std",
                "program": "code/std.cpp",
                "expectation": {
                    "expected_statuses": ["AC"],
                    "run_on": [],
                    "executed_cases": ["sample/1", "secret/max01"],
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                },
                "execution_domain": {
                    "run_on": [],
                    "executed_cases": ["sample/1", "secret/max01"],
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                },
                "calibration": {
                    "max_time": 1.0 / headroom,
                    "max_time_case": "secret/max01",
                    "headroom_factor": headroom,
                    "resource_kills": [],
                },
            },
            {
                "kind": "brute",
                "program": "code/brute.cpp",
                "expectation": {
                    "expected_statuses": ["TLE"],
                    "run_on": [],
                    "executed_cases": ["sample/1", "secret/stress01"],
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                },
                "execution_domain": {
                    "run_on": [],
                    "executed_cases": ["sample/1", "secret/stress01"],
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                },
                "calibration": {
                    "max_time": 1.0,
                    "resource_kills": [
                        {
                            "status": "TLE",
                            "case": "secret/stress01",
                            "limit_ratio": tle_ratio,
                            "evidence_kind": "exact",
                            "process_status": "completed",
                        }
                    ]
                },
            },
        ],
    }


class CalibrationPolicyTests(unittest.TestCase):
    def test_evidence_v2_identity(self):
        self.assertEqual(CALIBRATION_SCHEMA_VERSION, 2)
        self.assertEqual(EVIDENCE_FILENAME, "judge-evidence-v2.json")

    def test_config_validation(self):
        self.assertEqual(validate_calibration_config({}), [])
        self.assertEqual(
            validate_calibration_config({
                "calibration": {
                    "accepted_time_multiplier": 3.0,
                    "expected_tle_time_multiplier": 1.5,
                }
            }),
            [],
        )
        invalid = [
            {"calibration": []},
            {"calibration": {"accepted_time_multiplier": True}},
            {"calibration": {"accepted_time_multiplier": 0}},
            {"calibration": {"expected_tle_time_multiplier": 1}},
            {"calibration": {"expected_tle_time_multiplier": float("inf")}},
        ]
        for config in invalid:
            with self.subTest(config=config):
                self.assertTrue(validate_calibration_config(config))

    def test_missing_stale_and_margin_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            missing = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(missing["state"], "missing")
            self.assertEqual(missing["diagnostics"][0]["code"], "calibration_evidence_missing")

            write_calibration_evidence(problem, _evidence(source_hash="old"))
            stale = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(stale["state"], "stale")
            self.assertEqual(stale["diagnostics"][0]["code"], "calibration_evidence_stale")

            write_calibration_evidence(problem, _evidence(headroom=2.5, tle_ratio=1.2))
            current = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(current["state"], "current")
            self.assertEqual(
                {item["code"] for item in current["diagnostics"]},
                {"accepted_time_margin_low", "expected_tle_margin_low"},
            )

            write_calibration_evidence(problem, _evidence(headroom=3.0, tle_ratio=1.5))
            boundary = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(boundary["state"], "current")
            self.assertEqual(boundary["diagnostics"], [])

    def test_alternative_resource_status_does_not_require_unobserved_tle(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence()
            brute = evidence["solutions"][1]
            brute["expectation"]["expected_statuses"] = ["TLE", "MLE"]
            brute["calibration"]["resource_kills"] = [
                {
                    "status": "MLE",
                    "case": "secret/memory",
                    "limit_ratio": 1.0,
                    "evidence_kind": "lower_bound",
                }
            ]
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "current")
            self.assertNotIn(
                "expected_tle_margin_low",
                {item["code"] for item in result["diagnostics"]},
            )

    def test_structurally_incomplete_evidence_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence()
            evidence["solutions"] = []
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "invalid")
            self.assertEqual(result["diagnostics"][0]["code"], "calibration_evidence_invalid")

    def test_strategy_one_evidence_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence()
            evidence["strategy_version"] = 1
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "invalid")
            self.assertIn("strategy changed", result["diagnostics"][0]["message"])

    def test_stale_identity_precedes_strategy_and_structure_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence(source_hash="old")
            evidence["strategy_version"] = 1
            evidence["solutions"] = []
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "stale")
            self.assertIn("source/data", result["diagnostics"][0]["message"])

    def test_schema_precedes_source_and_platform_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence(source_hash="old")
            evidence["schema_version"] = 1
            evidence["measurement"]["platform"] = "DefinitelyNotThisPlatform"
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "stale")
            self.assertIn("unsupported schema", result["diagnostics"][0]["message"])

    def test_platform_precedes_strategy_and_structure_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence()
            evidence["measurement"]["platform"] = "DefinitelyNotThisPlatform"
            evidence["strategy_version"] = 1
            evidence["solutions"] = []
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "stale")
            self.assertIn("different platform", result["diagnostics"][0]["message"])

    def test_legacy_v1_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            legacy = problem / ".probhub/judge-evidence-v1.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}\n", encoding="utf-8")
            self.assertIsNone(read_calibration_evidence(problem))
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "missing")

    def test_execution_domain_must_match_expectation(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            evidence = _evidence()
            evidence["solutions"][0]["execution_domain"]["executed_cases"] = ["sample/1"]
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, {}, "source", "data")
            self.assertEqual(result["state"], "invalid")
            self.assertIn("execution domain", result["diagnostics"][0]["message"])

    def test_execution_domain_must_match_current_config(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            (problem / "data/sample").mkdir(parents=True)
            (problem / "data/secret").mkdir(parents=True)
            (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
            (problem / "data/secret/max01.in").write_text("1\n", encoding="utf-8")
            (problem / "data/secret/stress01.in").write_text("1\n", encoding="utf-8")
            config = {
                "solutions": {
                    "accepted": ["code/std.cpp"],
                    "brute": [{
                        "file": "code/brute.cpp",
                        "run_on": ["small"],
                        "expected": {"status": "TLE", "groups": ["small"]},
                    }],
                },
                "data": {
                    "sample_dir": "data/sample",
                    "secret_dir": "data/secret",
                    "groups": [{"name": "small", "patterns": ["secret/stress*"]}],
                },
            }
            evidence = _evidence()
            accepted = evidence["solutions"][0]
            accepted_cases = ["sample/1", "secret/max01", "secret/stress01"]
            accepted["expectation"]["executed_cases"] = accepted_cases
            accepted["execution_domain"]["executed_cases"] = accepted_cases
            brute = evidence["solutions"][1]
            brute_cases = ["sample/1", "secret/stress01"]
            brute["expectation"]["executed_cases"] = brute_cases
            brute["expectation"]["skipped_cases"] = ["secret/max01"]
            brute["execution_domain"]["executed_cases"] = brute_cases
            brute["execution_domain"]["skipped_cases"] = ["secret/max01"]
            write_calibration_evidence(problem, evidence)
            result = evaluate_calibration(problem, config, "source", "data")
            self.assertEqual(result["state"], "invalid")
            self.assertIn("current config", result["diagnostics"][0]["message"])

    def test_build_evidence_preserves_measurement_disclaimer(self):
        events = [
            {
                "type": "limits",
                "time_limit": 1,
                "memory_limit": 256,
                "output_limit": 64,
                "process_limit": 32,
                "platform": "Windows",
                "machine": "AMD64",
                "compiler": "g++ test",
            },
            {
                "type": "summary",
                "kind": "std",
                "program": "code/std.cpp",
                "stats": {"AC": 1},
                "expectation": {
                    "ok": True,
                    "run_on": [],
                    "executed_cases": ["sample/1"],
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                },
                "run_on": [],
                "executed_cases": ["sample/1"],
                "skipped_cases": [],
                "coverage_ok": True,
                "unexecuted_expected_cases": [],
                "calibration": {"max_time": 0.1},
            },
        ]
        evidence = build_judge_evidence(events, "source", "data", "now")
        self.assertFalse(evidence["measurement"]["target_guarantee"])
        self.assertEqual(evidence["measurement"]["note"], MEASUREMENT_NOTE)
        self.assertEqual(evidence["solutions"][0]["calibration"]["max_time"], 0.1)
        self.assertEqual(
            evidence["solutions"][0]["execution_domain"]["executed_cases"],
            ["sample/1"],
        )


class CalibrationLintStatusTests(unittest.TestCase):
    def make_problem(self, root, calibration=None):
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# A\n\n## 题目描述\nA\n\n## 输入格式\nA\n\n## 输出格式\nA\n",
            encoding="utf-8",
        )
        (problem / "code/validator.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        for suite in ("sample", "secret"):
            (problem / f"data/{suite}/1.in").write_text("1\n", encoding="utf-8")
            (problem / f"data/{suite}/1.ans").write_text("1\n", encoding="utf-8")
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "A",
            "limits": {"time": 1, "memory": 256},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"]},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        }
        if calibration is not None:
            config["calibration"] = calibration
        write_yaml(problem / "probhub.yaml", config)
        return problem, config

    def test_lint_reports_missing_evidence_as_non_blocking_structured_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _problem, _config = self.make_problem(root)
            result = lint_problem(root, {}, {"id": "A", "directory": "A"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["calibration"]["state"], "missing")
            self.assertEqual(result["diagnostics"][0]["code"], "calibration_evidence_missing")
            self.assertTrue(any("calibration_evidence_missing" in item for item in result["warnings"]))

    def test_invalid_calibration_threshold_is_a_lint_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_problem(root, {"expected_tle_time_multiplier": 1.0})
            result = lint_problem(root, {}, {"id": "A", "directory": "A"})
            self.assertFalse(result["ok"])
            self.assertTrue(any("expected_tle_time_multiplier" in item for item in result["errors"]))

    def test_status_exposes_current_calibration_without_affecting_build_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.make_problem(root)
            lint = lint_problem(root, {}, {"id": "A", "directory": "A"})
            evidence = _evidence(
                source_hash=lint["source_hash"],
                data_hash=lint["data_hash"],
            )
            evidence["solutions"] = evidence["solutions"][:1]
            domain_cases = ["sample/1", "secret/1"]
            evidence["solutions"][0]["expectation"]["executed_cases"] = domain_cases
            evidence["solutions"][0]["execution_domain"]["executed_cases"] = domain_cases
            write_calibration_evidence(
                problem,
                evidence,
            )
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "never-built")
            self.assertEqual(status["calibration"]["state"], "current")

    def test_build_publishes_evidence_only_when_staged_evidence_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged = root / "staged"
            live = root / "live"
            write_calibration_evidence(staged, {"value": "new"})
            write_calibration_evidence(live, {"value": "old"})
            _publish_calibration_evidence(staged, live)
            self.assertEqual(read_calibration_evidence(live)["value"], "new")

            missing = root / "missing"
            _publish_calibration_evidence(missing, live)
            self.assertEqual(read_calibration_evidence(live)["value"], "new")

    def test_newer_live_evidence_is_not_overwritten_by_older_build_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged = root / "staged"
            live = root / "live"
            write_calibration_evidence(
                staged, {"published_at": "2026-01-01T00:00:00+00:00", "value": "old"}
            )
            write_calibration_evidence(
                live, {"published_at": "2026-01-02T00:00:00+00:00", "value": "new"}
            )
            result = _publish_calibration_evidence(staged, live)
            self.assertFalse(result["published"])
            self.assertEqual(result["reason"], "newer_live_evidence_preserved")
            self.assertEqual(read_calibration_evidence(live)["value"], "new")

    def test_old_workspace_gitignore_is_migrated_for_evidence_and_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text("*.exe\n", encoding="utf-8")
            _ensure_local_gitignore(root)
            content = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("**/.probhub/judge-evidence-v1.json", content)
            self.assertIn("**/.probhub/judge-evidence.lock", content)
            self.assertIn("**/.probhub/judge-evidence-v1.json.*.tmp", content)
            self.assertIn("**/.probhub/judge-evidence-v2.json", content)
            self.assertIn("**/.probhub/judge-evidence-v2.json.*.tmp", content)


class LocalJudgeCalibrationTests(unittest.TestCase):
    def test_solution_summary_tracks_max_time_and_cache_provenance(self):
        summary = LOCAL_JUDGE.build_solution_calibration(
            "std",
            [
                {"case": "sample/1", "time": 0.1, "cached": False},
                {"case": "secret/max", "time": 0.25, "cached": True},
            ],
            1.0,
            {"accepted_time_multiplier": 3.0},
            [],
        )
        self.assertEqual(summary["max_time"], 0.25)
        self.assertEqual(summary["max_time_case"], "secret/max")
        self.assertEqual(summary["time_limit_ratio"], 0.25)
        self.assertEqual(summary["headroom_factor"], 4.0)
        self.assertEqual(summary["fresh_cases"], 1)
        self.assertEqual(summary["cached_cases"], 1)
        self.assertTrue(summary["accepted_check"]["ok"])

    def test_mle_and_ole_margins_are_explicit_lower_bounds(self):
        mle = LOCAL_JUDGE._status_only_resource_kill(
            "MLE",
            {
                "case": "secret/mem",
                "groups": [],
                "memory": 260,
                "memory_enforced": True,
                "termination_reason": "memory_limit",
            },
            {"MLE": 256, "OLE": 64},
        )
        ole = LOCAL_JUDGE._status_only_resource_kill(
            "OLE",
            {
                "case": "secret/out",
                "groups": [],
                "output_bytes": 65 * 1024 * 1024,
                "termination_reason": "output_limit",
            },
            {"MLE": 256, "OLE": 64},
        )
        self.assertEqual(mle["evidence_kind"], "lower_bound")
        self.assertGreater(mle["limit_ratio"], 1)
        self.assertEqual(ole["evidence_kind"], "lower_bound")
        self.assertGreater(ole["limit_ratio"], 1)

        inferred = LOCAL_JUDGE._status_only_resource_kill(
            "MLE",
            {
                "case": "secret/inferred",
                "groups": [],
                "memory": 255,
                "memory_enforced": True,
                "termination_reason": "inferred_memory_limit",
            },
            {"MLE": 256, "OLE": 64},
        )
        self.assertFalse(inferred["supported"])
        self.assertEqual(inferred["evidence_kind"], "inferred")

    def test_interactive_result_records_solution_output_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            stderr = Path(temp) / "solution.stderr"
            stderr.write_bytes(b"12345")
            transcript = {"entries": [], "truncated": False}
            result = LOCAL_JUDGE._interactive_result(
                "OLE",
                0.1,
                1,
                True,
                "output limit exceeded",
                transcript,
                traffic={"solution_to_interactor": 7},
                solution_stderr=str(stderr),
                termination_reason="output_limit",
            )
            self.assertEqual(result[-1]["output_bytes"], 12)
            self.assertEqual(result[-1]["termination_reason"], "output_limit")

    def test_tle_probe_distinguishes_exact_and_censored_measurements(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "solution.exe"
            binary.write_bytes(b"binary")
            input_path = root / "input.in"
            input_path.write_text("1\n", encoding="utf-8")

            def completed(_command, **kwargs):
                Path(kwargs["stdout_path"]).write_bytes(b"")
                Path(kwargs["stderr_path"]).write_bytes(b"")
                return {
                    "reason": "completed",
                    "message": "",
                    "returncode": 0,
                    "time": 1.2,
                    "memory": 1,
                    "memory_enforced": True,
                    "output_bytes": 0,
                }

            with patch.object(LOCAL_JUDGE, "run_managed_to_files", side_effect=completed):
                exact = LOCAL_JUDGE.probe_tle_margin(
                    str(binary), str(input_path), "secret/slow", "fingerprint",
                    1, 256, 64, 32, 2, cache=None,
                )
            self.assertEqual(exact["process_status"], "completed")
            self.assertEqual(exact["evidence_kind"], "exact")
            self.assertEqual(exact["limit_ratio"], 1.2)

            def timed_out(_command, **kwargs):
                Path(kwargs["stdout_path"]).write_bytes(b"")
                Path(kwargs["stderr_path"]).write_bytes(b"")
                return {
                    "reason": "time_limit",
                    "message": "time limit exceeded",
                    "returncode": -1,
                    "time": 2.0,
                    "memory": 1,
                    "memory_enforced": True,
                    "output_bytes": 0,
                }

            with patch.object(LOCAL_JUDGE, "run_managed_to_files", side_effect=timed_out):
                lower_bound = LOCAL_JUDGE.probe_tle_margin(
                    str(binary), str(input_path), "secret/slow", "fingerprint",
                    1, 256, 64, 32, 2, cache=None,
                )
            self.assertEqual(lower_bound["process_status"], "TLE")
            self.assertEqual(lower_bound["evidence_kind"], "lower_bound")
            self.assertEqual(lower_bound["limit_ratio"], 2.0)


if __name__ == "__main__":
    unittest.main()
