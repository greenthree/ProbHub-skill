import io
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.cli import main as cli_main
from probhub.generations import (
    assemble_exam_generation,
    create_problem_checkpoint,
    generation_status,
)
from probhub.io import write_yaml
from probhub.workspace import load_workspace, problem_entries


class GenerationTests(unittest.TestCase):
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
            self.assertIn("本题仍在开发中", Path(result["main_pdf"]).read_text(encoding="utf-8"))

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
