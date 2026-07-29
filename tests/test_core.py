import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.cli import build_parser, main as cli_main
from probhub.errors import ProbHubError
from probhub.hashing import hash_file
from probhub.io import read_yaml, write_json, write_yaml
from probhub.linting import (
    BUILD_MANIFEST_SCHEMA_VERSION,
    compute_data_hash,
    compute_source_hash,
    lint_workspace,
    problem_status,
)
from probhub.metadata import build_meta
from probhub.workspace import load_problem, load_workspace, problem_entries


class CoreWorkspaceTests(unittest.TestCase):
    def create_workspace(self, root):
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
            "lint": {"forbidden_patterns": ["114514"]},
        })
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# Test\n\n## 题目描述\n\nDescription.\n\n## 输入格式\n\nInput.\n\n## 输出格式\n\nOutput.\n",
            encoding="utf-8",
        )
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("2\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("2\n", encoding="utf-8")
        (problem / "code/validator.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        write_yaml(problem / "probhub.yaml", {
            "schema_version": 1,
            "id": "A",
            "name": "Test",
            "display_name": "Test",
            "limits": {"time": 1, "memory": 256, "output": 64},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        return problem

    def test_init_then_new(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["--json", "init", str(root), "--title", "Contest"]), 0)
                self.assertEqual(cli_main(["--workspace", str(root), "--json", "new", "A", "--name", "First"]), 0)
            _, workspace = load_workspace(root)
            self.assertEqual(problem_entries(workspace)[0]["id"], "A")
            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("**/.probhub/build.lock", gitignore)
            self.assertIn("**/.probhub/checkpoints/", gitignore)
            self.assertIn("**/.probhub/generations/", gitignore)
            self.assertIn("**/.probhub/stress/", gitignore)

    def test_cli_emits_machine_readable_error_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            output = io.StringIO()
            with (
                patch(
                    "probhub.cli.build_workspace",
                    side_effect=ProbHubError("busy", code="build_busy"),
                ),
                redirect_stdout(output),
            ):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "build",
                    "A",
                ])

            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["code"], "build_busy")

    def test_typeset_cli_defaults_to_all_workspace_problems(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            workspace_path = root / ".probhub/workspace.yaml"
            workspace = read_yaml(workspace_path)
            workspace["problems"].append({"id": "B", "directory": "B"})
            write_yaml(workspace_path, workspace)
            output = io.StringIO()

            with (
                patch(
                    "probhub.cli.typeset_workspace",
                    return_value={"ok": True, "pdfs": {}},
                ) as typeset,
                redirect_stdout(output),
            ):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "typeset",
                ])

            self.assertEqual(code, 0, output.getvalue())
            called_root, called_workspace, called_entries = typeset.call_args.args
            self.assertEqual(called_root, root.resolve())
            self.assertEqual(called_workspace["schema_version"], 1)
            self.assertEqual([entry["id"] for entry in called_entries], ["A", "B"])

    def test_typeset_cli_selects_only_explicit_problem_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            workspace_path = root / ".probhub/workspace.yaml"
            workspace = read_yaml(workspace_path)
            workspace["problems"].append({"id": "B", "directory": "B"})
            write_yaml(workspace_path, workspace)
            output = io.StringIO()

            with (
                patch(
                    "probhub.cli.typeset_workspace",
                    return_value={"ok": True, "pdfs": {}},
                ) as typeset,
                redirect_stdout(output),
            ):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "typeset",
                    "B",
                ])

            self.assertEqual(code, 0, output.getvalue())
            called_entries = typeset.call_args.args[2]
            self.assertEqual([entry["id"] for entry in called_entries], ["B"])

    def test_typeset_cli_emits_structured_probhub_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            output = io.StringIO()

            with (
                patch(
                    "probhub.cli.typeset_workspace",
                    side_effect=ProbHubError(
                        "injected Typst failure",
                        code="typeset_failed",
                    ),
                ),
                redirect_stdout(output),
            ):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "typeset",
                    "A",
                ])

            result = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "typeset_failed")
            self.assertEqual(result["error"], "injected Typst failure")

    def test_cli_reports_invalid_yaml_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            config_path = problem / "probhub.yaml"
            for payload in ("limits: [\n", "- not\n- a\n- mapping\n"):
                with self.subTest(payload=payload):
                    config_path.write_text(payload, encoding="utf-8")
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = cli_main([
                            "--workspace", str(root), "--json", "lint",
                        ])
                    self.assertEqual(code, 1)
                    result = json.loads(output.getvalue())
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["code"], "invalid_yaml")

    def test_cli_structures_unexpected_internal_errors(self):
        output = io.StringIO()
        with (
            patch(
                "probhub.cli.command_doctor",
                side_effect=RuntimeError("injected internal failure"),
            ),
            redirect_stdout(output),
        ):
            code = cli_main(["--json", "doctor"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["code"], "internal_error")
        self.assertIn("injected internal failure", result["error"])

    def test_new_command_scaffolds_schema_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["--workspace", str(root), "--json", "new", "B", "--name", "Second"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue((root / "B/probhub.yaml").is_file())
            self.assertTrue((root / "B/code").is_dir())
            _, config = load_problem(root, {"id": "B", "directory": "B"})
            self.assertEqual(config["judge"]["validator"], "code/validator.cpp")
            self.assertEqual(config["limits"]["output"], 64)
            self.assertEqual(config["limits"]["processes"], 32)
            self.assertEqual(
                [entry["file"] for entry in config["solutions"]["accepted"]],
                ["code/std.cpp", "code/std2.cpp"],
            )
            self.assertEqual(config["generators"], ["code/inmaker.cpp"])
            self.assertTrue((root / "B/code/std.cpp").is_file())
            self.assertTrue((root / "B/data/sample/1.in").is_file())
            _, workspace = load_workspace(root)
            self.assertEqual([entry["id"] for entry in problem_entries(workspace)], ["A", "B"])

    def test_judge_sample_check_and_build_accept_no_cache(self):
        parser = build_parser()
        self.assertTrue(parser.parse_args(["judge", "A", "--no-cache"]).no_cache)
        self.assertTrue(parser.parse_args(["sample-check", "A", "--no-cache"]).no_cache)
        self.assertTrue(parser.parse_args(["build", "A", "--no-cache"]).no_cache)

    def test_verify_package_can_use_workspace_problem_context(self):
        from probhub.package_tools import build_package, generate_domjudge_config

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            (problem / "problem.pdf").write_bytes(b"%PDF-1.4\n")
            generate_domjudge_config(problem, config)
            package = root / "A.zip"
            build_package(problem, package, config=config)
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main([
                    "--workspace",
                    str(root),
                    "--json",
                    "verify-package",
                    str(package),
                    "--require-pdf",
                    "--problem",
                    "A",
                ])

            result = json.loads(output.getvalue())
            self.assertEqual(code, 0, result)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["verification_scope"], "deep")
            self.assertTrue(result["checks"]["source_consistency"])
            self.assertTrue(result["checks"]["input_validator"])
            self.assertEqual(result["stats"]["validator_cases"], 2)

    def test_lint_metadata_and_stale_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            _, config = load_problem(root, entry)
            lint = lint_workspace(root, workspace)
            self.assertTrue(lint["ok"], lint)
            meta = build_meta(problem, config)
            self.assertEqual(meta["problem"]["samples"], [{"input": "1", "output": "1"}])
            self.assertEqual(problem_status(problem, config)["state"], "never-built")
            (problem / "problem.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "A.zip").write_bytes(b"zip")
            manifest = {
                "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
                "batch_id": "fixture-batch",
                "sealed_revision_id": "fixture-revision",
                "source_hash": compute_source_hash(problem, config),
                "data_hash": compute_data_hash(problem, config),
                "pdf_hash": hash_file(problem / "problem.pdf"),
                "package_hash": hash_file(root / "A.zip"),
            }
            write_json(problem / ".probhub/build-manifest.json", manifest)
            self.assertEqual(problem_status(problem, config)["state"], "current")

            manifest["schema_version"] = 1
            write_json(problem / ".probhub/build-manifest.json", manifest)
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["manifest_schema"])

            manifest["schema_version"] = BUILD_MANIFEST_SCHEMA_VERSION
            write_json(problem / ".probhub/build-manifest.json", manifest)
            manifest.pop("batch_id")
            write_json(problem / ".probhub/build-manifest.json", manifest)
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["batch_id"])

            manifest["batch_id"] = "fixture-batch"
            manifest.pop("sealed_revision_id")
            write_json(problem / ".probhub/build-manifest.json", manifest)
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["stale_fields"], ["sealed_revision_id"])

            manifest["sealed_revision_id"] = "fixture-revision"
            write_json(problem / ".probhub/build-manifest.json", manifest)
            with (problem / "problem.md").open("a", encoding="utf-8") as stream:
                stream.write("\nchanged\n")
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertIn("source_hash", status["stale_fields"])

    def test_source_hash_and_status_track_code_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            _, config = load_problem(root, entry)
            header = problem / "code/include/helper.HPP"
            header.parent.mkdir(parents=True)
            header.write_text("constexpr int value = 1;\n", encoding="utf-8")
            first_source_hash = compute_source_hash(problem, config)
            (problem / "problem.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "A.zip").write_bytes(b"zip")
            write_json(problem / ".probhub/build-manifest.json", {
                "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
                "batch_id": "fixture-batch",
                "sealed_revision_id": "fixture-revision",
                "source_hash": first_source_hash,
                "data_hash": compute_data_hash(problem, config),
                "pdf_hash": hash_file(problem / "problem.pdf"),
                "package_hash": hash_file(root / "A.zip"),
            })
            self.assertEqual(problem_status(problem, config)["state"], "current")

            header.write_text("constexpr int value = 2;\n", encoding="utf-8")

            self.assertNotEqual(first_source_hash, compute_source_hash(problem, config))
            status = problem_status(problem, config)
            self.assertEqual(status["state"], "stale")
            self.assertIn("source_hash", status["stale_fields"])

    def test_status_treats_corrupt_manifest_as_never_built(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            _, config = load_problem(root, problem_entries(workspace)[0])
            manifest_path = problem / ".probhub/build-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            for payload in (b'{"schema_version": 2, "batch', b"", b"[1, 2]"):
                manifest_path.write_bytes(payload)
                status = problem_status(problem, config)
                self.assertEqual(status["state"], "never-built", payload)
                self.assertTrue(status["manifest_error"], payload)
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["--workspace", str(root), "--json", "status"])
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["problems"]["A"]["state"], "never-built")

    def test_write_yaml_and_write_json_are_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            yaml_path = root / "nested/config.yaml"
            json_path = root / "nested/config.json"
            write_yaml(yaml_path, {"id": "A", "name": "Test"})
            write_json(json_path, {"schema_version": 1})
            self.assertEqual(sorted(yaml_path.parent.glob("*.tmp")), [])
            self.assertEqual(read_yaml(yaml_path), {"id": "A", "name": "Test"})
            json_text = json_path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(json_text), {"schema_version": 1})
            self.assertTrue(json_text.endswith("\n"))
            with patch("probhub.io.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_yaml(yaml_path, {"id": "B"})
                with self.assertRaises(OSError):
                    write_json(json_path, {"schema_version": 2})
            self.assertEqual(read_yaml(yaml_path), {"id": "A", "name": "Test"})
            self.assertEqual(json_path.read_text(encoding="utf-8"), json_text)
            self.assertEqual(sorted(yaml_path.parent.glob("*.tmp")), [])

    def test_source_hash_tracks_problem_statement_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            assets = problem / "assets"
            assets.mkdir()
            image = assets / "diagram.png"
            image.write_bytes(b"first image")
            with (problem / "problem.md").open("a", encoding="utf-8") as stream:
                stream.write("\n![diagram](assets/diagram.png)\n")

            root, workspace = load_workspace(root)
            _, config = load_problem(root, problem_entries(workspace)[0])
            first_hash = compute_source_hash(problem, config)
            image.write_bytes(b"second image")
            self.assertNotEqual(first_hash, compute_source_hash(problem, config))

    def test_lint_validates_custom_checker_and_interactor_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            root, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            config_path = problem / "probhub.yaml"

            _, config = load_problem(root, entry)
            config["judge"] = {
                "type": "custom",
                "validator": "code/validator.cpp",
                "checker": "code/checker.cpp",
            }
            write_yaml(config_path, config)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn("checker not found: code/checker.cpp", result["problems"][0]["errors"])

            (problem / "code/checker.cpp").write_text("int main(){}\n", encoding="utf-8")
            self.assertTrue(lint_workspace(root, workspace)["ok"])

            config["judge"] = {
                "type": "interactive",
                "validator": "code/validator.cpp",
                "interactor": "code/interactor.cpp",
            }
            write_yaml(config_path, config)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn("interactor not found: code/interactor.cpp", result["problems"][0]["errors"])

            (problem / "code/interactor.cpp").write_text("int main(){}\n", encoding="utf-8")
            self.assertTrue(lint_workspace(root, workspace)["ok"])

            config["judge"]["interactive"] = {"idle_limit": 0, "transcript_limit": -1}
            write_yaml(config_path, config)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn(
                "judge.interactive.idle_limit must be positive",
                result["problems"][0]["errors"],
            )
            self.assertIn(
                "judge.interactive.transcript_limit must be a non-negative integer",
                result["problems"][0]["errors"],
            )

    def test_lint_validates_typst_cover_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            _, workspace = load_workspace(root)
            workspace["typst"] = {
                "directory": "typst/contest",
                "cover": {"logo": "../outside.png", "logo_width": "wide"},
            }
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn("typst.cover.logo must stay inside the Typst directory", result["errors"])
            self.assertIn("typst.cover.logo_width must be a Typst length", result["errors"])

    def test_lint_reports_positional_stress_args_template_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            (problem / "code/brute.cpp").write_text("int main(){}\n", encoding="utf-8")
            (problem / "code/gen.cpp").write_text("int main(){}\n", encoding="utf-8")
            config = read_yaml(problem / "probhub.yaml")
            config["solutions"]["brute"] = ["code/brute.cpp"]
            config["stress"] = {"generator": "code/gen.cpp", "args": ["{}"]}
            write_yaml(problem / "probhub.yaml", config)
            _, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn(
                "invalid stress.args template: {}",
                result["problems"][0]["errors"],
            )

    def test_stress_arg_expansion_rejects_positional_template(self):
        from probhub.stressing import expand_generator_args

        with self.assertRaises(ProbHubError) as raised:
            expand_generator_args(["{}"], 1, 1)
        self.assertIn("invalid stress.args template", str(raised.exception))

    def test_lint_rejects_boolean_time_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            config = read_yaml(problem / "probhub.yaml")
            config["limits"]["time"] = True
            write_yaml(problem / "probhub.yaml", config)
            _, workspace = load_workspace(root)
            result = lint_workspace(root, workspace)
            self.assertFalse(result["ok"])
            self.assertIn(
                "limits.time must be a positive integer",
                result["problems"][0]["errors"],
            )

    def test_non_utf8_sample_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            (problem / "data/sample/1.in").write_bytes(b"\xff\xfe\x00garbage")
            _, config = load_problem(root, {"id": "A", "directory": "A"})
            with self.assertRaises(ProbHubError) as raised:
                build_meta(problem, config)
            self.assertEqual(raised.exception.code, "sample_not_utf8")

    def test_doctor_reports_hung_tool_instead_of_crashing(self):
        from probhub.doctor import run_doctor

        with (
            patch("probhub.doctor.shutil.which", return_value="/fake/tool"),
            patch(
                "probhub.doctor.run_managed_to_files",
                return_value={
                    "reason": "time_limit",
                    "message": "time limit exceeded",
                    "returncode": -1,
                },
            ),
        ):
            report = run_doctor()
        self.assertFalse(report["ok"])
        self.assertEqual(report["tools"]["g++"]["version"], "timed out after 10s")

    def test_doctor_rejects_old_node_and_missing_pinned_cjk_font(self):
        from probhub.doctor import run_doctor

        def command_version(command, args=("--version",)):
            versions = {
                "node": "v16.20.2",
                "typst": "typst 0.14.2",
                "g++": "g++ 13.2.0",
                "npm": "10.9.0",
            }
            return {"ok": True, "path": f"/fake/{command}", "version": versions[command]}

        with (
            patch("probhub.doctor._command_version", side_effect=command_version),
            patch(
                "probhub.doctor._command_probe",
                return_value={
                    "ok": True,
                    "path": "/fake/typst",
                    "lines": ["DejaVu Sans Mono"],
                    "diagnostic": None,
                },
            ),
        ):
            report = run_doctor()
        self.assertFalse(report["ok"])
        self.assertFalse(report["tools"]["node"]["version_ok"])
        self.assertEqual(report["tools"]["node"]["requirement"], ">=18")
        self.assertFalse(report["tools"]["typst"]["font_ok"])
        self.assertEqual(report["tools"]["typst"]["requirement"], "0.14.2")

    def test_publish_output_validators_stages_before_removing_old(self):
        from probhub.building import _publish_output_validators

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            (source / "validate").mkdir(parents=True)
            (source / "validate/validate.cpp").write_text("new\n", encoding="utf-8")
            destination = root / "problem/output_validators"
            (destination / "validate").mkdir(parents=True)
            (destination / "validate/validate.cpp").write_text("old\n", encoding="utf-8")

            _publish_output_validators(source, destination)

            self.assertEqual(
                (destination / "validate/validate.cpp").read_text(encoding="utf-8"),
                "new\n",
            )
            leftovers = [
                path.name
                for path in destination.parent.iterdir()
                if path.name != destination.name
            ]
            self.assertEqual(leftovers, [])

            _publish_output_validators(root / "missing-source", destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
