import io
import json
import os
import shlex
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from probhub.cli import main as cli_main
from probhub.errors import ProbHubError
from probhub.judging import judge_problem
from probhub.stressing import (
    _fixate_answer_bytes,
    _fixate_input_snapshot,
    _fixate_killer,
    _fixate_precheck,
    _publish_fixation,
    _recover_fixate_transactions,
    _stress_config,
    stress_problem,
)
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
    def test_fixate_conflicts_are_case_insensitive(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            secret = problem / "data" / "secret"
            secret.mkdir(parents=True)
            config = {
                "solutions": {"wrong": ["Code/Wrong.cpp"]},
                "data": {"secret_dir": "data/secret", "recipes": []},
            }
            self.assertEqual(
                _fixate_precheck(problem, config, "fresh", "code/wrong.cpp"),
                "Code/Wrong.cpp",
            )
            (secret / "Hunt01.IN").write_bytes(b"existing")
            with self.assertRaises(ProbHubError) as raised:
                _fixate_precheck(problem, config, "hunt01", "code/wrong.cpp")
            self.assertEqual(raised.exception.code, "fixate_exists")
            config["data"]["recipes"] = [{"case": "Recipe01"}]
            with self.assertRaises(ProbHubError) as raised:
                _fixate_precheck(problem, config, "recipe01", "code/wrong.cpp")
            self.assertEqual(raised.exception.code, "fixate_exists")

    def test_fixate_transaction_rolls_back_each_failed_replace(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temp:
                problem = Path(temp)
                secret = problem / "data" / "secret"
                secret.mkdir(parents=True)
                config_path = problem / "probhub.yaml"
                original = b"schema_version: 1\nid: A\n"
                config_path.write_bytes(original)
                calls = 0

                def replace(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(f"injected replace {fail_at}")
                    os.replace(source, target)

                with mock.patch("probhub.stressing._replace_fixate_path", side_effect=replace):
                    with self.assertRaises(ProbHubError) as raised:
                        _publish_fixation(
                            problem,
                            secret / "case.in",
                            secret / "case.ans",
                            config_path,
                            b"input\n",
                            b"answer\n",
                            {"schema_version": 1, "id": "A", "data": {}},
                        )
                self.assertEqual(raised.exception.code, "fixate_publish_failed")
                self.assertEqual(config_path.read_bytes(), original)
                self.assertFalse((secret / "case.in").exists())
                self.assertFalse((secret / "case.ans").exists())
                self.assertEqual(list(problem.glob(".probhub-fixate-*")), [])

    def test_fixate_transaction_stage_write_failure_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            secret = problem / "data" / "secret"
            secret.mkdir(parents=True)
            config_path = problem / "probhub.yaml"
            original = b"schema_version: 1\nid: A\n"
            config_path.write_bytes(original)
            with mock.patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_fixation(
                        problem,
                        secret / "case.in",
                        secret / "case.ans",
                        config_path,
                        b"input\n",
                        b"answer\n",
                        {"schema_version": 1, "id": "A"},
                    )
            self.assertEqual(raised.exception.code, "fixate_publish_failed")
            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse((secret / "case.in").exists())
            self.assertFalse((secret / "case.ans").exists())
            self.assertEqual(list(problem.glob(".probhub-fixate-*")), [])

    def test_fixate_transaction_restores_config_if_replace_raises_after_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            secret = problem / "data" / "secret"
            secret.mkdir(parents=True)
            config_path = problem / "probhub.yaml"
            original = b"schema_version: 1\nid: A\nname: original\n"
            config_path.write_bytes(original)
            calls = 0

            def replace_then_fail(source, target):
                nonlocal calls
                calls += 1
                os.replace(source, target)
                if calls == 3:
                    raise OSError("injected failure after config overwrite")

            with mock.patch(
                "probhub.stressing._replace_fixate_path",
                side_effect=replace_then_fail,
            ):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_fixation(
                        problem,
                        secret / "case.in",
                        secret / "case.ans",
                        config_path,
                        b"input\n",
                        b"answer\n",
                        {"schema_version": 1, "id": "A", "name": "replacement"},
                    )
            self.assertEqual(raised.exception.code, "fixate_publish_failed")
            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse((secret / "case.in").exists())
            self.assertFalse((secret / "case.ans").exists())
            self.assertEqual(list(problem.glob(".probhub-fixate-*")), [])

    def test_fixate_rollback_failure_preserves_and_recovers_backups(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            secret = problem / "data" / "secret"
            secret.mkdir(parents=True)
            config_path = problem / "probhub.yaml"
            original = b"schema_version: 1\nid: A\nname: original\n"
            config_path.write_bytes(original)
            real_replace = os.replace
            calls = 0

            def fail_second_publish(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publish failure")
                return real_replace(source, target)

            def fail_backup_restore(source, target):
                if Path(source).name.startswith("backup-"):
                    raise OSError("injected rollback failure")
                return real_replace(source, target)

            with (
                mock.patch(
                    "probhub.stressing._replace_fixate_path",
                    side_effect=fail_second_publish,
                ),
                mock.patch(
                    "probhub.stressing.os.replace",
                    side_effect=fail_backup_restore,
                ),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_fixation(
                        problem,
                        secret / "case.in",
                        secret / "case.ans",
                        config_path,
                        b"input\n",
                        b"answer\n",
                        {"schema_version": 1, "id": "A", "name": "replacement"},
                    )

            self.assertEqual(raised.exception.code, "fixate_rollback_failed")
            recovery_dirs = list(problem.glob(".probhub-fixate-*"))
            self.assertEqual(len(recovery_dirs), 1)
            self.assertTrue((recovery_dirs[0] / "journal.json").is_file())

            _recover_fixate_transactions(problem)
            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse((secret / "case.in").exists())
            self.assertFalse((secret / "case.ans").exists())
            self.assertEqual(list(problem.glob(".probhub-fixate-*")), [])

    def test_fixate_custom_answer_must_pass_checker(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            code = problem / "code"
            code.mkdir()
            for name in ("std.cpp", "checker.cpp"):
                (code / name).write_text("int main(){}\n", encoding="utf-8")
            config = {"solutions": {"accepted": ["code/std.cpp"]}, "limits": {"time": 1}}
            configured = {
                "checker": code / "checker.cpp",
                "tool_timeout": 1,
                "memory_limit": 256,
                "output_limit": 4,
                "process_limit": 8,
            }

            def prepare(_source, _build, role):
                return [role], None

            with (
                mock.patch("probhub.stressing._prepare_program", side_effect=prepare),
                mock.patch(
                    "probhub.stressing._run",
                    return_value={"status": "AC", "stdout": b"bad\n"},
                ),
                mock.patch(
                    "probhub.stressing._compare_custom",
                    return_value={"status": "WA", "match": False, "message": "bad jury"},
                ),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    _fixate_answer_bytes(problem, config, configured, b"1\n")
            self.assertEqual(raised.exception.code, "fixate_answer_failed")

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

    def test_fixate_rejects_live_config_and_source_drift(self):
        for drift in ("config", "generator"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
                self.assertEqual(code, 0)
                code, _ = run_cli(["--workspace", str(root), "--json", "new", "A"])
                self.assertEqual(code, 0)
                problem = root / "A"
                add_stress_config(problem)
                _, config = load_problem(root, {"id": "A", "directory": "A"})
                configured = _stress_config(problem, config, against="code/wrong.cpp")
                snapshot = _fixate_input_snapshot(problem, config, configured)
                if drift == "config":
                    config_path = problem / "probhub.yaml"
                    live = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                    live["stress"]["args"] = ["{seed}", "{round}"]
                    with config_path.open("w", encoding="utf-8", newline="\n") as stream:
                        yaml.safe_dump(live, stream, allow_unicode=True, sort_keys=False)
                else:
                    generator = problem / "code" / "inmaker.cpp"
                    generator.write_text(
                        generator.read_text(encoding="utf-8") + "\n// concurrent edit\n",
                        encoding="utf-8",
                    )
                with self.assertRaises(ProbHubError) as raised:
                    _fixate_killer(
                        root,
                        problem,
                        config,
                        configured,
                        {"input": b"1 1\n", "generator_args": ["7"]},
                        "drift01",
                        None,
                        "code/wrong.cpp",
                        snapshot,
                    )
                self.assertEqual(raised.exception.code, "fixate_inputs_changed")
                self.assertFalse((problem / "data/secret/drift01.in").exists())
                self.assertEqual(list(problem.glob(".probhub-fixate-*")), [])


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
            config_path = problem / "probhub.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["data"].setdefault("groups", []).append({
                "name": "HUNT01",
                "role": "wrong-solution-killer",
                "patterns": ["secret/HUNT01"],
                "targets": ["CODE/WRONG.CPP"],
            })
            with config_path.open("w", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)

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
            groups = {item["name"].casefold(): item for item in config["data"]["groups"]}
            self.assertEqual(groups["hunt01"]["name"], "HUNT01")
            self.assertEqual(groups["hunt01"]["patterns"], ["secret/hunt01"])
            self.assertEqual(groups["hunt01"]["targets"], ["code/wrong.cpp"])

            replay_tokens = shlex.split(hunt["replay_command"])
            self.assertEqual(replay_tokens[:3], ["probhub", "stress", "A"])
            self.assertIn("--against", replay_tokens)
            self.assertEqual(replay_tokens[replay_tokens.index("--against") + 1], "code/wrong.cpp")
            code, copied_replay = run_cli([
                "--workspace", str(root), "--json", *replay_tokens[1:],
            ])
            self.assertEqual(code, 0, copied_replay)
            self.assertEqual(copied_replay["problems"]["A"]["status"], "killer_confirmed")

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
