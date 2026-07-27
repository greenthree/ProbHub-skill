import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from probhub.cli import main as cli_main
from probhub.datagen import GEN_MANIFEST_PATH, generate_problem_data, problem_recipes, recipe_coverage
from probhub.errors import ProbHubError
from probhub.linting import lint_workspace
from probhub.workspace import load_problem, load_workspace


def run_cli(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(arguments)
    text = output.getvalue()
    return code, json.loads(text) if text.strip() else None


def set_recipes(problem_dir, recipes):
    config_path = Path(problem_dir) / "probhub.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["limits"]["time"] = 5  # headroom for loaded CI runners
    if recipes is None:
        (config.get("data") or {}).pop("recipes", None)
    else:
        config.setdefault("data", {})["recipes"] = recipes
    with config_path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
    return config


class RecipeParsingTests(unittest.TestCase):
    def test_parses_manual_and_generator_recipes(self):
        recipes = problem_recipes({
            "data": {
                "recipes": [
                    {"case": "max01", "args": ["max", 7]},
                    {"case": "hand01", "manual": True},
                    {"case": "special", "generator": "code/other.cpp", "args": "solo"},
                ],
            },
        })
        self.assertEqual(recipes[0], {
            "case": "max01", "manual": False, "generator": None, "args": ["max", "7"],
        })
        self.assertEqual(recipes[1], {"case": "hand01", "manual": True})
        self.assertEqual(recipes[2]["generator"], "code/other.cpp")
        self.assertEqual(recipes[2]["args"], ["solo"])

    def test_rejects_bad_structures(self):
        for bad in (
            {"data": {"recipes": "nope"}},
            {"data": {"recipes": [{"case": "a b"}]}},
            {"data": {"recipes": [{"case": "../x"}]}},
            {"data": {"recipes": [{"case": "a"}, {"case": "a"}]}},
            # Windows file systems collapse case-insensitive names.
            {"data": {"recipes": [{"case": "gen01"}, {"case": "GEN01"}]}},
            {"data": {"recipes": [{"case": "a", "manual": True, "args": ["x"]}]}},
            {"data": {"recipes": [{"case": "a", "args": {"k": 1}}]}},
        ):
            with self.assertRaises(ProbHubError):
                problem_recipes(bad)

    def test_normalize_newlines_collapses_cr_runs_and_keeps_bare_cr(self):
        from probhub.io import normalize_newlines

        self.assertEqual(normalize_newlines(b"a\r\nb\r\n"), b"a\nb\n")
        # Windows CRT doubles an explicit "\r\n" into "\r\r\n".
        self.assertEqual(normalize_newlines(b"a\r\r\nb"), b"a\nb")
        self.assertEqual(normalize_newlines(b"a\rb"), b"a\rb")

    def test_own_yaml_output_is_lf_only(self):
        from probhub.io import write_yaml

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "probe.yaml"
            write_yaml(target, {"schema_version": 1, "id": "A", "tags": ["x", "y"]})
            self.assertNotIn(b"\r\n", target.read_bytes())

    def test_coverage_lists_secret_cases_without_recipe(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            secret = problem / "data/secret"
            secret.mkdir(parents=True)
            for stem in ("alpha", "beta"):
                (secret / f"{stem}.in").write_text("1\n", encoding="utf-8")
            config = {"data": {"recipes": [{"case": "alpha", "manual": True}]}}
            _, uncovered = recipe_coverage(problem, config)
            self.assertEqual(uncovered, ["beta"])


class DatagenWorkspaceTests(unittest.TestCase):
    def make_workspace(self, root):
        code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
        self.assertEqual(code, 0)
        code, _ = run_cli(["--workspace", str(root), "--json", "new", "A"])
        self.assertEqual(code, 0)
        return root / "A"

    def test_gen_requires_recipes_and_known_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.make_workspace(Path(temp))
            set_recipes(problem, None)
            _, config = load_problem(Path(temp), {"id": "A", "directory": "A"})
            with self.assertRaises(ProbHubError) as raised:
                generate_problem_data(problem, config)
            self.assertEqual(raised.exception.code, "no_recipes")

            set_recipes(problem, [{"case": "x1", "manual": True}])
            _, config = load_problem(Path(temp), {"id": "A", "directory": "A"})
            with self.assertRaises(ProbHubError) as raised:
                generate_problem_data(problem, config, only=["missing"])
            self.assertEqual(raised.exception.code, "gen_config")

    def test_gen_rejects_interactive_problems(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
            self.assertEqual(code, 0)
            code, _ = run_cli([
                "--workspace", str(root), "--json", "new", "A", "--judge", "interactive",
            ])
            self.assertEqual(code, 0)
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            with self.assertRaises(ProbHubError) as raised:
                generate_problem_data(root / "A", config)
            self.assertEqual(raised.exception.code, "gen_unsupported")

    def test_gen_handles_all_manual_scaffold_without_compiling(self):
        # A fresh scaffold declares manual recipes for both secret cases, so a
        # plain plan run needs no toolchain at all — deterministically enforced
        # by making any compile attempt fail loudly.
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            with mock.patch(
                "probhub.datagen._prepare_program",
                side_effect=AssertionError("all-manual gen must not compile"),
            ):
                result = generate_problem_data(problem, config)
            self.assertTrue(result["ok"])
            self.assertEqual(result["pending"], [])
            self.assertEqual(result["summary"]["manual"], 2)
            self.assertEqual(result["summary"]["uncovered"], 0)

    def test_gen_rejects_escaping_secret_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            config_path = problem / "probhub.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["data"]["secret_dir"] = "../../escaped"
            with config_path.open("w", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
            _, loaded = load_problem(root, {"id": "A", "directory": "A"})
            with self.assertRaises(ProbHubError) as raised:
                generate_problem_data(problem, loaded, apply_changes=True)
            self.assertEqual(raised.exception.code, "gen_config")
            self.assertFalse((root.parent / "escaped").exists())

            _, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertTrue(any(
                "must stay inside the problem directory" in error
                for error in result["problems"][0]["errors"]
            ))

    def test_lint_warns_on_manual_recipe_without_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            (problem / "data/secret/random01.in").unlink()
            (problem / "data/secret/random01.ans").unlink()
            _, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertTrue(result["ok"], result["problems"][0]["errors"])
            self.assertTrue(any(
                "manual recipe(s) have no data files yet: random01" in warning
                for warning in result["problems"][0]["warnings"]
            ))

    @unittest.skipIf(os.name == "nt", "case-colliding files cannot coexist on Windows")
    def test_lint_rejects_case_colliding_secret_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            for stem in ("dup01", "DUP01"):
                (problem / f"data/secret/{stem}.in").write_text("1 2\n", encoding="utf-8")
                (problem / f"data/secret/{stem}.ans").write_text("3\n", encoding="utf-8")
            _, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertTrue(any(
                "collide case-insensitively" in error
                for error in result["problems"][0]["errors"]
            ))

    def test_gen_plan_writes_nothing_at_all(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
            ])

            def snapshot():
                return {
                    (path.relative_to(root).as_posix(), path.stat().st_size)
                    for path in root.rglob("*")
                    if path.is_file()
                }

            before = snapshot()
            code, result = run_cli(["--workspace", str(root), "--json", "gen", "A"])
            self.assertEqual(code, 0)
            self.assertFalse(result["applied"])
            self.assertFalse(result["manifest_written"])
            self.assertEqual(snapshot(), before)

    def test_gen_apply_fails_fast_when_writer_lock_is_busy(self):
        from probhub.build_lock import workspace_build_lock

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_workspace(root)
            with workspace_build_lock(root):
                code, result = run_cli([
                    "--workspace", str(root), "--json", "gen", "A", "--apply",
                ])
            self.assertEqual(code, 1)
            self.assertEqual(result.get("code"), "build_busy")

    def test_lint_reports_recipe_errors_and_coverage_warnings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            _, workspace = load_workspace(root)

            # The scaffold covers its secret cases with manual recipes.
            result = lint_workspace(root, workspace)
            self.assertTrue(result["ok"])
            self.assertFalse(any(
                "recipe" in warning.lower()
                for warning in result["problems"][0]["warnings"]
            ))

            set_recipes(problem, None)
            result = lint_workspace(root, workspace)
            self.assertTrue(result["ok"])
            self.assertTrue(any(
                "no generation recipes" in warning
                for warning in result["problems"][0]["warnings"]
            ))

            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "gen01", "generator": "code/nothere.cpp", "args": ["1"]},
            ])
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertTrue(any(
                "recipe generator not found" in error
                for error in result["problems"][0]["errors"]
            ))
            self.assertTrue(any(
                "have no generation recipe" in warning
                for warning in result["problems"][0]["warnings"]
            ))


