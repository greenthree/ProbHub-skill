import hashlib
import shutil
import tempfile
import time
import unittest
import io
import json
import os
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from probhub.cli import main as cli_main
from probhub.build_lock import workspace_build_lock, workspace_file_lock
from probhub.errors import ProbHubError
from probhub.mutation import (
    MAX_HIT_CASES,
    MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION,
    MUTATION_LOCK_FILENAME,
    MUTATION_EVIDENCE_SCHEMA_VERSION,
    MUTATION_JUDGE_MEMORY_BASE_MB,
    MUTATION_JUDGE_PROCESS_HEADROOM,
    MUTATION_JUDGE_PROCESS_BASE,
    MUTATION_SCHEDULER,
    MUTATION_SNAPSHOT_MODEL,
    MUTATION_OPERATOR_VERSION,
    _classify_judge_result,
    mutation_evidence_path,
    mutation_evidence_profile,
    mutation_test_problem,
    plan_mutations,
)
from probhub.io import read_yaml, write_yaml
from probhub.judging import judge_problem
from probhub.process_control import ProcessCancelled
from probhub.problem_snapshot import copy_problem_consistently
from probhub.reporting import (
    build_workspace_report,
    render_markdown_report,
    render_text_report,
)
from probhub.workspace import load_workspace


FIXTURE = Path(__file__).parent / "fixtures/workspaces/mutation/M01"


class MutationPlanningTests(unittest.TestCase):
    def test_masked_literals_and_comments_are_never_mutated(self):
        source = (
            '// if (x < 3)\n'
            'const char* text = "x < 3 && n == 0";\n'
            'int f(int n) { return n < 3 ? 1 : 0; }\n'
        )
        plan = plan_mutations(source)
        self.assertTrue(plan["mutations"])
        self.assertTrue(all(item.line == 3 for item in plan["mutations"]), plan)
        comparisons = [item for item in plan["mutations"] if item.operator == "comparison-boundary"]
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0].line, 3)
        self.assertEqual(comparisons[0].original, "<")

    def test_plan_is_deterministic_and_respects_limit(self):
        source = "int f(int x) { return x <= 3 ? 1 : 0; }\n"
        first = plan_mutations(source, max_mutants=2)
        second = plan_mutations(source, max_mutants=2)
        self.assertEqual(first, second)
        self.assertEqual(len(first["mutations"]), 2)
        self.assertTrue(first["truncated"])
        self.assertTrue(all(item.id.startswith(MUTATION_OPERATOR_VERSION + ":") for item in first["mutations"]))

    def test_serialized_mutation_text_is_bounded_without_changing_application(self):
        condition = " || ".join(f"flag_{index}" for index in range(300))
        source = f"int f() {{ if ({condition}) return 1; return 0; }}\n"
        plan = plan_mutations(source, operators=["boolean-negation"], max_mutants=1)
        mutation = plan["mutations"][0]
        record = mutation.as_dict()
        self.assertLessEqual(len(record["original"].encode("utf-8")), 512)
        self.assertLessEqual(len(record["replacement"].encode("utf-8")), 512)
        self.assertGreater(len(mutation.replacement.encode("utf-8")), 512)

    def test_exclusions_are_applied_before_limit_and_affect_plan_hash(self):
        source = "int f(int x) { if (x < 3) return x <= 4; return 0; }\n"
        baseline = plan_mutations(
            source,
            operators=["comparison-boundary"],
            max_mutants=2,
        )
        excluded_id = baseline["mutations"][0].id
        unmatched_id = "cpp-token-v1:comparison-boundary:99:1:0000000000000000"
        exclusions = [
            {"id": excluded_id, "reason": "equivalent after range validation"},
            {"id": unmatched_id, "reason": "kept to document an old review"},
        ]

        filtered = plan_mutations(
            source,
            operators=["comparison-boundary"],
            max_mutants=1,
            exclusions=exclusions,
        )

        self.assertEqual(filtered["raw_planned"], 2)
        self.assertEqual(filtered["excluded"], 1)
        self.assertEqual(filtered["planned"], 1)
        self.assertEqual(filtered["selected"], 1)
        self.assertFalse(filtered["truncated"])
        self.assertEqual(filtered["mutations"][0].id, baseline["mutations"][1].id)
        self.assertEqual(
            [item["status"] for item in filtered["exclusions"]],
            ["matched", "unmatched"],
        )

        changed_reason = plan_mutations(
            source,
            operators=["comparison-boundary"],
            max_mutants=1,
            exclusions=[
                {**exclusions[0], "reason": "different review reason"},
                exclusions[1],
            ],
        )
        self.assertNotEqual(filtered["plan_hash"], changed_reason["plan_hash"])

    def test_partial_operator_plan_distinguishes_out_of_scope_from_unmatched(self):
        source = "int f(int x) { if (x < 3) return 1; return 0; }\n"
        full = plan_mutations(source)
        comparison_id = next(
            item.id for item in full["mutations"]
            if item.operator == "comparison-boundary"
        )
        unmatched_id = "cpp-token-v1:comparison-boundary:99:1:0000000000000000"

        partial = plan_mutations(
            source,
            operators=["boolean-negation"],
            exclusions=[
                {"id": comparison_id, "reason": "reviewed comparison mutation"},
                {"id": unmatched_id, "reason": "removed historical mutation"},
            ],
        )

        self.assertEqual(partial["excluded"], 0)
        self.assertEqual(
            [item["status"] for item in partial["exclusions"]],
            ["out-of-scope", "unmatched"],
        )

    def test_classification_keeps_compile_and_infrastructure_out_of_kills(self):
        compile_invalid = _classify_judge_result(
            {
                "ok": False,
                "final": {"code": "accepted_compile_failed"},
                "events": [{"type": "compile", "kind": "std", "ok": False}],
            },
            "code/mutation.cpp",
        )
        self.assertEqual(compile_invalid[0], "compile-invalid")
        infrastructure = _classify_judge_result(
            {
                "ok": False,
                "final": {"code": "judge_failed"},
                "events": [{"type": "case", "program": "code/mutation.cpp", "status": "FAIL"}],
            },
            "code/mutation.cpp",
        )
        self.assertEqual(infrastructure[0], "infrastructure-failed")
        for code in ("judge_memory_limit", "judge_process_limit"):
            resource_failure = _classify_judge_result(
                {
                    "ok": False,
                    "final": {"code": code},
                    "events": [{
                        "type": "case",
                        "program": "code/mutation.cpp",
                        "case": "secret/boundary",
                        "status": "WA",
                    }],
                },
                "code/mutation.cpp",
            )
            self.assertEqual(resource_failure[0], "infrastructure-failed")
            self.assertEqual(resource_failure[2], code)


