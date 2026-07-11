import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from probhub.io import write_json, write_yaml


ROOT = Path(__file__).resolve().parents[1]


def load_ui():
    spec = importlib.util.spec_from_file_location("write_boundary_ui", ROOT / "scripts" / "ui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UiWriteBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = load_ui()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_cwd = Path.cwd()
        os.chdir(self.root)
        (self.root / ".probhub").mkdir()
        (self.root / "typst-statement/Contest").mkdir(parents=True)
        (self.root / "typst-statement/Contest/main.typ").write_text("// fixture\n", encoding="utf-8")
        write_yaml(self.root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "contest": {"title": "Fixture", "subtitle": "Contest", "author": "Team", "date": "2026"},
            "typst": {"directory": "typst-statement/Contest"},
            "problems": [{"id": "A", "directory": "A"}],
        })
        problem = self.root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# Alpha\n\n## 题目描述\n\nDescription.\n\n"
            "## 输入格式\n\nInput.\n\n## 输出格式\n\nOutput.\n",
            encoding="utf-8",
        )
        (problem / "diagram.png").write_bytes(b"fixture image")
        (problem / "code/hidden.png").write_bytes(b"not a statement asset")
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("2\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("2\n", encoding="utf-8")
        (problem / "code/validator.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        write_yaml(problem / "probhub.yaml", {
            "schema_version": 1,
            "id": "A",
            "name": "Alpha",
            "display_name": "Alpha",
            "difficulty": 2,
            "tags": ["math"],
            "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        for relative, content in (
            ("A/meta.json", b"old meta"),
            ("A/problem.yaml", b"old package config"),
            ("A/domjudge-problem.ini", b"old ini"),
            ("A/problem.pdf", b"old problem pdf"),
            ("A/.probhub/build-manifest.json", b"old manifest"),
            ("A.zip", b"old zip"),
            ("typst-statement/Contest/problems.json", b"old problems"),
            ("typst-statement/Contest/main.pdf", b"old main pdf"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.client = self.ui.app.test_client()

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.temp.cleanup()

    def file_state(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
            and ".preview" not in path.parts
            and path.relative_to(self.root).as_posix() != ".probhub/build.lock"
        }

    def load_payload(self):
        response = self.client.get("/api/data?subtitle=Contest")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_navigation_gets_are_byte_for_byte_read_only(self):
        before = self.file_state()
        self.assertEqual(self.client.get("/api/subtitles").status_code, 200)
        self.assertEqual(self.client.get("/api/data?subtitle=Contest").status_code, 200)
        self.assertEqual(self.client.get("/api/config/Contest").status_code, 200)
        self.assertEqual(self.client.get("/api/pdf-pages/Contest").status_code, 200)
        self.assertEqual(self.file_state(), before)

    def test_schema_statement_assets_are_served_read_only_with_path_boundaries(self):
        before = self.file_state()
        response = self.client.get("/api/problem-assets/Contest/A/diagram.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"fixture image")
        response.close()
        self.assertEqual(self.client.get("/api/problem-assets/Contest/A/code/hidden.png").status_code, 404)
        self.assertEqual(self.client.get("/api/problem-assets/Contest/A/problem.md").status_code, 404)
        self.assertEqual(self.client.get("/api/problem-assets/Contest/A/%2E%2E/outside.png").status_code, 404)
        self.assertEqual(self.file_state(), before)

    def test_schema_cover_save_updates_workspace_without_touching_typst_sources(self):
        response = self.client.get("/api/config/Contest")
        config = response.get_json()["config"]
        main_before = (self.root / "typst-statement/Contest/main.typ").read_bytes()
        config["title"] = "Updated Contest"
        config["logo_width"] = "8cm"
        response = self.client.post("/api/config/Contest", json=config)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        workspace = (self.root / ".probhub/workspace.yaml").read_text(encoding="utf-8")
        self.assertIn("title: Updated Contest", workspace)
        self.assertIn("logo_width: 8cm", workspace)
        self.assertEqual((self.root / "typst-statement/Contest/main.typ").read_bytes(), main_before)

    def test_stale_cover_revision_returns_conflict(self):
        config = self.client.get("/api/config/Contest").get_json()["config"]
        workspace = self.root / ".probhub/workspace.yaml"
        workspace.write_text(workspace.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        before = workspace.read_bytes()
        response = self.client.post("/api/config/Contest", json=config)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "source_conflict")
        self.assertEqual(workspace.read_bytes(), before)

    def test_schema_save_changes_sources_but_not_generated_artifacts(self):
        payload = self.load_payload()
        generated = {
            relative: (self.root / relative).read_bytes()
            for relative in (
                "A/meta.json",
                "A/problem.yaml",
                "A/domjudge-problem.ini",
                "A/problem.pdf",
                "A/.probhub/build-manifest.json",
                "A.zip",
                "typst-statement/Contest/problems.json",
                "typst-statement/Contest/main.pdf",
            )
        }
        payload[0]["problem"]["display_name"] = "Alpha Revised"
        payload[0]["problem"]["time_limit"] = 2
        payload[0]["statement"]["description"] = "Changed description."
        payload[0]["problem"]["samples"][0]["output"] = "2"

        response = self.client.post("/api/data", json={"subtitle": "Contest", "problems": payload})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["success"])
        self.assertIn("Changed description.", (self.root / "A/problem.md").read_text(encoding="utf-8"))
        self.assertEqual((self.root / "A/data/sample/1.ans").read_text(encoding="utf-8"), "2\n")
        config = (self.root / "A/probhub.yaml").read_text(encoding="utf-8")
        self.assertIn("display_name: Alpha Revised", config)
        self.assertIn("time: 2", config)
        for relative, content in generated.items():
            with self.subTest(relative=relative):
                self.assertEqual((self.root / relative).read_bytes(), content)

    def test_unchanged_schema_save_is_byte_for_byte_read_only(self):
        payload = self.load_payload()
        before = self.file_state()
        response = self.client.post("/api/data", json={"subtitle": "Contest", "problems": payload})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.file_state(), before)

    def test_schema_save_reorders_workspace_and_adds_sample_without_touching_artifacts(self):
        payload = self.load_payload()
        payload[0]["problem"]["samples"].append({"input": "3", "output": "3"})
        response = self.client.post("/api/data", json={"subtitle": "Contest", "problems": payload})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        saved = response.get_json()["problems"]
        self.assertEqual(len(saved[0]["problem"]["samples"]), 2)
        self.assertTrue((self.root / "A/data/sample/2.in").is_file())
        self.assertTrue((self.root / "A/data/sample/2.ans").is_file())
        self.assertEqual((self.root / "A/meta.json").read_bytes(), b"old meta")
        self.assertEqual((self.root / "A.zip").read_bytes(), b"old zip")

    def test_invalid_schema_edit_rolls_back_all_sources(self):
        payload = self.load_payload()
        before = self.file_state()
        payload[0]["problem"]["memory_limit"] = 300
        response = self.client.post("/api/data", json={"subtitle": "Contest", "problems": payload})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "invalid_editor_payload")
        self.assertEqual(self.file_state(), before)

    def test_stale_revision_returns_conflict_without_overwrite(self):
        payload = self.load_payload()
        statement = self.root / "A/problem.md"
        statement.write_text(statement.read_text(encoding="utf-8") + "\nExternal change.\n", encoding="utf-8")
        before = self.file_state()
        payload[0]["statement"]["description"] = "Editor change."
        response = self.client.post("/api/data", json={"subtitle": "Contest", "problems": payload})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "source_conflict")
        self.assertEqual(self.file_state(), before)

    def test_schema_preview_does_not_publish_live_artifacts(self):
        staged_pdf = self.root / "staged.pdf"
        staged_pdf.write_bytes(b"preview pdf")
        before = self.file_state()
        snapshot = mock.MagicMock()
        snapshot.root = self.root
        snapshot.workspace = {}
        snapshot.loaded_problems = ()
        context = mock.MagicMock()
        context.__enter__.return_value = snapshot
        plan = mock.MagicMock()
        with (
            mock.patch.object(self.ui, "create_build_plan", return_value=plan),
            mock.patch.object(self.ui, "create_build_snapshot", return_value=context),
            mock.patch.object(self.ui, "compile_collection", return_value=(self.root, staged_pdf, [])),
        ):
            response = self.client.post("/api/compile", json={"subtitle": "Contest"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["preview"])
        self.assertEqual(self.file_state(), before)
        self.assertEqual(self.ui._preview_pdf_path("Contest").read_bytes(), b"preview pdf")

    def test_schema_distribute_delegates_to_core_build(self):
        result = {
            "ok": True,
            "batch_id": "batch",
            "pdfs": {"A": {"path": str(self.root / "A/problem.pdf"), "pages": 1}},
        }
        with mock.patch.object(self.ui, "build_workspace", return_value=result) as build:
            response = self.client.post("/api/distribute", json={"subtitle": "Contest"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["batch_id"], "batch")
        build.assert_called_once()

    def test_schema_preview_reports_build_conflict_as_http_409(self):
        with mock.patch.object(
            self.ui,
            "create_build_plan",
            side_effect=self.ui.ProbHubError("busy", code="build_busy"),
        ):
            response = self.client.post("/api/compile", json={"subtitle": "Contest"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "build_busy")

    def test_schema_save_reports_writer_conflict_as_http_409(self):
        payload = self.load_payload()
        with mock.patch.object(
            self.ui,
            "workspace_build_lock",
            side_effect=self.ui.ProbHubError("busy", code="build_busy"),
        ):
            response = self.client.post("/api/data", json={"subtitle": "Contest", "problems": payload})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "build_busy")

    def test_schema_sandbox_selection_uses_workspace_id_not_generated_meta(self):
        info = self.ui._sandbox_problem_info("Contest", 0)
        self.assertTrue(info["matched"])
        self.assertTrue(os.path.samefile(info["dir"], self.root / "A"))
        self.assertEqual(info["name"], "Alpha")

    def test_existing_visual_template_and_theme_markers_are_unchanged(self):
        html = self.ui.HTML_TEMPLATE
        self.assertIn('html[data-theme="light"]', html)
        self.assertIn('html[data-theme="dark"]', html)
        self.assertIn('data-testid="cover-preview"', html)
        self.assertIn("_postWriterJson('/api/compile'", html)
        self.assertIn("_postWriterJson('/api/distribute'", html)
        self.assertIn("_queueWriter(operation)", html)
        self.assertIn("/api/problem-assets/", html)


if __name__ == "__main__":
    unittest.main()