@unittest.skipUnless(shutil.which("g++"), "g++ is required for datagen integration tests")
class DatagenIntegrationTests(unittest.TestCase):
    def make_workspace(self, root):
        code, _ = run_cli(["--json", "init", str(root), "--title", "Fixture"])
        self.assertEqual(code, 0)
        code, _ = run_cli(["--workspace", str(root), "--json", "new", "A"])
        self.assertEqual(code, 0)
        return root / "A"

    def test_plan_apply_replan_and_change_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "gen01", "args": ["small", "1"]},
            ])

            code, plan = run_cli(["--workspace", str(root), "--json", "gen", "A"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["pending"], ["gen01"])
            self.assertFalse((problem / "data/secret/gen01.in").exists())

            code, applied = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 0)
            self.assertTrue(applied["applied"])
            self.assertTrue((problem / "data/secret/gen01.in").is_file())
            self.assertTrue((problem / "data/secret/gen01.ans").is_file())
            self.assertTrue((problem / GEN_MANIFEST_PATH).is_file())
            self.assertNotIn(b"\r\n", (problem / "data/secret/gen01.in").read_bytes())

            code, replan = run_cli(["--workspace", str(root), "--json", "gen", "A"])
            self.assertEqual(code, 0)
            self.assertEqual(replan["pending"], [])
            self.assertEqual(replan["summary"]["unchanged"], 1)

            (problem / "data/secret/gen01.in").write_bytes(b"0 0\n")
            code, tampered = run_cli(["--workspace", str(root), "--json", "gen", "A"])
            self.assertEqual(code, 0)
            self.assertEqual(tampered["pending"], ["gen01"])
            by_case = {item["case"]: item for item in tampered["results"]}
            self.assertEqual(by_case["gen01"]["state"], "changed")
            self.assertIsNotNone(by_case["gen01"]["input"]["previous_sha256"])

            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "gen01", "args": ["small", "2"]},
            ])
            code, reseeded = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 0)
            self.assertTrue(reseeded["applied"])
            code, final_plan = run_cli(["--workspace", str(root), "--json", "gen", "A"])
            self.assertEqual(final_plan["pending"], [])

            # A CRLF copy normalises equal but is not byte-consistent: it must
            # show as changed, and --apply must restore the LF bytes.
            lf_bytes = (problem / "data/secret/gen01.in").read_bytes()
            (problem / "data/secret/gen01.in").write_bytes(lf_bytes.replace(b"\n", b"\r\n"))
            code, crlf_plan = run_cli(["--workspace", str(root), "--json", "gen", "A"])
            self.assertEqual(code, 0)
            self.assertEqual(crlf_plan["pending"], ["gen01"])
            code, _ = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 0)
            self.assertEqual((problem / "data/secret/gen01.in").read_bytes(), lf_bytes)

            code, judged = run_cli(["--workspace", str(root), "--json", "judge", "A"])
            self.assertEqual(code, 0, judged)

    def test_partial_case_apply_preserves_manifest_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "gen01", "args": ["small", "1"]},
                {"case": "gen02", "args": ["small", "2"]},
            ])
            code, _ = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 0)
            manifest = json.loads((problem / GEN_MANIFEST_PATH).read_text(encoding="utf-8"))
            self.assertEqual(sorted(manifest["cases"]), ["gen01", "gen02"])

            code, partial = run_cli([
                "--workspace", str(root), "--json", "gen", "A", "--case", "gen02", "--apply",
            ])
            self.assertEqual(code, 0)
            self.assertTrue(partial["manifest_written"])
            manifest = json.loads((problem / GEN_MANIFEST_PATH).read_text(encoding="utf-8"))
            self.assertEqual(sorted(manifest["cases"]), ["gen01", "gen02"])
            self.assertEqual(manifest["generator_sources"], ["code/inmaker.cpp"])

            # A corrupt manifest degrades loudly: warn and rebuild from this run.
            (problem / GEN_MANIFEST_PATH).write_text("{broken", encoding="utf-8")
            code, rebuilt = run_cli([
                "--workspace", str(root), "--json", "gen", "A", "--case", "gen02", "--apply",
            ])
            self.assertEqual(code, 0)
            self.assertTrue(any(
                "gen manifest is unreadable" in warning for warning in rebuilt["warnings"]
            ))
            manifest = json.loads((problem / GEN_MANIFEST_PATH).read_text(encoding="utf-8"))
            self.assertEqual(sorted(manifest["cases"]), ["gen02"])

    def test_generator_crash_and_accepted_failure_block_all_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            with (problem / "code/crashgen.cpp").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write("int main() { return 3; }\n")
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "boom01", "generator": "code/crashgen.cpp", "args": []},
            ])
            code, result = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 1)
            self.assertEqual(result["failures"][0]["stage"], "generator")
            self.assertFalse((problem / "data/secret/boom01.in").exists())
            self.assertFalse((problem / GEN_MANIFEST_PATH).exists())

            with (problem / "code/std.cpp").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write("int main() { return 7; }\n")
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "gen01", "args": ["small", "1"]},
            ])
            code, result = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 1)
            self.assertEqual(result["failures"][0]["stage"], "accepted")
            self.assertFalse((problem / "data/secret/gen01.in").exists())
            self.assertFalse((problem / GEN_MANIFEST_PATH).exists())

    def test_failures_block_all_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            with (problem / "code/badgen.cpp").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write('#include <cstdio>\nint main() { printf("9000000000 1\\n"); return 0; }\n')
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "good01", "args": ["small", "1"]},
                {"case": "bad01", "generator": "code/badgen.cpp", "args": []},
                {"case": "hand01", "manual": True},
            ])

            code, result = run_cli(["--workspace", str(root), "--json", "gen", "A", "--apply"])
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "gen_failed")
            self.assertFalse(result["applied"])
            failure = result["failures"][0]
            self.assertEqual(failure["case"], "bad01")
            self.assertEqual(failure["stage"], "validator")
            self.assertFalse((problem / "data/secret/good01.in").exists())
            self.assertFalse((problem / "data/secret/bad01.in").exists())
            self.assertFalse((problem / GEN_MANIFEST_PATH).exists())
            self.assertTrue(any(
                "manual recipe has no data files yet: hand01" in warning
                for warning in result["warnings"]
            ))


if __name__ == "__main__":
    unittest.main()
