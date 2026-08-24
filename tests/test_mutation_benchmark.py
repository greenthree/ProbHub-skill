import importlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


BENCHMARKS = Path(__file__).parent / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
benchmark = importlib.import_module("benchmark_mutation")


def synthetic_tasks(root, specs):
    root = Path(root)
    baseline = root / "baseline"
    baseline.mkdir()
    (baseline / "sentinel").write_text("baseline", encoding="utf-8")
    workers = root / "workers"
    workers.mkdir()
    return tuple(
        benchmark.WorkerTask(
            index=index,
            mutation_id=f"mutation-{index}",
            mutation=None,
            baseline=str(baseline),
            worker_root=str(workers / f"worker-{index}"),
            source="",
            source_relative="code/std.cpp",
            config={},
            execution_profile={},
            mutant_timeout=1.0,
            cache_profile="cold",
            synthetic=spec,
        )
        for index, spec in enumerate(specs)
    )


class SpawnSchedulerTests(unittest.TestCase):
    def test_workflow_keeps_matrices_single_run_and_expands_observations_only(self):
        workflow = (
            Path(__file__).parents[1]
            / ".github/workflows/mutation-benchmark.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("OBSERVATION_REPETITIONS:", workflow)
        self.assertIn("--matrix `\n              --profile $profile.Name `\n              --repetitions 1 `", workflow)
        self.assertEqual(
            workflow.count("--repetitions $env:OBSERVATION_REPETITIONS"), 2
        )
        self.assertIn("--contention-profile mixed-runtime", workflow)

    def test_jobs_one_still_uses_spawn_scheduler_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived"},
                {"classification": "killed"},
                {"classification": "compile-invalid"},
            ])
            result = benchmark.run_spawn_scheduler(tasks, 1, timeout=10)

        self.assertEqual(result["planned"], 3)
        self.assertEqual(result["started"], 3)
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["executed"], 3)
        self.assertEqual(result["max_active"], 1)
        self.assertEqual(
            [item["id"] for item in result["mutations"]],
            ["mutation-0", "mutation-1", "mutation-2"],
        )
        self.assertEqual(result["classifications"]["survived"], 1)
        self.assertEqual(result["classifications"]["killed"], 1)
        self.assertEqual(result["classifications"]["compile-invalid"], 1)
        if result["resource_peak"]["process_count_observed"]:
            if result["resource_peak"]["resource_provider"] == "linux-procfs":
                self.assertTrue(result["resource_peak"]["process_count_exact"])
            else:
                self.assertFalse(result["resource_peak"]["process_count_exact"])
        self.assertTrue(result["workers_cleaned"])

    def test_jobs_two_is_bounded_and_aggregates_in_plan_order(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived", "delay": 0.15},
                {"classification": "killed", "delay": 0.01},
                {"classification": "survived", "delay": 0.02},
                {"classification": "killed", "delay": 0.01},
            ])
            result = benchmark.run_spawn_scheduler(tasks, 2, timeout=10)

        self.assertEqual(result["max_active"], 2)
        self.assertEqual(
            [item["index"] for item in result["mutations"]],
            [0, 1, 2, 3],
        )
        self.assertEqual(result["executed"], 4)
        self.assertEqual(result["completed"], 4)
        self.assertTrue(result["workers_cleaned"])

    def test_jobs_four_reaches_four_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived", "delay": 0.1}
                for _ in range(4)
            ])
            result = benchmark.run_spawn_scheduler(tasks, 4, timeout=10)

        self.assertEqual(result["max_active"], 4)
        self.assertEqual(result["completed"], 4)
        self.assertTrue(result["workers_cleaned"])

    def test_worker_exception_stops_new_work_and_cleans_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived", "delay": 1.0},
                {"raise": "fixture failure", "delay": 0.05},
                {"classification": "survived"},
                {"classification": "survived"},
            ])
            result = benchmark.run_spawn_scheduler(
                tasks, 2, timeout=10, cancel_grace=1.0
            )

        self.assertEqual(result["stop_code"], "infrastructure-failed")
        self.assertEqual(result["executed"], 2)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["classifications"]["infrastructure-failed"], 1)
        self.assertEqual(result["classifications"]["cancelled"], 3)
        self.assertEqual(
            result["mutations"][1]["diagnostic"]["code"], "RuntimeError"
        )
        self.assertTrue(result["workers_cleaned"])

    def test_ready_infrastructure_failure_prevents_refill(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived", "delay": 0.5},
                {"raise": "fixture failure"},
                {"classification": "survived"},
            ])
            result = benchmark.run_spawn_scheduler(tasks, 2, timeout=10)

        self.assertEqual(result["stop_code"], "infrastructure-failed")
        self.assertEqual(result["started"], 2)
        self.assertEqual(result["classifications"]["cancelled"], 2)

    def test_worker_cleanup_failure_prevents_refill_before_result_is_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived", "delay": 10.0},
                {
                    "classification": "survived",
                    "cleanup_failure": True,
                    "cleanup_failure_send_delay": 0.5,
                },
                {"classification": "survived"},
            ])
            result = benchmark.run_spawn_scheduler(tasks, 2, timeout=10)

        self.assertEqual(result["stop_code"], "infrastructure-failed")
        self.assertEqual(result["started"], 2)
        self.assertEqual(
            result["mutations"][1]["diagnostic"]["code"],
            "worker_cleanup_failed",
        )
        self.assertEqual(result["mutations"][2]["classification"], "cancelled")

    def test_hard_termination_cleans_detached_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"hard_kill_descendant": True},
            ])
            pid_path = Path(temp) / "hard-child-0.pid"
            result = benchmark.run_spawn_scheduler(
                tasks, 1, timeout=10.0, cancel_grace=0
            )
            child_pid = int(pid_path.read_text(encoding="utf-8"))

        self.assertFalse(benchmark.process_alive(child_pid))
        self.assertTrue(result["descendants_cleaned"])
        self.assertTrue(result["workers_cleaned"])

    def test_forced_stop_without_descendant_claim_always_reports_false(self):
        process = type(
            "Process",
            (),
            {
                "pid": None,
                "is_alive": lambda self: False,
            },
        )()

        result = benchmark._stop_worker(
            process, allow_descendant_claim=False
        )

        self.assertTrue(result["worker_stopped"])
        self.assertFalse(result["descendants_cleaned"])

    def test_real_judge_worker_returns_cooperatively_after_cancellation(self):
        compiler = benchmark.shutil.which("g++")
        if (
            not compiler
            or not Path(compiler).is_file()
            or not benchmark.os.access(compiler, benchmark.os.X_OK)
        ):
            self.skipTest("g++ toolchain is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "baseline"
            benchmark.shutil.copytree(
                benchmark.FIXTURE_ROOT / benchmark.PROBLEM_ID,
                baseline,
            )
            config = benchmark.read_yaml(baseline / "probhub.yaml")
            source_relative = benchmark.normalize_solution_entries(config)["std"][0][
                "file"
            ]
            source = (baseline / source_relative).read_text(encoding="utf-8")
            task = benchmark.WorkerTask(
                index=0,
                mutation_id="cooperative-cancellation",
                mutation=None,
                baseline=str(baseline),
                worker_root=str(root / "worker"),
                source=source,
                source_relative=source_relative,
                config=config,
                execution_profile=benchmark._mutation_execution_profile(config),
                mutant_timeout=30,
                cache_profile="cold",
            )
            cancel_event = threading.Event()
            sent = []

            class Connection:
                def send(self, value):
                    sent.append(value)

                def close(self):
                    pass

            real_judge_problem = benchmark.judge_problem

            def cancelling_judge(*args, **kwargs):
                canceller = threading.Timer(0.01, cancel_event.set)
                canceller.start()
                try:
                    return real_judge_problem(*args, **kwargs)
                finally:
                    canceller.cancel()
                    canceller.join()

            started = benchmark.time.monotonic()
            with patch(
                "benchmark_mutation.apply_mutation",
                return_value=source,
            ), patch(
                "benchmark_mutation.judge_problem",
                side_effect=cancelling_judge,
            ):
                benchmark._worker_entry(task, cancel_event, Connection())
            elapsed = benchmark.time.monotonic() - started

        self.assertLess(elapsed, 10.0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["classification"], "cancelled")
        self.assertEqual(sent[0]["diagnostic"]["code"], "cancelled")
        self.assertTrue(sent[0]["descendants_cleaned"])
        self.assertTrue(sent[0]["worker_cleaned"])

    def test_real_like_worker_is_hard_stopped_after_mutant_timeout_and_grace(self):
        with tempfile.TemporaryDirectory() as temp:
            task = synthetic_tasks(temp, [{
                "delay": 30.0,
                "ignore_cancel": True,
                "real_like": True,
                "request_cancel": True,
            }])[0]
            task = benchmark.WorkerTask(
                **{**task.__dict__, "mutant_timeout": 0.1}
            )
            started = benchmark.time.monotonic()
            result = benchmark.run_spawn_scheduler(
                (task,), 1, timeout=10.0, cancel_grace=0.05
            )
            elapsed = benchmark.time.monotonic() - started

        self.assertLess(elapsed, 5.0)
        self.assertEqual(result["stop_code"], "infrastructure-failed")
        self.assertTrue(result["mutations"][0]["worker_stopped"])
        self.assertFalse(result["mutations"][0]["descendants_cleaned"])
        self.assertTrue(result["workers_cleaned"])

    def test_scheduler_timeout_is_structured_and_cleans_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [
                {"classification": "survived", "delay": 1.0},
                {"classification": "survived"},
            ])
            result = benchmark.run_spawn_scheduler(
                tasks, 1, timeout=0.1, cancel_grace=0.5
            )

        self.assertEqual(result["stop_code"], "scheduler-timeout")
        self.assertEqual(result["executed"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["classifications"]["cancelled"], 2)
        self.assertTrue(result["workers_cleaned"])

    def test_managed_synthetic_failure_cleans_descendant_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [{
                "spawn_descendant": True,
                "managed_timeout": 0.2,
                "classification": "infrastructure-failed",
            }])
            result = benchmark.run_spawn_scheduler(tasks, 1, timeout=10)

        self.assertEqual(result["stop_code"], "infrastructure-failed")
        self.assertEqual(
            result["mutations"][0]["diagnostic"]["code"],
            "synthetic-time_limit",
        )
        self.assertTrue(result["descendants_cleaned"])
        self.assertTrue(result["workers_cleaned"])

    def test_prepare_plan_captures_fixed_fixture_hashes_in_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = benchmark.prepare_plan(
                Path(temp),
                max_mutants=4,
                operators=None,
                mutant_timeout=60,
                cache_profile="cold",
            )
            live = benchmark._live_input_hashes(plan.live_problem)

        self.assertEqual(plan.live_problem.name, "M01")
        self.assertEqual(len(plan.tasks), 4)
        self.assertEqual(live["source"], plan.source_hash)
        self.assertEqual(live["data"], plan.data_hash)
        self.assertEqual(len(plan.plan_hash), 64)
        self.assertEqual(len(plan.baseline_hash), 64)
        self.assertGreaterEqual(plan.snapshot_seconds, 0)
        self.assertGreaterEqual(plan.planning_seconds, 0)
        self.assertTrue(all(task.cache_profile == "cold" for task in plan.tasks))
        self.assertTrue(all(task.primer_root is None for task in plan.tasks))

    def test_fixture_profiles_have_distinct_candidate_scales(self):
        expected = {
            "balanced": ("mutation", "M01", 5),
            "many-light": ("mutation-many-light", "M02", 30),
            "few-heavy": ("mutation-few-heavy", "M03", 3),
        }
        for profile_name, (workspace, problem_id, raw_candidates) in expected.items():
            with self.subTest(profile=profile_name), tempfile.TemporaryDirectory() as temp:
                plan = benchmark.prepare_plan(
                    Path(temp),
                    max_mutants=benchmark.MAX_MUTANTS,
                    operators=None,
                    mutant_timeout=60,
                    profile_name=profile_name,
                )
            self.assertEqual(plan.profile_name, profile_name)
            self.assertEqual(plan.problem_id, problem_id)
            self.assertEqual(plan.live_root.name, workspace)
            self.assertEqual(plan.live_problem.name, problem_id)
            self.assertEqual(plan.candidate_counts["raw"], raw_candidates)
            self.assertEqual(plan.candidate_counts["selected"], raw_candidates)
            self.assertFalse(plan.candidate_counts["truncated"])

    def test_profile_selection_is_forwarded_through_matrix(self):
        calls = []

        def fake_run(**kwargs):
            calls.append(kwargs["profile_name"])
            return {
                "wall_seconds": 1.0,
                "mutants_per_second": 1.0,
                "mutations": [],
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_matrix(
                jobs_values=(1, 2),
                repetitions=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
                profile_name="many-light",
            )

        self.assertEqual(calls, ["many-light", "many-light"])
        self.assertEqual(report["fixture_profile"], "many-light")

    def test_fixture_profile_targets_match_ci_selection(self):
        self.assertEqual(
            benchmark.FIXTURE_PROFILES["many-light"]["candidate_target"],
            {"raw": 30, "ci_selected": 8},
        )
        self.assertEqual(
            benchmark.FIXTURE_PROFILES["mixed-runtime"]["candidate_target"],
            {"raw": 38, "ci_selected": 6},
        )

    def test_real_warm_primer_reuses_cache_and_preserves_signature(self):
        cold = benchmark.run_benchmark(
            jobs=1,
            timeout=120,
            max_mutants=1,
            operators=["comparison-boundary"],
            mutant_timeout=60,
            cache_profile="cold",
        )
        warm = benchmark.run_benchmark(
            jobs=1,
            timeout=120,
            max_mutants=1,
            operators=["comparison-boundary"],
            mutant_timeout=60,
            cache_profile="warm",
        )

        self.assertEqual(
            benchmark._comparison_signature(cold),
            benchmark._comparison_signature(warm),
        )
        self.assertEqual(
            benchmark._comparison_identity(cold),
            benchmark._comparison_identity(warm),
        )
        self.assertTrue(warm["cache_observation"]["primers_unchanged"])
        self.assertTrue(warm["cache_observation"]["primer_results_equivalent"])
        self.assertTrue(warm["judge_artifacts_unchanged"])
        self.assertIn(
            warm["mutations"][0]["case_measurement_origin"],
            {"cached-primer", "mixed-fresh-and-cached-primer"},
        )
        if warm["mutations"][0]["case_measurement_origin"] == "cached-primer":
            self.assertIsNone(warm["mutations"][0]["max_case_seconds"])
        self.assertIsNotNone(warm["mutations"][0]["max_cached_case_seconds"])
        sections = warm["cache_summary"]["sections"]
        self.assertGreater(sections["compile"]["hits"], 0)
        self.assertGreater(sections["validator"]["hits"], 0)
        self.assertGreater(sections["case"]["hits"], 0)
        self.assertEqual(
            warm["cache_observation"]["warm_cache_complete"], False
        )

    def test_warm_cache_requires_every_section_to_be_observed(self):
        sections = {
            section: {"hits": 1, "misses": 0, "observed": True}
            for section in benchmark.CACHE_SECTIONS
        }
        summary = {"workers_observed": 1, "sections": sections}

        self.assertTrue(benchmark._warm_cache_is_complete("warm", summary, 1))
        sections["probe"] = {"hits": 0, "misses": 0, "observed": False}
        self.assertFalse(benchmark._warm_cache_is_complete("warm", summary, 1))

    def test_formal_artifact_snapshot_detects_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = root / "M01"
            problem.mkdir()
            before = benchmark._formal_artifact_hashes(root, problem)
            (problem / "problem.pdf").write_bytes(b"pdf")
            after = benchmark._formal_artifact_hashes(root, problem)

        self.assertNotEqual(before, after)
        self.assertIn("M01/problem.pdf", after)

    def test_fixture_hash_tracks_empty_directories_and_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before = benchmark._hash_tree(root)
            (root / "empty").mkdir()
            directory_hash = benchmark._hash_tree(root)
            self.assertNotEqual(before, directory_hash)
            target = root / "target.txt"
            target.write_text("same", encoding="utf-8")
            file_hash = benchmark._hash_tree(root)
            target.unlink()
            try:
                target.symlink_to(root / "missing.txt")
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")
            self.assertNotEqual(file_hash, benchmark._hash_tree(root))

    def test_jobs_are_limited_to_shadow_matrix(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = synthetic_tasks(temp, [])
            with self.assertRaises(ValueError):
                benchmark.run_spawn_scheduler(tasks, 3, timeout=1)

    def test_report_contract_contains_required_safety_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = benchmark.prepare_plan(
                Path(temp),
                max_mutants=1,
                operators=["comparison-boundary"],
                mutant_timeout=60,
            )
            scheduled = {
                "planned": 1,
                "started": 1,
                "completed": 1,
                "executed": 1,
                "classifications": {name: 0 for name in benchmark.CLASSIFICATIONS},
                "mutations": [],
                "scheduler_wall_seconds": 1.0,
                "scheduler_mutants_per_second": 1.0,
                "max_active": 1,
                "resource_peak": {
                    "process_count": 2,
                    "process_count_observed": True,
                    "process_count_exact": True,
                    "process_count_scope": "active-worker-process-trees",
                    "resource_provider": "linux-procfs",
                    "rss_mb": 32.5,
                    "rss_observed": True,
                    "samples": 3,
                    "parent_cpu_seconds": 0.1,
                    "direct_children_cpu_seconds": 0.2,
                    "parent_cpu_observed": True,
                    "direct_children_cpu_observed": True,
                    "cpu_scope": "scheduler-and-direct-workers-only",
                },
                "stop_code": None,
                "workers_cleaned": True,
                "descendants_cleaned": True,
            }
            scheduled["classifications"]["survived"] = 1
            with patch(
                "benchmark_mutation.run_spawn_scheduler", return_value=scheduled
            ):
                report = benchmark.run_benchmark(
                    jobs=1,
                    timeout=10,
                    max_mutants=1,
                    operators=["comparison-boundary"],
                    mutant_timeout=60,
                )

        required = {
            "schema_version", "planned", "executed", "classifications",
            "mutations", "wall_seconds", "mutants_per_second", "hashes",
            "live_inputs_unchanged", "formal_artifacts_unchanged",
            "mutation_evidence_unchanged", "fixture_unchanged",
            "workers_cleaned", "descendants_cleaned",
        }
        self.assertTrue(required.issubset(report))
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(
            report["execution_profile"]["scheduler"],
            "spawn-bounded-shadow-v1",
        )
        self.assertEqual(report["execution_profile"]["jobs"], 1)
        self.assertEqual(report["execution_profile"]["mutant_timeout_seconds"], 60.0)
        self.assertEqual(
            report["execution_profile"]["cooperative_cancel_grace_seconds"],
            benchmark.CANCEL_GRACE_SECONDS,
        )
        self.assertEqual(
            report["execution_profile"][
                "real_worker_forced_stop_after_cancel_seconds"
            ],
            benchmark.CANCEL_GRACE_SECONDS + 60.0,
        )
        self.assertFalse(
            report["execution_profile"][
                "forced_real_worker_descendant_cleanup_claim"
            ]
        )
        self.assertFalse(report["performance_gate"])
        self.assertEqual(report["fixture"]["profile"], "balanced")
        self.assertEqual(report["fixture"]["problem_id"], "M01")
        self.assertEqual(report["candidate_plan"]["selected"], 1)
        self.assertEqual(report["resource_summary"]["process_count_peak"], 2)
        self.assertTrue(report["resource_summary"]["process_count_exact"])
        self.assertEqual(report["resource_summary"]["active_rss_mb_peak"], 32.5)
        self.assertEqual(report["failure_summary"]["infrastructure_failed"], 0)
        self.assertIn("cpu_scope", report["resource_summary"])

    def test_resource_and_failure_summaries_are_stable(self):
        scheduled = {
            "classifications": {
                "infrastructure-failed": 1,
                "cancelled": 2,
            },
            "mutations": [
                {
                    "classification": "infrastructure-failed",
                    "diagnostic": {"code": "judge_timeout"},
                    "elapsed_seconds": 3,
                    "worker_cpu_seconds": 0.5,
                    "worker_children_cpu_seconds": 0.75,
                    "max_case_seconds": 1.25,
                    "max_case_memory_mb": 64,
                },
                {
                    "classification": "cancelled",
                    "diagnostic": {"code": "scheduler-timeout"},
                    "elapsed_seconds": None,
                },
                {
                    "classification": "cancelled",
                    "diagnostic": {"code": "scheduler-timeout"},
                    "elapsed_seconds": None,
                },
            ],
            "stop_code": "infrastructure-failed",
        }
        resources = benchmark._resource_summary(
            scheduled["mutations"],
            {
                "process_count": 7,
                "process_count_observed": True,
                "process_count_exact": False,
                "process_count_scope": "cumulative-worker-pids-seen-upper-bound",
                "resource_provider": "pid-snapshot-fallback",
                "rss_mb": None,
                "rss_observed": False,
                "samples": 5,
                "parent_cpu_seconds": 0.25,
                "direct_children_cpu_seconds": 1.5,
                "parent_cpu_observed": True,
                "direct_children_cpu_observed": True,
                "cpu_scope": "scheduler-and-direct-workers-only",
            },
        )
        failures = benchmark._failure_summary(scheduled)

        self.assertEqual(resources["case_time_seconds_max"], 1.25)
        self.assertEqual(resources["case_memory_mb_max"], 64)
        self.assertEqual(resources["process_count_peak"], 7)
        self.assertFalse(resources["process_count_exact"])
        self.assertEqual(
            resources["process_count_scope"],
            "cumulative-worker-pids-seen-upper-bound",
        )
        self.assertFalse(resources["active_rss_observed"])
        self.assertEqual(resources["direct_worker_cpu_seconds"], 1.5)
        self.assertTrue(resources["direct_worker_cpu_observed"])
        self.assertEqual(resources["worker_cpu_seconds_sum"], 0.5)
        self.assertEqual(resources["worker_children_cpu_seconds_sum"], 0.75)
        self.assertTrue(resources["worker_cpu_observed"])
        self.assertEqual(
            failures["diagnostic_codes"],
            {"judge_timeout": 1, "scheduler-timeout": 2},
        )

    def test_missing_worker_cpu_is_null_instead_of_zero(self):
        resources = benchmark._resource_summary(
            [{"classification": "cancelled", "elapsed_seconds": None}],
            {},
        )

        self.assertIsNone(resources["worker_cpu_seconds_sum"])
        self.assertFalse(resources["worker_cpu_observed"])
        self.assertEqual(resources["worker_cpu_samples"], 0)
        self.assertIsNone(resources["worker_children_cpu_seconds_sum"])
        self.assertFalse(resources["worker_children_cpu_observed"])

    def test_matrix_runs_sequentially_and_checks_structured_equivalence(self):
        calls = []

        def fake_run(**kwargs):
            calls.append(kwargs["jobs"])
            return {
                "wall_seconds": float(kwargs["jobs"]),
                "mutants_per_second": 1.0 / kwargs["jobs"],
                "mutations": [{
                    "id": "m1",
                    "classification": "survived",
                    "final_code": "all_expectations_met",
                    "hit_cases": [],
                    "hit_cases_total": 0,
                }],
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_matrix(
                jobs_values=(1, 2, 4),
                repetitions=3,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertEqual(calls, [1, 2, 4, 2, 4, 1, 4, 1, 2])
        self.assertTrue(report["results_equivalent"])
        self.assertTrue(report["classifications_equivalent"])
        self.assertTrue(report["diagnostics_equivalent"])
        self.assertTrue(report["identities_equivalent"])
        self.assertEqual(report["summary"]["1"]["speedup_vs_jobs_1"], 1.0)
        self.assertFalse(report["summary"]["1"]["statistics_meaningful"])
        self.assertFalse(
            report["summary"]["1"]["distribution_statistics_meaningful"]
        )
        self.assertFalse(report["summary"]["1"]["tail_statistics_meaningful"])
        self.assertIsNone(report["summary"]["1"]["p95_wall_seconds"])
        self.assertIsNone(report["summary"]["1"]["iqr_wall_seconds"])

    def test_matrix_observes_different_hit_cases_without_failing_classification(self):
        cases = iter(("sample/a", "secret/a"))

        def fake_run(**kwargs):
            case = next(cases)
            return {
                "wall_seconds": float(kwargs["jobs"]),
                "mutants_per_second": 1.0,
                "mutations": [{
                    "id": "m1",
                    "classification": "killed",
                    "final_code": "expectation_not_met",
                    "hit_cases": [{
                        "case": case,
                        "status": "WA",
                        "termination_reason": None,
                        "failure_kind": None,
                    }],
                    "hit_cases_total": 1,
                }],
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_matrix(
                jobs_values=(1, 2),
                repetitions=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertTrue(report["results_equivalent"])
        self.assertTrue(report["classifications_equivalent"])
        self.assertFalse(report["diagnostics_equivalent"])

    def test_matrix_rejects_classification_drift(self):
        classifications = iter(("killed", "survived"))

        def fake_run(**kwargs):
            return {
                "wall_seconds": float(kwargs["jobs"]),
                "mutants_per_second": 1.0,
                "mutations": [{
                    "id": "m1",
                    "classification": next(classifications),
                    "final_code": "expectation_not_met",
                    "hit_cases": [],
                    "hit_cases_total": 0,
                }],
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_matrix(
                jobs_values=(1, 2),
                repetitions=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertFalse(report["results_equivalent"])
        self.assertFalse(report["classifications_equivalent"])

    def test_matrix_rejects_identity_drift_between_runs(self):
        plans = iter(("plan-a", "plan-b"))

        def fake_run(**kwargs):
            return {
                "wall_seconds": float(kwargs["jobs"]),
                "mutants_per_second": 1.0,
                "mutations": [],
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": next(plans),
                    "baseline": "baseline",
                },
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_matrix(
                jobs_values=(1, 2),
                repetitions=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertTrue(report["classifications_equivalent"])
        self.assertTrue(report["diagnostics_equivalent"])
        self.assertFalse(report["identities_equivalent"])
        self.assertFalse(report["results_equivalent"])

    def test_distribution_summary_requires_enough_samples(self):
        short = benchmark._distribution_summary(
            [3, None, float("nan"), True, 1, 2], "seconds"
        )
        distribution = benchmark._distribution_summary(
            [5, 1, 4, 2, 3], "seconds"
        )

        self.assertEqual(short["median_seconds"], 2.0)
        self.assertIsNone(short["iqr_seconds"])
        self.assertFalse(short["distribution_statistics_meaningful"])
        self.assertEqual(distribution["median_seconds"], 3.0)
        self.assertEqual(distribution["iqr_seconds"], 3.0)
        self.assertTrue(distribution["distribution_statistics_meaningful"])
        self.assertIsNone(distribution["p95_seconds"])

    def test_distribution_summary_reports_missing_and_unsafe_coverage(self):
        missing = benchmark._distribution_summary(
            [],
            "rss_mb",
            requested_runs=5,
            completed_runs=5,
            safe_runs=5,
        )
        unsafe = benchmark._distribution_summary(
            [1, 2, 3, 4, 5],
            "seconds",
            requested_runs=5,
            completed_runs=5,
            safe_runs=4,
            failed_runs=1,
        )

        self.assertEqual(missing["requested_runs"], 5)
        self.assertEqual(missing["observed_runs"], 0)
        self.assertEqual(missing["missing_runs"], 5)
        self.assertIsNone(missing["median_rss_mb"])
        self.assertFalse(unsafe["distribution_statistics_meaningful"])
        self.assertIsNone(unsafe["iqr_seconds"])

    def test_distribution_summary_requires_twenty_safe_samples_for_p95(self):
        nineteen = benchmark._distribution_summary(range(1, 20), "seconds")
        twenty = benchmark._distribution_summary(range(1, 21), "seconds")

        self.assertIsNone(nineteen["p95_seconds"])
        self.assertEqual(twenty["p95_seconds"], 19.0)
        self.assertTrue(twenty["tail_statistics_meaningful"])

    def test_contention_series_rejects_reference_signature_drift(self):
        calls = 0

        def fake_once(**kwargs):
            nonlocal calls
            calls += 1
            profiles = kwargs["profiles"]
            references = {
                profile: {
                    "hashes": {
                        "source": profile,
                        "data": profile,
                        "plan": profile,
                        "baseline": profile,
                    },
                    "mutations": [{
                        "id": profile,
                        "classification": "killed",
                        "final_code": "expectation_not_met",
                        "hit_cases": [{"case": f"case-{calls}"}],
                        "hit_cases_total": 1,
                    }],
                }
                for profile in profiles
            }
            return {
                "cpu_attribution": "test",
                "wall_seconds": 1.0,
                "host_resource_observation": {"observed": False},
                "references": references,
                "reports": references,
                "comparisons": {
                    profile: {"slowdown_vs_reference": 1.0}
                    for profile in profiles
                },
                "results_equivalent": True,
                "all_safe": True,
            }

        with patch(
            "benchmark_mutation._run_contention_once", side_effect=fake_once
        ):
            report = benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                repetitions=2,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertFalse(report["results_equivalent"])

    def test_contention_series_rejects_contended_signature_drift(self):
        calls = 0

        def fake_once(**kwargs):
            nonlocal calls
            calls += 1
            profiles = kwargs["profiles"]
            references = {
                profile: {
                    "hashes": {
                        "source": profile,
                        "data": profile,
                        "plan": profile,
                        "baseline": profile,
                    },
                    "mutations": [{"id": profile, "hit_cases": []}],
                }
                for profile in profiles
            }
            reports = {
                profile: {
                    **references[profile],
                    "mutations": [{
                        "id": profile,
                        "hit_cases": ([] if calls == 1 else [{"case": "drift"}]),
                    }],
                }
                for profile in profiles
            }
            return {
                "cpu_attribution": "test",
                "wall_seconds": 1.0,
                "host_resource_observation": {"observed": False},
                "references": references,
                "reports": reports,
                "comparisons": {
                    profile: {"slowdown_vs_reference": 1.0}
                    for profile in profiles
                },
                "results_equivalent": True,
                "all_safe": True,
            }

        with patch(
            "benchmark_mutation._run_contention_once", side_effect=fake_once
        ):
            report = benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                repetitions=2,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertFalse(report["results_equivalent"])

    def test_contention_series_rotates_order_and_summarizes_slowdown(self):
        orders = []

        def fake_once(**kwargs):
            orders.append(kwargs["profiles"])
            index = len(orders)
            profiles = kwargs["profiles"]
            references = {
                profile: {
                    "hashes": {
                        "source": profile,
                        "data": profile,
                        "plan": profile,
                        "baseline": profile,
                    },
                    "mutations": [],
                }
                for profile in profiles
            }
            return {
                "cpu_attribution": "test",
                "wall_seconds": float(index),
                "host_resource_observation": {
                    "observed": True,
                    "process_count_peak": index + 2,
                    "active_rss_mb_peak": 100 + index,
                },
                "references": references,
                "reports": references,
                "comparisons": {
                    profile: {
                        "slowdown_vs_reference": 1.0 + index / 10.0,
                    }
                    for profile in profiles
                },
                "results_equivalent": True,
                "all_safe": True,
            }

        with patch(
            "benchmark_mutation._run_contention_once", side_effect=fake_once
        ):
            report = benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                repetitions=5,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertEqual(
            orders,
            [
                ("many-light", "few-heavy"),
                ("few-heavy", "many-light"),
                ("many-light", "few-heavy"),
                ("few-heavy", "many-light"),
                ("many-light", "few-heavy"),
            ],
        )
        self.assertTrue(report["results_equivalent"])
        self.assertTrue(report["all_safe"])
        self.assertEqual(report["compatibility_run"], 1)
        self.assertEqual(report["observation_coverage"]["safe_runs"], 5)
        self.assertEqual(
            report["summary"]["slowdown"]["many-light"][
                "median_slowdown_vs_reference"
            ],
            1.3,
        )
        self.assertTrue(
            report["summary"]["wall_seconds"][
                "distribution_statistics_meaningful"
            ]
        )

    def test_contention_runs_two_profiles_independently(self):
        calls = []

        def fake_run(**kwargs):
            profile = kwargs["profile_name"]
            calls.append(profile)
            return {
                "wall_seconds": 1.0 if profile == "many-light" else 2.0,
                "resource_summary": {
                    "scheduler_parent_cpu_seconds": 1.0,
                    "scheduler_parent_cpu_observed": True,
                    "direct_worker_cpu_seconds": 2.0,
                    "direct_worker_cpu_observed": True,
                },
                "execution_profile": {
                    "judge_memory_limit_mb": 4096,
                    "judge_process_limit": 80,
                },
                "planned": 1,
                "completed": 1,
                "classifications": {
                    "infrastructure-failed": 0,
                    "cancelled": 0,
                },
                "stop_code": None,
                "mutations": [{
                    "id": profile,
                    "classification": "survived",
                    "final_code": "all_expectations_met",
                    "hit_cases": [],
                    "hit_cases_total": 0,
                }],
                "hashes": {
                    "source": profile + "-source",
                    "data": profile + "-data",
                    "plan": profile + "-plan",
                    "baseline": profile + "-baseline",
                },
                "live_inputs_unchanged": True,
                "formal_artifacts_unchanged": True,
                "mutation_evidence_unchanged": True,
                "judge_artifacts_unchanged": True,
                "fixture_unchanged": True,
                "workers_cleaned": True,
                "descendants_cleaned": True,
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=2,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertEqual(calls.count("many-light"), 2)
        self.assertEqual(calls.count("few-heavy"), 2)
        self.assertEqual(report["failure_scope"], "per-problem-independent")
        self.assertTrue(report["results_equivalent"])
        self.assertTrue(report["all_safe"])
        synchronization = report["launch_synchronization"]
        self.assertTrue(synchronization["barrier_released"])
        self.assertEqual(
            synchronization["profiles_ready"], ["few-heavy", "many-light"]
        )
        self.assertEqual(
            synchronization["profiles_started"], ["few-heavy", "many-light"]
        )
        self.assertIsInstance(synchronization["launch_skew_seconds"], float)
        resources = report["configured_worker_resources"]
        self.assertEqual(resources["active_worker_tokens"], 4)
        self.assertEqual(
            set(resources["profiles"]), {"many-light", "few-heavy"}
        )
        self.assertEqual(resources["theoretical_memory_limit_mb"], 16384)
        self.assertEqual(resources["theoretical_process_limit"], 320)
        self.assertNotIn("host_job_budget", report)
        self.assertIsNone(
            report["reports"]["many-light"]["resource_summary"][
                "scheduler_parent_cpu_seconds"
            ]
        )

    def test_contention_keeps_peer_result_when_one_profile_fails(self):
        counts = {"many-light": 0, "few-heavy": 0}

        def fake_run(**kwargs):
            profile = kwargs["profile_name"]
            counts[profile] += 1
            if profile == "many-light" and counts[profile] == 2:
                raise benchmark.ProbHubError("fixture failed", code="fixture_failed")
            return {
                "wall_seconds": 1.0,
                "planned": 0,
                "completed": 0,
                "classifications": {
                    "infrastructure-failed": 0,
                    "cancelled": 0,
                },
                "stop_code": None,
                "mutations": [],
                "hashes": {
                    "source": profile,
                    "data": profile,
                    "plan": profile,
                    "baseline": profile,
                },
                "live_inputs_unchanged": True,
                "formal_artifacts_unchanged": True,
                "mutation_evidence_unchanged": True,
                "judge_artifacts_unchanged": True,
                "fixture_unchanged": True,
                "workers_cleaned": True,
                "descendants_cleaned": True,
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertEqual(report["reports"]["many-light"]["error"], "fixture_failed")
        self.assertIn("mutations", report["reports"]["few-heavy"])
        self.assertFalse(report["results_equivalent"])
        self.assertFalse(report["all_safe"])

    def test_contention_broken_barrier_is_unsafe(self):
        complete = {
            "wall_seconds": 1.0,
            "planned": 0,
            "completed": 0,
            "classifications": {
                "infrastructure-failed": 0,
                "cancelled": 0,
            },
            "stop_code": None,
            "mutations": [],
            "hashes": {},
            "live_inputs_unchanged": True,
            "formal_artifacts_unchanged": True,
            "mutation_evidence_unchanged": True,
            "judge_artifacts_unchanged": True,
            "fixture_unchanged": True,
            "workers_cleaned": True,
            "descendants_cleaned": True,
            "execution_profile": {},
        }

        class BrokenBarrier:
            def __init__(self, parties):
                self.parties = parties

            def wait(self, timeout=None):
                raise threading.BrokenBarrierError

        with patch("benchmark_mutation.run_benchmark", return_value=complete), patch(
            "benchmark_mutation.threading.Barrier", BrokenBarrier
        ):
            report = benchmark._run_contention_once(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertFalse(report["launch_synchronization"]["barrier_released"])
        self.assertFalse(report["all_safe"])

    def test_contention_rejects_host_oversubscription(self):
        with self.assertRaises(ValueError):
            benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=4,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

    def test_aggregate_modes_reject_stable_infrastructure_failure(self):
        failed = {
            "wall_seconds": 1.0,
            "resource_summary": {"judge_wall_seconds_sum": 1.0},
            "planned": 1,
            "completed": 0,
            "classifications": {
                "infrastructure-failed": 1,
                "cancelled": 0,
            },
            "stop_code": "infrastructure-failed",
            "mutations": [{
                "id": "m1",
                "classification": "infrastructure-failed",
                "final_code": "judge_timeout",
                "hit_cases": [],
                "hit_cases_total": 0,
            }],
            "hashes": {
                "source": "source",
                "data": "data",
                "plan": "plan",
                "baseline": "baseline",
            },
            "cache_observation": {
                "primer_results_equivalent": True,
                "warm_cache_complete": False,
                "warm_cache_partial": True,
            },
            "live_inputs_unchanged": True,
            "formal_artifacts_unchanged": True,
            "mutation_evidence_unchanged": True,
            "judge_artifacts_unchanged": True,
            "fixture_unchanged": True,
            "workers_cleaned": True,
            "descendants_cleaned": True,
        }
        with patch("benchmark_mutation.run_benchmark", return_value=failed):
            pair = benchmark.run_cache_pair(
                jobs=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )
            contention = benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertTrue(pair["results_equivalent"])
        self.assertFalse(pair["all_safe"])
        self.assertTrue(contention["results_equivalent"])
        self.assertFalse(contention["all_safe"])

    def test_output_path_rejects_committed_fixture(self):
        output = benchmark.FIXTURE_ROOT / "report.json"
        with self.assertRaises(ValueError):
            benchmark._validate_output_path(output)

    def test_main_does_not_write_failure_inside_committed_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture_root = Path(temp) / "fixtures"
            output = fixture_root / "mutation" / "report.json"
            stdout = io.StringIO()
            with patch(
                "benchmark_mutation.FIXTURE_WORKSPACES_ROOT", fixture_root
            ), patch("sys.stdout", stdout):
                exit_code = benchmark.main([
                    "--output", str(output),
                    "--max-mutants", "1",
                ])

            report = benchmark.json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "ValueError")
        self.assertFalse(output.exists())

    def test_cache_pair_compares_measured_judge_phases(self):
        def fake_run(**kwargs):
            warm = kwargs["cache_profile"] == "warm"
            return {
                "resource_summary": {
                    "judge_wall_seconds_sum": 1.0 if warm else 2.0,
                },
                "planned": 1,
                "completed": 1,
                "classifications": {
                    "infrastructure-failed": 0,
                    "cancelled": 0,
                },
                "stop_code": None,
                "mutations": [{
                    "id": "m1",
                    "classification": "survived",
                    "final_code": "all_expectations_met",
                    "hit_cases": [],
                    "hit_cases_total": 0,
                }],
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
                "cache_observation": {
                    "primer_results_equivalent": True,
                    "warm_cache_complete": warm,
                    "warm_cache_partial": False,
                },
                "live_inputs_unchanged": True,
                "formal_artifacts_unchanged": True,
                "mutation_evidence_unchanged": True,
                "judge_artifacts_unchanged": True,
                "fixture_unchanged": True,
                "workers_cleaned": True,
                "descendants_cleaned": True,
            }

        with patch("benchmark_mutation.run_benchmark", side_effect=fake_run):
            report = benchmark.run_cache_pair(
                jobs=1,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertTrue(report["results_equivalent"])
        self.assertTrue(report["all_safe"])
        self.assertEqual(report["warm_speedup_vs_cold"], 2.0)
        self.assertFalse(report["host_cache_controlled"])

    def test_cache_pair_series_rotates_order_and_summarizes_speedup(self):
        orders = []

        def fake_once(**kwargs):
            orders.append(kwargs["profile_order"])
            index = len(orders)
            base = {
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
                "mutations": [],
            }
            return {
                "reports": {"cold": base, "warm": base},
                "results_equivalent": True,
                "all_safe": True,
                "measured_judge_wall_seconds": {
                    "cold": float(index * 2),
                    "warm": float(index),
                },
                "warm_speedup_vs_cold": float(index),
                "warm_cache_complete": True,
                "warm_cache_partial": False,
            }

        with patch(
            "benchmark_mutation._run_cache_pair_once", side_effect=fake_once
        ):
            report = benchmark.run_cache_pair(
                jobs=1,
                repetitions=5,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertEqual(
            orders,
            [
                ("cold", "warm"),
                ("warm", "cold"),
                ("cold", "warm"),
                ("warm", "cold"),
                ("cold", "warm"),
            ],
        )
        self.assertTrue(report["results_equivalent"])
        self.assertTrue(report["all_safe"])
        self.assertEqual(
            report["summary"]["warm_speedup_vs_cold"]["median_speedup"],
            3.0,
        )
        self.assertTrue(
            report["summary"]["warm_speedup_vs_cold"][
                "distribution_statistics_meaningful"
            ]
        )

    def test_aggregate_modes_reject_repetitions_over_limit(self):
        arguments = {
            "timeout": 10,
            "max_mutants": 1,
            "operators": None,
            "mutant_timeout": 10,
            "repetitions": benchmark.MAX_REPETITIONS + 1,
        }
        with self.assertRaisesRegex(ValueError, "repetitions must be in"):
            benchmark.run_matrix(jobs_values=(1,), **arguments)
        with self.assertRaisesRegex(ValueError, "repetitions must be in"):
            benchmark.run_cache_pair(jobs=1, **arguments)
        with self.assertRaisesRegex(ValueError, "repetitions must be in"):
            benchmark.run_contention(
                profiles=("many-light", "few-heavy"),
                jobs_per_profile=1,
                **arguments,
            )

    def test_cache_pair_series_rejects_identity_drift(self):
        calls = 0

        def fake_once(**kwargs):
            nonlocal calls
            calls += 1
            base = {
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": f"plan-{calls}",
                    "baseline": "baseline",
                },
                "mutations": [],
            }
            return {
                "reports": {"cold": base, "warm": base},
                "results_equivalent": True,
                "all_safe": True,
                "measured_judge_wall_seconds": {"cold": 2.0, "warm": 1.0},
                "warm_speedup_vs_cold": 2.0,
                "warm_cache_complete": True,
                "warm_cache_partial": False,
            }

        with patch(
            "benchmark_mutation._run_cache_pair_once", side_effect=fake_once
        ):
            report = benchmark.run_cache_pair(
                jobs=1,
                repetitions=2,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertFalse(report["results_equivalent"])

    def test_cache_pair_series_rejects_warm_signature_drift(self):
        calls = 0

        def fake_once(**kwargs):
            nonlocal calls
            calls += 1
            cold = {
                "hashes": {
                    "source": "source",
                    "data": "data",
                    "plan": "plan",
                    "baseline": "baseline",
                },
                "mutations": [{"id": "m1", "hit_cases": []}],
            }
            warm = {
                **cold,
                "mutations": [{
                    "id": "m1",
                    "hit_cases": ([] if calls == 1 else [{"case": "drift"}]),
                }],
            }
            return {
                "reports": {"cold": cold, "warm": warm},
                "results_equivalent": True,
                "all_safe": True,
                "measured_judge_wall_seconds": {"cold": 2.0, "warm": 1.0},
                "warm_speedup_vs_cold": 2.0,
                "warm_cache_complete": True,
                "warm_cache_partial": False,
            }

        with patch(
            "benchmark_mutation._run_cache_pair_once", side_effect=fake_once
        ):
            report = benchmark.run_cache_pair(
                jobs=1,
                repetitions=2,
                timeout=10,
                max_mutants=1,
                operators=None,
                mutant_timeout=10,
            )

        self.assertFalse(report["results_equivalent"])

    def test_judge_cancellation_is_classified_as_cancelled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "baseline"
            (baseline / "code").mkdir(parents=True)
            (baseline / "code" / "std.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            task = benchmark.WorkerTask(
                index=0,
                mutation_id="mutation-0",
                mutation=type(
                    "Mutation",
                    (),
                    {"start": 0, "end": 0, "replacement": ""},
                )(),
                baseline=str(baseline),
                worker_root=str(root / "worker"),
                source="int main() { return 0; }\n",
                source_relative="code/std.cpp",
                config={"solutions": {"accepted": [{"file": "code/std.cpp"}]}},
                execution_profile={
                    "judge_output_limit_bytes": 1024,
                    "judge_memory_limit_mb": 256,
                    "judge_process_limit": 8,
                },
                mutant_timeout=10,
                cache_profile="cold",
            )
            cancel_event = type("Cancel", (), {"is_set": lambda self: True})()
            with patch(
                "benchmark_mutation.apply_mutation", return_value=task.source
            ), patch(
                "benchmark_mutation.judge_problem",
                return_value={
                    "ok": False,
                    "final": {"code": "judge_cancelled"},
                    "events": [],
                },
            ):
                with self.assertRaises(benchmark.ProcessCancelled):
                    benchmark._run_judge_task(task, cancel_event)

    def test_main_writes_structured_failure_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            with patch(
                "benchmark_mutation.run_benchmark",
                side_effect=benchmark.ProbHubError(
                    "benchmark failed", code="benchmark_failed"
                ),
            ), patch("sys.stdout", new_callable=io.StringIO):
                exit_code = benchmark.main([
                    "--jobs", "1",
                    "--output", str(output),
                ])

            report = benchmark.json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "benchmark_failed")

    def test_main_writes_argument_failure_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            with patch("sys.stdout", new_callable=io.StringIO):
                exit_code = benchmark.main([
                    "--repetitions", "invalid",
                    "--output", str(output),
                ])
            report = benchmark.json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "ValueError")

    def test_main_writes_utf8_bytes_to_non_utf8_console(self):
        class NonUtf8Console:
            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, value):
                value.encode("cp1252")

            def flush(self):
                pass

        report = {
            "message": "\u4e2d\u6587\u8bca\u65ad",
            "classifications": {
                "infrastructure-failed": 0,
                "cancelled": 0,
            },
            "live_inputs_unchanged": True,
            "formal_artifacts_unchanged": True,
            "mutation_evidence_unchanged": True,
            "judge_artifacts_unchanged": True,
            "fixture_unchanged": True,
            "workers_cleaned": True,
            "descendants_cleaned": True,
        }
        console = NonUtf8Console()

        with patch("benchmark_mutation.run_benchmark", return_value=report), patch(
            "sys.stdout", console
        ):
            exit_code = benchmark.main(["--jobs", "1"])

        payload = benchmark.json.loads(console.buffer.getvalue().decode("utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["message"], "\u4e2d\u6587\u8bca\u65ad")


if __name__ == "__main__":
    unittest.main()
