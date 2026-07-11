import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from probhub.building import build_workspace
from probhub.cli import command_status
from probhub.io import write_yaml
from probhub.linting import problem_status
from probhub.workspace import load_problem, load_workspace, problem_entries


class BatchBuildTests(unittest.TestCase):
    def create_problem(self, root, problem_id, name):
        problem = root / problem_id
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            f"# {name}\n\n"
            "## 题目描述\n\nDescription.\n\n"
            "## 输入格式\n\nInput.\n\n"
            "## 输出格式\n\nOutput.\n",
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
            "id": problem_id,
            "name": name,
            "display_name": name,
            "limits": {"time": 1, "memory": 256, "output": 64},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        return problem

    def create_workspace(self, root):
        (root / ".probhub").mkdir(parents=True)
        (root / "typst/contest").mkdir(parents=True)
        (root / "typst/contest/main.typ").write_text("// fixture\n", encoding="utf-8")
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "typst": {"directory": "typst/contest"},
            "problems": [
                {"id": "A", "directory": "A"},
                {"id": "B", "directory": "B"},
            ],
        })
        self.create_problem(root, "A", "Alpha")
        self.create_problem(root, "B", "Beta")
        return load_workspace(root)

    def test_multi_problem_build_compiles_collection_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)

            def fake_judge(root, problem_dir, use_cache=True):
                return {
                    "ok": True,
                    "returncode": 0,
                    "final": {"type": "final", "status": "all_expectations_met"},
                }

            def fake_compile(root, workspace, loaded):
                main_pdf = root / "typst/contest/main.pdf"
                main_pdf.write_bytes(b"%PDF-1.4\n")
                return root / "typst/contest", main_pdf, []

            def fake_extract(main_pdf, loaded, only_ids=None):
                outputs = {}
                for problem_dir, config in loaded:
                    if only_ids and config["id"] not in only_ids:
                        continue
                    output = problem_dir / "problem.pdf"
                    output.write_bytes(b"%PDF-1.4\n" + config["id"].encode("ascii"))
                    outputs[config["id"]] = {"path": str(output), "pages": 1}
                return outputs

            def fake_package(root, problem_dir, config, require_pdf=True):
                output = root / f"{config['id']}.zip"
                output.write_bytes(b"zip-" + config["id"].encode("ascii"))
                return output, {"ok": True, "errors": [], "warnings": [], "stats": {}}

            with (
                patch("probhub.building.judge_problem", side_effect=fake_judge) as judge,
                patch("probhub.building.compile_collection", side_effect=fake_compile) as compile_collection,
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract) as extract,
                patch("probhub.building.package_problem", side_effect=fake_package) as package,
            ):
                result = build_workspace(
                    root,
                    workspace,
                    entries,
                    run_judge=True,
                    use_judge_cache=False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(judge.call_count, 2)
            self.assertEqual(compile_collection.call_count, 1)
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(package.call_count, 2)
            collection_hashes = {
                manifest["collection_hash"] for manifest in result["manifests"].values()
            }
            self.assertEqual(len(collection_hashes), 1)
            self.assertEqual(
                {manifest["schema_version"] for manifest in result["manifests"].values()},
                {2},
            )

            loaded = dict(
                (entry["id"], load_problem(root, entry))
                for entry in entries
            )
            for problem_id, (problem_dir, config) in loaded.items():
                with self.subTest(problem_id=problem_id):
                    self.assertEqual(
                        problem_status(problem_dir, config, root, workspace)["state"],
                        "current",
                    )

    def test_collection_hash_tracks_other_problem_typeset_inputs_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)
            problem_b, _ = load_problem(root, entries[1])
            assets = problem_b / "assets"
            assets.mkdir()
            image = assets / "diagram.png"
            original_image = b"first image"
            image.write_bytes(original_image)
            with (problem_b / "problem.md").open("a", encoding="utf-8") as stream:
                stream.write("\n![diagram](assets/diagram.png)\n")

            def fake_compile(root, workspace, loaded):
                main_pdf = root / "typst/contest/main.pdf"
                main_pdf.write_bytes(b"%PDF-1.4\n")
                return root / "typst/contest", main_pdf, []

            def fake_extract(main_pdf, loaded, only_ids=None):
                problem_dir, config = loaded[0]
                output = problem_dir / "problem.pdf"
                output.write_bytes(b"%PDF-1.4\nA")
                return {config["id"]: {"path": str(output), "pages": 1}}

            def fake_package(root, problem_dir, config, require_pdf=True):
                output = root / f"{config['id']}.zip"
                output.write_bytes(b"zip-A")
                return output, {"ok": True, "errors": [], "warnings": [], "stats": {}}

            with (
                patch("probhub.building.compile_collection", side_effect=fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract),
                patch("probhub.building.package_problem", side_effect=fake_package),
            ):
                build_workspace(root, workspace, [entries[0]], run_judge=False)

            problem_a, config_a = load_problem(root, entries[0])
            original_statement = (problem_b / "problem.md").read_text(encoding="utf-8")

            image.write_bytes(b"second image")
            status = problem_status(problem_a, config_a, root, workspace)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["collection_hash"])
            image.write_bytes(original_image)
            self.assertEqual(
                problem_status(problem_a, config_a, root, workspace)["state"],
                "current",
            )

            (problem_b / "problem.md").write_text(
                original_statement + "\nChanged.\n",
                encoding="utf-8",
            )
            status = problem_status(problem_a, config_a, root, workspace)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["collection_hash"])

            (problem_b / "problem.md").write_text(original_statement, encoding="utf-8")
            (problem_b / "data/secret/1.in").write_text("3\n", encoding="utf-8")
            self.assertEqual(
                problem_status(problem_a, config_a, root, workspace)["state"],
                "current",
            )

            (problem_b / "code/std.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
            self.assertEqual(
                problem_status(problem_a, config_a, root, workspace)["state"],
                "current",
            )

            (problem_b / "data/sample/1.in").write_text("4\n", encoding="utf-8")
            status = problem_status(problem_a, config_a, root, workspace)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["collection_hash"])

    def test_manifest_keeps_pre_typeset_collection_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)
            problem_b, _ = load_problem(root, entries[1])

            def fake_compile(root, workspace, loaded):
                statement = problem_b / "problem.md"
                statement.write_text(
                    statement.read_text(encoding="utf-8") + "\nChanged during build.\n",
                    encoding="utf-8",
                )
                main_pdf = root / "typst/contest/main.pdf"
                main_pdf.write_bytes(b"%PDF-1.4\n")
                return root / "typst/contest", main_pdf, []

            def fake_extract(main_pdf, loaded, only_ids=None):
                problem_dir, config = loaded[0]
                output = problem_dir / "problem.pdf"
                output.write_bytes(b"%PDF-1.4\nA")
                return {config["id"]: {"path": str(output), "pages": 1}}

            def fake_package(root, problem_dir, config, require_pdf=True):
                output = root / f"{config['id']}.zip"
                output.write_bytes(b"zip-A")
                return output, {"ok": True, "errors": [], "warnings": [], "stats": {}}

            with (
                patch("probhub.building.compile_collection", side_effect=fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract),
                patch("probhub.building.package_problem", side_effect=fake_package),
            ):
                build_workspace(root, workspace, [entries[0]], run_judge=False)

            problem_a, config_a = load_problem(root, entries[0])
            status = problem_status(problem_a, config_a, root, workspace)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["collection_hash"])

    def test_status_reuses_collection_hash_for_multiple_problems(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            args = SimpleNamespace(problem=["A", "B"])
            current = {"state": "current"}

            with (
                patch("probhub.cli.workspace_context", return_value=(root, workspace)),
                patch("probhub.cli.compute_workspace_hash", return_value="workspace") as workspace_hash,
                patch("probhub.cli.compute_collection_hash", return_value="collection") as collection_hash,
                patch("probhub.cli.problem_status", return_value=current) as status,
            ):
                result = command_status(args)

            self.assertTrue(result["ok"])
            workspace_hash.assert_called_once_with(root, workspace)
            collection_hash.assert_called_once_with(root, workspace)
            self.assertEqual(status.call_count, 2)
            for call in status.call_args_list:
                self.assertEqual(call.kwargs["workspace_hash"], "workspace")
                self.assertEqual(call.kwargs["collection_hash"], "collection")


if __name__ == "__main__":
    unittest.main()
