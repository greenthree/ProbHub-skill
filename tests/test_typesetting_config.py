import tempfile
import unittest
from pathlib import Path
from unittest import mock

from probhub.typesetting import compile_collection


class TypesettingConfigTests(unittest.TestCase):
    def test_workspace_cover_config_uses_temporary_typst_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            typst_dir = root / "typst/contest"
            typst_dir.mkdir(parents=True)
            main = typst_dir / "main.typ"
            lib = typst_dir.parent / "lib.typ"
            main.write_text(
                '#import "../lib.typ": contest-conf\n'
                '#show: contest-conf.with(\n'
                '  title: "Old",\n  subtitle: "Old Sub",\n'
                '  author: "Old Team",\n  date: "Old Date",\n)\n',
                encoding="utf-8",
            )
            lib.write_text(
                'v(1em)\nalign(center, image("old.png", width: 9cm))\nv(2em)\n',
                encoding="utf-8",
            )
            original_main = main.read_bytes()
            original_lib = lib.read_bytes()
            workspace = {
                "contest": {"title": "New", "subtitle": "Final", "author": "Team", "date": "2026"},
                "typst": {
                    "directory": "typst/contest",
                    "cover": {
                        "logo": "new.png",
                        "logo_width": "8cm",
                        "logo_space_above": "-1em",
                        "logo_space_below": "3em",
                    },
                },
            }

            def fake_run(command, **kwargs):
                compiled = root / command[-2]
                text = compiled.read_text(encoding="utf-8")
                self.assertIn('title: "New"', text)
                self.assertIn('subtitle: "Final"', text)
                match = next(path for path in typst_dir.parent.glob(".probhub-lib-*.typ"))
                lib_text = match.read_text(encoding="utf-8")
                self.assertIn('image("new.png", width: 8cm)', lib_text)
                self.assertIn("v(-1em)", lib_text)
                self.assertIn("v(3em)", lib_text)
                (root / command[-1]).write_bytes(b"pdf")
                return {"reason": "completed", "returncode": 0}

            with (
                mock.patch("probhub.typesetting.write_typst_collection", return_value=(typst_dir, [])),
                mock.patch("probhub.typesetting.run_managed_to_files", side_effect=fake_run),
            ):
                _, output, _ = compile_collection(root, workspace, [])

            self.assertEqual(output.read_bytes(), b"pdf")
            self.assertEqual(main.read_bytes(), original_main)
            self.assertEqual(lib.read_bytes(), original_lib)
            self.assertEqual(list(root.rglob(".probhub-*.typ")), [])

    def test_typst_failure_returns_bounded_diagnostics_and_cleans_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            typst_dir = root / "typst/contest"
            typst_dir.mkdir(parents=True)
            main = typst_dir / "main.typ"
            main.write_text(
                '#show: document.with(title: "Old")\n',
                encoding="utf-8",
            )
            workspace = {
                "contest": {"title": "New"},
                "typst": {"directory": "typst/contest"},
            }

            def fake_run(command, **kwargs):
                Path(kwargs["stderr_path"]).write_text("syntax error", encoding="utf-8")
                return {"reason": "completed", "returncode": 1, "message": ""}

            with (
                mock.patch("probhub.typesetting.write_typst_collection", return_value=(typst_dir, [])),
                mock.patch("probhub.typesetting.run_managed_to_files", side_effect=fake_run),
            ):
                with self.assertRaisesRegex(Exception, "syntax error"):
                    compile_collection(root, workspace, [])

            self.assertEqual(list(root.rglob(".probhub-*.typ")), [])

    def test_cover_config_requires_shared_typst_library(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            typst_dir = root / "typst/contest"
            typst_dir.mkdir(parents=True)
            (typst_dir / "main.typ").write_text("// fixture\n", encoding="utf-8")
            workspace = {
                "typst": {
                    "directory": "typst/contest",
                    "cover": {"logo": "logo.png"},
                },
            }

            with mock.patch(
                "probhub.typesetting.write_typst_collection",
                return_value=(typst_dir, []),
            ):
                with self.assertRaisesRegex(Exception, "requires the shared library"):
                    compile_collection(root, workspace, [])

            self.assertEqual(list(root.rglob(".probhub-*.typ")), [])


if __name__ == "__main__":
    unittest.main()
