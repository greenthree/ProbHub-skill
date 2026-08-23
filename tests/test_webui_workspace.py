import os
import tempfile
import unittest
from pathlib import Path

from probhub.errors import ProbHubError
from probhub.io import write_yaml
from probhub.webui_workspace import sandbox_problem_info


class SandboxProblemInfoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.problem = self.root / "A"
        (self.problem / "code").mkdir(parents=True)
        (self.problem / "data/sample").mkdir(parents=True)
        (self.problem / "data/secret").mkdir(parents=True)
        for relative in (
            "data/sample/1.in",
            "data/secret/1.in",
            "code/validator.cpp",
            "code/std.cpp",
            "code/brute.cpp",
            "code/wrong.cpp",
            "code/generator.cpp",
        ):
            (self.problem / relative).write_text("int main() {}\n", encoding="utf-8")
        self.workspace = {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
        }
        self.config = {
            "schema_version": 1,
            "id": "A",
            "display_name": "Alpha",
            "limits": {"time": 2, "memory": 512},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {
                "accepted": ["code/std.cpp"],
                "brute": ["code/brute.cpp"],
                "wrong": ["code/wrong.cpp"],
            },
            "generators": ["code/generator.cpp"],
        }
        write_yaml(self.problem / "probhub.yaml", self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_reads_limits_data_and_configured_roles(self):
        info = sandbox_problem_info(self.root, self.workspace, 0, script_exists=True)
        self.assertEqual(info["name"], "Alpha")
        self.assertEqual(info["limits"], {"time": 2, "memory": 512})
        self.assertEqual(info["sample_count"], 1)
        self.assertEqual(info["secret_count"], 1)
        self.assertEqual(info["data_count"], 2)
        self.assertEqual(info["files"]["validator"], "code/validator.cpp")
        self.assertEqual(info["files"]["std"], ["code/std.cpp"])
        self.assertEqual(info["files"]["brute"], ["code/brute.cpp"])
        self.assertEqual(info["files"]["wrong"], ["code/wrong.cpp"])
        self.assertEqual(info["files"]["other"], ["code/generator.cpp"])
        self.assertTrue(info["runnable"])

    def test_index_and_workspace_directory_boundaries_fail_closed(self):
        with self.assertRaises(ValueError):
            sandbox_problem_info(self.root, self.workspace, -1)
        with self.assertRaises(ValueError):
            sandbox_problem_info(self.root, self.workspace, 1)

        outside = self.root / "outside.cpp"
        outside.write_text("outside", encoding="utf-8")
        self.config["judge"]["validator"] = "../outside.cpp"
        self.config["solutions"]["accepted"] = ["C:/outside.cpp", "code/std.cpp"]
        self.config["generators"] = ["code/../outside.cpp", "code/generator.cpp"]
        write_yaml(self.problem / "probhub.yaml", self.config)
        info = sandbox_problem_info(self.root, self.workspace, 0, script_exists=True)
        self.assertFalse(info["files"]["validator"])
        self.assertEqual(info["files"]["std"], ["code/std.cpp"])
        self.assertEqual(info["files"]["other"], ["code/generator.cpp"])

    def test_missing_or_unsupported_config_is_reported_without_becoming_runnable(self):
        (self.problem / "probhub.yaml").unlink()
        info = sandbox_problem_info(self.root, self.workspace, 0, script_exists=True)
        self.assertEqual(info["reason"], "migration_required")
        self.assertFalse(info["runnable"])

        (self.problem / "probhub.yaml").write_text("id: [", encoding="utf-8")
        info = sandbox_problem_info(self.root, self.workspace, 0, script_exists=True)
        self.assertEqual(info["reason"], "invalid_config")
        self.assertFalse(info["runnable"])

        write_yaml(self.problem / "probhub.yaml", {"schema_version": 2, "id": "A"})
        info = sandbox_problem_info(self.root, self.workspace, 0, script_exists=True)
        self.assertEqual(info["reason"], "unsupported_schema")
        self.assertFalse(info["runnable"])

    def test_role_lists_are_counted_but_bounded(self):
        accepted = []
        for index in range(5):
            source = f"code/std-{index}.cpp"
            (self.problem / source).write_text("int main() {}\n", encoding="utf-8")
            accepted.append(source)
        self.config["solutions"]["accepted"] = accepted
        write_yaml(self.problem / "probhub.yaml", self.config)
        info = sandbox_problem_info(self.root, self.workspace, 0, files_per_role=2)
        self.assertEqual(info["file_counts"]["std"], 5)
        self.assertEqual(len(info["files"]["std"]), 2)
        self.assertTrue(info["files_truncated"])

    def test_linked_configured_files_are_ignored(self):
        outside = self.root / "outside.cpp"
        outside.write_text("outside", encoding="utf-8")
        linked = self.problem / "code/linked.cpp"
        try:
            os.symlink(outside, linked)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        self.config["solutions"]["wrong"] = ["code/linked.cpp"]
        write_yaml(self.problem / "probhub.yaml", self.config)
        info = sandbox_problem_info(self.root, self.workspace, 0)
        self.assertEqual(info["files"]["wrong"], [])


if __name__ == "__main__":
    unittest.main()