class MutationFixtureTests(unittest.TestCase):
    def _cleanup_evidence(self, problem):
        evidence = mutation_evidence_path(problem)
        evidence.unlink(missing_ok=True)
        lock = problem / ".probhub" / "mutation.lock"
        lock.unlink(missing_ok=True)
        try:
            (problem / ".probhub").rmdir()
        except OSError:
            pass

    def test_weak_data_survivor_is_killed_after_fixture_strengthening(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            before = (problem / "code/std.cpp").read_bytes()
            baseline = judge_problem(root, problem, use_cache=False)
            self.assertTrue(baseline["ok"], baseline)
            weak = mutation_test_problem(
                root,
                problem,
                use_cache=False,
                operators=["comparison-boundary"],
                max_mutants=64,
            )
            self.assertTrue(weak["ok"], weak)
            weak_boundary = next(
                item for item in weak["evidence"]["mutations"]
                if item["operator"] == "comparison-boundary"
                and item["original"] == "<"
                and item["replacement"] == "<="
            )
            self.assertEqual(weak_boundary["classification"], "survived")
            self.assertGreater(weak["evidence"]["summary"]["survived"], 0)
            self.assertEqual((problem / "code/std.cpp").read_bytes(), before)
            secret = problem / "data/secret"
            (secret / "strong.in").write_text("3\n", encoding="utf-8")
            (secret / "strong.ans").write_text("3\n", encoding="utf-8")
            strong = mutation_test_problem(
                root,
                problem,
                use_cache=False,
                operators=["comparison-boundary"],
                max_mutants=64,
            )
            self.assertTrue(strong["ok"], strong)
            strong_boundary = next(
                item for item in strong["evidence"]["mutations"]
                if item["id"] == weak_boundary["id"]
            )
            self.assertEqual(strong_boundary["classification"], "killed")
            config = read_yaml(problem / "probhub.yaml")
            profile = mutation_evidence_profile(problem, config)
            self.assertEqual(profile["state"], "current")
            evidence_path = mutation_evidence_path(problem)
            original_evidence = evidence_path.read_bytes()
            evidence_payload = json.loads(original_evidence.decode("utf-8"))
            evidence_payload["schema_version"] = 2.0
            evidence_path.write_text(json.dumps(evidence_payload), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "invalid")
            evidence_payload = json.loads(original_evidence.decode("utf-8"))
            evidence_payload["mutations"][0]["line"] = True
            evidence_path.write_text(json.dumps(evidence_payload), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "invalid")
            evidence_payload = json.loads(original_evidence.decode("utf-8"))
            evidence_payload["builder_fingerprint"]["schema_version"] = True
            evidence_path.write_text(json.dumps(evidence_payload), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "invalid")
            evidence_payload = json.loads(original_evidence.decode("utf-8"))
            fingerprint = evidence_payload["builder_fingerprint"]
            fingerprint["probhub_version"] = "tampered-version"
            fingerprint_payload = {
                key: value
                for key, value in fingerprint.items()
                if key not in {"digest", "available"}
            }
            fingerprint["digest"] = hashlib.sha256(json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            evidence_path.write_text(json.dumps(evidence_payload), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "stale")
            evidence_payload = json.loads(original_evidence.decode("utf-8"))
            evidence_payload["plan_hash"] = "0" * 64
            evidence_path.write_text(json.dumps(evidence_payload), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "stale")

    def test_excluded_mutant_publishes_auditable_v2_evidence_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            config_path = problem / "probhub.yaml"
            config = read_yaml(config_path)
            source = (problem / "code/std.cpp").read_text(encoding="utf-8")
            mutation_id = plan_mutations(
                source,
                operators=["comparison-boundary"],
                max_mutants=64,
            )["mutations"][0].id
            reason = "equivalent under the validated non-negative input invariant"
            unmatched_id = "cpp-token-v1:comparison-boundary:99:1:0000000000000000"
            unmatched_reason = "historical review record for a removed branch"
            config["mutation"] = {
                "schema_version": 1,
                "exclusions": [
                    {"id": mutation_id, "reason": reason},
                    {"id": unmatched_id, "reason": unmatched_reason},
                ],
            }
            write_yaml(config_path, config)

            result = mutation_test_problem(
                root,
                problem,
                use_cache=False,
                operators=["comparison-boundary"],
                max_mutants=64,
            )

            self.assertTrue(result["ok"], result)
            evidence = result["evidence"]
            self.assertEqual(evidence["schema_version"], MUTATION_EVIDENCE_SCHEMA_VERSION)
            self.assertEqual(evidence["raw_planned"], 1)
            self.assertEqual(evidence["excluded"], 1)
            self.assertEqual(evidence["planned"], 0)
            self.assertEqual(evidence["selected"], 0)
            self.assertEqual(evidence["executed"], 0)
            self.assertEqual(evidence["exclusions"], [
                {"id": mutation_id, "reason": reason, "status": "matched"},
                {"id": unmatched_id, "reason": unmatched_reason, "status": "unmatched"},
            ])
            self.assertEqual(mutation_evidence_path(problem).name, "mutation-evidence-v2.json")

            current_config = read_yaml(config_path)
            profile = mutation_evidence_profile(problem, current_config)
            self.assertEqual(profile["state"], "current", profile)
            self.assertEqual(profile["planning"], {
                "raw": 1,
                "excluded": 1,
                "effective": 0,
                "selected": 0,
                "configured_exclusions": 2,
                "out_of_scope_exclusions": 0,
                "unmatched_exclusions": 1,
            })
            self.assertEqual(
                [item["code"] for item in profile["diagnostics"]],
                ["mutation_exclusion_unmatched"],
            )

            _, workspace = load_workspace(root)
            report = build_workspace_report(root, workspace)
            self.assertEqual(report["summary"]["mutation_raw"], 1)
            self.assertEqual(report["summary"]["mutation_excluded"], 1)
            self.assertEqual(report["summary"]["mutation_effective"], 0)
            self.assertEqual(report["summary"]["mutation_out_of_scope_exclusions"], 0)
            self.assertEqual(report["summary"]["mutation_unmatched_exclusions"], 1)
            self.assertIn(reason, render_markdown_report(report))
            self.assertIn(unmatched_reason, render_markdown_report(report))
            self.assertIn("raw=1 excluded=1 effective=0", render_text_report(report))

            evidence_path = mutation_evidence_path(problem)
            original_evidence = evidence_path.read_bytes()
            payload = json.loads(original_evidence.decode("utf-8"))
            payload["excluded"] = 0
            evidence_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                mutation_evidence_profile(problem, current_config)["state"],
                "invalid",
            )
            evidence_path.write_bytes(original_evidence)

            current_config["mutation"]["exclusions"][0]["reason"] = "changed reason"
            write_yaml(config_path, current_config)
            changed_reason_profile = mutation_evidence_profile(problem, current_config)
            self.assertEqual(changed_reason_profile["state"], "stale")
            self.assertEqual(changed_reason_profile["exclusions"][0]["reason"], "changed reason")

            source_path = problem / "code/std.cpp"
            source_path.write_text(
                "// shift stable IDs by one line\n"
                + source_path.read_text(encoding="utf-8")
                + "\nint mutation_helper(int value) { return value < 10; }\n",
                encoding="utf-8",
            )
            shifted_profile = mutation_evidence_profile(problem, current_config)
            self.assertEqual(shifted_profile["state"], "stale")
            self.assertIsNone(shifted_profile["status"])
            self.assertEqual(shifted_profile["summary"], {})
            self.assertEqual(shifted_profile["mutants"], 0)
            self.assertGreater(
                shifted_profile["planning"]["selected"],
                json.loads(original_evidence.decode("utf-8"))["selected"],
            )
            self.assertEqual(shifted_profile["exclusions"][0]["status"], "unmatched")
            self.assertEqual(
                [item["code"] for item in shifted_profile["diagnostics"]],
                ["mutation_exclusion_unmatched"],
            )
            _, workspace = load_workspace(root)
            shifted_report = build_workspace_report(root, workspace)
            self.assertEqual(shifted_report["problems"][0]["mutation"]["summary"], {})
            self.assertEqual(shifted_report["summary"]["mutation_killed"], 0)
            self.assertEqual(shifted_report["summary"]["mutation_survived"], 0)
            self.assertIn("unmatched", render_markdown_report(shifted_report))
            self.assertIn("unmatched; changed reason", render_text_report(shifted_report))

    def test_partial_operator_evidence_reports_other_valid_exclusion_out_of_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            config_path = problem / "probhub.yaml"
            config = read_yaml(config_path)
            source = (problem / "code/std.cpp").read_text(encoding="utf-8")
            comparison_id = next(
                item.id for item in plan_mutations(source)["mutations"]
                if item.operator == "comparison-boundary"
            )
            config["mutation"] = {
                "schema_version": 1,
                "exclusions": [{
                    "id": comparison_id,
                    "reason": "reviewed comparison mutation",
                }],
            }
            write_yaml(config_path, config)
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }

            with patch("probhub.mutation.judge_problem", return_value=passed):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["boolean-negation"],
                    max_mutants=1,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["evidence"]["exclusions"][0]["status"], "out-of-scope")
            profile = mutation_evidence_profile(problem, read_yaml(config_path))
            self.assertEqual(profile["state"], "current", profile)
            self.assertEqual(profile["planning"]["out_of_scope_exclusions"], 1)
            self.assertEqual(profile["planning"]["unmatched_exclusions"], 0)
            self.assertEqual(profile["diagnostics"], [])
            _, workspace = load_workspace(root)
            report = build_workspace_report(root, workspace)
            self.assertEqual(report["summary"]["mutation_out_of_scope_exclusions"], 1)
            self.assertIn("out-of-scope", render_markdown_report(report))
            self.assertIn("out-of-scope-exclusions=1", render_text_report(report))

    def test_execution_profile_is_strict_but_optional_for_legacy_v2_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            with patch("probhub.mutation.judge_problem", return_value=passed):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )

            self.assertTrue(result["ok"], result)
            config = read_yaml(problem / "probhub.yaml")
            evidence_path = mutation_evidence_path(problem)
            original = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "current")

            legacy = json.loads(json.dumps(original))
            legacy.pop("execution_profile")
            evidence_path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "current")

            changed_budget = json.loads(json.dumps(original))
            changed_budget["execution_profile"]["judge_memory_limit_mb"] += 1
            evidence_path.write_text(json.dumps(changed_budget), encoding="utf-8")
            self.assertEqual(mutation_evidence_profile(problem, config)["state"], "current")

            invalid_values = [
                {**original["execution_profile"], "schema_version": True},
                {**original["execution_profile"], "scheduler": "parallel-v1"},
                {**original["execution_profile"], "snapshot": "live-v1"},
                {**original["execution_profile"], "mutant_timeout_seconds": float("nan")},
                {**original["execution_profile"], "judge_output_limit_bytes": 0},
                {**original["execution_profile"], "judge_memory_limit_mb": True},
                {**original["execution_profile"], "judge_process_limit": -1},
                {**original["execution_profile"], "unknown": 1},
            ]
            for invalid in invalid_values:
                payload = json.loads(json.dumps(original))
                payload["execution_profile"] = invalid
                evidence_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(
                    mutation_evidence_profile(problem, config)["state"],
                    "invalid",
                    invalid,
                )

    def test_nested_accepted_source_keeps_its_relative_include_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            nested = problem / "code/solutions"
            nested.mkdir()
            (nested / "helper.h").write_text(
                "inline long long add_term(long long total, int value) { return total + value; }\n",
                encoding="utf-8",
            )
            (nested / "std.cpp").write_text(
                '#include <iostream>\n'
                '#include "helper.h"\n'
                'int main() {\n'
                '    int n;\n'
                '    if (!(std::cin >> n)) return 0;\n'
                '    long long answer = 0;\n'
                '    for (int i = 0; i < n; ++i) answer = add_term(answer, i);\n'
                "    std::cout << answer << '\\n';\n"
                '}\n',
                encoding="utf-8",
            )
            (problem / "code/std.cpp").unlink()
            config = read_yaml(problem / "probhub.yaml")
            config["solutions"]["accepted"] = ["code/solutions/std.cpp"]
            write_yaml(problem / "probhub.yaml", config)

            result = mutation_test_problem(
                root,
                problem,
                use_cache=False,
                operators=["comparison-boundary"],
                max_mutants=1,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["evidence"]["source_path"], "code/solutions/std.cpp")
            self.assertNotEqual(
                result["evidence"]["mutations"][0]["classification"],
                "compile-invalid",
            )

    def test_configured_source_path_survives_resolved_path_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            alias_source = root.parent / "resolved-alias.cpp"
            alias_source.write_bytes((problem / "code/std.cpp").read_bytes())
            workers = []

            def inspect_worker(_execution_root, worker, **_kwargs):
                worker = Path(worker)
                workers.append(worker)
                config = read_yaml(worker / "probhub.yaml")
                self.assertEqual(
                    config["solutions"]["accepted"][0]["file"],
                    "code/std.cpp",
                )
                self.assertTrue((worker / "code/std.cpp").is_file())
                return {
                    "ok": True,
                    "final": {"code": "all_expectations_met"},
                    "events": [],
                }

            with patch(
                "probhub.mutation.resolve_solution_source",
                return_value=(alias_source, None, None),
            ), patch(
                "probhub.mutation.judge_problem",
                side_effect=inspect_worker,
            ):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(len(workers), 1)
            self.assertEqual(result["evidence"]["source_path"], "code/std.cpp")

    def test_single_snapshot_releases_build_lock_and_isolates_mutant_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            (problem / "problem.pdf").write_bytes(b"generated pdf")
            (problem / "meta.json").write_text("{}", encoding="utf-8")
            (problem / "problem.yaml").write_text("name: generated\n", encoding="utf-8")
            (problem / "code/stale.exe").write_bytes(b"generated executable")
            (problem / ".probhub").mkdir()
            (problem / ".probhub/stale-cache.json").write_text("{}", encoding="utf-8")
            live_before = {
                path.relative_to(problem).as_posix(): path.read_bytes()
                for path in problem.rglob("*")
                if path.is_file()
            }
            calls = []
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }

            def inspect_worker(execution_root, worker, **kwargs):
                execution_root = Path(execution_root).resolve()
                worker = Path(worker).resolve()
                self.assertNotEqual(execution_root, root.resolve())
                self.assertNotEqual(worker, problem.resolve())
                self.assertEqual(worker.parent, execution_root)
                self.assertFalse((worker / "problem.pdf").exists())
                self.assertFalse((worker / "meta.json").exists())
                self.assertFalse((worker / "problem.yaml").exists())
                self.assertFalse((worker / "code/stale.exe").exists())
                self.assertFalse((worker / ".probhub/stale-cache.json").exists())
                self.assertFalse((worker / "worker-contamination").exists())
                with workspace_build_lock(root):
                    pass
                (worker / "worker-contamination").write_text(
                    str(len(calls)), encoding="utf-8"
                )
                calls.append((execution_root, worker, kwargs))
                return passed

            with patch(
                "probhub.mutation.copy_problem_consistently",
                wraps=copy_problem_consistently,
            ) as snapshot, patch(
                "probhub.mutation.judge_problem",
                side_effect=inspect_worker,
            ):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary", "boolean-negation"],
                    max_mutants=2,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(snapshot.call_count, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                [item["id"] for item in result["evidence"]["mutations"]],
                [item.id for item in plan_mutations(
                    (problem / "code/std.cpp").read_text(encoding="utf-8"),
                    operators=["comparison-boundary", "boolean-negation"],
                    max_mutants=2,
                )["mutations"]],
            )
            profile = result["evidence"]["execution_profile"]
            self.assertEqual(profile["schema_version"], MUTATION_EXECUTION_PROFILE_SCHEMA_VERSION)
            self.assertEqual(profile["scheduler"], MUTATION_SCHEDULER)
            self.assertEqual(profile["snapshot"], MUTATION_SNAPSHOT_MODEL)
            self.assertEqual(profile["judge_memory_limit_mb"], MUTATION_JUDGE_MEMORY_BASE_MB)
            self.assertEqual(
                profile["judge_process_limit"],
                max(MUTATION_JUDGE_PROCESS_BASE, 2 * 32 + MUTATION_JUDGE_PROCESS_HEADROOM),
            )
            for _, _, kwargs in calls:
                self.assertEqual(
                    kwargs["output_limit_bytes"],
                    profile["judge_output_limit_bytes"],
                )
                self.assertEqual(
                    kwargs["memory_limit_mb"],
                    profile["judge_memory_limit_mb"],
                )
                self.assertEqual(
                    kwargs["process_limit"],
                    profile["judge_process_limit"],
                )
                self.assertTrue(callable(kwargs["cancel_check"]))
            live_after = {
                path.relative_to(problem).as_posix(): path.read_bytes()
                for path in problem.rglob("*")
                if path.is_file()
                and path != mutation_evidence_path(problem)
                and path != problem / ".probhub/mutation.lock"
            }
            self.assertEqual(live_after, live_before)

    def test_live_input_change_after_snapshot_uses_baseline_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            original_input = (problem / "data/sample/basic.in").read_bytes()
            calls = 0

            def change_live_input(_execution_root, worker, **_kwargs):
                nonlocal calls
                calls += 1
                self.assertEqual(
                    (Path(worker) / "data/sample/basic.in").read_bytes(),
                    original_input,
                )
                (problem / "data/sample/basic.in").write_text("99\n", encoding="utf-8")
                return {
                    "ok": True,
                    "final": {"code": "all_expectations_met"},
                    "events": [],
                }

            with patch(
                "probhub.mutation.judge_problem",
                side_effect=change_live_input,
            ):
                with self.assertRaises(ProbHubError) as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                    )

            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertEqual(calls, 1)
            self.assertEqual(evidence.read_bytes(), previous)

    def test_snapshot_change_aborts_before_judge_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            original_copy = shutil.copytree
            copy_depth = 0

            def copy_then_change(*args, **kwargs):
                nonlocal copy_depth
                top_level = copy_depth == 0
                copy_depth += 1
                try:
                    result = original_copy(*args, **kwargs)
                finally:
                    copy_depth -= 1
                if top_level:
                    (problem / "code/std.cpp").write_text(
                        (problem / "code/std.cpp").read_text(encoding="utf-8")
                        + "\n// changed while copying\n",
                        encoding="utf-8",
                    )
                return result

            with patch(
                "probhub.problem_snapshot.shutil.copytree",
                side_effect=copy_then_change,
            ), patch("probhub.mutation.judge_problem") as execute:
                with self.assertRaises(ProbHubError) as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                    )

            self.assertEqual(raised.exception.code, "inputs_changed")
            execute.assert_not_called()
            self.assertEqual(evidence.read_bytes(), previous)

    def test_active_judge_cancellation_is_structured_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            cancelled = False

            def cancel_active_judge(*_args, **_kwargs):
                nonlocal cancelled
                cancelled = True
                raise ProcessCancelled("execution cancelled")

            with patch(
                "probhub.mutation.judge_problem",
                side_effect=cancel_active_judge,
            ):
                with self.assertRaises(ProbHubError) as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                        cancel_check=lambda: cancelled,
                    )

            self.assertEqual(raised.exception.code, "mutation_cancelled")
            self.assertEqual(evidence.read_bytes(), previous)

    def test_per_mutant_judge_timeout_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            timed_out = {
                "ok": False,
                "final": {"code": "judge_timeout"},
                "events": [],
            }
            with patch("probhub.mutation.judge_problem", return_value=timed_out):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )

            self.assertFalse(result["ok"], result)
            self.assertEqual(result["status"], "infrastructure-failed")
            self.assertEqual(
                result["evidence"]["mutations"][0]["classification"],
                "infrastructure-failed",
            )

    def test_worker_preparation_failure_is_structured_and_keeps_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            original_copytree = shutil.copytree

            def fail_worker_copy(*args, **kwargs):
                target = Path(args[1])
                if target.name == "problem" and target.parent.name == "execution":
                    raise OSError("worker copy failed")
                return original_copytree(*args, **kwargs)

            with patch(
                "probhub.mutation.shutil.copytree",
                side_effect=fail_worker_copy,
            ), patch("probhub.mutation.judge_problem") as execute:
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )

            self.assertFalse(result["ok"], result)
            self.assertEqual(result["status"], "infrastructure-failed")
            mutation = result["evidence"]["mutations"][0]
            self.assertEqual(mutation["classification"], "infrastructure-failed")
            self.assertEqual(
                mutation["diagnostic"]["code"],
                "mutation_worker_prepare_failed",
            )
            execute.assert_not_called()
            self.assertEqual(evidence.read_bytes(), previous)

    def test_remaining_deadline_is_forwarded_to_each_judge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            with patch(
                "probhub.mutation._compiler_fingerprint",
                return_value={"available": True},
            ), patch("probhub.mutation.judge_problem", return_value=passed) as execute:
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                    deadline=time.monotonic() + 5.0,
                )

            self.assertTrue(result["ok"], result)
            forwarded = execute.call_args.kwargs["timeout"]
            self.assertGreater(forwarded, 0)
            self.assertLessEqual(forwarded, 5.0)

    def test_hit_case_evidence_is_bounded_and_reports_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            hit_count = MAX_HIT_CASES + 5
            failed = {
                "ok": False,
                "final": {"code": "expectation_not_met"},
                "events": [
                    {
                        "type": "case",
                        "program": "code/std.cpp",
                        "case": f"secret/case-{index}",
                        "status": "WA",
                        "termination_reason": "completed",
                        "failure_kind": None,
                    }
                    for index in range(hit_count)
                ],
            }
            with patch(
                "probhub.mutation._compiler_fingerprint",
                return_value={"available": True},
            ), patch("probhub.mutation.judge_problem", return_value=failed):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )

            mutation = result["evidence"]["mutations"][0]
            self.assertEqual(mutation["classification"], "killed")
            self.assertEqual(len(mutation["hit_cases"]), MAX_HIT_CASES)
            self.assertEqual(mutation["hit_cases_total"], hit_count)
            self.assertTrue(mutation["hit_cases_truncated"])

    def test_oversized_evidence_keeps_previous_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            with patch(
                "probhub.mutation._compiler_fingerprint",
                return_value={"available": True},
            ), patch("probhub.mutation.judge_problem", return_value=passed), patch(
                "probhub.mutation.MAX_MUTATION_EVIDENCE_BYTES", 1
            ):
                with self.assertRaisesRegex(ProbHubError, "evidence exceeds") as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                    )

            self.assertEqual(raised.exception.code, "mutation_evidence_limit")
            self.assertEqual(evidence.read_bytes(), previous)

    def test_none_max_mutants_is_normalized_in_current_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            with patch("probhub.mutation.judge_problem", return_value=passed):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=None,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["evidence"]["max_mutants"], 256)
            profile = mutation_evidence_profile(problem, read_yaml(problem / "probhub.yaml"))
            self.assertEqual(profile["state"], "current", profile)

    def test_returned_fingerprint_cannot_mutate_the_process_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            with patch("probhub.mutation.judge_problem", return_value=passed):
                first = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )
                expected_digest = first["evidence"]["builder_fingerprint"]["digest"]
                first["evidence"]["builder_fingerprint"]["digest"] = "0" * 64
                first["evidence"]["builder_fingerprint"]["compiler_flags"].append("-DBAD")
                second = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )

            self.assertEqual(
                second["evidence"]["builder_fingerprint"]["digest"],
                expected_digest,
            )
            self.assertNotIn(
                "-DBAD",
                second["evidence"]["builder_fingerprint"]["compiler_flags"],
            )
            profile = mutation_evidence_profile(problem, read_yaml(problem / "probhub.yaml"))
            self.assertEqual(profile["state"], "current", profile)

    def test_legacy_token_fingerprint_is_stale_instead_of_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            with patch("probhub.mutation.judge_problem", return_value=passed):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                    max_mutants=1,
                )
            self.assertTrue(result["ok"], result)

            evidence_path = mutation_evidence_path(problem)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence.pop("locator_version")
            fingerprint = evidence["builder_fingerprint"]
            fingerprint.pop("locator_version")
            fingerprint.pop("parser_versions")
            fingerprint["parser"] = "mask-cpp-non-code+token-regex"
            fingerprint_payload = {
                key: value
                for key, value in fingerprint.items()
                if key not in {"available", "digest"}
            }
            fingerprint["digest"] = hashlib.sha256(json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            profile = mutation_evidence_profile(problem, read_yaml(problem / "probhub.yaml"))
            self.assertEqual(profile["state"], "stale", profile)
            self.assertEqual(profile["summary"], {})

    def test_syntax_failure_keeps_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            (problem / "code/std.cpp").write_text(
                "int main( { return value < 3; }\n",
                encoding="utf-8",
            )

            with self.assertRaises(ProbHubError) as raised:
                mutation_test_problem(root, problem)

            self.assertEqual(raised.exception.code, "mutation_syntax_invalid")
            self.assertEqual(evidence.read_bytes(), previous)

    def test_parser_cancel_and_deadline_keep_previous_evidence(self):
        for parser_code, expected_code in (
            ("mutation_parser_cancelled", "mutation_cancelled"),
            ("mutation_parser_timeout", "mutation_timeout"),
        ):
            with self.subTest(parser_code=parser_code), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "workspace"
                shutil.copytree(FIXTURE.parent, root)
                problem = root / "M01"
                evidence = mutation_evidence_path(problem)
                evidence.parent.mkdir(parents=True, exist_ok=True)
                previous = b"previous evidence\n"
                evidence.write_bytes(previous)
                cancel_check = lambda: False
                deadline = time.monotonic() + 0.5
                with patch(
                    "probhub.mutation.locate_cpp_mutation_syntax",
                    side_effect=ProbHubError("parser stopped", code=parser_code),
                ) as locate:
                    with self.assertRaises(ProbHubError) as raised:
                        mutation_test_problem(
                            root,
                            problem,
                            cancel_check=cancel_check,
                            deadline=deadline,
                        )
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(evidence.read_bytes(), previous)
                self.assertIs(locate.call_args.kwargs["cancel_check"], cancel_check)
                self.assertEqual(locate.call_args.kwargs["deadline"], deadline)
                self.assertGreater(locate.call_args.kwargs["timeout"], 0)
                self.assertLessEqual(locate.call_args.kwargs["timeout"], 0.5)

    def test_evidence_publish_failure_keeps_previous_bytes_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            passed = {
                "ok": True,
                "final": {"code": "all_expectations_met"},
                "events": [],
            }
            original_replace = os.replace

            def fail_evidence_publish(source, target):
                if Path(target).resolve() == evidence.resolve():
                    raise OSError("simulated publish failure")
                return original_replace(source, target)

            with patch("probhub.mutation.judge_problem", return_value=passed), patch(
                "probhub.io.os.replace", side_effect=fail_evidence_publish
            ):
                with self.assertRaisesRegex(ProbHubError, "failed to publish") as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                    )

            self.assertEqual(raised.exception.code, "mutation_evidence_publish_failed")
            self.assertEqual(evidence.read_bytes(), previous)
            self.assertEqual(list(evidence.parent.glob(evidence.name + ".*.tmp")), [])

    def test_mutation_lock_contention_keeps_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)

            with workspace_file_lock(problem, MUTATION_LOCK_FILENAME, no_follow=True), patch(
                "probhub.mutation._compiler_fingerprint", return_value={"available": True}
            ):
                with self.assertRaises(ProbHubError) as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                    )

            self.assertEqual(raised.exception.code, "mutation_busy")
            self.assertEqual(evidence.read_bytes(), previous)

    def test_single_slow_judge_expires_overall_deadline_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)

            def slow_judge(*args, **kwargs):
                time.sleep(0.03)
                return {
                    "ok": False,
                    "final": {"code": "judge_timeout"},
                    "events": [],
                }

            with patch(
                "probhub.mutation._compiler_fingerprint",
                return_value={"available": True},
            ), patch("probhub.mutation.judge_problem", side_effect=slow_judge):
                with self.assertRaisesRegex(ProbHubError, "deadline exceeded") as raised:
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        max_mutants=1,
                        deadline=time.monotonic() + 0.01,
                    )

            self.assertEqual(raised.exception.code, "mutation_timeout")
            self.assertEqual(evidence.read_bytes(), previous)

    def test_cli_returns_one_problem_level_without_nested_problems(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            output = io.StringIO()
            with redirect_stdout(output), patch(
                "probhub.cli.mutation_test_problem",
                return_value={"ok": True, "status": "passed", "evidence": {"summary": {}}},
            ) as execute:
                code = cli_main([
                    "--workspace", str(root), "--json", "mutation", "M01",
                    "--operator", "comparison-boundary",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0, payload)
            self.assertEqual(set(payload["problems"]), {"M01"})
            self.assertNotIn("problems", payload["problems"]["M01"])
            self.assertEqual(execute.call_args.kwargs["operators"], ["comparison-boundary"])

    def test_cancel_and_infrastructure_failure_keep_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b'{"schema_version": 1, "status": "passed"}\n'
            evidence.write_bytes(previous)
            fingerprint = {
                "schema_version": 1,
                "probhub_version": "fixture",
                "compiler": "g++",
                "compiler_identity": "fixture compiler",
                "compiler_flags": ["-O2", "-std=c++17"],
                "parser": "fixture",
                "operator_version": "fixture",
                "digest": "fixture",
                "available": True,
            }
            with patch("probhub.mutation._compiler_fingerprint", return_value=fingerprint):
                with self.assertRaisesRegex(ProbHubError, "cancelled"):
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                        cancel_check=lambda: True,
                    )
            self.assertEqual(evidence.read_bytes(), previous)

            judge_failure = {
                "ok": False,
                "final": {"code": "judge_failed"},
                "events": [{
                    "type": "case",
                    "program": "code/mutation.cpp",
                    "status": "FAIL",
                }],
            }
            with patch("probhub.mutation._compiler_fingerprint", return_value=fingerprint), patch(
                "probhub.mutation.judge_problem", return_value=judge_failure
            ):
                result = mutation_test_problem(
                    root,
                    problem,
                    operators=["comparison-boundary"],
                )
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["status"], "infrastructure-failed")
            self.assertEqual(evidence.read_bytes(), previous)

    def test_input_change_aborts_without_replacing_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            shutil.copytree(FIXTURE.parent, root)
            problem = root / "M01"
            evidence = mutation_evidence_path(problem)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            previous = b"previous evidence\n"
            evidence.write_bytes(previous)
            with patch("probhub.mutation._compiler_fingerprint", return_value={"available": True}), patch(
                "probhub.mutation.compute_data_hash", side_effect=["data-before", "data-after"]
            ):
                with self.assertRaisesRegex(ProbHubError, "inputs changed"):
                    mutation_test_problem(
                        root,
                        problem,
                        operators=["comparison-boundary"],
                    )
            self.assertEqual(evidence.read_bytes(), previous)
