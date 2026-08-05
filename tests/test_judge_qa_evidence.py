import copy
import json
import platform
import tempfile
import unittest
from pathlib import Path

from probhub import __version__
from probhub.calibration import SANDBOX_CACHE_SCHEMA_VERSION
from probhub.io import atomic_write_json
from probhub.judge_qa import evaluate_judge_qa_evidence, inspect_judge_qa
from probhub.judge_qa_evidence import (
    JUDGE_QA_EVIDENCE_SCHEMA_VERSION,
    JUDGE_QA_POLICY_VERSION,
    MAX_JUDGE_QA_EVIDENCE_BYTES,
    evidence_path,
)
from probhub.linting import (
    compute_data_hash,
    compute_source_hash,
    lint_workspace,
    problem_status,
)
from probhub.reporting import (
    build_workspace_report,
    render_markdown_report,
    render_text_report,
)
from probhub.workspace import load_problem, load_workspace, problem_entries
from tests.fixture_support import copy_workspace_fixture


class JudgeQAEvidenceTests(unittest.TestCase):
    def copy_fixture(self, name="checker-qa"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = copy_workspace_fixture(name, temporary.name)
        root, workspace = load_workspace(fixture.root)
        entry = problem_entries(workspace)[0]
        problem, config = load_problem(root, entry)
        inspection = inspect_judge_qa(problem, config)
        return fixture, root, workspace, entry, problem, config, inspection

    @staticmethod
    def valid_evidence(problem, config, inspection):
        judge = config["judge"]
        compilers = [
            {
                "role": "validator",
                "source": judge["validator"],
                "kind": "cpp17",
                "compiler_identity": "fixture-compiler",
            },
            {
                "role": "checker" if inspection["judge_type"] == "custom" else "interactor",
                "source": judge[
                    "checker" if inspection["judge_type"] == "custom" else "interactor"
                ],
                "kind": "cpp17",
                "compiler_identity": "fixture-compiler",
            },
        ]
        contestant_sources = sorted({
            (case.get("contestant") or {}).get("source")
            for case in inspection["cases"]
            if (case.get("contestant") or {}).get("source")
        })
        for source in contestant_sources:
            compilers.append({
                "role": "contestant",
                "source": source,
                "kind": "python" if source.endswith(".py") else "cpp17",
                **(
                    {"compiler_identity": "fixture-compiler"}
                    if source.endswith(".cpp")
                    else {}
                ),
            })
        validators = [
            {
                "input": value,
                "ok": True,
                "diagnostic": {"present": False, "bytes": 0, "truncated": False},
            }
            for value in sorted({case["input"] for case in inspection["cases"]})
        ]
        cases = []
        for case in inspection["cases"]:
            expected = dict(case["expected"])
            cases.append({
                "id": case["id"],
                "purpose": case["purpose"],
                "expected": expected,
                "actual": {
                    **expected,
                    "actor": "session",
                    "execution_status": "completed",
                },
                "matched": True,
                "infrastructure_failed": False,
                "diagnostic": {"present": True, "bytes": 7, "truncated": False},
            })
        probes = [
            {
                "id": probe,
                "purpose": "robustness-probe",
                "expected": {},
                "actual": {"status": "WA"},
                "matched": True,
                "infrastructure_failed": False,
                "manual_review_required": False,
                "diagnostic": {"present": True, "bytes": 7, "truncated": False},
            }
            for probe in (inspection.get("robustness") or {}).get("probes") or []
        ]
        return {
            "schema_version": JUDGE_QA_EVIDENCE_SCHEMA_VERSION,
            "policy_version": JUDGE_QA_POLICY_VERSION,
            "sandbox_schema_version": SANDBOX_CACHE_SCHEMA_VERSION,
            "probhub_version": __version__,
            "source_hash": compute_source_hash(problem, config),
            "data_hash": compute_data_hash(problem, config),
            "fixture_hash": inspection["fixture_hash"],
            "published_at": "2026-08-05T00:00:00+00:00",
            "measurement": {
                "platform": platform.system(),
                "machine": platform.machine(),
                "target_guarantee": False,
            },
            "limits": {},
            "cache": {
                "mode": "normal",
                "compile_hits": 0,
                "compile_misses": len(compilers),
                "write_errors": [],
            },
            "compilers": compilers,
            "validators": validators,
            "cases": cases,
            "probes": probes,
            "cleanup": {"ok": True, "snapshot_removed": True},
        }

    def evaluate(self, problem, config, inspection):
        return evaluate_judge_qa_evidence(
            problem,
            config,
            inspection,
            compute_source_hash(problem, config),
            compute_data_hash(problem, config),
        )

    def write_valid(self, problem, config, inspection):
        evidence = self.valid_evidence(problem, config, inspection)
        atomic_write_json(evidence_path(problem), evidence)
        return evidence

    def test_not_configured_ignores_orphan_evidence(self):
        _, _, _, _, problem, config, inspection = self.copy_fixture("custom")
        evidence_path(problem).parent.mkdir(parents=True, exist_ok=True)
        evidence_path(problem).write_text("not-json", encoding="utf-8")

        result = self.evaluate(problem, config, inspection)

        self.assertEqual(result["state"], "not-configured", result)
        self.assertEqual(result["diagnostics"], [])

    def test_missing_and_current_states_are_non_blocking_lint_results(self):
        _, root, workspace, _, problem, config, inspection = self.copy_fixture()
        missing = lint_workspace(root, workspace)
        self.assertTrue(missing["ok"], missing)
        qa = missing["problems"][0]["judge_qa"]["evidence"]
        self.assertEqual(qa["state"], "missing", qa)
        self.assertIn("judge_qa_evidence_missing", {
            item["code"] for item in qa["diagnostics"]
        })

        self.write_valid(problem, config, inspection)
        current = lint_workspace(root, workspace)
        qa = current["problems"][0]["judge_qa"]["evidence"]
        self.assertTrue(current["ok"], current)
        self.assertEqual(qa["state"], "current", qa)
        self.assertEqual(qa["matched_cases"], qa["declared_cases"])

    def test_stale_reasons_have_stable_precedence(self):
        _, _, _, _, problem, config, inspection = self.copy_fixture()
        base = self.valid_evidence(problem, config, inspection)
        mutations = {
            "schema": lambda item: item.update(schema_version=999),
            "policy": lambda item: item.update(policy_version=999),
            "sandbox": lambda item: item.update(sandbox_schema_version=999),
            "core": lambda item: item.update(probhub_version="0.0.0"),
            "inputs": lambda item: item.update(source_hash="changed"),
            "platform": lambda item: item["measurement"].update(platform="Other"),
        }
        for reason, mutate in mutations.items():
            with self.subTest(reason=reason):
                evidence = copy.deepcopy(base)
                mutate(evidence)
                evidence["cases"] = []
                atomic_write_json(evidence_path(problem), evidence)
                result = self.evaluate(problem, config, inspection)
                self.assertEqual(result["state"], "stale", result)
                self.assertEqual(result["reason"], reason, result)

    def test_invalid_file_states_are_distinct_from_missing(self):
        _, _, _, _, problem, config, inspection = self.copy_fixture()
        path = evidence_path(problem)
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = (
            (b"{", "invalid_json"),
            (b"[]", "invalid_json"),
            (b"x" * (MAX_JUDGE_QA_EVIDENCE_BYTES + 1), "too_large"),
        )
        for payload, reason in cases:
            with self.subTest(reason=reason):
                path.write_bytes(payload)
                result = self.evaluate(problem, config, inspection)
                self.assertEqual(result["state"], "invalid", result)
                self.assertEqual(result["reason"], reason, result)

    def test_structure_failures_cannot_be_reported_as_current(self):
        _, _, _, _, problem, config, inspection = self.copy_fixture()
        base = self.valid_evidence(problem, config, inspection)

        def add_stream(item):
            item["cases"][0]["stdout"] = "forbidden"

        mutations = (
            lambda item: item.update(cleanup={"ok": False, "snapshot_removed": True}),
            lambda item: item["measurement"].update(target_guarantee=True),
            lambda item: item["cases"].pop(),
            lambda item: item["cases"][0].update(matched=False),
            lambda item: item["validators"].pop(),
            lambda item: item["compilers"].pop(),
            lambda item: item["probes"].pop(),
            add_stream,
            lambda item: item["cases"][0].update(message="feedback-secret"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                evidence = copy.deepcopy(base)
                mutate(evidence)
                atomic_write_json(evidence_path(problem), evidence)
                result = self.evaluate(problem, config, inspection)
                self.assertEqual(result["state"], "invalid", result)
                self.assertEqual(result["reason"], "structure", result)

    def test_probe_ac_is_current_but_requires_manual_review(self):
        _, _, _, _, problem, config, inspection = self.copy_fixture()
        evidence = self.valid_evidence(problem, config, inspection)
        evidence["probes"][0]["actual"]["status"] = "AC"
        evidence["probes"][0]["manual_review_required"] = True
        atomic_write_json(evidence_path(problem), evidence)

        result = self.evaluate(problem, config, inspection)

        self.assertEqual(result["state"], "current", result)
        self.assertEqual(result["manual_review_probes"], 1)
        self.assertIn("judge_qa_probe_manual_review_required", {
            item["code"] for item in result["diagnostics"]
        })

    def test_status_exposes_qa_without_changing_formal_state(self):
        _, root, workspace, entry, problem, config, inspection = self.copy_fixture()
        self.write_valid(problem, config, inspection)

        status = problem_status(problem, config, root, workspace)

        self.assertEqual(status["state"], "never-built", status)
        self.assertEqual(status["judge_qa"]["evidence"]["state"], "current")

    def test_report_exposes_only_bounded_current_qa_summaries(self):
        _, root, workspace, _, problem, config, inspection = self.copy_fixture()
        evidence = self.write_valid(problem, config, inspection)
        evidence["cases"][0]["resources"] = {"sentinel_secret": "hidden"}
        atomic_write_json(evidence_path(problem), evidence)

        report = build_workspace_report(root, workspace)
        item = report["problems"][0]["judge_qa"]
        text = render_text_report(report)
        markdown = render_markdown_report(report)

        self.assertEqual(report["summary"]["judge_qa_states"], {"current": 1})
        self.assertEqual(item["state"], "current", item)
        self.assertEqual(item["matched_cases"], item["declared_cases"])
        self.assertNotIn("resources", item["cases"][0])
        self.assertNotIn("sentinel_secret", json.dumps(report))
        self.assertIn("QA current", text)
        self.assertIn("### Judge QA", markdown)

        evidence["source_hash"] = "stale"
        atomic_write_json(evidence_path(problem), evidence)
        stale = build_workspace_report(root, workspace)
        stale_item = stale["problems"][0]["judge_qa"]
        self.assertEqual(stale_item["state"], "stale")
        self.assertEqual(stale_item["cases"], [])
        self.assertNotIn("stale-result-must-stay-hidden", json.dumps(stale))


if __name__ == "__main__":
    unittest.main()
