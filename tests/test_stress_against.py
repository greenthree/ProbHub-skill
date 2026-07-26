import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from probhub.cli import main as cli_main
from probhub.errors import ProbHubError
from probhub.judging import judge_problem
from probhub.stressing import stress_problem
from probhub.workspace import load_problem


def run_cli(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(arguments)
    text = output.getvalue()
    return code, json.loads(text) if text.strip() else None


def add_stress_config(problem_dir):
    config_path = Path(problem_dir) / "probhub.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["limits"]["time"] = 5  # headroom for loaded CI runners
    config["stress"] = {
        "generator": "code/inmaker.cpp",
        "args": ["{seed}"],
        "brute": "code/std.cpp",
        "rounds": 50,
    }
    with config_path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)


class StressAgainstValidationTests(unittest.TestCase):
    def test_fixate_flag_requires_against(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ProbHubError) as raised:
                stress_problem(root, root, {"id": "A"}, fixate="k01")
            self.assertIn("--fixate requires --against", str(raised.exception))

    def test_fixate_flag_rejects_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ProbHubError) as raised:
                stress_problem(
                    root, root, {"id": "A"},
                    replay="latest", against="code/wrong.cpp", fixate="k01",
                )
            self.assertIn("--fixate cannot be combined with --replay", str(raised.exception))


@unittest.skipUnless(shutil.which("g++"), "g++ is required for stress --against integration tests")
class StressAgainstIntegrationTests(unittest.TestCase):
    def make_problem(self, root):
        code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
        self.assertEqual(code, 0)
        code, _ = run_cli(["--workspace", str(root), "--json", "new", "A"])
        self.assertEqual(code, 0)
        add_stress_config(root / "A")
        return root / "A"

    def test_killer_hunt_fixates_and_closes_the_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_problem(root)

            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/wrong.cpp",
                "--fixate", "hunt01", "--rounds", "30", "--seed", "7",
            ])
            self.assertEqual(code, 0, result)
            hunt = result["problems"]["A"]
            self.assertEqual(hunt["status"], "killer_found")
            self.assertTrue(hunt["ok"])
            fixated = hunt["fixated"]
            self.assertEqual(fixated["case"], "hunt01")
            self.assertEqual(fixated["target"], "code/wrong.cpp")
            self.assertTrue((problem / "data/secret/hunt01.in").is_file())
            self.assertTrue((problem / "data/secret/hunt01.ans").is_file())
            self.assertNotIn(b"\r\n", (problem / "data/secret/hunt01.in").read_bytes())

            _, config = load_problem(root, {"id": "A", "directory": "A"})
            recipes = {item["case"]: item for item in config["data"]["recipes"]}
            self.assertEqual(recipes["hunt01"]["generator"], "code/inmaker.cpp")
            self.assertEqual(recipes["hunt01"]["args"], ["7"])
            groups = {item["name"]: item for item in config["data"]["groups"]}
            self.assertIn("secret/hunt01", groups["hunt01"]["patterns"])
            self.assertIn("code/wrong.cpp", groups["hunt01"]["targets"])

            code, replayed = run_cli(["--workspace", str(root), "--json", "gen", "A", "--case", "hunt01"])
            self.assertEqual(code, 0)
            by_case = {item["case"]: item for item in replayed["results"]}
            self.assertEqual(by_case["hunt01"]["state"], "unchanged")

            judged = judge_problem(root, problem)
            self.assertTrue(judged["ok"], judged["final"])

            code, again = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/wrong.cpp",
                "--fixate", "hunt01", "--rounds", "5", "--seed", "7",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(again.get("code"), "fixate_exists")

    def test_equivalent_target_reports_not_separated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_problem(root)
            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/std2.cpp",
                "--rounds", "4", "--seed", "11",
            ])
            self.assertEqual(code, 0)
            outcome = result["problems"]["A"]
            self.assertTrue(outcome["ok"])
            self.assertEqual(outcome["status"], "not_separated")
            self.assertEqual(outcome["rounds_completed"], 4)

    def test_normal_stress_semantics_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_problem(root)
            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--rounds", "3", "--seed", "5",
            ])
            self.assertEqual(code, 0)
            outcome = result["problems"]["A"]
            self.assertEqual(outcome["status"], "passed")
            self.assertIsNone(outcome["counterexample"])


if __name__ == "__main__":
    unittest.main()
