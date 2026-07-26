import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from probhub.build_lock import workspace_build_lock
from probhub.cli import main as cli_main
from probhub.judging import judge_problem
from probhub.linting import lint_workspace
from probhub.scaffold import JUDGE_TYPES, scaffold_config, scaffold_files
from probhub.workspace import load_problem, load_workspace


def run_cli(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(arguments)
    text = output.getvalue()
    return code, json.loads(text) if text.strip() else None


class ScaffoldTests(unittest.TestCase):
    def init_workspace(self, root):
        code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
        self.assertEqual(code, 0)

    def test_scaffold_lints_clean_and_creates_all_files_for_every_judge_type(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_workspace(root)
            for index, judge_type in enumerate(JUDGE_TYPES):
                problem_id = f"P{index}"
                code, result = run_cli([
                    "--workspace", str(root), "--json",
                    "new", problem_id, "--name", f"Demo {judge_type}",
                    "--judge", judge_type,
                ])
                self.assertEqual(code, 0)
                self.assertEqual(result["judge"], judge_type)
                problem_dir = root / problem_id
                for relative in scaffold_files(f"Demo {judge_type}", judge_type):
                    self.assertTrue(
                        (problem_dir / relative).is_file(),
                        f"{judge_type} scaffold missing {relative}",
                    )
                self.assertEqual(
                    (problem_dir / "code/checker.cpp").is_file(),
                    judge_type == "custom",
                )
                self.assertEqual(
                    (problem_dir / "code/interactor.cpp").is_file(),
                    judge_type == "interactive",
                )
                _, config = load_problem(root, {"id": problem_id, "directory": problem_id})
                self.assertEqual(config["judge"]["type"], judge_type)
                self.assertEqual(
                    [entry["file"] for entry in config["solutions"]["accepted"]],
                    ["code/std.cpp", "code/std2.cpp"],
                )
                self.assertEqual(config["solutions"]["brute"], [])
                self.assertEqual(
                    [entry["file"] for entry in config["solutions"]["wrong"]],
                    ["code/wrong.cpp", "code/wrong2.cpp"],
                )
                self.assertEqual(config["data"]["groups"][0]["name"], "overflow")

            _, workspace = load_workspace(root)
            lint = lint_workspace(root, workspace)
            self.assertTrue(
                lint["ok"],
                [problem["errors"] for problem in lint["problems"]],
            )

    def test_scaffold_statement_avoids_default_forbidden_patterns(self):
        for judge_type in JUDGE_TYPES:
            for content in scaffold_files("Demo", judge_type).values():
                for pattern in ("TODO", "FIXME", "114514", "待补充"):
                    self.assertNotIn(pattern, content)

    def test_scaffold_config_rejects_unknown_judge_type(self):
        with self.assertRaises(ValueError):
            scaffold_files("Demo", "batch")
        with self.assertRaises(ValueError):
            scaffold_config("A", "Demo", "float")

    def test_new_rejects_duplicate_id_and_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_workspace(root)
            code, _ = run_cli(["--workspace", str(root), "--json", "new", "A"])
            self.assertEqual(code, 0)
            code, result = run_cli(["--workspace", str(root), "--json", "new", "A"])
            self.assertEqual(code, 1)
            self.assertIn("already exists", result["error"])
            (root / "B").mkdir()
            (root / "B/keep.txt").write_text("x", encoding="utf-8")
            code, result = run_cli(["--workspace", str(root), "--json", "new", "B"])
            self.assertEqual(code, 1)
            self.assertIn("not empty", result["error"])

    def test_new_fails_fast_when_workspace_writer_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_workspace(root)
            with workspace_build_lock(root):
                code, result = run_cli(["--workspace", str(root), "--json", "new", "A"])
            self.assertEqual(code, 1)
            self.assertEqual(result.get("code"), "build_busy")
            self.assertFalse((root / "A").exists())


@unittest.skipUnless(shutil.which("g++"), "g++ is required for scaffold judge integration tests")
class ScaffoldJudgeIntegrationTests(unittest.TestCase):
    def relax_time_limit(self, problem_dir):
        # Loaded CI runners need headroom over the production default of 1s,
        # especially for interactive judging with its extra process startup.
        import yaml

        config_path = problem_dir / "probhub.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["limits"]["time"] = 5
        with config_path.open("w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)

    def test_fresh_scaffold_passes_judge_for_all_three_judge_types(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
            self.assertEqual(code, 0)
            for index, judge_type in enumerate(JUDGE_TYPES):
                problem_id = f"P{index}"
                code, _ = run_cli([
                    "--workspace", str(root), "--json",
                    "new", problem_id, "--judge", judge_type,
                ])
                self.assertEqual(code, 0)
                self.relax_time_limit(root / problem_id)
                result = judge_problem(root, root / problem_id)
                self.assertTrue(
                    result["ok"],
                    f"{judge_type} scaffold failed judge: {result['final']}",
                )
                self.assertEqual(result["final"].get("code"), "all_expectations_met")


if __name__ == "__main__":
    unittest.main()
