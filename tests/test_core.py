import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.cli import build_parser, main as cli_main
from probhub.errors import ProbHubError
from probhub.hashing import hash_file
from probhub.io import write_json, write_yaml
from probhub.linting import (
    BUILD_MANIFEST_SCHEMA_VERSION,
    compute_data_hash,
    compute_source_hash,
    lint_workspace,
    problem_status,
)
from probhub.metadata import build_meta
from probhub.workspace import load_problem, load_workspace, problem_entries


class CoreWorkspaceTests(unittest.TestCase):
    def create_workspace(self, root):
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
            "lint": {"forbidden_patterns": ["114514"]},
        })
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# Test\n\n## 题目描述\n\nDescription.\n\n## 输入格式\n\nInput.\n\n## 输出格式\n\nOutput.\n",
            encoding="utf-8",
        )
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("2\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("2\n", encoding="utf-8")
        (problem / "code/validator.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        write_yaml(problem / "probhub.yaml", {
            "schema_version": 1,
            "id": "A",
            "name": "Test",
            "display_name": "Test",
            "limits": {"time": 1, "memory": 256, "output": 64},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        return problem

    def test_init_then_new(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["--json", "init", str(root), "--title", "Contest"]), 0)
                self.assertEqual(cli_main(["--workspace", str(root), "--json", "new", "A", "--name", "First"]), 0)
            _, workspace = load_workspace(root)
            self.assertEqual(problem_entries(workspace)[0]["id"], "A")
            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("**/.probhub/build.lock", gitignore)
            self.assertIn("**/.probhub/stress/", gitignore)

    def test_cli_emits_machine_readable_error_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            output = io.StringIO()
            with (
                patch(
                    "probhub.cli.build_workspace",
                    side_effect=ProbHubError("busy", code="build_busy"),
                ),
                redirect_stdout(output),
            ):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "build",
                    "A",
                ])

            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["code"], "build_busy")

    def test_new_command_scaffolds_schema_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["--workspace", str(root), "--json", "new", "B", "--name", "Second"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue((root / "B/probhub.yaml").is_file())
            self.assertTrue((root / "B/code").is_dir())
            _, config = load_problem(root, {"id": "B", "directory": "B"})
            self.assertEqual(config["judge"]["validator"], "code/validator.cpp")
            self.assertEqual(config["limits"]["output"], 64)
            self.assertEqual(config["limits"]["processes"], 32)
            self.assertEqual(config["solutions"]["accepted"], ["code/std.cpp"])
            self.assertEqual(config["generators"], ["code/inmaker.cpp"])
            _, workspace = load_workspace(root)
            self.assertEqual([entry["id"] for entry in problem_entries(workspace)], ["A", "B"])

    def test_judge_and_build_accept_no_cache(self):
        parser = build_parser()
        self.assertTrue(parser.parse_args(["judge", "A", "--no-cache"]).no_cache)
        self.assertTrue(parser.parse_args(["build", "A", "--no-cache"]).no_cache)

    def test_lint_metadata_and_stale_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            _, config = load_problem(root, entry)
            lint = lint_workspace(root, workspace)
            self.assertTrue(lint["ok"], lint)
            meta = build_meta(problem, config)
            self.assertEqual(meta["problem"]["samples"], [{"input": "1", "output": "1"}])
            self.assertEqual(problem_status(problem, config)["state"], "never-built")
            (problem / "problem.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "A.zip").write_bytes(b"zip")
            manifest = {
                "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
                "batch_id": "fixture-batch",
                "source_hash": compute_source_hash(problem, config),
                "data_hash": compute_data_hash(problem, config),
                "pdf_hash": hash_file(problem / "problem.pdf"),
                "package_hash": hash_file(root / "A.zip"),
            }
            write_json(problem / ".probhub/build-manifest.json", manifest)
            self.assertEqual(problem_status(problem, config)["state"], "current")

            manifest["schema_version"] = 1
            write_json(problem / ".probhub/build-manifest.json", manifest)
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["manifest_schema"])

            manifest["schema_version"] = BUILD_MANIFEST_SCHEMA_VERSION
            write_json(problem / ".probhub/build-manifest.json", manifest)
            manifest.pop("batch_id")
            write_json(problem / ".probhub/build-manifest.json", manifest)
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["batch_id"])

            manifest["batch_id"] = "fixture-batch"
            write_json(problem / ".probhub/build-manifest.json", manifest)
            with (problem / "problem.md").open("a", encoding="utf-8") as stream:
                stream.write("\nchanged\n")
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertIn("source_hash", status["stale_fields"])

    def test_source_hash_tracks_problem_statement_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            assets = problem / "assets"
            assets.mkdir()
            image = assets / "diagram.png"
            image.write_bytes(b"first image")
            with (problem / "problem.md").open("a", encoding="utf-8") as stream:
                stream.write("\n![diagram](assets/diagram.png)\n")

            root, workspace = load_workspace(root)
            _, config = load_problem(root, problem_entries(workspace)[0])
            first_hash = compute_source_hash(problem, config)
            image.write_bytes(b"second image")
            self.assertNotEqual(first_hash, compute_source_hash(problem, config))

    def test_lint_validates_custom_checker_and_interactor_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            config_path = problem / "probhub.yaml"

            _, config = load_problem(root, entry)
            config["judge"] = {
                "type": "custom",
                "validator": "code/validator.cpp",
                "checker": "code/checker.cpp",
            }
            write_yaml(config_path, config)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn("checker not found: code/checker.cpp", result["problems"][0]["errors"])

            (problem / "code/checker.cpp").write_text("int main(){}\n", encoding="utf-8")
            self.assertTrue(lint_workspace(root, workspace)["ok"])

            config["judge"] = {
                "type": "interactive",
                "validator": "code/validator.cpp",
                "interactor": "code/interactor.cpp",
            }
            write_yaml(config_path, config)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn("interactor not found: code/interactor.cpp", result["problems"][0]["errors"])

            (problem / "code/interactor.cpp").write_text("int main(){}\n", encoding="utf-8")
            self.assertTrue(lint_workspace(root, workspace)["ok"])

            config["judge"]["interactive"] = {"idle_limit": 0, "transcript_limit": -1}
            write_yaml(config_path, config)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn(
                "judge.interactive.idle_limit must be positive",
                result["problems"][0]["errors"],
            )
            self.assertIn(
                "judge.interactive.transcript_limit must be a non-negative integer",
                result["problems"][0]["errors"],
            )

    def test_lint_validates_typst_cover_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            _, workspace = load_workspace(root)
            workspace["typst"] = {
                "directory": "typst/contest",
                "cover": {"logo": "../outside.png", "logo_width": "wide"},
            }
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn("typst.cover.logo must stay inside the Typst directory", result["errors"])
            self.assertIn("typst.cover.logo_width must be a Typst length", result["errors"])


if __name__ == "__main__":
    unittest.main()
