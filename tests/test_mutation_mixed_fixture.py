import importlib
import shutil
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

from probhub.io import read_yaml
from probhub.judging import judge_problem
from probhub.linting import lint_workspace
from probhub.mutation import MUTATION_OPERATORS, plan_mutations
from probhub.workspace import load_workspace
from tests.fixture_support import copy_workspace_fixture


BENCHMARKS = Path(__file__).parent / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
benchmark = importlib.import_module("benchmark_mutation")


def independent_reachable(rows, columns, period, seed):
    def blocked(row, column):
        return (
            row > 0
            and column > 0
            and (37 * row + 61 * column + seed) % period == 0
        )

    seen = {(0, 0)}
    pending = deque([(0, 0)])
    while pending:
        row, column = pending.popleft()
        for delta_row, delta_column in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            next_row = row + delta_row
            next_column = column + delta_column
            candidate = (next_row, next_column)
            if (
                0 <= next_row < rows
                and 0 <= next_column < columns
                and not blocked(next_row, next_column)
                and candidate not in seen
            ):
                seen.add(candidate)
                pending.append(candidate)
    return len(seen)


class MixedRuntimeMutationFixtureTests(unittest.TestCase):
    def test_fixture_is_lintable_and_has_a_nontrivial_mutation_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = copy_workspace_fixture("mutation-mixed-runtime", temp)
            root, workspace = load_workspace(fixture.root)
            lint = lint_workspace(root, workspace)
            config = read_yaml(fixture.config_path)
            source_path = fixture.problem / config["solutions"]["accepted"][0]
            plan = plan_mutations(
                source_path.read_text(encoding="utf-8"),
                operators=MUTATION_OPERATORS,
            )

        self.assertTrue(lint["ok"], lint)
        self.assertEqual(fixture.problem_id, "M04")
        self.assertEqual(plan["raw_planned"], 38)
        self.assertEqual(
            {mutation.operator for mutation in plan["mutations"]},
            set(MUTATION_OPERATORS),
        )

    def test_fixture_combines_tiny_corridor_sparse_and_heavy_cases(self):
        fixture_root = (
            Path(__file__).parent
            / "fixtures/workspaces/mutation-mixed-runtime/M04"
        )
        inputs = {
            path.stem: tuple(map(int, path.read_text(encoding="utf-8").split()))
            for path in (fixture_root / "data").glob("**/*.in")
        }

        self.assertEqual(inputs["single"][:2], (1, 1))
        self.assertIn(1, inputs["corridor"][:2])
        self.assertGreater(inputs["sparse"][0] * inputs["sparse"][1], 40_000)
        self.assertGreater(inputs["heavy"][0] * inputs["heavy"][1], 1_000_000)
        self.assertEqual(inputs["disconnected"], (3, 3, 2, 1))

    def test_all_answers_match_an_independent_bfs(self):
        fixture_root = (
            Path(__file__).parent
            / "fixtures/workspaces/mutation-mixed-runtime/M04"
        )
        for input_path in (fixture_root / "data").glob("**/*.in"):
            with self.subTest(case=input_path.stem):
                values = tuple(map(int, input_path.read_text(encoding="utf-8").split()))
                expected = independent_reachable(*values)
                answer = int(input_path.with_suffix(".ans").read_text(encoding="utf-8"))
                self.assertEqual(answer, expected)

        rows, columns, period, seed = (3, 3, 2, 1)
        open_cells = sum(
            not (
                row > 0
                and column > 0
                and (37 * row + 61 * column + seed) % period == 0
            )
            for row in range(rows)
            for column in range(columns)
        )
        self.assertLess(
            independent_reachable(rows, columns, period, seed), open_cells
        )

    def test_ci_subset_uses_stable_ids_across_bfs_core(self):
        fixture_root = (
            Path(__file__).parent
            / "fixtures/workspaces/mutation-mixed-runtime/M04"
        )
        source = (fixture_root / "code/std.cpp").read_text(encoding="utf-8")
        plan = plan_mutations(
            source,
            operators=MUTATION_OPERATORS,
            max_mutants=100,
        )
        by_id = {mutation.id: mutation for mutation in plan["mutations"]}
        selected_ids = benchmark.FIXTURE_PROFILES["mixed-runtime"][
            "ci_mutation_ids"
        ]
        selected = [by_id[mutation_id] for mutation_id in selected_ids]

        self.assertEqual(len(selected), 6)
        self.assertEqual(
            {mutation.operator for mutation in selected}, set(MUTATION_OPERATORS)
        )
        self.assertEqual(
            {mutation.line for mutation in selected}, {22, 34, 40, 43, 45}
        )

    def test_benchmark_plan_uses_the_pinned_bfs_core_subset(self):
        expected = benchmark.FIXTURE_PROFILES["mixed-runtime"][
            "ci_mutation_ids"
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan = benchmark.prepare_plan(
                Path(temp),
                max_mutants=6,
                operators=None,
                mutant_timeout=120,
                profile_name="mixed-runtime",
            )

        self.assertEqual(tuple(task.mutation_id for task in plan.tasks), expected)
        self.assertEqual(plan.candidate_counts["raw"], 38)
        self.assertEqual(plan.candidate_counts["selected"], 6)


@unittest.skipUnless(shutil.which("g++"), "g++ is required for fixture execution")
class MixedRuntimeMutationFixtureExecutionTests(unittest.TestCase):
    def test_no_cache_judge_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = copy_workspace_fixture("mutation-mixed-runtime", temp)
            result = judge_problem(
                fixture.root,
                fixture.problem,
                use_cache=False,
                timeout=120,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["final"]["code"], "all_expectations_met")

    def test_shadow_run_preserves_fixture_and_all_artifact_classes(self):
        report = benchmark.run_benchmark(
            jobs=1,
            timeout=180,
            max_mutants=1,
            operators=None,
            mutant_timeout=120,
            profile_name="mixed-runtime",
        )

        self.assertEqual(report["candidate_plan"]["raw"], 38)
        self.assertEqual(report["candidate_plan"]["selected"], 1)
        for field in (
            "live_inputs_unchanged",
            "formal_artifacts_unchanged",
            "mutation_evidence_unchanged",
            "judge_artifacts_unchanged",
            "fixture_unchanged",
            "workers_cleaned",
            "descendants_cleaned",
        ):
            self.assertTrue(report[field], field)


if __name__ == "__main__":
    unittest.main()
