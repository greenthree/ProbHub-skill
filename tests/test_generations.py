import copy
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.build_lock import workspace_file_lock
from probhub.builder_fingerprint import BUILD_MANIFEST_SCHEMA_VERSION
from probhub.cli import main as cli_main
from probhub.errors import ProbHubError
from probhub.generations import (
    CHECKPOINTS_DIR,
    GENERATION_SCHEMA_VERSION,
    _problem_storage_key,
    assemble_exam_generation,
    create_problem_checkpoint,
    generation_status,
    latest_checkpoint,
)
from probhub.io import write_yaml
from probhub.linting import compute_data_hash, compute_source_hash
from probhub.workspace import load_problem
from probhub.workspace import load_workspace, problem_entries


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.builder_fingerprint = self.fingerprint("typst-0.14.2")
        self._builder_patch = patch(
            "probhub.generations.compute_builder_fingerprint",
            side_effect=lambda *_args, **_kwargs: dict(self.builder_fingerprint),
        )
        self._builder_patch.start()
        self.addCleanup(self._builder_patch.stop)

    @staticmethod
    def fingerprint(identity):
        return {
            "schema_version": 1,
            "probhub_version": "0.6.2",
            "build_manifest_schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
            "generation_schema_version": GENERATION_SCHEMA_VERSION,
            "typst_version": identity,
            "pypdf_version": "6.14.2",
            "template_hash": "template",
            "font": {
                "family": "Noto Sans CJK SC",
                "policy": "bundled-only-v1",
                "sha256": "font",
            },
            "digest": identity,
        }

    def create_problem(self, root, problem_id, name):
        problem = root / problem_id
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            f"# {name}\n\n"
            f"## 题目描述\n\n{name} revision one.\n\n"
            "## 输入格式\n\nInput.\n\n"
            "## 输出格式\n\nOutput.\n",
            encoding="utf-8",
        )
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("2\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("2\n", encoding="utf-8")
        for filename in ("validator.cpp", "std.cpp", "brute.cpp", "stress_generator.cpp"):
            (problem / "code" / filename).write_text("int main(){}\n", encoding="utf-8")
        write_yaml(problem / "probhub.yaml", {
            "schema_version": 1,
            "id": problem_id,
            "name": name,
            "display_name": name,
            "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {
                "accepted": ["code/std.cpp"],
                "brute": [],
                "wrong": [],
            },
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        return problem

    def create_workspace(self, root):
        (root / ".probhub").mkdir(parents=True)
        (root / "typst/contest").mkdir(parents=True)
        (root / "typst/contest/main.typ").write_text("// fixture\n", encoding="utf-8")
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "contest": {"title": "Fixture"},
            "typst": {"directory": "typst/contest"},
            "problems": [
                {"id": "A", "directory": "A"},
                {"id": "B", "directory": "B"},
            ],
        })
        self.create_problem(root, "A", "Alpha")
        self.create_problem(root, "B", "Beta")
        return load_workspace(root)

    def fake_compile(self, root, workspace, loaded):
        typst_dir = root / "typst/contest"
        content = []
        for problem_dir, config in loaded:
            content.append((problem_dir / "problem.md").read_text(encoding="utf-8"))
            (problem_dir / "meta.json").write_text(
                json.dumps({"id": config["id"]}),
                encoding="utf-8",
            )
        (typst_dir / "problems.json").write_text("[]\n", encoding="utf-8")
        main_pdf = typst_dir / "main.pdf"
        main_pdf.write_bytes("\n---\n".join(content).encode("utf-8"))
        return typst_dir, main_pdf, []

    def fake_extract(self, main_pdf, loaded, only_ids=None):
        outputs = {}
        for problem_dir, config in loaded:
            if only_ids and config["id"] not in only_ids:
                continue
            output = problem_dir / "problem.pdf"
            output.write_bytes(b"pdf-" + config["id"].encode("ascii"))
            outputs[config["id"]] = {"path": str(output), "pages": 1}
        return outputs

    def test_checkpoint_revisions_are_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entry = problem_entries(workspace)[0]
            first = create_problem_checkpoint(root, workspace, entry)
            first_statement = Path(first["problem_dir"]) / "problem.md"

            (root / "A/problem.md").write_text(
                first_statement.read_text(encoding="utf-8").replace(
                    "revision one",
                    "revision two",
                ),
                encoding="utf-8",
            )
            second = create_problem_checkpoint(root, workspace, entry)

            self.assertNotEqual(first["revision_id"], second["revision_id"])
            self.assertIn("revision one", first_statement.read_text(encoding="utf-8"))
            self.assertIn(
                "revision two",
                (Path(second["problem_dir"]) / "problem.md").read_text(encoding="utf-8"),
            )

    def test_checkpoint_rejects_manifest_state_forgery(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entry = problem_entries(workspace)[0]
            checkpoint = create_problem_checkpoint(root, workspace, entry)
            manifest_path = Path(checkpoint["path"]) / "revision.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "sealed"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaises(ProbHubError) as raised:
                latest_checkpoint(root, entry["id"])

            self.assertEqual(raised.exception.code, "checkpoint_invalid")
            self.assertIn("identity mismatch", str(raised.exception))

    def test_sealed_checkpoint_requires_hashes_from_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entry = problem_entries(workspace)[0]

            with self.assertRaises(ProbHubError) as raised:
                create_problem_checkpoint(
                    root,
                    workspace,
                    entry,
                    state="sealed",
                    evidence={"judge": {"ok": True}},
                )

            self.assertEqual(raised.exception.code, "seal_revision_required")
            self.assertIsNone(latest_checkpoint(root, entry["id"]))

    def test_sealed_checkpoint_rejects_change_after_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entry = problem_entries(workspace)[0]
            problem_dir, config = load_problem(root, entry)
            source_hash = compute_source_hash(problem_dir, config)
            data_hash = compute_data_hash(problem_dir, config)
            statement = problem_dir / "problem.md"
            statement.write_text(
                statement.read_text(encoding="utf-8") + "\nEdit after verification.\n",
                encoding="utf-8",
            )

            with self.assertRaises(ProbHubError) as raised:
                create_problem_checkpoint(
                    root,
                    workspace,
                    entry,
                    state="sealed",
                    evidence={"judge": {"ok": True}},
                    expected_source_hash=source_hash,
                    expected_data_hash=data_hash,
                )

            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertIsNone(latest_checkpoint(root, entry["id"]))

    def test_sealed_checkpoint_rejects_concurrent_change_during_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            entry = problem_entries(workspace)[0]
            problem_dir, config = load_problem(root, entry)
            source_hash = compute_source_hash(problem_dir, config)
            data_hash = compute_data_hash(problem_dir, config)
            copy_started = threading.Event()
            continue_copy = threading.Event()
            real_copytree = shutil.copytree

            def delayed_copytree(source, destination, *args, **kwargs):
                if not copy_started.is_set():
                    copy_started.set()
                    self.assertTrue(continue_copy.wait(timeout=5))
                return real_copytree(source, destination, *args, **kwargs)

            with (
                patch(
                    "probhub.generations.shutil.copytree",
                    side_effect=delayed_copytree,
                ),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(
                    create_problem_checkpoint,
                    root,
                    workspace,
                    entry,
                    "sealed",
                    {"judge": {"ok": True}},
                    expected_source_hash=source_hash,
                    expected_data_hash=data_hash,
                )
                self.assertTrue(copy_started.wait(timeout=5))
                statement = problem_dir / "problem.md"
                statement.write_text(
                    statement.read_text(encoding="utf-8") + "\nConcurrent edit.\n",
                    encoding="utf-8",
                )
                continue_copy.set()
                with self.assertRaises(ProbHubError) as raised:
                    future.result(timeout=5)

            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertIsNone(latest_checkpoint(root, entry["id"]))

    def test_generation_uses_checkpoints_and_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)

            (root / "B/problem.md").write_text(
                "# Beta\n\n## 题目描述\n\nUNPUBLISHED LIVE CHANGE.\n\n"
                "## 输入格式\n\nInput.\n\n## 输出格式\n\nOutput.\n",
                encoding="utf-8",
            )

            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile) as compile_collection,
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                first = assemble_exam_generation(root)
                second = assemble_exam_generation(root)

            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(first["generation_id"], second["generation_id"])
            self.assertEqual(compile_collection.call_count, 1)
            content = Path(first["main_pdf"]).read_text(encoding="utf-8")
            self.assertIn("Beta revision one", content)
            self.assertNotIn("UNPUBLISHED LIVE CHANGE", content)

    def test_artifact_builder_changes_create_new_generations(self):
        cases = (
            (("probhub_version",), "0.6.3"),
            (("typst_version",), "0.14.3"),
            (("pypdf_version",), "6.15.0"),
            (("template_hash",), "template-two"),
            (("font", "sha256"), "font-two"),
        )
        for field_path, changed_value in cases:
            with self.subTest(field=".".join(field_path)), tempfile.TemporaryDirectory() as temp:
                self.builder_fingerprint = self.fingerprint("0.14.2")
                root, workspace = self.create_workspace(Path(temp))
                for entry in problem_entries(workspace):
                    create_problem_checkpoint(root, workspace, entry)

                with (
                    patch("probhub.generations.compile_collection", side_effect=self.fake_compile) as compile_collection,
                    patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
                ):
                    first = assemble_exam_generation(root)
                    changed = copy.deepcopy(self.builder_fingerprint)
                    target = changed
                    for key in field_path[:-1]:
                        target = target[key]
                    target[field_path[-1]] = changed_value
                    changed["digest"] = "changed-" + ".".join(field_path)
                    self.builder_fingerprint = changed
                    second = assemble_exam_generation(root)

                self.assertNotEqual(first["generation_id"], second["generation_id"])
                self.assertEqual(compile_collection.call_count, 2)
                self.assertEqual(
                    second["manifest"]["builder_fingerprint"],
                    self.builder_fingerprint,
                )

    def test_concurrent_generation_requests_singleflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)

            def slow_compile(root, workspace, loaded):
                time.sleep(0.2)
                return self.fake_compile(root, workspace, loaded)

            with (
                patch("probhub.generations.compile_collection", side_effect=slow_compile) as compile_collection,
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [executor.submit(assemble_exam_generation, root) for _ in range(2)]
                results = [future.result(timeout=5) for future in futures]

            self.assertEqual(compile_collection.call_count, 1)
            self.assertEqual(results[0]["generation_id"], results[1]["generation_id"])
            self.assertEqual(sorted(result["cached"] for result in results), [False, True])

    def test_generation_uses_placeholder_when_problem_has_no_valid_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            create_problem_checkpoint(root, workspace, problem_entries(workspace)[0])
            (root / "B/probhub.yaml").unlink()

            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                result = assemble_exam_generation(root)

            by_id = {item["problem_id"]: item for item in result["manifest"]["problems"]}
            self.assertEqual(by_id["A"]["state"], "draft")
            self.assertEqual(by_id["B"]["state"], "placeholder")
            self.assertFalse(result["complete"])
            self.assertEqual(len(result["missing"]), 1)
            self.assertEqual(result["missing"][0]["problem_id"], "B")
            self.assertIn("problem config not found", result["missing"][0]["reason"])
            self.assertEqual(result["missing"], result["manifest"]["missing"])
            self.assertIn("本题仍在开发中", Path(result["main_pdf"]).read_text(encoding="utf-8"))

    def test_assemble_fails_explicitly_when_checkpoint_lock_stays_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            create_problem_checkpoint(root, workspace, problem_entries(workspace)[0])
            lock_path = CHECKPOINTS_DIR / f"{_problem_storage_key('B')}.lock"

            with (
                workspace_file_lock(root, lock_path),
                patch("probhub.generations.CHECKPOINT_BUSY_WAIT_SECONDS", 0.3),
                patch("probhub.generations.CHECKPOINT_BUSY_POLL_SECONDS", 0.05),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    assemble_exam_generation(root)

            self.assertEqual(raised.exception.code, "checkpoint_busy")
            self.assertIn("B", str(raised.exception))

    def test_assemble_waits_for_concurrent_seal_to_release_checkpoint_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            create_problem_checkpoint(root, workspace, problem_entries(workspace)[0])
            lock_path = CHECKPOINTS_DIR / f"{_problem_storage_key('B')}.lock"
            acquired = threading.Event()

            def hold_lock_briefly():
                with workspace_file_lock(root, lock_path):
                    acquired.set()
                    time.sleep(0.4)

            holder = threading.Thread(target=hold_lock_briefly)
            holder.start()
            try:
                self.assertTrue(acquired.wait(timeout=5))
                with (
                    patch("probhub.generations.CHECKPOINT_BUSY_WAIT_SECONDS", 5.0),
                    patch("probhub.generations.CHECKPOINT_BUSY_POLL_SECONDS", 0.05),
                    patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                    patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
                ):
                    result = assemble_exam_generation(root)
            finally:
                holder.join(timeout=5)

            self.assertTrue(result["complete"])
            self.assertEqual(result["missing"], [])
            states = {
                item["problem_id"]: item["state"]
                for item in result["manifest"]["problems"]
            }
            self.assertEqual(states["B"], "draft")

    def test_assemble_replaces_corrupt_existing_generation_with_fresh_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)

            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                first = assemble_exam_generation(root)
                manifest_path = Path(first["path"]) / "manifest.json"
                manifest_path.write_text("{}\n", encoding="utf-8")

                second = assemble_exam_generation(root)

            self.assertEqual(first["generation_id"], second["generation_id"])
            self.assertFalse(second["cached"])
            published = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(published["generation_id"], second["generation_id"])
            tmp_root = root / ".probhub/generation-tmp"
            self.assertEqual(
                [] if not tmp_root.is_dir() else list(tmp_root.iterdir()),
                [],
            )

    def test_assemble_cleans_stage_after_late_publish_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)

            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
                patch("probhub.generations.os.replace", side_effect=OSError("disk full")),
            ):
                with self.assertRaises(OSError):
                    assemble_exam_generation(root)

            tmp_root = root / ".probhub/generation-tmp"
            self.assertEqual(
                [] if not tmp_root.is_dir() else list(tmp_root.iterdir()),
                [],
            )

    def test_seal_records_evidence_and_requests_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            config_path = root / "A/probhub.yaml"
            config = __import__("yaml").safe_load(config_path.read_text(encoding="utf-8"))
            config["solutions"]["brute"] = ["code/brute.cpp"]
            config["stress"] = {
                "generator": "code/stress_generator.cpp",
                "accepted": "code/std.cpp",
                "brute": "code/brute.cpp",
                "rounds": 5,
            }
            write_yaml(config_path, config)
            output = io.StringIO()
            judge_result = {
                "ok": True,
                "returncode": 0,
                "final": {
                    "type": "final",
                    "status": "passed",
                    "code": "all_expectations_met",
                },
                "cache": {},
            }
            stress_result = {
                "ok": True,
                "status": "passed",
                "rounds_requested": 7,
                "rounds_completed": 7,
                "master_seed": 99,
            }
            with (
                patch("probhub.cli.judge_problem", return_value=judge_result),
                patch("probhub.cli.stress_problem", return_value=stress_result) as stress,
                patch(
                    "probhub.cli.assemble_exam_generation",
                    return_value={"ok": True, "generation_id": "fixture-generation"},
                ) as assemble,
                patch(
                    "probhub.cli.compute_builder_fingerprint",
                    return_value=self.builder_fingerprint,
                ),
                redirect_stdout(output),
            ):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "seal",
                    "A",
                    "--rounds",
                    "7",
                    "--seed",
                    "99",
                ])

            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            checkpoint = payload["checkpoint"]
            self.assertEqual(checkpoint["state"], "sealed")
            self.assertTrue(checkpoint["evidence"]["judge"]["ok"])
            self.assertEqual(checkpoint["evidence"]["stress"]["rounds_completed"], 7)
            self.assertEqual(payload["generation"]["generation_id"], "fixture-generation")
            self.assertEqual(stress.call_args.kwargs["rounds"], 7)
            self.assertEqual(stress.call_args.kwargs["master_seed"], 99)
            self.assertEqual(
                assemble.call_args.kwargs["expected_builder_fingerprint"],
                self.builder_fingerprint,
            )

    def test_generation_status_reports_schema_and_builder_staleness(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)
            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                result = assemble_exam_generation(root)

            manifest_path = Path(result["path"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            status = generation_status(root)
            self.assertFalse(status["ok"])
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["generation_schema"])

            manifest["schema_version"] = GENERATION_SCHEMA_VERSION
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.builder_fingerprint = self.fingerprint("typst-0.14.3")
            status = generation_status(root)
            self.assertEqual(status["state"], "stale")
            self.assertIn(
                "builder_fingerprint.typst_version",
                status["stale_fields"],
            )

    def test_generation_status_distinguishes_unavailable_builder_from_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)
            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                assemble_exam_generation(root)

            with patch(
                "probhub.generations.compute_builder_fingerprint",
                side_effect=ProbHubError(
                    "typst was not found",
                    code="builder_fingerprint_failed",
                ),
            ):
                status = generation_status(root)

            self.assertFalse(status["ok"])
            self.assertEqual(status["state"], "stale")
            self.assertEqual(
                status["stale_fields"],
                ["builder_fingerprint.unavailable"],
            )
            self.assertEqual(
                status["builder_fingerprint_error"]["code"],
                "builder_fingerprint_failed",
            )

    def test_expected_builder_probe_failure_is_builder_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            failure = ProbHubError(
                "typst disappeared",
                code="builder_fingerprint_failed",
            )
            with patch(
                "probhub.generations.compute_builder_fingerprint",
                side_effect=failure,
            ):
                with self.assertRaises(ProbHubError) as raised:
                    assemble_exam_generation(
                        root,
                        expected_builder_fingerprint=self.builder_fingerprint,
                    )

            self.assertEqual(raised.exception.code, "builder_changed")

    def test_cached_generation_recheck_failure_is_builder_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)
            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                assemble_exam_generation(root)

            failure = ProbHubError(
                "pypdf disappeared",
                code="builder_fingerprint_failed",
            )
            with patch(
                "probhub.generations.compute_builder_fingerprint",
                side_effect=[self.builder_fingerprint, failure],
            ):
                with self.assertRaises(ProbHubError) as raised:
                    assemble_exam_generation(root)

            self.assertEqual(raised.exception.code, "builder_changed")

    def test_late_builder_probe_failure_does_not_publish_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)
            failure = ProbHubError(
                "font disappeared",
                code="builder_fingerprint_failed",
            )
            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
                patch(
                    "probhub.generations.compute_builder_fingerprint",
                    side_effect=[
                        self.builder_fingerprint,
                        self.builder_fingerprint,
                        failure,
                    ],
                ),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    assemble_exam_generation(root)

            self.assertEqual(raised.exception.code, "builder_changed")
            self.assertFalse((root / ".probhub/generations/current.json").exists())
            generation_root = root / ".probhub/generations"
            self.assertEqual(
                [] if not generation_root.is_dir() else list(generation_root.glob("*/manifest.json")),
                [],
            )
            tmp_root = root / ".probhub/generation-tmp"
            self.assertEqual(
                [] if not tmp_root.is_dir() else list(tmp_root.iterdir()),
                [],
            )

    def test_generation_status_detects_pdf_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root, workspace = self.create_workspace(Path(temp))
            for entry in problem_entries(workspace):
                create_problem_checkpoint(root, workspace, entry)
            with (
                patch("probhub.generations.compile_collection", side_effect=self.fake_compile),
                patch("probhub.generations.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                result = assemble_exam_generation(root)

            self.assertTrue(generation_status(root)["ok"])
            Path(result["main_pdf"]).write_bytes(b"changed")
            self.assertFalse(generation_status(root)["ok"])
            self.assertEqual(generation_status(root)["state"], "invalid")


if __name__ == "__main__":
    unittest.main()
