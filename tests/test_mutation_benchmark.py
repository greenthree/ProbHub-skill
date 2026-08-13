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
            use_cache=False,
            synthetic=spec,
        )
        for index, spec in enumerate(specs)
    )


class SpawnSchedulerTests(unittest.TestCase):
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
                execution_profile={
                    "judge_output_limit_bytes": 1024 * 1024,
                    "judge_memory_limit_mb": 256,
                    "judge_process_limit": 8,
                },
                mutant_timeout=30,
                use_cache=False,
            )
            cancel_event = threading.Event()
            sent = []

            class Connection:
                def send(self, value):
                    sent.append(value)

                def close(self):
                    pass

            canceller = threading.Timer(1.0, cancel_event.set)
            started = benchmark.time.monotonic()
            canceller.start()
            try:
                with patch(
                    "benchmark_mutation.apply_mutation",
                    return_value=source,
                ):
                    benchmark._worker_entry(task, cancel_event, Connection())
            finally:
                canceller.cancel()
                canceller.join()
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
            }])[0]
            task = benchmark.WorkerTask(
                **{**task.__dict__, "mutant_timeout": 0.1}
            )
            started = benchmark.time.monotonic()
            result = benchmark.run_spawn_scheduler(
                (task,), 1, timeout=0.05, cancel_grace=0.05
            )
            elapsed = benchmark.time.monotonic() - started

        self.assertLess(elapsed, 5.0)
        self.assertEqual(result["stop_code"], "scheduler-timeout")
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
                use_cache=True,
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
        self.assertTrue(all(task.use_cache for task in plan.tasks))

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
        self.assertEqual(report["schema_version"], 1)
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
        self.assertTrue(report["identities_equivalent"])
        self.assertEqual(report["summary"]["1"]["speedup_vs_jobs_1"], 1.0)
        self.assertFalse(report["summary"]["1"]["statistics_meaningful"])
        self.assertFalse(
            report["summary"]["1"]["distribution_statistics_meaningful"]
        )
        self.assertFalse(report["summary"]["1"]["tail_statistics_meaningful"])
        self.assertIsNone(report["summary"]["1"]["p95_wall_seconds"])
        self.assertIsNone(report["summary"]["1"]["iqr_wall_seconds"])

    def test_matrix_rejects_equal_hit_counts_with_different_cases(self):
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

        self.assertFalse(report["results_equivalent"])

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
        self.assertFalse(report["identities_equivalent"])
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
                use_cache=False,
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


if __name__ == "__main__":
    unittest.main()
