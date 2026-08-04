import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from probhub.cli import main as cli_main
from probhub.datagen import (
    GEN_MANIFEST_PATH,
    _publish_generated_files,
    _recover_generated_transactions,
    generate_problem_data,
    problem_config_identity,
    problem_recipes,
    recipe_coverage,
)
from probhub.errors import ProbHubError
from probhub.linting import compute_source_hash, lint_workspace
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

    def test_coverage_matches_recipe_case_insensitively(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            secret = problem / "data/secret"
            secret.mkdir(parents=True)
            (secret / "Alpha.in").write_text("1\n", encoding="utf-8")
            config = {"data": {"recipes": [{"case": "alpha", "manual": True}]}}
            _, uncovered = recipe_coverage(problem, config)
            self.assertEqual(uncovered, [])


class DatagenTransactionTests(unittest.TestCase):
    def make_published_files(self, root):
        secret = root / "data/secret"
        manifest = root / GEN_MANIFEST_PATH
        secret.mkdir(parents=True)
        manifest.parent.mkdir(parents=True)
        finals = [secret / "gen01.in", secret / "gen01.ans", manifest]
        for index, path in enumerate(finals):
            path.write_bytes(f"old-{index}\n".encode("ascii"))
        payloads = [
            (finals[0], b"new-input\n"),
            (finals[1], b"new-answer\n"),
            (finals[2], b'{\n  "schema_version": 1\n}\n'),
        ]
        return finals, payloads

    def test_each_replace_failure_rolls_back_every_published_file(self):
        real_replace = os.replace
        for fail_at in range(1, 7):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                finals, payloads = self.make_published_files(root)
                before = {path: path.read_bytes() for path in finals}
                calls = 0

                def injected_replace(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(f"injected replace failure {fail_at}")
                    return real_replace(source, target)

                with mock.patch("probhub.datagen.os.replace", side_effect=injected_replace):
                    with self.assertRaises(ProbHubError) as raised:
                        _publish_generated_files(root, payloads)
                self.assertEqual(raised.exception.code, "gen_write_failed")
                self.assertEqual({path: path.read_bytes() for path in finals}, before)
                self.assertEqual(list(root.glob(".probhub-gen-publish-*")), [])

    def test_manifest_stage_failure_changes_no_published_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            finals, payloads = self.make_published_files(root)
            before = {path: path.read_bytes() for path in finals}
            real_write_bytes = Path.write_bytes

            def injected_write(path, payload):
                if b'"schema_version"' in payload:
                    raise OSError("injected manifest staging failure")
                return real_write_bytes(path, payload)

            with mock.patch.object(Path, "write_bytes", autospec=True, side_effect=injected_write):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_generated_files(root, payloads)
            self.assertEqual(raised.exception.code, "gen_write_failed")
            self.assertEqual({path: path.read_bytes() for path in finals}, before)
            self.assertEqual(list(root.glob(".probhub-gen-publish-*")), [])

    def test_next_apply_recovers_interrupted_publish_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            final = root / "data/secret/gen01.in"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"new partial\n")
            transaction = root / ".probhub-gen-publish-interrupted"
            transaction.mkdir()
            (transaction / "backup-0").write_bytes(b"old complete\n")
            (transaction / "journal.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "entries": [{
                        "final": "data/secret/gen01.in",
                        "backup": "backup-0",
                        "existed": True,
                    }],
                }),
                encoding="utf-8",
            )

            _recover_generated_transactions(root)

            self.assertEqual(final.read_bytes(), b"old complete\n")
            self.assertFalse(transaction.exists())


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
        # All-manual recipe sets need no toolchain at all — deterministically
        # enforced by making any compile attempt fail loudly. The production
        # scaffold itself now includes one real generated recipe.
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
            ])
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
            (problem / "data/secret/overflow01.in").unlink()
            (problem / "data/secret/overflow01.ans").unlink()
            _, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertTrue(result["ok"], result["problems"][0]["errors"])
            self.assertTrue(any(
                "manual recipe(s) have no data files yet: overflow01" in warning
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

    def test_gen_apply_reloads_problem_config_after_acquiring_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
            ])

            @contextmanager
            def edit_before_lock_yields(_root):
                set_recipes(problem, [
                    {"case": "random01", "manual": True},
                ])
                yield

            with mock.patch(
                "probhub.cli.workspace_build_lock",
                side_effect=edit_before_lock_yields,
            ):
                code, result = run_cli([
                    "--workspace", str(root), "--json", "gen", "A", "--apply",
                ])

            self.assertEqual(code, 0, result)
            self.assertEqual(result["summary"]["manual"], 1)
            manifest = json.loads((problem / GEN_MANIFEST_PATH).read_text(encoding="utf-8"))
            self.assertEqual(manifest["cases"], {})

    def test_config_identity_fence_rejects_stale_and_midrun_config(self):
        import probhub.datagen as datagen

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            config_path = problem / "probhub.yaml"
            original = config_path.read_bytes()
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            expected = problem_config_identity(problem)
            before_data = {
                path.relative_to(problem).as_posix(): path.read_bytes()
                for path in (problem / "data").rglob("*")
                if path.is_file()
            }

            config_path.write_bytes(original + b"\n# concurrent edit\n")
            with self.assertRaises(ProbHubError) as raised:
                generate_problem_data(
                    problem,
                    config,
                    apply_changes=True,
                    expected_config_identity=expected,
                )
            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertFalse((problem / GEN_MANIFEST_PATH).exists())

            config_path.write_bytes(original)
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            expected = problem_config_identity(problem)
            real_merge = datagen._merged_manifest_cases

            def edit_during_publish(*args, **kwargs):
                merged = real_merge(*args, **kwargs)
                config_path.write_bytes(original + b"\n# edit during gen\n")
                return merged

            with mock.patch("probhub.datagen._merged_manifest_cases", side_effect=edit_during_publish):
                with self.assertRaises(ProbHubError) as raised:
                    generate_problem_data(
                        problem,
                        config,
                        apply_changes=True,
                        expected_config_identity=expected,
                    )
            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertFalse((problem / GEN_MANIFEST_PATH).exists())
            self.assertEqual({
                path.relative_to(problem).as_posix(): path.read_bytes()
                for path in (problem / "data").rglob("*")
                if path.is_file()
            }, before_data)

    def test_source_identity_fence_rejects_midrun_header_change(self):
        import probhub.datagen as datagen

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            header = problem / "code/helper.hpp"
            header.write_text("#pragma once\n", encoding="utf-8")
            expected_config = problem_config_identity(problem)
            expected_source = compute_source_hash(problem, config)
            real_merge = datagen._merged_manifest_cases

            def edit_source_during_publish(*args, **kwargs):
                merged = real_merge(*args, **kwargs)
                header.write_text("#pragma once\n#define CHANGED 1\n", encoding="utf-8")
                return merged

            with mock.patch(
                "probhub.datagen._merged_manifest_cases",
                side_effect=edit_source_during_publish,
            ):
                with self.assertRaises(ProbHubError) as raised:
                    generate_problem_data(
                        problem,
                        config,
                        apply_changes=True,
                        expected_config_identity=expected_config,
                        expected_source_hash=expected_source,
                    )
            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertFalse((problem / GEN_MANIFEST_PATH).exists())

    def test_custom_checker_acceptance_and_failure_classification(self):
        def fixture(root):
            problem = root / "A"
            (problem / "code").mkdir(parents=True)
            for name in ("validator.cpp", "std.cpp", "checker.cpp", "gen.cpp"):
                (problem / "code" / name).write_text("int main(){}\n", encoding="utf-8")
            config = {
                "schema_version": 1,
                "id": "A",
                "limits": {"time": 1, "memory": 256, "output": 4, "processes": 8},
                "judge": {
                    "type": "custom",
                    "validator": "code/validator.cpp",
                    "checker": "code/checker.cpp",
                },
                "solutions": {"accepted": ["code/std.cpp"]},
                "generators": ["code/gen.cpp"],
                "data": {
                    "secret_dir": "data/secret",
                    "recipes": [{"case": "gen01", "args": ["1"]}],
                },
            }
            return problem, config

        def prepare(_source, _build_dir, role):
            return [role], None

        def run(command, _input, *_args, **_kwargs):
            stdout = b"1 1\n" if command[0].startswith("generator") else b""
            if command[0] == "accepted":
                stdout = b"2\n"
            return {"status": "AC", "stdout": stdout, "stderr": b"", "message": ""}

        outcomes = (
            ({"verdict": "WA", "execution_status": "completed", "message": "rejected"}, "WA", None),
            ({"verdict": None, "execution_status": "completed", "message": "checker failed"}, "FAIL", None),
            ({
                "verdict": None,
                "execution_status": "time_limit",
                "message": "checker timed out",
            }, "FAIL", "TLE"),
        )
        for comparison, expected_status, execution_status in outcomes:
            with self.subTest(comparison=comparison), tempfile.TemporaryDirectory() as temp:
                problem, config = fixture(Path(temp))
                with (
                    mock.patch("probhub.datagen._prepare_program", side_effect=prepare),
                    mock.patch("probhub.datagen._run", side_effect=run),
                    mock.patch("probhub.datagen.run_checker_to_files", return_value=comparison),
                ):
                    result = generate_problem_data(problem, config, apply_changes=True)
                self.assertFalse(result["ok"])
                failure = result["failures"][0]
                self.assertEqual(failure["stage"], "checker")
                self.assertEqual(failure["status"], expected_status)
                self.assertEqual(failure.get("execution_status"), execution_status)
                self.assertFalse((problem / "data/secret/gen01.in").exists())
                self.assertFalse((problem / GEN_MANIFEST_PATH).exists())

        with tempfile.TemporaryDirectory() as temp:
            problem, config = fixture(Path(temp))
            accepted = {"verdict": "AC", "execution_status": "completed", "message": ""}

            def accept_checker(*args, **_kwargs):
                self.assertEqual(Path(args[3]).read_bytes(), b"2\n")
                return accepted

            with (
                mock.patch("probhub.datagen._prepare_program", side_effect=prepare),
                mock.patch("probhub.datagen._run", side_effect=run),
                mock.patch(
                    "probhub.datagen.run_checker_to_files", side_effect=accept_checker
                ) as checker,
            ):
                result = generate_problem_data(problem, config, apply_changes=True)
            self.assertTrue(result["ok"])
            self.assertTrue(result["applied"])
            self.assertEqual((problem / "data/secret/gen01.ans").read_bytes(), b"2\n")
            self.assertTrue((problem / GEN_MANIFEST_PATH).is_file())
            self.assertEqual(checker.call_args.args[2], checker.call_args.args[3])

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

    @unittest.skipIf(os.name == "nt", "case-insensitive file systems reuse the same path")
    def test_apply_reuses_existing_case_insensitive_stem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.make_workspace(root)
            set_recipes(problem, [
                {"case": "random01", "manual": True},
                {"case": "overflow01", "manual": True},
                {"case": "GEN01", "args": ["small", "1"]},
            ])
            lower_input = problem / "data/secret/gen01.in"
            lower_answer = problem / "data/secret/gen01.ans"
            lower_input.write_text("0 0\n", encoding="utf-8")
            lower_answer.write_text("0\n", encoding="utf-8")

            code, result = run_cli([
                "--workspace", str(root), "--json", "gen", "A", "--apply",
            ])

            self.assertEqual(code, 0, result)
            self.assertTrue(lower_input.is_file())
            self.assertTrue(lower_answer.is_file())
            self.assertFalse((problem / "data/secret/GEN01.in").exists())
            self.assertFalse((problem / "data/secret/GEN01.ans").exists())

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
