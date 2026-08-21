import tempfile
import unittest
from pathlib import Path

from probhub.errors import ProbHubError
from probhub.webui_preview import (
    preview_cache_dir,
    preview_pages_dir,
    preview_pdf_path,
    validate_typst_directory,
    workspace_preview_key,
)


class WebUiPreviewPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.preview_root = self.root / "preview-cache"
        self.preview_root.mkdir()
        (self.root / "typst-statement" / "Contest").mkdir(parents=True)
        self.workspace = {
            "schema_version": 1,
            "contest": {"title": "Fixture", "subtitle": "Contest"},
            "typst": {"directory": "typst-statement/Contest"},
            "problems": [{"id": "A", "directory": "A"}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_key_is_opaque_stable_and_does_not_use_subtitle_as_path(self):
        first = workspace_preview_key(self.root, self.workspace, "Contest")
        second = workspace_preview_key(self.root, dict(self.workspace), "Contest")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        pdf = preview_pdf_path(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            create=True,
        )
        self.assertEqual(pdf.name, "main.pdf")
        self.assertNotIn("Contest", pdf.parts)
        self.assertEqual(pdf.parent.parent, self.preview_root)

    def test_unicode_identity_remains_inside_cache_root(self):
        unicode_dir = self.root / "typst-statement" / "竞赛"
        unicode_dir.mkdir(parents=True)
        workspace = dict(self.workspace)
        workspace["typst"] = {"directory": "typst-statement/竞赛"}
        key = workspace_preview_key(self.root, workspace, "竞赛")
        pdf = preview_pdf_path(self.preview_root, self.root, workspace, "竞赛", create=True)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(pdf.parent.parent, self.preview_root)
        self.assertNotIn("竞赛", pdf.parts)

    def test_request_path_forms_fail_closed(self):
        for subtitle in ("../escape", "C:relative", "Contest:other", "Contest\\other", "Contest%3Aother", "contest"):
            with self.subTest(subtitle=subtitle):
                with self.assertRaises(ProbHubError) as raised:
                    workspace_preview_key(self.root, self.workspace, subtitle)
                self.assertIn(raised.exception.code, {"unknown_subtitle", "invalid_preview_path"})
        self.assertFalse((self.root / "escape").exists())

    def test_typst_directory_ambiguous_forms_fail_closed(self):
        for value in ("C:relative", "C:/outside", "typst-statement\\Contest", "typst-statement/../outside", "typst-statement/%3A"):
            with self.subTest(value=value):
                workspace = dict(self.workspace)
                workspace["typst"] = {"directory": value}
                with self.assertRaises(ProbHubError) as raised:
                    validate_typst_directory(self.root, workspace)
                self.assertEqual(raised.exception.code, "invalid_workspace_path")
        self.assertEqual(list(self.preview_root.iterdir()), [])

    def test_page_cache_has_fixed_safe_components(self):
        pages = preview_pages_dir(self.preview_root, self.root, self.workspace, "Contest", create=True)
        self.assertEqual(pages.name, ".pages")
        self.assertEqual(pages.parent.parent, self.preview_root)
        self.assertNotIn("Contest", pages.parts)


if __name__ == "__main__":
    unittest.main()
