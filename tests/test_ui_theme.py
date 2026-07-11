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

    def test_index_contains_persistent_light_and_dark_theme_controls(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('html[data-theme="light"]', html)
        self.assertIn('html[data-theme="dark"]', html)
        self.assertIn("localStorage.setItem('probhub-theme', this.theme)", html)
        self.assertIn('@click="toggleTheme()"', html)
        self.assertIn('@input.stop @change.stop="switchSubtitle()"', html)
        self.assertIn('切换到护眼暗夜模式', html)
        self.assertIn("theme === 'dark' ? 'Light' : 'Dark'", html)
        self.assertIn("theme === 'light' ? 'filter:none'", html)
        self.assertIn("x-show=\"theme === 'dark'\" class=\"absolute inset-0 bg-black/20", html)
        self.assertIn('<template x-if="currentSubtitle && pdfPages.length > 0">', html)
        self.assertNotIn('x-show="pdfPages.length > 0"', html)
        self.assertIn('data-testid="cover-preview"', html)

    def test_initial_load_fetches_page_count_before_rendering_preview_image(self):
        html = self.ui.HTML_TEMPLATE
        init_start = html.index("initApp() {")
        load_data_start = html.index("loadData() {", init_start)
        init_app = html[init_start:load_data_start]
        self.assertIn("this.currentSubtitle = subs[0]", init_app)
        self.assertIn("this.loadPdfPages();", init_app)
        self.assertNotIn("/api/pdf-page/' + encodeURIComponent(currentSubtitle)", init_app)

    def test_pdf_page_count_ignores_stale_subtitle_responses(self):
        html = self.ui.HTML_TEMPLATE
        load_start = html.index("loadPdfPages() {")
        sortable_start = html.index("initSortable() {", load_start)
        loader = html[load_start:sortable_start]
        self.assertIn("const requestedSubtitle = this.currentSubtitle", loader)
        self.assertIn("encodeURIComponent(requestedSubtitle)", loader)
        self.assertIn("if (this.currentSubtitle !== requestedSubtitle) return", loader)
        self.assertIn("switchSubtitle() {\n                    this.pdfPages = [];", loader)

    def test_theme_uses_css_variables_without_changing_api_markup(self):
        html = self.ui.HTML_TEMPLATE
        self.assertIn("rgb(var(--ink-bg) / <alpha-value>)", html)
        self.assertIn('x-data="probhub()"', html)
        self.assertIn("fetch('/api/subtitles')", html)
        self.assertIn("fetch('/api/compile'", html)
        self.assertIn("fetch('/api/distribute'", html)
        self.assertIn("fetch('/api/submission/run'", html)

    def test_pdf_preview_serves_relative_cache_from_workspace(self):
        original_cwd = Path.cwd()
        original_base_dir = self.ui.BASE_DIR
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            preview = root / "typst-statement" / "QA" / ".preview"
            preview.mkdir(parents=True)
            pdf = preview.parent / "main.pdf"
            png = preview / "page-1.png"
            pdf.write_bytes(b"fixture pdf")
            png.write_bytes(b"fixture png")
            os.utime(png, (pdf.stat().st_mtime + 1, pdf.stat().st_mtime + 1))
            os.chdir(root)
            self.ui.BASE_DIR = "typst-statement"
            with mock.patch("pypdf.PdfReader", return_value=mock.Mock(pages=[object()])):
                response = self.client.get("/api/pdf-page/QA/0")
            self.assertEqual(response.status_code, 200)
            payload = response.get_data()
            response.close()
            self.assertEqual(payload, b"fixture png")
        finally:
            self.ui.BASE_DIR = original_base_dir
            os.chdir(original_cwd)
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
