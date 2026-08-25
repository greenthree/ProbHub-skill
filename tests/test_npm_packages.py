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

from scripts.check_release import npm_pack_manifest


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
        self.assertIn("requirements.lock", main["files"])
        self.assertIn("scripts/webui/**", main["files"])
        self.assertIn("!scripts/audit_python_dependencies.py", main["files"])
        self.assertIn("!scripts/update_python_dependency_lock.py", main["files"])
        self.assertEqual(
            "python scripts/update_python_dependency_lock.py && python scripts/update_python_dependency_lock.py --source requirements-audit.txt --lock requirements-audit.lock",
            main["scripts"]["python:lock:check"],
        )
        self.assertEqual(
            "python scripts/update_python_dependency_lock.py --apply && python scripts/update_python_dependency_lock.py --source requirements-audit.txt --lock requirements-audit.lock --apply",
            main["scripts"]["python:lock:update"],
        )

    def test_judge_qa_runtime_and_contract_are_published(self):
        main_files = {
            entry["path"] for entry in npm_pack_manifest(dry_run=True)["files"]
        }
        self.assertIn("logo.svg", main_files)
        self.assertIn("school-badge.png", main_files)
        self.assertNotIn("references/usts.png", main_files)
        self.assertIn("probhub/judge_qa_evidence.py", main_files)
        self.assertIn("probhub/judge_qa_runtime.py", main_files)
        self.assertIn("probhub/mutation.py", main_files)
        self.assertIn("probhub/mutation_config.py", main_files)
        self.assertIn("probhub/mutation_syntax.py", main_files)
        self.assertIn("probhub/mutation_parser_worker.py", main_files)
        self.assertIn("references/checker-interactor.md", main_files)
        self.assertIn("references/mutation-testing.md", main_files)
        self.assertIn("requirements.lock", main_files)
        self.assertNotIn("requirements-audit.txt", main_files)
        self.assertNotIn("requirements-audit.lock", main_files)
        self.assertIn("probhub/dependency_lock.py", main_files)
        self.assertNotIn("scripts/update_python_dependency_lock.py", main_files)

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

    def test_installation_guides_share_the_supported_system_python_flow(self):
        documents = {
            "main README": (ROOT / "README.md").read_text(encoding="utf-8"),
            "compatibility README": (ROOT / "compat/probhub-skill/README.md").read_text(encoding="utf-8"),
            "Agent installation reference": (ROOT / "references/installation.md").read_text(encoding="utf-8"),
        }
        windows = (
            "npm install -g probhub\n"
            "$env:PROBHUB_ALLOW_SYSTEM_PYTHON = \"1\"\n"
            "probhub-skill\n"
            "probhub doctor"
        )
        linux = (
            "npm install -g probhub\n"
            "PROBHUB_ALLOW_SYSTEM_PYTHON=1 probhub-skill\n"
            "probhub doctor"
        )
        for label, content in documents.items():
            with self.subTest(document=label):
                self.assertIn("Node.js 18", content)
                self.assertIn("CPython 3.10", content)
                self.assertIn("3.12", content)
                self.assertIn("x86_64", content)
                self.assertIn("python3-pip", content)
                self.assertIn(windows, content)
                self.assertIn(linux, content)
                self.assertIn("probhub --json ui --check", content)
                self.assertNotIn("python3 -m venv", content)
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/installation.md", skill)
        installation = documents["Agent installation reference"]
        self.assertIn("PIP_BREAK_SYSTEM_PACKAGES=1", installation)
        self.assertIn("pip install --user", installation)
        self.assertIn("不会移除 `--user`", installation)

    def test_installation_guides_explain_whole_directory_replacement(self):
        chinese_documents = {
            "main README": (ROOT / "README.md").read_text(encoding="utf-8"),
            "compatibility README": (ROOT / "compat/probhub-skill/README.md").read_text(encoding="utf-8"),
            "Agent installation reference": (ROOT / "references/installation.md").read_text(encoding="utf-8"),
        }
        for label, content in chinese_documents.items():
            with self.subTest(document=label):
                self.assertIn("完整目录整体替换", content)
                self.assertIn("本地手工修改不会保留", content)

        english = (ROOT / "README_EN.md").read_text(encoding="utf-8")
        self.assertIn("replaces both Skill directories as whole directories", english)
        self.assertIn("Local manual changes inside them are not preserved", english)

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

    def test_skill_requires_aggregate_validator_enforcement_review(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        verification = (ROOT / "references/verification-modes.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "constraint_reconciliation.aggregate_constraints",
            "statement_only",
            "aggregate_constraint_mismatch",
            "足够宽的累加类型",
            "每组目标量恰好累计一次",
            "ensuref",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)
        self.assertIn("aggregate-limit-derivation.md", verification)
        self.assertIn("上限内接受/超限拒绝", verification)
        self.assertNotIn("ensuref", verification)

    def test_skill_routes_and_covers_aggregate_limit_derivation(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        verification = (ROOT / "references/verification-modes.md").read_text(
            encoding="utf-8"
        )
        reference_path = ROOT / "references/aggregate-limit-derivation.md"
        reference = reference_path.read_text(encoding="utf-8")

        self.assertIn("references/aggregate-limit-derivation.md", skill)
        self.assertIn("aggregate-limit-derivation.md", verification)
        release_check = (ROOT / "scripts/check_release.py").read_text(encoding="utf-8")
        self.assertIn("references/aggregate-limit-derivation.md", release_check)

        scenarios = {
            "test count range": ("1 <= T <= T_max", "5 <= T_max <= 100000"),
            "linear high-demand default": ("C(n)/n", "sum(n_i) <= 10 * N"),
            "joint feasibility": ("T_max * n_min <= K * N", "T_max >= K"),
            "n log n": ("O(n log n)", "large_case_equivalents"),
            "quadratic": ("O(n^2)", "K * C(N)"),
            "graph": ("O(n+m)", "sum(m_i) <= K*M"),
            "two-dimensional": ("O(nm)", "sum(n_i*m_i) <= K*N*M"),
            "fixed per-case cost": ("T * F", "T*U"),
            "high test count": ("100000", "input_io + output_io"),
            "runtime calibration": ("accepted_max_time * 3 <= TL", "target_guarantee: false"),
            "decision record": ("aggregate_limit_derivation:", "decision: accepted"),
            "joint decision record": ("jointly_reachable: true", "decision: needs_review"),
        }
        for scenario, markers in scenarios.items():
            with self.subTest(scenario=scenario):
                for marker in markers:
                    self.assertIn(marker, reference)

        self.assertIn("本文不是 Core Schema", reference)
        self.assertIn("decision: needs_review", reference)
        self.assertIn("常见候选区间", reference)
        self.assertIn("不是所有题目的 seal 硬门槛", reference)
        self.assertIn("10N` 不是普适规则", reference)
        self.assertIn("accepted/Validator/Checker/Interactor", verification)
        self.assertNotIn("推导的 `T_max` 必须在 `5..100000`", verification)

    def test_npm_pack_json_accepts_legacy_and_current_single_package_shapes(self):
        from scripts import check_release

        manifest = {"name": "probhub", "files": [{"path": "package.json"}]}
        command = ["npm", "pack", "--json"]
        self.assertEqual(
            check_release._parse_npm_json(json.dumps([manifest]), command),
            manifest,
        )
        self.assertEqual(
            check_release._parse_npm_json(
                json.dumps({"probhub": manifest}),
                command,
            ),
            manifest,
        )

        for payload in ([], {}, {"probhub": []}, {"one": manifest, "two": manifest}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    check_release.ReleaseCheckError,
                    "unexpected npm pack response",
                ):
                    check_release._parse_npm_json(json.dumps(payload), command)

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
        self.assertEqual(metadata["tag"], "v0.7.0")

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

    def test_stdlib_bootstrap_reports_runtime_import_failure_without_traceback(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "probhub", "--json", "lint"],
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
        self.assertEqual(payload["code"], "runtime_dependency_missing")
        self.assertEqual(payload["module"], "yaml")
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
        with tempfile.TemporaryDirectory(prefix="ProbHub 中文路径 ") as temp:
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
            self.assertEqual(version.stdout.strip(), "0.7.0")

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
            self.assertNotIn("已整体替换现有 ProbHub Skill 目录", installed.stdout)
            self.assertFalse(sentinel.exists())
            for target in (
                project / ".claude/skills/probhub",
                project / ".agents/skills/probhub",
            ):
                marker = json.loads(
                    (target / ".probhub-version.json").read_text(encoding="utf-8")
                )
                self.assertEqual(marker["version"], "0.7.0")
                self.assertEqual(
                    (target / "logo.svg").read_bytes(),
                    (ROOT / "logo.svg").read_bytes(),
                )
                self.assertEqual(
                    (target / "school-badge.png").read_bytes(),
                    (ROOT / "school-badge.png").read_bytes(),
                )

            for target in (
                project / ".claude/skills/probhub",
                project / ".agents/skills/probhub",
            ):
                (target / "manual.txt").write_text("local change\n", encoding="utf-8")

            reinstalled = subprocess.run(
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
            self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
            self.assertEqual(
                reinstalled.stdout.count("已整体替换现有 ProbHub Skill 目录"),
                2,
            )
            self.assertIn("本地手工修改不会保留", reinstalled.stdout)
            for target in (
                project / ".claude/skills/probhub",
                project / ".agents/skills/probhub",
            ):
                self.assertFalse((target / "manual.txt").exists())

    def test_dependency_installer_requires_explicit_system_python_consent(self):
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
        self.assertIn("PROBHUB_ALLOW_SYSTEM_PYTHON=1", error.getvalue())

    def test_dependency_installer_accepts_explicit_system_python_consent(self):
        from probhub import install_deps

        completed = {"reason": "completed", "returncode": 0, "message": None}
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=False),
            patch.dict(
                os.environ,
                {
                    "PROBHUB_ALLOW_SYSTEM_PYTHON": "1",
                    "PYTHONHOME": "polluted-home",
                    "PYTHONPATH": "polluted-path",
                    "PYTHONSTARTUP": "polluted-startup",
                },
                clear=False,
            ),
            patch("probhub.install_deps.verify_wheelhouse", return_value={"ok": True}),
            patch("probhub.install_deps.run_managed_to_files", return_value=completed) as run,
        ):
            code = install_deps.main()
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        download_command = run.call_args_list[0].args[0]
        command = run.call_args_list[1].args[0]
        self.assertIn(sys.executable, command)
        self.assertIn("pip", command)
        self.assertIn("install", command)
        self.assertIn("--user", command)
        self.assertIn("--no-index", command)
        self.assertIn("--find-links", command)
        self.assertIn("--force-reinstall", command)
        self.assertIn("download", download_command)
        self.assertIn("--dest", download_command)
        self.assertIn(str(ROOT / "requirements.lock"), command)
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["PIP_BREAK_SYSTEM_PACKAGES"], "1")
        self.assertNotIn("PYTHONHOME", child_env)
        self.assertNotIn("PYTHONPATH", child_env)
        self.assertNotIn("PYTHONSTARTUP", child_env)

    def test_dependency_installer_keeps_virtual_environment_local(self):
        from probhub import install_deps

        completed = {"reason": "completed", "returncode": 0, "message": None}
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=True),
            patch("probhub.install_deps.verify_wheelhouse", return_value={"ok": True}),
            patch("probhub.install_deps.run_managed_to_files", return_value=completed) as run,
        ):
            code = install_deps.main()
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("--user", run.call_args_list[1].args[0])
        self.assertNotIn("PIP_BREAK_SYSTEM_PACKAGES", run.call_args.kwargs["env"])

    def test_dependency_installer_rejects_an_invalid_lock_before_starting_pip(self):
        from probhub import install_deps
        from probhub.dependency_lock import DependencyLockError

        error = io.StringIO()
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=True),
            patch(
                "probhub.install_deps.load_dependency_lock",
                side_effect=DependencyLockError("dependency_lock_missing", "lock missing"),
            ),
            patch("probhub.install_deps.run_managed_to_files") as run,
            redirect_stderr(error),
        ):
            code = install_deps.main()

        self.assertEqual(code, 1)
        self.assertIn("dependency_lock_missing", error.getvalue())
        run.assert_not_called()

    def test_dependency_download_failure_never_starts_install(self):
        from probhub import install_deps

        failed = {"reason": "completed", "returncode": 1, "message": None}
        error = io.StringIO()
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=True),
            patch("probhub.install_deps.run_managed_to_files", return_value=failed) as run,
            patch("probhub.install_deps.verify_wheelhouse") as verify,
            redirect_stderr(error),
        ):
            code = install_deps.main()

        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("download", run.call_args.args[0])
        verify.assert_not_called()
        self.assertIn("dependency download failed", error.getvalue())

    def test_invalid_downloaded_wheel_never_starts_install(self):
        from probhub import install_deps
        from probhub.dependency_lock import DependencyLockError

        completed = {"reason": "completed", "returncode": 0, "message": None}
        error = io.StringIO()
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=True),
            patch("probhub.install_deps.run_managed_to_files", return_value=completed) as run,
            patch(
                "probhub.install_deps.verify_wheelhouse",
                side_effect=DependencyLockError(
                    "dependency_wheel_hash_mismatch",
                    "tampered wheel",
                ),
            ),
            redirect_stderr(error),
        ):
            code = install_deps.main()

        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("dependency_wheel_hash_mismatch", error.getvalue())

    def test_dependency_installer_explains_missing_ubuntu_pip(self):
        from probhub import install_deps

        def missing_pip(_command, **kwargs):
            kwargs["stderr_path"].write_text(
                "/usr/bin/python3: No module named pip\n",
                encoding="utf-8",
            )
            return {"reason": "completed", "returncode": 1, "message": None}

        error = io.StringIO()
        with (
            patch("probhub.install_deps._inside_virtual_environment", return_value=False),
            patch.dict(os.environ, {"PROBHUB_ALLOW_SYSTEM_PYTHON": "1"}, clear=False),
            patch("probhub.install_deps.run_managed_to_files", side_effect=missing_pip),
            redirect_stderr(error),
        ):
            code = install_deps.main()
        self.assertEqual(code, 1)
        self.assertIn("sudo apt install python3-pip", error.getvalue())

    def test_windows_dependency_installer_uses_a_bounded_node_supervisor(self):
        from probhub import install_deps

        requirements = ROOT / "requirements.lock"
        with (
            patch("probhub.install_deps.os.name", "nt"),
            patch("probhub.install_deps.shutil.which", return_value="C:/node/node.exe"),
            patch.dict(os.environ, {"PYTHON": "C:/python/python.exe"}, clear=False),
        ):
            command = install_deps._pip_install_command(
                requirements,
                user_install=False,
                wheelhouse="C:/tmp/wheelhouse",
            )
        self.assertEqual(command[:2], ["C:/node/node.exe", "-e"])
        self.assertEqual(command[3:7], ["C:/python/python.exe", "-m", "pip", "install"])
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--no-index", command)
        self.assertIn("--find-links", command)
        self.assertEqual(command[-1], str(requirements))

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
