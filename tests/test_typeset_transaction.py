import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probhub.building import typeset_workspace
from probhub.errors import ProbHubError
from probhub.io import write_yaml
from probhub.scaffold import scaffold_config, scaffold_files
from probhub.workspace import load_workspace, problem_entries


class TypesetTransactionTests(unittest.TestCase):
    def create_workspace(self, root):
        (root / ".probhub").mkdir(parents=True)
        typst_dir = root / "typst/contest"
        typst_dir.mkdir(parents=True)
        (typst_dir / "main.typ").write_text("// fixture\n", encoding="utf-8")
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "typst": {"directory": "typst/contest"},
            "problems": [
                {"id": "A", "directory": "A"},
                {"id": "B", "directory": "B"},
            ],
        })
        for problem_id, name in (("A", "Alpha"), ("B", "Beta")):
            problem_dir = root / problem_id
            for relative, content in scaffold_files(name, "standard").items():
                path = problem_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
            write_yaml(
                problem_dir / "probhub.yaml",
                scaffold_config(problem_id, name, "standard"),
            )
        _, workspace = load_workspace(root)
        return workspace

    def seed_artifacts(self, root):
        paths = [
            root / "typst/contest/problems.json",
            root / "typst/contest/main.pdf",
        ]
        for problem_id in ("A", "B"):
            problem_dir = root / problem_id
            paths.extend((
                problem_dir / "meta.json",
                problem_dir / "problem.pdf",
                problem_dir / "problem.yaml",
                problem_dir / "domjudge-problem.ini",
                problem_dir / ".probhub/build-manifest.json",
                root / f"{problem_id}.zip",
            ))
        before = {}
        for index, path in enumerate(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"old-{index}-{path.name}".encode("utf-8")
            path.write_bytes(payload)
            before[path] = payload
        return before

    def fake_compile(self, root, workspace, loaded):
        for problem_dir, config in loaded:
            (problem_dir / "meta.json").write_text(
                f'{{"id":"{config["id"]}"}}\n',
                encoding="utf-8",
            )
        typst_dir = root / "typst/contest"
        (typst_dir / "problems.json").write_text("[]\n", encoding="utf-8")
        main_pdf = typst_dir / "main.pdf"
        main_pdf.write_bytes(b"new main pdf")
        return typst_dir, main_pdf, []

    def fake_extract(self, main_pdf, loaded, only_ids=None):
        outputs = {}
        for problem_dir, config in loaded:
            problem_id = config["id"]
            if only_ids and problem_id not in only_ids:
                continue
            output = problem_dir / "problem.pdf"
            output.write_bytes(b"new problem pdf-" + problem_id.encode("ascii"))
            outputs[problem_id] = {"path": str(output), "pages": 1}
        return outputs

    def assert_artifacts_equal(self, before):
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload, path)

    def test_compile_failure_leaves_live_artifacts_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)

            def failing_compile(snapshot_root, snapshot_workspace, loaded):
                self.fake_compile(snapshot_root, snapshot_workspace, loaded)
                raise ProbHubError("injected Typst failure", code="typeset_failed")

            with patch("probhub.building.compile_collection", side_effect=failing_compile):
                with self.assertRaises(ProbHubError) as raised:
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertEqual(raised.exception.code, "typeset_failed")
            self.assert_artifacts_equal(before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])
            self.assertEqual(list(root.parent.glob(f".{root.name}-probhub-*")), [])

    def test_extraction_failure_does_not_publish_earlier_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)

            def failing_extract(main_pdf, loaded, only_ids=None):
                first_dir, _ = loaded[0]
                (first_dir / "problem.pdf").write_bytes(b"staged first PDF")
                raise ProbHubError("injected second problem extraction failure")

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=failing_extract),
            ):
                with self.assertRaisesRegex(ProbHubError, "second problem"):
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assert_artifacts_equal(before)

    def test_publish_failure_rolls_back_all_typeset_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)
            replacements = 0

            def failing_replace(source, target):
                nonlocal replacements
                replacements += 1
                if replacements == 4:
                    raise OSError("injected typeset publish failure")
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=self.fake_extract),
                patch("probhub.building._replace_publish_path", side_effect=failing_replace),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertEqual(raised.exception.code, "publish_failed")
            self.assert_artifacts_equal(before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])

    def test_input_change_during_publish_staging_aborts_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)
            statement = root / "A/problem.md"
            real_copyfile = __import__("shutil").copyfile
            changed = False

            def copy_and_change_live_input(source, target, *args, **kwargs):
                nonlocal changed
                result = real_copyfile(source, target, *args, **kwargs)
                target_path = Path(target)
                if not changed and any(
                    part.name.startswith("build-publish-")
                    for part in target_path.parents
                ):
                    statement.write_text(
                        statement.read_text(encoding="utf-8")
                        + "\nChanged while staging the publish transaction.\n",
                        encoding="utf-8",
                    )
                    changed = True
                return result

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=self.fake_extract),
                patch("probhub.building.shutil.copyfile", side_effect=copy_and_change_live_input),
                patch(
                    "probhub.building._replace_publish_path",
                    side_effect=AssertionError("commit must not begin"),
                ),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertTrue(changed)
            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertIn("A.source_hash", str(raised.exception))
            self.assert_artifacts_equal(before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])

    def test_success_publishes_only_typeset_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)
            entries = [problem_entries(workspace)[0]]

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                result = typeset_workspace(root, workspace, entries)

            self.assertTrue(result["ok"])
            self.assertEqual(set(result["pdfs"]), {"A"})
            self.assertEqual(
                Path(result["main_pdf"]).resolve(),
                (root / "typst/contest/main.pdf").resolve(),
            )
            self.assertEqual((root / "typst/contest/main.pdf").read_bytes(), b"new main pdf")
            self.assertEqual((root / "A/problem.pdf").read_bytes(), b"new problem pdf-A")
            self.assertEqual((root / "B/problem.pdf").read_bytes(), before[root / "B/problem.pdf"])
            for problem_id in ("A", "B"):
                for relative in (
                    "problem.yaml",
                    "domjudge-problem.ini",
                    ".probhub/build-manifest.json",
                ):
                    path = root / problem_id / relative
                    self.assertEqual(path.read_bytes(), before[path], path)
                package = root / f"{problem_id}.zip"
                self.assertEqual(package.read_bytes(), before[package], package)


if __name__ == "__main__":
    unittest.main()
