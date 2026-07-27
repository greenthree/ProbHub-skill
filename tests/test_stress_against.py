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

    def test_killer_outcome_classification(self):
        from probhub.stressing import _is_killer_outcome

        self.assertTrue(_is_killer_outcome({
            "kind": "counterexample", "reason": "output_mismatch",
        }))
        self.assertTrue(_is_killer_outcome({
            "kind": "counterexample", "reason": "brute_tle",
            "brute": {"reason": "time_limit"},
        }))
        # The target never started: infrastructure, not a kill.
        self.assertFalse(_is_killer_outcome({
            "kind": "counterexample", "reason": "brute_re",
            "brute": {"reason": "start_error"},
        }))
        self.assertFalse(_is_killer_outcome({
            "kind": "counterexample", "reason": "accepted_re",
        }))
        self.assertFalse(_is_killer_outcome({
            "kind": "infrastructure", "reason": "validator_rejected",
        }))

    def test_custom_judge_requires_checker_for_stress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ProbHubError) as raised:
                stress_problem(root, root, {
                    "id": "A",
                    "stress": {"generator": "code/inmaker.cpp"},
                    "judge": {"type": "custom", "validator": "code/validator.cpp"},
                })
            self.assertIn("judge.checker", str(raised.exception))

    def test_fixate_precheck_fails_before_hunting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
            self.assertEqual(code, 0)
            code, _ = run_cli(["--workspace", str(root), "--json", "new", "A"])
            self.assertEqual(code, 0)
            add_stress_config(root / "A")
            # An invalid case name fails before any compilation happens.
            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/wrong.cpp",
                "--fixate", "bad name!",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(result.get("code"), "fixate_invalid")
            # An existing secret case fails fast too.
            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/wrong.cpp",
                "--fixate", "random01",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(result.get("code"), "fixate_exists")


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

            code, confirmed = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/wrong.cpp", "--replay", "latest",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(confirmed["problems"]["A"]["status"], "killer_confirmed")

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

    def test_fixate_answer_comes_from_first_declared_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_problem(root)
            # Judge-equivalent but byte-different trusted solution for stress.
            with (problem / "code/std_spacey.cpp").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "#include <cstdio>\n"
                    "int main() {\n"
                    "    long long a, b;\n"
                    '    if (std::scanf("%lld %lld", &a, &b) != 2) return 1;\n'
                    '    std::printf("%lld \\n", a + b);\n'
                    "    return 0;\n"
                    "}\n"
                )
            config_path = problem / "probhub.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["stress"]["accepted"] = "code/std_spacey.cpp"
            with config_path.open("w", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)

            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/wrong.cpp",
                "--fixate", "k01", "--rounds", "30", "--seed", "7",
            ])
            self.assertEqual(code, 0, result)
            answer = (problem / "data/secret/k01.ans").read_bytes()
            self.assertNotIn(b" \n", answer)

            code, replayed = run_cli([
                "--workspace", str(root), "--json", "gen", "A", "--case", "k01",
            ])
            self.assertEqual(code, 0)
            by_case = {item["case"]: item for item in replayed["results"]}
            self.assertEqual(by_case["k01"]["state"], "unchanged")

    def test_fixate_rejects_undeclared_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_problem(root)
            shutil.copyfile(problem / "code/wrong.cpp", problem / "code/experiment.cpp")
            code, result = run_cli([
                "--workspace", str(root), "--json",
                "stress", "A", "--against", "code/experiment.cpp",
                "--fixate", "exp01", "--rounds", "30", "--seed", "7",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(result.get("code"), "fixate_undeclared")
            self.assertFalse((problem / "data/secret/exp01.in").exists())
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            cases = [item["case"] for item in config["data"].get("recipes") or []]
            self.assertNotIn("exp01", cases)

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
