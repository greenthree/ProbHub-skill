import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from probhub.build_lock import workspace_build_lock
from probhub.building import (
    _assert_publish_targets_available,
    build_workspace,
    publish_build,
)
from probhub.cli import command_status
from probhub.errors import ProbHubError
from probhub.hashing import hash_file
from probhub.io import write_yaml
from probhub.linting import problem_status
from probhub.workspace import load_problem, load_workspace, problem_entries


class BatchBuildTests(unittest.TestCase):
    def write_compiled_fixture(self, root, loaded):
        typst_dir = root / "typst/contest"
        for problem_dir, config in loaded:
            (problem_dir / "meta.json").write_text(
                '{"id":"' + config["id"] + '"}\n',
                encoding="utf-8",
            )
        (typst_dir / "problems.json").write_text("[]\n", encoding="utf-8")
        main_pdf = typst_dir / "main.pdf"
        main_pdf.write_bytes(b"%PDF-1.4\n")
        return typst_dir, main_pdf, []

    def write_package_fixture(self, root, problem_dir, config, require_pdf=True):
        (problem_dir / "problem.yaml").write_text(
            f"name: {config['id']}\nlimits:\n  memory: 256\n",
            encoding="utf-8",
        )
        (problem_dir / "domjudge-problem.ini").write_text(
            "timelimit='1'\n",
            encoding="utf-8",
        )
        output = root / f"{config['id']}.zip"
        output.write_bytes(b"zip-" + config["id"].encode("ascii"))
        return output, {"ok": True, "errors": [], "warnings": [], "stats": {}}

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
                return self.write_compiled_fixture(root, loaded)

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
                return self.write_package_fixture(
                    root,
                    problem_dir,
                    config,
                    require_pdf=require_pdf,
                )

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
            batch_ids = {
                manifest["batch_id"] for manifest in result["manifests"].values()
            }
            self.assertEqual(batch_ids, {result["batch_id"]})
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

    def test_build_publishes_compiled_binaries_with_matching_cache_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)

            def fake_judge(root, problem_dir, use_cache=True):
                binary = problem_dir / "code/std.exe"
                binary.write_bytes(b"compiled-" + problem_dir.name.encode("ascii"))
                compile_entries = {
                    "code/std.cpp": {
                        "fingerprint": "fixture",
                        "binary_digest": hash_file(binary),
                    },
                    "code/missing.cpp": {
                        "fingerprint": "missing",
                        "binary_digest": "0" * 64,
                    },
                    "../escape.cpp": {
                        "fingerprint": "escape",
                        "binary_digest": hash_file(binary),
                    },
                    "C:/escape.cpp": {
                        "fingerprint": "drive",
                        "binary_digest": hash_file(binary),
                    },
                    "..\\escape.cpp": {
                        "fingerprint": "backslash",
                        "binary_digest": hash_file(binary),
                    },
                }
                cache_path = problem_dir / ".probhub/sandbox-cache-v1.json"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps({
                        "schema_version": 2,
                        "compile": compile_entries,
                        "validator": {},
                        "case": {},
                    }),
                    encoding="utf-8",
                )
                return {
                    "ok": True,
                    "returncode": 0,
                    "final": {"type": "final", "status": "passed", "code": "all_expectations_met"},
                }

            def fake_extract(main_pdf, loaded, only_ids=None):
                outputs = {}
                for problem_dir, config in loaded:
                    if only_ids and config["id"] not in only_ids:
                        continue
                    output = problem_dir / "problem.pdf"
                    output.write_bytes(b"%PDF-1.4\n" + config["id"].encode("ascii"))
                    outputs[config["id"]] = {"path": str(output), "pages": 1}
                return outputs

            with (
                patch("probhub.building.judge_problem", side_effect=fake_judge),
                patch(
                    "probhub.building.compile_collection",
                    side_effect=lambda root, workspace, loaded: self.write_compiled_fixture(root, loaded),
                ),
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract),
                patch("probhub.building.package_problem", side_effect=self.write_package_fixture),
            ):
                build_workspace(root, workspace, entries)

            for entry in entries:
                problem_dir, _ = load_problem(root, entry)
                with self.subTest(problem_id=entry["id"]):
                    binary = problem_dir / "code/std.exe"
                    self.assertEqual(
                        binary.read_bytes(),
                        b"compiled-" + entry["id"].encode("ascii"),
                    )
                    cache = json.loads(
                        (problem_dir / ".probhub/sandbox-cache-v1.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(list(cache["compile"]), ["code/std.cpp"])
            self.assertFalse((root.parent / "escape.exe").exists())

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
                return self.write_compiled_fixture(root, loaded)

            def fake_extract(main_pdf, loaded, only_ids=None):
                problem_dir, config = loaded[0]
                output = problem_dir / "problem.pdf"
                output.write_bytes(b"%PDF-1.4\nA")
                return {config["id"]: {"path": str(output), "pages": 1}}

            def fake_package(root, problem_dir, config, require_pdf=True):
                return self.write_package_fixture(
                    root,
                    problem_dir,
                    config,
                    require_pdf=require_pdf,
                )

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

    def test_build_rejects_inputs_changed_before_manifest_publish(self):
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
                return self.write_compiled_fixture(root, loaded)

            def fake_extract(main_pdf, loaded, only_ids=None):
                problem_dir, config = loaded[0]
                output = problem_dir / "problem.pdf"
                output.write_bytes(b"%PDF-1.4\nA")
                return {config["id"]: {"path": str(output), "pages": 1}}

            def fake_package(root, problem_dir, config, require_pdf=True):
                return self.write_package_fixture(
                    root,
                    problem_dir,
                    config,
                    require_pdf=require_pdf,
                )

            with (
                patch("probhub.building.compile_collection", side_effect=fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract),
                patch("probhub.building.package_problem", side_effect=fake_package),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    build_workspace(root, workspace, [entries[0]], run_judge=False)

            self.assertEqual(raised.exception.code, "inputs_changed")
            problem_a, _ = load_problem(root, entries[0])
            self.assertFalse((problem_a / ".probhub/build-manifest.json").exists())

    def test_build_rejects_selected_data_changed_after_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)
            problem_a, _ = load_problem(root, entries[0])

            def fake_compile(root, workspace, loaded):
                (problem_a / "data/secret/1.in").write_text("changed\n", encoding="utf-8")
                return self.write_compiled_fixture(root, loaded)

            def fake_extract(main_pdf, loaded, only_ids=None):
                problem_dir, config = loaded[0]
                output = problem_dir / "problem.pdf"
                output.write_bytes(b"%PDF-1.4\nA")
                return {config["id"]: {"path": str(output), "pages": 1}}

            with (
                patch("probhub.building.compile_collection", side_effect=fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract),
                patch(
                    "probhub.building.package_problem",
                    side_effect=self.write_package_fixture,
                ),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    build_workspace(root, workspace, [entries[0]], run_judge=False)

            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertIn("A.data_hash", str(raised.exception))
            self.assertFalse((problem_a / ".probhub/build-manifest.json").exists())

    def test_later_problem_failure_leaves_all_live_artifacts_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)
            artifacts = []
            for entry in entries:
                problem_dir, _ = load_problem(root, entry)
                for relative, content in (
                    ("meta.json", b"old meta"),
                    ("problem.pdf", b"old problem pdf"),
                    ("problem.yaml", b"old yaml"),
                    ("domjudge-problem.ini", b"old ini"),
                    (".probhub/build-manifest.json", b"old manifest"),
                ):
                    path = problem_dir / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content + entry["id"].encode("ascii"))
                    artifacts.append((path, path.read_bytes()))
                package = root / f"{entry['id']}.zip"
                package.write_bytes(b"old zip" + entry["id"].encode("ascii"))
                artifacts.append((package, package.read_bytes()))
            for relative, content in (
                ("typst/contest/main.pdf", b"old main pdf"),
                ("typst/contest/problems.json", b"old problems"),
            ):
                path = root / relative
                path.write_bytes(content)
                artifacts.append((path, path.read_bytes()))

            def fake_extract(main_pdf, loaded, only_ids=None):
                outputs = {}
                for problem_dir, config in loaded:
                    if only_ids and config["id"] not in only_ids:
                        continue
                    output = problem_dir / "problem.pdf"
                    output.write_bytes(b"%PDF-1.4\n" + config["id"].encode("ascii"))
                    outputs[config["id"]] = {"path": str(output), "pages": 1}
                return outputs

            def failing_package(root, problem_dir, config, require_pdf=True):
                if config["id"] == "B":
                    raise ProbHubError("B package failed")
                return self.write_package_fixture(
                    root,
                    problem_dir,
                    config,
                    require_pdf=require_pdf,
                )

            with (
                patch(
                    "probhub.building.compile_collection",
                    side_effect=lambda root, workspace, loaded: self.write_compiled_fixture(
                        root,
                        loaded,
                    ),
                ),
                patch("probhub.building.extract_problem_pdfs", side_effect=fake_extract),
                patch("probhub.building.package_problem", side_effect=failing_package),
            ):
                with self.assertRaisesRegex(ProbHubError, "B package failed"):
                    build_workspace(root, workspace, entries, run_judge=False)

            for path, content in artifacts:
                with self.subTest(path=path):
                    self.assertEqual(path.read_bytes(), content)

    def test_build_lints_unselected_collection_inputs_before_typesetting(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entries = problem_entries(workspace)
            problem_b, _ = load_problem(root, entries[1])
            (problem_b / "code/validator.cpp").unlink()

            with patch("probhub.building.compile_collection") as compile_collection:
                with self.assertRaisesRegex(ProbHubError, "B: validator not found"):
                    build_workspace(root, workspace, [entries[0]], run_judge=False)

            compile_collection.assert_not_called()

    def test_build_rejects_typst_directory_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            workspace["typst"]["directory"] = "../outside"
            write_yaml(root / ".probhub/workspace.yaml", workspace)

            with self.assertRaisesRegex(
                ProbHubError,
                "Typst directory must stay inside the workspace",
            ):
                build_workspace(
                    root,
                    workspace,
                    [problem_entries(workspace)[0]],
                    run_judge=False,
                )

    def test_workspace_build_lock_rejects_a_second_writer_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".probhub").mkdir()
            with workspace_build_lock(root):
                with self.assertRaises(ProbHubError) as raised:
                    with workspace_build_lock(root):
                        pass
                self.assertEqual(raised.exception.code, "build_busy")

            with workspace_build_lock(root):
                pass

    def test_windows_locked_artifact_is_reported_before_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            problem, _ = load_problem(root, problem_entries(workspace)[0])
            target = problem / "problem.pdf"
            target.write_bytes(b"old pdf")
            plan = SimpleNamespace(
                root=root,
                typst_relative=Path("typst/contest"),
                problems=(SimpleNamespace(problem_dir=problem),),
                selected=(
                    SimpleNamespace(
                        problem_dir=problem,
                        config={"id": "A"},
                    ),
                ),
            )

            real_open = __import__("os").open

            def locked_open(path, flags):
                if Path(path) == target:
                    raise PermissionError(13, "in use")
                return real_open(path, flags)

            with (
                patch("probhub.building.os.name", "nt"),
                patch("probhub.building.os.open", side_effect=locked_open),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    _assert_publish_targets_available(plan)

            self.assertEqual(raised.exception.code, "artifact_busy")
            self.assertIn(str(target), str(raised.exception))
            self.assertEqual(target.read_bytes(), b"old pdf")

    def test_publish_preflight_failure_happens_before_any_replacement(self):
        failure = ProbHubError("locked", code="artifact_busy")
        with (
            patch(
                "probhub.building._assert_publish_targets_available",
                side_effect=failure,
            ),
            patch("probhub.building._replace_staged_file") as replace,
        ):
            with self.assertRaises(ProbHubError) as raised:
                publish_build(
                    SimpleNamespace(),
                    SimpleNamespace(),
                    manifests={},
                    pdfs={},
                    packages={},
                    publish_cache=False,
                )

        self.assertIs(raised.exception, failure)
        replace.assert_not_called()

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
