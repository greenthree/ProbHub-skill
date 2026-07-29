import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class NpmPackageMetadataTests(unittest.TestCase):
    def load_json(self, relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8-sig"))

    def test_main_and_compatibility_packages_share_exact_version(self):
        main = self.load_json("package.json")
        compat = self.load_json("compat/probhub-skill/package.json")
        python_version = {}
        exec((ROOT / "probhub/__init__.py").read_text(encoding="utf-8"), python_version)

        self.assertEqual("probhub", main["name"])
        self.assertEqual("probhub-skill", compat["name"])
        self.assertEqual(main["version"], python_version["__version__"])
        self.assertEqual(main["version"], compat["version"])
        self.assertEqual({"probhub": main["version"]}, compat["dependencies"])
        lock = self.load_json("package-lock.json")
        self.assertEqual(main["version"], lock["version"])
        self.assertEqual(main["version"], lock["packages"][""]["version"])
        self.assertNotIn("dependencies", main)
        self.assertNotIn("dependencies", lock["packages"][""])
        self.assertEqual({"node": ">=18"}, main["engines"])
        self.assertEqual(main["engines"], compat["engines"])
        self.assertIn("--require-head-tag", main["scripts"]["prepublishOnly"])
        self.assertIn("--require-clean", main["scripts"]["prepublishOnly"])
        self.assertIn("--require-head-tag", compat["scripts"]["prepublishOnly"])
        self.assertIn("CHANGELOG.md", main["files"])

    def test_both_packages_expose_cli_and_skill_installer(self):
        expected_bins = {
            "probhub-skill": "bin/init.js",
            "probhub": "bin/probhub.js",
        }
        main = self.load_json("package.json")
        compat = self.load_json("compat/probhub-skill/package.json")

        self.assertEqual(expected_bins, main["bin"])
        self.assertEqual(expected_bins, compat["bin"])
        self.assertIn("require('probhub/bin/init.js')", (ROOT / "compat/probhub-skill/bin/init.js").read_text(encoding="utf-8"))
        self.assertIn("require('probhub/bin/probhub.js')", (ROOT / "compat/probhub-skill/bin/probhub.js").read_text(encoding="utf-8"))

    def test_release_metadata_gate_passes_for_the_source_tree(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_release.py"), "--json"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_publish_release_gate_requires_matching_tag_and_clean_worktree(self):
        from scripts import check_release

        def completed(stdout=""):
            return {
                "reason": "completed",
                "returncode": 0,
                "stdout": stdout,
                "stderr": "",
            }

        with (
            patch("scripts.check_release.shutil.which", return_value="git"),
            patch(
                "scripts.check_release.run_bounded",
                side_effect=[completed("abc\n"), completed("abc\n"), completed("")],
            ),
        ):
            metadata = check_release.validate_metadata(
                require_head_tag=True,
                require_clean=True,
            )
        self.assertEqual(metadata["tag"], "v0.6.0")

        with (
            patch("scripts.check_release.shutil.which", return_value="git"),
            patch(
                "scripts.check_release.run_bounded",
                side_effect=[
                    completed("abc\n"),
                    completed("abc\n"),
                    completed(" M package.json\n"),
                ],
            ),
            self.assertRaisesRegex(check_release.ReleaseCheckError, "must be clean"),
        ):
            check_release.validate_metadata(
                require_head_tag=True,
                require_clean=True,
            )

    def test_stdlib_bootstrap_reports_missing_modules_without_traceback(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "probhub", "--json", "doctor"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["python_modules"]["yaml"])
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_skill_installer_rejects_source_directory_and_descendants(self):
        for cwd in (ROOT, ROOT / "probhub"):
            with self.subTest(cwd=cwd):
                result = subprocess.run(
                    [shutil.which("node"), str(ROOT / "bin/init.js"), "--local", "--skip-python-deps"],
                    cwd=cwd,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("不能在源码目录", result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_node_entrypoints_cannot_be_shadowed_by_workspace_module(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            sentinel = project / "shadow-executed"
            shadow_payload = (
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['PROBHUB_SHADOW_SENTINEL']).write_text('executed')\n"
                "raise SystemExit(97)\n"
            )
            for shadow_name in ("probhub.py", "runpy.py", "sitecustomize.py"):
                (project / shadow_name).write_text(shadow_payload, encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "PYTHON": sys.executable,
                "PROBHUB_SHADOW_SENTINEL": str(sentinel),
            })

            version = subprocess.run(
                [shutil.which("node"), str(ROOT / "bin/probhub.js"), "--version"],
                cwd=project,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), "0.6.0")

            installed = subprocess.run(
                [
                    shutil.which("node"),
                    str(ROOT / "bin/init.js"),
                    "--local",
                    "--skip-python-deps",
                ],
                cwd=project,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertFalse(sentinel.exists())
            for target in (
                project / ".claude/skills/probhub",
                project / ".agents/skills/probhub",
            ):
                marker = json.loads(
                    (target / ".probhub-version.json").read_text(encoding="utf-8")
                )
                self.assertEqual(marker["version"], "0.6.0")

    def test_dependency_installer_requires_an_explicit_virtual_environment(self):
        from probhub import install_deps

        error = io.StringIO()
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=False),
            patch.dict(os.environ, {}, clear=False),
            redirect_stderr(error),
        ):
            os.environ.pop("PROBHUB_ALLOW_SYSTEM_PYTHON", None)
            code = install_deps.main()
        self.assertEqual(code, 1)
        self.assertIn("virtual environment", error.getvalue())

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_dependency_install_failure_preserves_both_existing_skills(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            project = root / "project"
            (source / "bin").mkdir(parents=True)
            (source / "probhub").mkdir()
            project.mkdir()
            shutil.copy2(ROOT / "bin/init.js", source / "bin/init.js")
            shutil.copy2(ROOT / "bin/python.js", source / "bin/python.js")
            shutil.copy2(ROOT / "package.json", source / "package.json")
            (source / "probhub/__init__.py").write_text("", encoding="utf-8")
            (source / "probhub/install_deps.py").write_text(
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            sentinel = root / "install-skill-called"
            (source / "probhub/install_skill.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['PROBHUB_TEST_SENTINEL']).write_text('called')\n",
                encoding="utf-8",
            )
            targets = (
                project / ".claude/skills/probhub",
                project / ".agents/skills/probhub",
            )
            for index, target in enumerate(targets):
                target.mkdir(parents=True)
                (target / "old.txt").write_text(f"old-{index}\n", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "PYTHON": sys.executable,
                "PROBHUB_TEST_SENTINEL": str(sentinel),
            })
            env.pop("PROBHUB_SKIP_PYTHON_DEPS", None)
            result = subprocess.run(
                [shutil.which("node"), str(source / "bin/init.js"), "--local"],
                cwd=project,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(sentinel.exists())
            for index, target in enumerate(targets):
                self.assertEqual(
                    (target / "old.txt").read_text(encoding="utf-8"),
                    f"old-{index}\n",
                )
            self.assertEqual(list(project.glob(".probhub-skill-*-*")), [])


if __name__ == "__main__":
    unittest.main()
