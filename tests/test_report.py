import io
import json
import platform
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from probhub.calibration import build_judge_evidence, write_calibration_evidence
from probhub.cli import main as cli_main
from probhub.io import write_yaml
from probhub.linting import compute_data_hash, compute_source_hash, lint_workspace
from probhub.reporting import (
    _risk_profile,
    build_workspace_report,
    render_markdown_report,
    render_text_report,
)
from probhub.workspace import load_problem, load_workspace


class WorkspaceReportTests(unittest.TestCase):
    def create_workspace(self, root):
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "contest": {"title": "Report Contest", "subtitle": "Final"},
            "problems": [{"id": "A", "directory": "A"}],
        })
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# Report Problem\n\n"
            "## 题目描述\n\n求值。\n\n"
            "## 输入格式\n\n第一行输入测试用例数 $T$。每组输入整数 $n$（$1\\le n\\le 100$）。\n\n"
            "所有测试用例中的 $n$ 之和不超过 $100$。\n\n"
            "## 输出格式\n\n输出 $n$。\n",
            encoding="utf-8",
        )
        (problem / "code/validator.cpp").write_text(
            'int main(){ int T = inf.readInt(1, 10, "T"); int n = inf.readInt(1, 100, "n"); '
            'long long sum_n = 0; sum_n += n; ensuref(sum_n <= 100, "sum"); }\n',
            encoding="utf-8",
        )
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "code/wrong.cpp").write_text("int main(){}\n", encoding="utf-8")
        cases = {
            "sample/1": "1\n",
            "secret/random01": "7\n",
            "secret/random02": "8\n",
            "secret/killer01": "42\n",
        }
        for name, payload in cases.items():
            suite, stem = name.split("/", 1)
            (problem / f"data/{suite}/{stem}.in").write_text(payload, encoding="utf-8")
            (problem / f"data/{suite}/{stem}.ans").write_text(payload, encoding="utf-8")
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "Report Problem",
            "display_name": "Report Problem",
            "difficulty": 3,
            "tags": ["math", "implementation"],
            "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {
                "accepted": [{
                    "file": "code/std.cpp",
                    "expected": {"status": "AC", "all": True},
                }],
                "brute": [],
                "wrong": [{
                    "file": "code/wrong.cpp",
                    "expected": {"status": "WA", "groups": ["killer"]},
                }],
            },
            "data": {
                "sample_dir": "data/sample",
                "secret_dir": "data/secret",
                "groups": [
                    {
                        "name": "random",
                        "role": "random",
                        "patterns": ["secret/random*"],
                    },
                    {
                        "name": "killer",
                        "role": "wrong-solution-killer",
                        "patterns": ["secret/killer*"],
                        "targets": ["code/wrong.cpp"],
                    },
                ],
                "recipes": [
                    {"case": "random01", "manual": True},
                    {"case": "random02", "manual": True},
                    {"case": "killer01", "manual": True},
                ],
            },
        }
        write_yaml(problem / "probhub.yaml", config)
        return problem

    def publish_evidence(self, root, problem):
        _, workspace = load_workspace(root)
        _, config = load_problem(root, {"id": "A", "directory": "A"})
        lint = lint_workspace(root, workspace)["problems"][0]
        cases = ["sample/1", "secret/killer01", "secret/random01", "secret/random02"]
        events = [
            {
                "type": "limits",
                "platform": platform.system(),
                "machine": platform.machine(),
                "compiler": "fixture",
                "time_limit": 1,
                "memory_limit": 256,
                "output_limit": 64,
                "process_limit": 32,
                "calibration_policy": {
                    "accepted_time_multiplier": 3.0,
                    "expected_tle_time_multiplier": 1.5,
                },
            },
            {"type": "cache", "mode": "enabled", "case_hits": 0, "case_misses": 8},
            {
                "type": "summary",
                "kind": "std",
                "program": "code/std.cpp",
                "stats": {"AC": 4},
                "expectation": {
                    "ok": True,
                    "expected_statuses": ["AC"],
                    "run_on": [],
                    "executed_cases": cases,
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                    "matched_cases": cases,
                    "selected_cases": cases,
                },
                "run_on": [],
                "executed_cases": cases,
                "skipped_cases": [],
                "coverage_ok": True,
                "unexecuted_expected_cases": [],
                "calibration": {
                    "max_time": 0.2,
                    "max_time_case": "secret/random02",
                    "time_limit_ratio": 0.2,
                    "headroom_factor": 5.0,
                    "accepted_check": {"required_multiplier": 3.0, "ok": True},
                    "resource_kills": [],
                },
            },
            {
                "type": "summary",
                "kind": "wrong",
                "program": "code/wrong.cpp",
                "stats": {"AC": 3, "WA": 1},
                "expectation": {
                    "ok": True,
                    "expected_statuses": ["WA"],
                    "run_on": [],
                    "executed_cases": cases,
                    "skipped_cases": [],
                    "coverage_ok": True,
                    "unexecuted_expected_cases": [],
                    "matched_cases": ["secret/killer01"],
                    "selected_cases": ["secret/killer01"],
                },
                "run_on": [],
                "executed_cases": cases,
                "skipped_cases": [],
                "coverage_ok": True,
                "unexecuted_expected_cases": [],
                "calibration": {
                    "max_time": 0.01,
                    "max_time_case": "secret/killer01",
                    "time_limit_ratio": 0.01,
                    "headroom_factor": 100.0,
                    "accepted_check": {"required_multiplier": 3.0, "ok": True},
                    "resource_kills": [],
                },
            },
        ]
        evidence = build_judge_evidence(
            events,
            compute_source_hash(problem, config),
            compute_data_hash(problem, config),
            "2026-07-28T12:00:00Z",
        )
        self.assertEqual(lint["source_hash"], evidence["source_hash"])
        write_calibration_evidence(problem, evidence)

    def snapshot(self, root):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_report_summarizes_tests_groups_recipes_headroom_and_kills(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            self.publish_evidence(root, problem)
            _, workspace = load_workspace(root)

            report = build_workspace_report(root, workspace)

            self.assertTrue(report["ok"])
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["summary"]["secret_cases"], 3)
            item = report["problems"][0]
            self.assertEqual(item["label"], "A")
            self.assertEqual(item["difficulty"], 3)
            self.assertEqual(item["tags"], ["math", "implementation"])
            self.assertEqual(item["groups"][0]["secret_cases"], 2)
            self.assertAlmostEqual(item["groups"][0]["secret_ratio"], 2 / 3, places=6)
            self.assertEqual(item["recipes"]["coverage_ratio"], 1.0)
            self.assertEqual(item["recipes"]["random"], 2)
            self.assertEqual(item["recipes"]["targeted"], 1)
            self.assertEqual(item["calibration"]["primary_headroom"], 5.0)
            self.assertEqual(item["risk"]["risk_level"], "high")
            self.assertEqual(item["risk"]["delivery_state"], "review")
            self.assertFalse(item["risk"]["verification_complete"])
            self.assertEqual(report["summary"]["risk_levels"], {"high": 1})
            self.assertEqual(report["summary"]["verification_incomplete"], 1)
            self.assertEqual(report["summary"]["delivery_states"], {"review": 1})
            self.assertEqual(item["aggregate_constraints"]["state"], "matched")
            self.assertEqual(item["aggregate_constraints"]["summary"]["matched"], 1)
            row = item["kill_matrix"]["rows"][0]
            self.assertEqual(row["cells"]["killer"]["state"], "killed")
            self.assertEqual(row["cells"]["random"]["state"], "not-targeted")
            codes = {diagnostic["code"] for diagnostic in item["diagnostics"]}
            self.assertIn("report_random_ratio_high", codes)
            self.assertIn("report_near_boundary_recipe_missing", codes)

    def test_risk_profile_distinguishes_ready_from_incomplete_evidence(self):
        ready = _risk_profile({
            "lint": {"errors": []},
            "diagnostics": [],
            "difficulty": 3,
            "aggregate_constraints": {
                "analysis_state": "complete",
                "multi_case_detected": False,
                "summary": {"statement_only": 0, "validator_only": 0, "dynamic": 0},
            },
            "calibration": {"state": "current"},
            "recipes": {"coverage_ratio": 1.0},
            "judge_qa": {"manual_review_probes": 0},
        })
        self.assertEqual(ready["risk_level"], "low")
        self.assertEqual(ready["delivery_state"], "ready")
        self.assertTrue(ready["verification_complete"])

        incomplete = _risk_profile({
            "lint": {"errors": []},
            "diagnostics": [{
                "code": "calibration_evidence_missing",
                "severity": "warning",
            }],
            "difficulty": 5,
            "aggregate_constraints": {
                "analysis_state": "partial",
                "multi_case_detected": True,
                "summary": {"statement_only": 1, "validator_only": 0, "dynamic": 0},
            },
            "calibration": {"state": "missing"},
            "recipes": {"coverage_ratio": 0.5},
            "judge_qa": {"manual_review_probes": 0},
        })
        self.assertEqual(incomplete["risk_level"], "high")
        self.assertEqual(incomplete["delivery_state"], "review")
        self.assertFalse(incomplete["verification_complete"])

    def test_risk_profile_blocks_error_diagnostics_even_without_lint_errors(self):
        profile = _risk_profile({
            "lint": {"errors": []},
            "diagnostics": [{
                "code": "report_internal_error",
                "severity": "error",
            }],
            "difficulty": 3,
            "aggregate_constraints": {
                "analysis_state": "complete",
                "multi_case_detected": False,
                "summary": {"statement_only": 0, "validator_only": 0, "dynamic": 0},
            },
            "calibration": {"state": "current"},
            "recipes": {"coverage_ratio": 1.0},
            "judge_qa": {"manual_review_probes": 0},
        })
        self.assertEqual(profile["risk_level"], "blocked")
        self.assertEqual(profile["delivery_state"], "blocked")
        self.assertFalse(profile["verification_complete"])
        self.assertIn("diagnostic_errors", {item["code"] for item in profile["signals"]})

    def test_report_accepts_precomputed_lint_result_without_relinting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            _, workspace = load_workspace(root)
            lint = lint_workspace(root, workspace)
            with mock.patch("probhub.reporting.lint_workspace", side_effect=AssertionError("unexpected relint")):
                report = build_workspace_report(root, workspace, lint_result=lint)
            self.assertEqual(report["ok"], lint["ok"])

    def test_report_is_byte_for_byte_read_only_and_cli_supports_all_formats(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            self.publish_evidence(root, problem)
            before = self.snapshot(root)

            outputs = {}
            for arguments, name in (
                (["--workspace", str(root), "report"], "text"),
                (["--workspace", str(root), "report", "A", "--format", "markdown"], "markdown"),
                (["--workspace", str(root), "--json", "report"], "json"),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli_main(arguments)
                self.assertEqual(code, 0, output.getvalue())
                outputs[name] = output.getvalue()

            self.assertIn("ProbHub 工作区报告", outputs["text"])
            self.assertIn("verification-incomplete=1/1", outputs["text"])
            self.assertIn("击杀矩阵", outputs["text"])
            self.assertIn("near-boundary=0/3", outputs["text"])
            self.assertIn("累计约束: matched", outputs["text"])
            self.assertIn("# ProbHub 工作区报告", outputs["markdown"])
            self.assertIn("| 题号 | ID |", outputs["markdown"])
            self.assertIn("near-boundary 0/3", outputs["markdown"])
            self.assertIn("累计约束：matched", outputs["markdown"])
            json_report = json.loads(outputs["json"])
            self.assertEqual(json_report["schema_version"], 1)
            self.assertEqual(
                json_report["problems"][0]["aggregate_constraints"]["state"],
                "matched",
            )
            self.assertEqual(self.snapshot(root), before)

    def test_verify_alias_uses_the_same_read_only_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            report_output = io.StringIO()
            verify_output = io.StringIO()
            with redirect_stdout(report_output):
                report_code = cli_main(["--workspace", str(root), "report"])
            with redirect_stdout(verify_output):
                verify_code = cli_main(["--workspace", str(root), "verify"])
            self.assertEqual(report_code, 0, report_output.getvalue())
            self.assertEqual(verify_code, 0, verify_output.getvalue())
            self.assertEqual(report_output.getvalue(), verify_output.getvalue())

    def test_missing_recipes_and_targeted_groups_are_structured_warnings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            config_path = problem / "probhub.yaml"
            import yaml

            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["data"].pop("recipes")
            config["data"]["groups"] = []
            write_yaml(config_path, config)
            _, workspace = load_workspace(root)

            report = build_workspace_report(root, workspace)
            item = report["problems"][0]
            codes = {diagnostic["code"] for diagnostic in item["diagnostics"]}
            self.assertIn("report_recipe_missing", codes)
            self.assertIn("report_targeted_data_missing", codes)
            self.assertEqual(item["kill_matrix"]["columns"], ["killer"])
            self.assertEqual(item["kill_matrix"]["rows"][0]["cells"]["killer"]["state"], "unknown")

    def test_expected_groups_count_as_targeted_data_without_group_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            config_path = problem / "probhub.yaml"
            import yaml

            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["data"]["groups"][1].pop("targets")
            write_yaml(config_path, config)
            _, workspace = load_workspace(root)

            item = build_workspace_report(root, workspace)["problems"][0]

            self.assertEqual(item["targeted_secret_cases"], 1)
            self.assertEqual(item["recipes"]["targeted"], 1)
            codes = {diagnostic["code"] for diagnostic in item["diagnostics"]}
            self.assertNotIn("report_targeted_data_missing", codes)

    def test_renderers_do_not_mutate_the_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            _, workspace = load_workspace(root)
            report = build_workspace_report(root, workspace)
            before = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertIn("Report Contest", render_text_report(report))
            self.assertIn("Report Contest", render_markdown_report(report))
            self.assertEqual(json.dumps(report, ensure_ascii=False, sort_keys=True), before)

    def test_report_refuses_pending_publish_transaction_without_recovery_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            transaction = root / ".probhub/build-publish-fixture"
            transaction.mkdir(parents=True)
            marker = transaction / "journal.json"
            marker.write_text("interrupted", encoding="utf-8")
            before = self.snapshot(root)
            output = io.StringIO()

            with redirect_stdout(output):
                code = cli_main(["--workspace", str(root), "--json", "report"])

            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["code"], "recovery_required")
            self.assertEqual(self.snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
