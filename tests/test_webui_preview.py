import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from probhub.errors import ProbHubError
from probhub.webui_preview import (
    count_preview_pages,
    preview_cache_dir,
    preview_pages_dir,
    preview_pdf_path,
    publish_preview_pdf,
    render_preview_page,
    select_preview_pdf,
    validate_typst_directory,
    workspace_preview_key,
)
import probhub.webui_preview as webui_preview


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

    def test_extended_length_prefix_is_normalized_for_containment(self):
        if os.name != "nt":
            self.skipTest("Windows path representation only")
        nested = self.preview_root / "nested"
        nested.mkdir()
        extended = Path("\\\\?\\" + str(nested))
        self.assertTrue(webui_preview._is_within(extended, self.preview_root))


class WebUiPreviewArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.preview_root = Path(self.temp.name) / "preview-cache"
        self.typst_dir = self.root / "typst-statement" / "Contest"
        self.typst_dir.mkdir(parents=True)
        self.preview_root.mkdir()
        self.workspace = {
            "schema_version": 1,
            "contest": {"title": "Fixture", "subtitle": "Contest"},
            "typst": {"directory": "typst-statement/Contest"},
            "problems": [{"id": "A", "directory": "A"}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_select_publish_and_count_keep_formal_pdf_as_fallback(self):
        formal_pdf = self.typst_dir / "main.pdf"
        formal_pdf.write_bytes(b"formal")
        self.assertEqual(
            select_preview_pdf(self.preview_root, self.root, self.workspace, "Contest"),
            formal_pdf,
        )
        self.assertEqual(
            count_preview_pages(
                self.preview_root,
                self.root,
                self.workspace,
                "Contest",
                page_count_fn=lambda path: 7 if path == formal_pdf else 0,
            ),
            7,
        )

        staged = Path(self.temp.name) / "staged.pdf"
        staged.write_bytes(b"preview")
        published = publish_preview_pdf(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            staged,
        )
        self.assertEqual(published.read_bytes(), b"preview")
        self.assertEqual(
            select_preview_pdf(self.preview_root, self.root, self.workspace, "Contest"),
            published,
        )

    def test_invalid_formal_pdf_is_not_used_as_a_fallback(self):
        formal_pdf = self.typst_dir / "main.pdf"
        formal_pdf.mkdir()
        self.assertIsNone(
            select_preview_pdf(self.preview_root, self.root, self.workspace, "Contest")
        )
        self.assertEqual(
            count_preview_pages(
                self.preview_root,
                self.root,
                self.workspace,
                "Contest",
                page_count_fn=mock.Mock(side_effect=AssertionError("must not inspect")),
            ),
            0,
        )

    def test_failed_preview_publish_preserves_previous_bytes_and_cleans_staging(self):
        staged = Path(self.temp.name) / "staged.pdf"
        staged.write_bytes(b"old")
        published = publish_preview_pdf(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            staged,
        )
        staged.write_bytes(b"new")

        def fail_copy(_source, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError("copy failed")

        with self.assertRaisesRegex(OSError, "copy failed"):
            publish_preview_pdf(
                self.preview_root,
                self.root,
                self.workspace,
                "Contest",
                staged,
                copy_file_fn=fail_copy,
            )
        self.assertEqual(published.read_bytes(), b"old")
        self.assertEqual(list(published.parent.glob(".main-*.pdf.tmp")), [])

        with self.assertRaisesRegex(OSError, "replace failed"):
            publish_preview_pdf(
                self.preview_root,
                self.root,
                self.workspace,
                "Contest",
                staged,
                replace_fn=mock.Mock(side_effect=OSError("replace failed")),
            )
        self.assertEqual(published.read_bytes(), b"old")
        self.assertEqual(list(published.parent.glob(".main-*.pdf.tmp")), [])

    def test_page_cache_uses_pdf_content_not_only_timestamp(self):
        pdf = self.typst_dir / "main.pdf"
        pdf.write_bytes(b"first")
        fixed_ns = 1_800_000_000_000_000_000
        os.utime(pdf, ns=(fixed_ns, fixed_ns))
        calls = []

        def render(command, **_kwargs):
            calls.append(command)
            source = Path(command[-2]).read_bytes()
            Path(command[-1] + ".png").write_bytes(b"page:" + source)
            return {"reason": "completed", "returncode": 0}

        first = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=render,
        )
        second = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=render,
        )
        self.assertEqual(first["cache"], "rendered")
        self.assertEqual(second["cache"], "hit")
        self.assertEqual(first["path"].read_bytes(), b"page:first")
        self.assertEqual(len(calls), 1)

        pdf.write_bytes(b"other")
        os.utime(pdf, ns=(fixed_ns, fixed_ns))
        changed = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=render,
        )
        self.assertEqual(changed["cache"], "rendered")
        self.assertEqual(changed["path"].read_bytes(), b"page:other")
        self.assertEqual(len(calls), 2)

    def test_failed_page_render_preserves_previous_png_and_cleans_staging(self):
        pdf = self.typst_dir / "main.pdf"
        pdf.write_bytes(b"old-pdf")

        def succeed(command, **_kwargs):
            Path(command[-1] + ".png").write_bytes(b"old-png")
            return {"reason": "completed", "returncode": 0}

        previous = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=succeed,
        )
        pdf.write_bytes(b"new-pdf")

        def fail(command, **kwargs):
            Path(command[-1] + ".png").write_bytes(b"partial")
            Path(kwargs["stderr_path"]).write_text("renderer failed", encoding="utf-8")
            return {"reason": "completed", "returncode": 1}

        failed = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=fail,
        )
        self.assertEqual(failed["status"], "render_failed")
        self.assertIn("renderer failed", failed["error"])
        self.assertEqual(previous["path"].read_bytes(), b"old-png")
        self.assertEqual(
            [path for path in previous["path"].parent.iterdir() if path.name.startswith(".page-")],
            [],
        )

    def test_page_render_budgets_and_rejects_oversized_png(self):
        pdf = self.typst_dir / "main.pdf"
        pdf.write_bytes(b"large")
        captured = {}

        def oversized(command, **kwargs):
            captured["additional_output_paths"] = kwargs["additional_output_paths"]
            Path(command[-1] + ".png").write_bytes(
                b"x" * (4 * 1024 * 1024 + 1)
            )
            return {"reason": "completed", "returncode": 0}

        result = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=oversized,
        )
        self.assertEqual(result["status"], "render_failed")
        self.assertIn("oversized PNG", result["error"])
        self.assertEqual(len(captured["additional_output_paths"]), 1)
        self.assertEqual(list(self.preview_root.rglob("*.png")), [])

    def test_page_render_rejects_pdf_changed_during_renderer(self):
        pdf = self.typst_dir / "main.pdf"
        pdf.write_bytes(b"before")

        def replace_source(command, **_kwargs):
            pdf.write_bytes(b"after")
            Path(command[-1] + ".png").write_bytes(b"wrong-source")
            return {"reason": "completed", "returncode": 0}

        result = render_preview_page(
            self.preview_root,
            self.root,
            self.workspace,
            "Contest",
            0,
            page_count_fn=lambda _path: 1,
            run_managed_fn=replace_source,
        )
        self.assertEqual(result["status"], "render_failed")
        self.assertIn("PDF changed", result["error"])
        self.assertEqual(list(self.preview_root.rglob("*.png")), [])

    def test_concurrent_same_page_requests_publish_once(self):
        pdf = self.typst_dir / "main.pdf"
        pdf.write_bytes(b"parallel")
        calls = 0
        calls_lock = threading.Lock()

        def render(command, **_kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            Path(command[-1] + ".png").write_bytes(b"complete-png")
            return {"reason": "completed", "returncode": 0}

        def request_page():
            return render_preview_page(
                self.preview_root,
                self.root,
                self.workspace,
                "Contest",
                0,
                page_count_fn=lambda _path: 1,
                run_managed_fn=render,
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _index: request_page(), range(4)))
        self.assertTrue(all(result["status"] == "ok" for result in results))
        self.assertEqual(calls, 1)
        self.assertEqual(results[0]["path"].read_bytes(), b"complete-png")


if __name__ == "__main__":
    unittest.main()
