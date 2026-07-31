import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from probhub import cli, webui_runtime
from probhub.errors import ProbHubError
from probhub.io import write_yaml


class WebUiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".probhub").mkdir()
        write_yaml(self.root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "contest": {"title": "Fixture", "subtitle": "Contest"},
            "typst": {"directory": "typst-statement/Contest"},
            "problems": [],
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_check_loads_packaged_webui_from_explicit_workspace(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main([
                "--workspace", str(self.root),
                "--json", "ui", "--check",
            ])
        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["workspace"]), self.root.resolve())
        self.assertEqual(Path(result["runtime"]).name, "ui.py")

    def test_run_webui_uses_workspace_as_cwd_and_restores_caller(self):
        previous = Path.cwd()
        observed = {}
        module = mock.Mock()

        def run_server(**kwargs):
            observed["cwd"] = Path.cwd()
            observed["kwargs"] = kwargs

        module.run_server = run_server
        with mock.patch.object(
            webui_runtime,
            "_load_webui_module",
            return_value=(module, Path("installed/scripts/ui.py")),
        ):
            result = webui_runtime.run_webui(
                self.root,
                port=34567,
                open_browser=False,
            )

        self.assertEqual(observed["cwd"], self.root.resolve())
        self.assertEqual(observed["kwargs"], {"port": 34567, "open_browser": False})
        self.assertEqual(Path.cwd(), previous)
        self.assertTrue(result["ok"])

    def test_missing_packaged_webui_is_a_stable_error(self):
        with mock.patch("probhub.webui_runtime.Path.is_file", return_value=False):
            with self.assertRaises(ProbHubError) as raised:
                webui_runtime._webui_script()
        self.assertEqual(raised.exception.code, "webui_runtime_missing")

    def test_missing_packaged_webui_asset_is_a_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "scripts/ui.py"
            script.parent.mkdir()
            script.write_text("", encoding="utf-8")
            (script.parent / "webui").mkdir()
            with self.assertRaises(ProbHubError) as raised:
                webui_runtime._require_webui_assets(script)
        self.assertEqual(raised.exception.code, "webui_runtime_missing")
        self.assertIn("asset-manifest.json", str(raised.exception))

    def test_tampered_packaged_webui_asset_is_a_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "scripts/ui.py"
            asset_root = script.parent / "webui"
            asset_root.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            (asset_root / "index.html").write_text("tampered", encoding="utf-8")
            (asset_root / "asset-manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "files": {"index.html": "sha256:" + "0" * 64},
            }), encoding="utf-8")
            with self.assertRaises(ProbHubError) as raised:
                webui_runtime._require_webui_assets(script)
        self.assertEqual(raised.exception.code, "webui_runtime_invalid")
        self.assertIn("hash mismatch", str(raised.exception))

    def test_incomplete_packaged_webui_manifest_is_a_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "scripts/ui.py"
            asset_root = script.parent / "webui"
            asset_root.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            index = b"index"
            (asset_root / "index.html").write_bytes(index)
            (asset_root / "app.js").write_text("app", encoding="utf-8")
            import hashlib

            (asset_root / "asset-manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "files": {
                    "index.html": "sha256:" + hashlib.sha256(index).hexdigest(),
                },
            }), encoding="utf-8")
            with self.assertRaises(ProbHubError) as raised:
                webui_runtime._require_webui_assets(script)
        self.assertEqual(raised.exception.code, "webui_runtime_invalid")
        self.assertIn("inventory", str(raised.exception))

    def test_webui_dependency_failure_is_a_stable_error(self):
        loader = mock.Mock()
        loader.exec_module.side_effect = ModuleNotFoundError("missing", name="flask")
        spec = mock.Mock(loader=loader)
        with (
            mock.patch.object(webui_runtime, "_webui_script", return_value=Path("ui.py")),
            mock.patch("probhub.webui_runtime.importlib.util.spec_from_file_location", return_value=spec),
            mock.patch("probhub.webui_runtime.importlib.util.module_from_spec", return_value=mock.Mock()),
            self.assertRaises(ProbHubError) as raised,
        ):
            webui_runtime._load_webui_module()
        self.assertEqual(raised.exception.code, "webui_dependency_missing")


if __name__ == "__main__":
    unittest.main()
