import json
import tempfile
import time
import unittest
from pathlib import Path

from probhub.cli import build_parser
from probhub.errors import ProbHubError
from probhub.io import write_yaml
from probhub.linting import compute_source_hash, lint_workspace
from probhub.process_control import process_alive
from probhub.stressing import expand_generator_args, stress_problem
from probhub.workspace import load_problem, load_workspace, problem_entries


class StressTests(unittest.TestCase):
    def create_workspace(self, root, judge_type="standard"):
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
        })
        problem = root / "A"
        code = problem / "code"
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        code.mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# Stress\n\n## 题目描述\n\nD.\n\n## 输入格式\n\nI.\n\n## 输出格式\n\nO.\n",
            encoding="utf-8",
        )
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("2\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("2\n", encoding="utf-8")
        (code / "generator.py").write_text(
            "import sys\nseed=int(sys.argv[1])\nprint(seed % 4)\n",
            encoding="utf-8",
        )
        (code / "validator.py").write_text(
            "import sys\nvalue=sys.stdin.read().strip()\nint(value)\n",
            encoding="utf-8",
        )
        (code / "std.py").write_text(
            "import sys\nprint(int(sys.stdin.read()), '  ')\n",
            encoding="utf-8",
        )
        (code / "brute.py").write_text(
            "import sys\nx=int(sys.stdin.read())\nprint(0 if x == 2 else x)\n",
            encoding="utf-8",
        )
        judge = {"type": judge_type, "validator": "code/validator.py"}
        if judge_type == "custom":
            judge["checker"] = "code/checker.py"
            (code / "checker.py").write_text(
                "import sys\n"
                "answer=int(open(sys.argv[2], encoding='utf-8').read())\n"
                "actual=int(sys.stdin.read())\n"
                "raise SystemExit(42 if abs(answer) == abs(actual) else 43)\n",
                encoding="utf-8",
            )
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "Stress",
            "limits": {"time": 1, "memory": 256},
            "statement": {"source": "problem.md"},
            "judge": judge,
            "solutions": {
                "accepted": ["code/std.py"],
                "brute": ["code/brute.py"],
                "wrong": [],
            },
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
            "stress": {
                "generator": "code/generator.py",
                "args": ["{seed}"],
                "rounds": 10,
                "time_limit": 2,
            },
        }
        write_yaml(problem / "probhub.yaml", config)
        return problem, config

    def test_parser_accepts_stress_options(self):
        args = build_parser().parse_args([
            "stress", "A", "--rounds", "25", "--seed", "7", "--replay", "latest"
        ])
        self.assertEqual(args.problem, ["A"])
        self.assertEqual(args.rounds, 25)
        self.assertEqual(args.seed, 7)
        self.assertEqual(args.replay, "latest")

    def test_generator_arg_templates(self):
        self.assertEqual(
            expand_generator_args(["--seed", "{seed}", "--round", "{round}"], 9, 3),
            ["--seed", "9", "--round", "3"],
        )

    def test_saves_and_replays_first_counterexample(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root)
            result = stress_problem(root, problem, config, rounds=5, master_seed=1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "counterexample")
            self.assertEqual(result["reason"], "output_mismatch")
            self.assertEqual(result["round"], 2)
            self.assertEqual(result["seed"], 2)
            artifact = root / result["counterexample"]
            self.assertEqual((artifact / "input.in").read_text(encoding="utf-8").strip(), "2")
            self.assertTrue((artifact / "accepted.out").is_file())
            self.assertTrue((artifact / "brute.out").is_file())
            metadata = json.loads((artifact / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["master_seed"], 1)
            self.assertEqual(metadata["generator_args"], ["2"])
            self.assertIn("--replay", result["replay_command"])

            replay = stress_problem(root, problem, config, replay="latest")
            self.assertFalse(replay["ok"])
            self.assertTrue(replay["replay"])
            self.assertEqual(replay["reason"], "output_mismatch")

            (problem / "code/brute.py").write_text(
                "import sys\nprint(int(sys.stdin.read()))\n", encoding="utf-8"
            )
            replay = stress_problem(root, problem, config, replay=artifact)
            self.assertTrue(replay["ok"])
            self.assertEqual(replay["status"], "passed")
            self.assertNotIn("generator", [item["role"] for item in replay["compile"]])


    def test_replay_rejects_paths_outside_problem_stress_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root)
            outside = root / "outside.in"
            outside.write_text("2\n", encoding="utf-8")
            with self.assertRaisesRegex(ProbHubError, "must stay inside"):
                stress_problem(root, problem, config, replay=outside)

    def test_lint_rejects_stress_path_traversal_and_boolean_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root)
            (root / "outside.py").write_text("print(1)\n", encoding="utf-8")
            config["stress"]["generator"] = "../outside.py"
            config["stress"]["rounds"] = True
            config["stress"]["tool_timeout"] = float("nan")
            config["limits"]["output"] = 0
            config["limits"]["processes"] = False
            write_yaml(problem / "probhub.yaml", config)
            root, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            errors = result["problems"][0]["errors"]
            self.assertTrue(any("must stay inside" in error for error in errors), errors)
            self.assertIn("stress.rounds must be a positive integer", errors)
            self.assertIn("stress.tool_timeout must be a positive finite number", errors)
            self.assertIn("limits.output must be a positive integer in MB", errors)
            self.assertIn("limits.processes must be a positive integer", errors)
            entry = problem_entries(workspace)[0]
            problem, loaded = load_problem(root, entry)
            compute_source_hash(problem, loaded)


    def test_replay_rejects_invalid_seed_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root)
            result = stress_problem(root, problem, config, rounds=5, master_seed=1)
            artifact = root / result["counterexample"]
            metadata_path = artifact / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["seed"] = "not-a-number"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(ProbHubError, "invalid seed or round"):
                stress_problem(root, problem, config, replay=artifact)


    def test_generator_and_solution_output_limits_are_structured(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root)
            config["limits"]["output"] = 1
            (problem / "code/generator.py").write_text(
                "import sys\nsys.stdout.write('1\\n' * 1000000)\n", encoding="utf-8"
            )
            result = stress_problem(root, problem, config, rounds=1, master_seed=1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "infrastructure")
            self.assertEqual(result["reason"], "generator_ole")
            artifact = root / result["counterexample"]
            self.assertLessEqual((artifact / "generator.out").stat().st_size, 1024 * 1024)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root)
            config["limits"]["output"] = 1
            (problem / "code/std.py").write_text(
                "import sys\nsys.stdin.read()\nsys.stdout.write('x' * 2000000)\n",
                encoding="utf-8",
            )
            result = stress_problem(root, problem, config, rounds=1, master_seed=1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "counterexample")
            self.assertEqual(result["reason"], "accepted_ole")


    def test_checker_timeout_kills_descendant_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root, judge_type="custom")
            pid_file = problem / "checker-child.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            (problem / "code/checker.py").write_text(
                "import subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            config["stress"]["tool_timeout"] = 0.8
            result = stress_problem(root, problem, config, rounds=1, master_seed=1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "infrastructure")
            self.assertEqual(result["reason"], "checker_failed")
            self.assertTrue(pid_file.is_file(), "checker child did not start")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while process_alive(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_alive(child_pid), f"checker child {child_pid} survived")

    def test_custom_checker_compares_brute_against_standard_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, config = self.create_workspace(root, judge_type="custom")
            (problem / "code/brute.py").write_text(
                "import sys\nprint(-int(sys.stdin.read()))\n", encoding="utf-8"
            )
            result = stress_problem(root, problem, config, rounds=4, master_seed=1)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["judge_type"], "custom")
            self.assertEqual(result["rounds_completed"], 4)

    def test_lint_validates_stress_and_hashes_generator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem, _ = self.create_workspace(root)
            root, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertTrue(result["ok"], result)
            entry = problem_entries(workspace)[0]
            _, config = load_problem(root, entry)
            first_hash = compute_source_hash(problem, config)
            aliased_problem = problem / ".." / problem.name
            self.assertEqual(first_hash, compute_source_hash(aliased_problem, config))
            with (problem / "code/generator.py").open("a", encoding="utf-8") as stream:
                stream.write("# changed\n")
            self.assertNotEqual(first_hash, compute_source_hash(problem, config))

            config["stress"]["args"] = ["{unknown}"]
            write_yaml(problem / "probhub.yaml", config)
            errors = lint_workspace(root, workspace)["problems"][0]["errors"]
            self.assertIn("invalid stress.args template: {unknown}", errors)


if __name__ == "__main__":
    unittest.main()
