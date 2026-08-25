import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.build_lock import workspace_build_lock, workspace_file_lock
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
        code, result = run_cli(["--json", "init", str(root), "--title", "Fixture"])
        self.assertEqual(code, 0)
        return result

    def test_init_installs_reproducible_typst_template_without_overwriting_existing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.init_workspace(root)
            expected = {
                "typst-statement/lib.typ": "references/lib.typ",
                "typst-statement/probhub.svg": "logo.svg",
                "typst-statement/正式赛/main.typ": "references/main.typ",
                "typst-statement/正式赛/problems.typ": "references/problems.typ",
            }
            package_root = Path(__file__).resolve().parents[1]
            for target, source in expected.items():
                self.assertEqual((root / target).read_bytes(), (package_root / source).read_bytes())
            _, workspace = load_workspace(root, allow_empty=True)
            self.assertEqual(workspace["typst"]["creation_timestamp"], 0)
            self.assertEqual(workspace["typst"]["cover"], {
                "logo": "probhub.svg",
                "logo_width": "9cm",
                "logo_space_above": "0em",
                "logo_space_below": "0em",
            })
            self.assertFalse((root / "typst-statement/usts.png").exists())
            self.assertEqual(len(result["typst"]["created"]), 4)

            main_typ = root / "typst-statement/正式赛/main.typ"
            main_typ.write_text("// custom\n", encoding="utf-8")
            code, forced = run_cli(["--json", "init", str(root), "--title", "Changed", "--force"])
            self.assertEqual(code, 0)
            self.assertEqual(main_typ.read_text(encoding="utf-8"), "// custom\n")
            self.assertIn(
                main_typ.resolve(),
                [Path(value).resolve() for value in forced["typst"]["preserved"]],
            )

    def test_init_rejects_path_like_subtitle_before_writing_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            code, result = run_cli(["--json", "init", str(root), "--subtitle", "../escape"])
            self.assertEqual(code, 1)
            self.assertEqual(result["code"], "invalid_subtitle")
            self.assertFalse((root / ".probhub/workspace.yaml").exists())
            self.assertFalse((root / "typst-statement").exists())

    def test_init_rejects_control_characters_and_linked_template_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            for subtitle in ("bad\x01name", "bad\x7fname"):
                code, result = run_cli(["--json", "init", str(root), "--subtitle", subtitle])
                self.assertEqual(code, 1)
                self.assertEqual(result["code"], "invalid_subtitle")
                self.assertFalse((root / ".probhub/workspace.yaml").exists())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "typst-statement").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            code, result = run_cli(["--json", "init", str(root)])
            self.assertEqual(code, 1)
            self.assertEqual(result["code"], "init_path_link")
            self.assertEqual(list(outside.iterdir()), [])

    def test_init_rolls_back_templates_workspace_and_gitignore_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "new-workspace"
            with patch("probhub.cli.write_yaml", side_effect=OSError("fixture write failure")):
                code, result = run_cli(["--json", "init", str(root)])
            self.assertEqual(code, 1)
            self.assertEqual(result["code"], "init_write_failed")
            self.assertFalse((root / ".probhub/workspace.yaml").exists())
            self.assertFalse((root / ".gitignore").exists())
            self.assertFalse((root / "typst-statement").exists())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_workspace(root)
            workspace_path = root / ".probhub/workspace.yaml"
            gitignore_path = root / ".gitignore"
            workspace_before = workspace_path.read_bytes()
            gitignore_before = gitignore_path.read_bytes()
            templates_before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in (root / "typst-statement").rglob("*")
                if path.is_file()
            }
            with patch("probhub.cli._ensure_local_gitignore", side_effect=OSError("fixture gitignore failure")):
                code, result = run_cli([
                    "--json", "init", str(root), "--force", "--title", "Changed",
                ])
            self.assertEqual(code, 1)
            self.assertEqual(result["code"], "init_write_failed")
            self.assertEqual(workspace_path.read_bytes(), workspace_before)
            self.assertEqual(gitignore_path.read_bytes(), gitignore_before)
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in (root / "typst-statement").rglob("*")
                    if path.is_file()
                },
                templates_before,
            )

    def test_init_fails_fast_when_workspace_writer_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with workspace_file_lock(root, ".probhub/build.lock"):
                code, result = run_cli(["--json", "init", str(root)])
            self.assertEqual(code, 1)
            self.assertEqual(result["code"], "build_busy")
            self.assertFalse((root / ".probhub/workspace.yaml").exists())

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
                self.assertEqual(
                    config["solutions"]["accepted"][1]["independence"],
                    {
                        "from": "code/std.cpp",
                        "basis": "key_implementation",
                        "note": "使用按位异或与进位迭代实现加法，不依赖 std.cpp 的直接算术相加。",
                    },
                )
                std2 = (problem_dir / "code/std2.cpp").read_text(encoding="utf-8")
                self.assertIn("x ^= y", std2)
                self.assertIn("(x & y) << 1", std2)
                self.assertNotIn("a + b", std2)
                self.assertEqual(config["solutions"]["brute"], [])
                self.assertEqual(
                    [entry["file"] for entry in config["solutions"]["wrong"]],
                    ["code/wrong.cpp", "code/wrong2.cpp"],
                )
                self.assertEqual(config["data"]["groups"][0]["name"], "overflow")
                expected_random_recipe = (
                    {"case": "random01", "manual": True}
                    if judge_type == "interactive"
                    else {
                        "case": "random01",
                        "generator": "code/inmaker.cpp",
                        "args": ["random", "1"],
                    }
                )
                self.assertEqual(config["data"]["recipes"][0], expected_random_recipe)

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

    def test_new_rejects_unsafe_ids_and_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_workspace(root)
            code, result = run_cli(["--workspace", str(root), "--json", "new", "../pwn"])
            self.assertEqual(code, 1)
            self.assertIn("invalid problem id", result["error"])
            code, result = run_cli([
                "--workspace", str(root), "--json", "new", "A", "--directory", "../outside",
            ])
            self.assertEqual(code, 1)
            self.assertIn("stay inside the workspace", result["error"])
            self.assertFalse((root.parent / "outside").exists())
            code, result = run_cli([
                "--workspace", str(root), "--json", "new", "A", "--directory", "problems/L05",
            ])
            self.assertEqual(code, 0)
            self.assertTrue((root / "problems/L05/probhub.yaml").is_file())
            _, workspace = load_workspace(root)
            self.assertEqual(workspace["problems"][0]["directory"], "problems/L05")

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
