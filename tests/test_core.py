import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from probhub.cli import build_parser, main as cli_main
from probhub.hashing import hash_file
from probhub.io import write_json, write_yaml
from probhub.linting import compute_data_hash, compute_source_hash, lint_workspace, problem_status
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
                "source_hash": compute_source_hash(problem, config),
                "data_hash": compute_data_hash(problem, config),
                "pdf_hash": hash_file(problem / "problem.pdf"),
                "package_hash": hash_file(root / "A.zip"),
            }
            write_json(problem / ".probhub/build-manifest.json", manifest)
            self.assertEqual(problem_status(problem, config)["state"], "current")
            with (problem / "problem.md").open("a", encoding="utf-8") as stream:
                stream.write("\nchanged\n")
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertIn("source_hash", status["stale_fields"])


if __name__ == "__main__":
    unittest.main()
