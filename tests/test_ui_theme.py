import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class UiThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("theme_ui", ROOT / "scripts" / "ui.py")
        cls.ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ui)
        cls.client = cls.ui.app.test_client()
        cls.css = (cls.ui.WEBUI_ASSET_DIR / "app.css").read_text(encoding="utf-8")
        cls.javascript = (cls.ui.WEBUI_ASSET_DIR / "app.js").read_text(encoding="utf-8")

    def test_index_contains_persistent_light_and_dark_theme_controls(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('html[data-theme="light"]', self.css)
        self.assertIn('html[data-theme="dark"]', self.css)
        self.assertIn("localStorage.setItem('probhub-theme', this.theme)", self.javascript)
        self.assertIn('@click="toggleTheme()"', html)
        self.assertIn('@input.stop @change.stop="switchSubtitle()"', html)
        self.assertIn('切换到护眼暗夜模式', html)
        self.assertIn("theme === 'dark' ? 'Light' : 'Dark'", html)
        self.assertIn("theme === 'light' ? 'filter:none'", html)
        self.assertIn("x-show=\"theme === 'dark'\" class=\"absolute inset-0 bg-black/20", html)
        self.assertIn('<template x-if="currentSubtitle && pdfPages.length > 0">', html)
        self.assertNotIn('x-show="pdfPages.length > 0"', html)
        self.assertIn('data-testid="cover-preview"', html)
        self.assertIn("`.probhub/workspace.yaml` 中该题的 `directory` 配置", html)
        self.assertNotIn("`meta.json` 的 `display_name`", html)

    def test_initial_load_fetches_page_count_before_rendering_preview_image(self):
        html = self.javascript
        init_start = html.index("initApp() {")
        load_data_start = html.index("loadData() {", init_start)
        init_app = html[init_start:load_data_start]
        self.assertIn("this.currentSubtitle = subs[0]", init_app)
        self.assertIn("this.loadPdfPages();", init_app)
        self.assertNotIn("/api/pdf-page/' + encodeURIComponent(currentSubtitle)", init_app)

    def test_pdf_page_count_ignores_stale_subtitle_responses(self):
        html = self.javascript
        load_start = html.index("loadPdfPages() {")
        sortable_start = html.index("initSortable() {", load_start)
        loader = html[load_start:sortable_start]
        self.assertIn("const requestedSubtitle = this.currentSubtitle", loader)
        self.assertIn("encodeURIComponent(requestedSubtitle)", loader)
        self.assertIn("if (this.currentSubtitle !== requestedSubtitle) return", loader)
        self.assertIn("switchSubtitle() {\n                    clearTimeout(this._coverSaveTimer);", loader)
        self.assertIn("clearTimeout(this._coverSaveTimer);\n                    this.pdfPages = [];", loader)

    def test_theme_uses_css_variables_without_changing_api_markup(self):
        html = self.ui.HTML_TEMPLATE
        tailwind = (self.ui.WEBUI_ASSET_DIR / "tailwind.config.cjs").read_text(encoding="utf-8")
        self.assertIn("rgb(var(--ink-bg) / <alpha-value>)", tailwind)
        self.assertIn("--primary-top: 235 199 124", self.css)
        self.assertIn("--primary-top: 226 198 139", self.css)
        self.assertIn("--editor-surface: 250 247 241", self.css)
        self.assertIn("--editor-surface: 36 42 36", self.css)
        self.assertIn("--preview-surface: 253 251 247", self.css)
        self.assertIn("--preview-surface: 30 35 30", self.css)
        self.assertIn("background: linear-gradient(180deg, rgb(var(--primary-top)), rgb(var(--primary-bottom)))", self.css)
        self.assertIn('class="editor-surface w-full h-56', html)
        self.assertIn('class="preview-surface w-full h-56', html)
        self.assertIn('x-data="probhub()"', html)
        self.assertIn("fetch('/api/subtitles')", self.javascript)
        self.assertIn("_postWriterJson('/api/compile'", self.javascript)
        self.assertIn("_postWriterJson('/api/distribute'", self.javascript)
        self.assertIn("fetch('/api/submission/run'", self.javascript)

    def test_pdf_preview_renders_into_process_temp_cache(self):
        original_cwd = Path.cwd()
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            statement = root / "typst-statement" / "QA"
            statement.mkdir(parents=True)
            pdf = statement / "main.pdf"
            pdf.write_bytes(b"fixture pdf")
            (root / ".probhub").mkdir()
            (root / ".probhub" / "workspace.yaml").write_text(
                "schema_version: 1\n"
                "typst:\n"
                "  directory: typst-statement/QA\n"
                "problems:\n"
                "  - id: A\n"
                "    directory: A\n",
                encoding="utf-8",
            )
            os.chdir(root)
            def fake_render(command, **kwargs):
                Path(command[-1] + ".png").write_bytes(b"fixture png")
                return {"reason": "completed", "returncode": 0}

            with (
                mock.patch.object(self.ui, "bounded_pdf_page_count", return_value=1),
                mock.patch.object(self.ui, "run_managed_to_files", side_effect=fake_render),
            ):
                response = self.client.get("/api/pdf-page/QA/0")
            self.assertEqual(response.status_code, 200)
            payload = response.get_data()
            response.close()
            self.assertEqual(payload, b"fixture png")
            self.assertFalse((statement / ".preview").exists())
        finally:
            os.chdir(original_cwd)
            temp.cleanup()

    def test_invalid_pdf_is_bounded_and_preserves_api_shape(self):
        original_cwd = Path.cwd()
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            statement = root / "typst-statement" / "QA"
            statement.mkdir(parents=True)
            (statement / "main.pdf").write_bytes(b"malformed pdf")
            (root / ".probhub").mkdir()
            (root / ".probhub" / "workspace.yaml").write_text(
                "schema_version: 1\n"
                "typst:\n"
                "  directory: typst-statement/QA\n"
                "problems:\n"
                "  - id: A\n"
                "    directory: A\n",
                encoding="utf-8",
            )
            os.chdir(root)
            error = self.ui.ProbHubError(
                "PDF processing timed out",
                code="pdf_processing_failed",
            )
            with mock.patch.object(
                self.ui,
                "bounded_pdf_page_count",
                side_effect=error,
            ):
                count = self.client.get("/api/pdf-pages/QA")
                page = self.client.get("/api/pdf-page/QA/0")
            self.assertEqual(count.status_code, 200)
            self.assertEqual(count.get_json(), {"pages": 0})
            self.assertEqual(page.status_code, 500)
            self.assertEqual(page.get_data(as_text=True), "Invalid PDF")
        finally:
            os.chdir(original_cwd)
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
