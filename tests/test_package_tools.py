import importlib.util
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from probhub.errors import ProbHubError
from probhub.package_tools import (
    build_package,
    build_verified_package,
    generate_domjudge_config,
    validate_output_validator_source,
    verify_package,
)

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = load("verify_package")
PACKAGE = load("package_problem")


class PackageToolsTests(unittest.TestCase):
    def create_problem(self, root):
        (root / "data" / "sample").mkdir(parents=True)
        (root / "data" / "secret").mkdir(parents=True)
        (root / "data" / "sample" / "1.in").write_text("1\n", encoding="utf-8")
        (root / "data" / "sample" / "1.ans").write_text("1\n", encoding="utf-8")
        (root / "data" / "secret" / "1.in").write_text("2\n", encoding="utf-8")
        (root / "data" / "secret" / "1.ans").write_text("2\n", encoding="utf-8")
        (root / "problem.yaml").write_text("name: 'test'\nlimits:\n  memory: 256\n", encoding="utf-8")
        (root / "domjudge-problem.ini").write_text("timelimit='1'\n", encoding="utf-8")
        (root / "problem.pdf").write_bytes(b"%PDF-1.4\n")

    def standard_config(self, validator="code/validator.py"):
        return {
            "id": "A",
            "name": "test",
            "display_name": "test",
            "limits": {"time": 1, "memory": 256, "output": 64, "processes": 8},
            "judge": {"type": "standard", "validator": validator},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        }

    def valid_entries(self):
        return {
            "problem.yaml": b"name: test\nlimits:\n  memory: 256\n",
            "domjudge-problem.ini": b"timelimit='1'\n",
            "problem.pdf": b"%PDF-1.4\n",
            "data/sample/1.in": b"1\n",
            "data/sample/1.ans": b"1\n",
            "data/secret/1.in": b"2\n",
            "data/secret/1.ans": b"2\n",
        }

    def write_zip(self, path, entries, modes=None):
        modes = modes or {}
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            for name, payload in entries.items():
                info = ZipInfo(name)
                info.compress_type = ZIP_DEFLATED
                info.external_attr = modes.get(name, 0o100644) << 16
                archive.writestr(info, payload)

    def diagnostic_codes(self, result):
        return {item["code"] for item in result.get("diagnostics", [])}

    def test_deterministic_package_and_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            first = temp / "first.zip"
            second = temp / "second.zip"
            PACKAGE.build_package(problem, first)
            PACKAGE.build_package(problem, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            result = VERIFY.verify_package(first, require_pdf=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["verification_scope"], "structural")
            self.assertEqual(result["stats"]["sample_cases"], 1)
            self.assertEqual(result["stats"]["secret_cases"], 1)

    def test_package_normalizes_testcase_newlines_without_mutating_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            payloads = {
                "data/sample/1.in": b"a\r\nb\rc\n",
                "data/sample/1.ans": b"x\r\ny\r",
                "data/secret/1.in": b"1\r\n2\r\n",
                "data/secret/1.ans": b"3\r4\n",
            }
            for relative, payload in payloads.items():
                (problem / relative).write_bytes(payload)

            first = temp / "first.zip"
            build_package(problem, first)
            for relative, payload in payloads.items():
                self.assertEqual((problem / relative).read_bytes(), payload)
            with ZipFile(first) as archive:
                for relative, payload in payloads.items():
                    self.assertEqual(
                        archive.read(relative),
                        payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n"),
                    )

            for relative, payload in payloads.items():
                (problem / relative).write_bytes(
                    payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                )
            second = temp / "second.zip"
            build_package(problem, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_verify_rejects_crlf_bare_cr_and_mixed_testcase_text(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            for style, payload in (
                ("crlf", b"1\r\n"),
                ("bare_cr", b"1\r2\n"),
                ("mixed", b"1\r\n2\r3\n"),
            ):
                with self.subTest(style=style):
                    entries = self.valid_entries()
                    entries["data/sample/1.in"] = payload
                    package = temp / f"{style}.zip"
                    self.write_zip(package, entries)
                    result = verify_package(package, require_pdf=True)
                    self.assertFalse(result["ok"], result)
                    diagnostic = next(
                        item
                        for item in result["diagnostics"]
                        if item["code"] == "package_data_not_lf"
                    )
                    self.assertEqual(diagnostic["entry"], "data/sample/1.in")
                    self.assertEqual(diagnostic["newline_style"], style)

    def test_verify_rejects_case_collisions_executables_caches_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            entries = self.valid_entries()
            entries["Problem.yaml"] = b"name: collision\nlimits:\n  memory: 256\n"
            entries["output_validators/validate/stale.exe"] = b"MZ"
            entries["output_validators/validate/__pycache__/old.pyc"] = b"cache"
            entries["output_validators/validate/link.hpp"] = b"target"
            package = temp / "unsafe.zip"
            self.write_zip(
                package,
                entries,
                modes={"output_validators/validate/link.hpp": 0o120777},
            )

            result = verify_package(package, require_pdf=True)
            codes = self.diagnostic_codes(result)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["verification_scope"], "structural")
            self.assertIn("package_case_collision", codes)
            self.assertIn("package_executable_entry", codes)
            self.assertIn("package_cache_entry", codes)
            self.assertIn("package_symlink_entry", codes)

    def test_verify_rejects_encrypted_and_unsupported_compression_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            for label, offset, value, expected_code in (
                ("encrypted", 8, 1, "package_encrypted_entry"),
                ("compression", 10, 99, "package_unsupported_compression"),
            ):
                with self.subTest(label=label):
                    package = temp / f"{label}.zip"
                    self.write_zip(package, self.valid_entries())
                    payload = bytearray(package.read_bytes())
                    central = payload.find(b"PK\x01\x02")
                    self.assertGreaterEqual(central, 0)
                    struct.pack_into("<H", payload, central + offset, value)
                    package.write_bytes(payload)
                    result = verify_package(package, require_pdf=True)
                    self.assertFalse(result["ok"], result)
                    self.assertIn(expected_code, self.diagnostic_codes(result))

    def test_verify_rejects_crc_corruption_and_cleans_extraction_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "corrupt.zip"
            with ZipFile(package, "w", compression=ZIP_STORED) as archive:
                for name, content in self.valid_entries().items():
                    info = ZipInfo(name)
                    info.compress_type = ZIP_STORED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, content)
            with ZipFile(package) as archive:
                info = archive.getinfo("data/sample/1.in")
            payload = bytearray(package.read_bytes())
            name_length, extra_length = struct.unpack_from(
                "<HH", payload, info.header_offset + 26
            )
            data_offset = info.header_offset + 30 + name_length + extra_length
            payload[data_offset] ^= 0x01
            package.write_bytes(payload)

            created = []
            real_temporary_directory = tempfile.TemporaryDirectory

            def tracked_temporary_directory(*args, **kwargs):
                context = real_temporary_directory(*args, **kwargs)
                created.append(Path(context.name))
                return context

            with mock.patch(
                "probhub.package_tools.tempfile.TemporaryDirectory",
                side_effect=tracked_temporary_directory,
            ):
                result = verify_package(package, require_pdf=True)
            self.assertFalse(result["ok"], result)
            self.assertIn("package_extract_failed", self.diagnostic_codes(result))
            self.assertTrue(created)
            self.assertTrue(all(not path.exists() for path in created))

    def test_verify_enforces_entry_and_total_uncompressed_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            package = temp / "large.zip"
            entries = self.valid_entries()
            entries["data/secret/1.in"] = b"123456789\n"
            self.write_zip(package, entries)

            entry_result = verify_package(
                package,
                require_pdf=True,
                max_entry_bytes=5,
            )
            self.assertIn("package_entry_too_large", self.diagnostic_codes(entry_result))

            total_result = verify_package(
                package,
                require_pdf=True,
                max_entry_bytes=1024,
                max_total_bytes=10,
            )
            self.assertIn("package_total_too_large", self.diagnostic_codes(total_result))

    def test_verify_rejects_archive_size_before_opening_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "oversized.zip"
            package.write_bytes(b"not-a-zip")
            with mock.patch("probhub.package_tools.ZipFile") as zip_file:
                result = verify_package(package, max_archive_bytes=4)
            self.assertIn("package_archive_too_large", self.diagnostic_codes(result))
            zip_file.assert_not_called()

    def test_verify_rejects_declared_entry_count_before_opening_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "too-many.zip"
            package.write_bytes(
                b"PK\x05\x06"
                + struct.pack("<4H2LH", 0, 0, 2, 2, 0, 0, 0)
            )
            with mock.patch("probhub.package_tools.ZipFile") as zip_file:
                result = verify_package(package, max_entries=1)
            self.assertIn("package_too_many_entries", self.diagnostic_codes(result))
            zip_file.assert_not_called()

    def test_verify_rejects_underreported_entry_count_before_opening_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "underreported.zip"
            self.write_zip(package, self.valid_entries())
            payload = bytearray(package.read_bytes())
            eocd = payload.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(eocd, 0)
            struct.pack_into("<HH", payload, eocd + 8, 1, 1)
            package.write_bytes(payload)
            with mock.patch("probhub.package_tools.ZipFile") as zip_file:
                result = verify_package(package, max_entries=2)
            self.assertIn("package_too_many_entries", self.diagnostic_codes(result))
            zip_file.assert_not_called()

    def test_verify_rejects_noncanonical_windows_paths_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            entries = self.valid_entries()
            entries["data/sample/CON.in"] = b"1\n"
            entries["data/secret/case.in:stream"] = b"2\n"
            entries['data/secret/a?.in'] = b"2\n"
            entries['data/secret/a*.ans'] = b"2\n"
            entries['data/secret/a|b.in'] = b"2\n"
            unsafe = temp / "unsafe-paths.zip"
            self.write_zip(unsafe, entries)
            unsafe_result = verify_package(unsafe, require_pdf=True)
            self.assertIn("package_unsafe_path", self.diagnostic_codes(unsafe_result))

            entries = self.valid_entries()
            entries["problem.yaml"] = (
                b"name: test\nname: duplicate\nlimits:\n  memory: 256\n"
            )
            duplicate_yaml = temp / "duplicate-yaml.zip"
            self.write_zip(duplicate_yaml, entries)
            duplicate_result = verify_package(duplicate_yaml, require_pdf=True)
            self.assertIn(
                "package_invalid_problem_yaml",
                self.diagnostic_codes(duplicate_result),
            )

            entries = self.valid_entries()
            entries["problem.yaml"] = (
                b"name: test\nlimits:\n  memory: nope\nvalidation: mystery\n"
            )
            entries["domjudge-problem.ini"] = b"timelimit='-1'\n"
            invalid_config = temp / "invalid-config.zip"
            self.write_zip(invalid_config, entries)
            invalid_result = verify_package(invalid_config, require_pdf=True)
            invalid_codes = self.diagnostic_codes(invalid_result)
            self.assertIn("package_invalid_memory_limit", invalid_codes)
            self.assertIn("package_invalid_judge_type", invalid_codes)
            self.assertIn("package_invalid_time_limit", invalid_codes)

            entries = self.valid_entries()
            entries["domjudge-problem.ini"] = (
                b"timelimit='1'\ntimelimit='999'\nunknown='value'\n"
            )
            duplicate_ini = temp / "duplicate-ini.zip"
            self.write_zip(duplicate_ini, entries)
            duplicate_result = verify_package(duplicate_ini, require_pdf=True)
            duplicate_codes = self.diagnostic_codes(duplicate_result)
            self.assertIn("package_duplicate_domjudge_ini_key", duplicate_codes)
            self.assertIn("package_unknown_domjudge_ini_key", duplicate_codes)

            entries = self.valid_entries()
            entries["domjudge-problem.ini"] = b"TIMELIMIT='1'\n"
            uppercase_ini = temp / "uppercase-ini.zip"
            self.write_zip(uppercase_ini, entries)
            uppercase_result = verify_package(uppercase_ini, require_pdf=True)
            self.assertIn(
                "package_unknown_domjudge_ini_key",
                self.diagnostic_codes(uppercase_result),
            )

    def test_verify_rejects_nonlowercase_testcase_extensions(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "uppercase-data.zip"
            entries = self.valid_entries()
            entries["data/secret/unchecked.IN"] = b"3\n"
            entries["data/secret/unchecked.ANS"] = b"3\n"
            self.write_zip(package, entries)
            result = verify_package(package, require_pdf=True)
            self.assertFalse(result["ok"], result)
            self.assertIn(
                "package_noncanonical_data_extension",
                self.diagnostic_codes(result),
            )

    def test_deep_verify_compares_config_data_and_runs_input_validator(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            code = problem / "code"
            code.mkdir()
            validator = code / "validator.py"
            validator.write_text(
                "import sys\n"
                "payload = sys.stdin.buffer.read()\n"
                "raise SystemExit(0 if payload == b'1\\n' else 1)\n",
                encoding="utf-8",
            )
            config = self.standard_config()
            package = temp / "A.zip"
            build_package(problem, package, config=config)

            result = verify_package(
                package,
                require_pdf=True,
                expected_config=config,
                source_problem_dir=problem,
                run_validator=True,
            )
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["verification_scope"], "deep")
            self.assertTrue(result["checks"]["source_consistency"])
            self.assertTrue(result["checks"]["input_validator"])
            rejected = [
                item
                for item in result["diagnostics"]
                if item["code"] == "package_validator_rejected_input"
            ]
            self.assertEqual([item["entry"] for item in rejected], ["data/secret/1.in"])
            self.assertEqual(result["stats"]["validator_cases"], 2)

            config["name"] = "different"
            config["limits"]["time"] = 2
            config["limits"]["memory"] = 512
            config["judge"]["type"] = "custom"
            mismatch = verify_package(
                package,
                require_pdf=True,
                expected_config=config,
                source_problem_dir=problem,
                run_validator=False,
            )
            mismatch_codes = self.diagnostic_codes(mismatch)
            self.assertIn("package_name_mismatch", mismatch_codes)
            self.assertIn("package_time_mismatch", mismatch_codes)
            self.assertIn("package_memory_mismatch", mismatch_codes)
            self.assertIn("package_judge_type_mismatch", mismatch_codes)

            config["name"] = "test"
            config["limits"]["time"] = 1
            config["limits"]["memory"] = 256
            config["judge"]["type"] = "standard"
            (problem / "data/sample/1.ans").write_bytes(b"changed\n")
            changed = verify_package(
                package,
                require_pdf=True,
                expected_config=config,
                source_problem_dir=problem,
                run_validator=False,
            )
            self.assertIn("package_data_content_mismatch", self.diagnostic_codes(changed))

    def test_custom_data_directories_map_to_domjudge_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            (problem / "fixtures/samples").mkdir(parents=True)
            (problem / "fixtures/secrets").mkdir(parents=True)
            (problem / "fixtures/samples/example.in").write_bytes(b"1\r\n")
            (problem / "fixtures/samples/example.ans").write_bytes(b"1\r\n")
            (problem / "fixtures/secrets/main.in").write_bytes(b"2\r\n")
            (problem / "fixtures/secrets/main.ans").write_bytes(b"2\r\n")
            config = self.standard_config()
            config["data"] = {
                "sample_dir": "fixtures/samples",
                "secret_dir": "fixtures/secrets",
            }
            package = temp / "A.zip"
            build_package(problem, package, config=config)

            with ZipFile(package) as archive:
                names = set(archive.namelist())
                self.assertIn("data/sample/example.in", names)
                self.assertIn("data/secret/main.in", names)
                self.assertNotIn("data/sample/1.in", names)
                self.assertEqual(archive.read("data/sample/example.in"), b"1\n")
            result = verify_package(
                package,
                require_pdf=True,
                expected_config=config,
                source_problem_dir=problem,
                run_validator=False,
            )
            self.assertTrue(result["ok"], result)

    def test_custom_data_directories_cannot_escape_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            config = self.standard_config()
            config["data"]["sample_dir"] = "../outside"
            with self.assertRaises(ProbHubError) as raised:
                build_package(problem, temp / "A.zip", config=config)
            self.assertEqual(raised.exception.code, "unsafe_package_source")

            package = temp / "existing.zip"
            self.write_zip(package, self.valid_entries())
            result = verify_package(
                package,
                require_pdf=True,
                expected_config=config,
                source_problem_dir=problem,
                run_validator=False,
            )
            self.assertFalse(result["ok"], result)
            self.assertIn("unsafe_package_source", self.diagnostic_codes(result))

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliable on Windows CI")
    def test_deep_verify_rejects_leaf_data_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            outside = temp / "outside.in"
            outside.write_bytes(b"outside\n")
            sample = problem / "data/sample/1.in"
            sample.unlink()
            sample.symlink_to(outside)
            config = self.standard_config()
            package = temp / "existing.zip"
            self.write_zip(package, self.valid_entries())

            result = verify_package(
                package,
                require_pdf=True,
                expected_config=config,
                source_problem_dir=problem,
                run_validator=False,
            )
            self.assertFalse(result["ok"], result)
            self.assertIn("unsafe_package_source", self.diagnostic_codes(result))

    def test_custom_and_interactive_packages_include_managed_validator_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            for judge_type, source_key, validation in (
                ("custom", "checker", "custom"),
                ("interactive", "interactor", "custom interactive"),
            ):
                with self.subTest(judge_type=judge_type):
                    problem = temp / judge_type
                    self.create_problem(problem)
                    code = problem / "code"
                    code.mkdir()
                    source = code / f"{source_key}.cpp"
                    source.write_text('#include "testlib.h"\nint main(){}\n', encoding="utf-8")
                    stale = problem / "output_validators/validate/stale.exe"
                    stale.parent.mkdir(parents=True)
                    stale.write_bytes(b"old executable")
                    config = {
                        "name": judge_type,
                        "limits": {"time": 1, "memory": 256},
                        "judge": {
                            "type": judge_type,
                            source_key: f"code/{source_key}.cpp",
                        },
                    }
                    generate_domjudge_config(problem, config)
                    self.assertFalse(stale.exists())
                    self.assertIsNotNone(validate_output_validator_source(problem, config))
                    output = temp / f"{judge_type}.zip"
                    PACKAGE.build_package(problem, output)
                    result = VERIFY.verify_package(output, require_pdf=True)
                    self.assertTrue(result["ok"], result)
                    with ZipFile(output) as archive:
                        names = set(archive.namelist())
                        problem_yaml = archive.read("problem.yaml").decode("utf-8")
                    self.assertIn(f"validation: {validation}", problem_yaml)
                    self.assertIn("output_validators/validate/validate.cpp", names)
                    self.assertIn("output_validators/validate/testlib.h", names)

    def test_verify_rejects_packaged_output_validator_compile_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "invalid-checker.zip"
            entries = self.valid_entries()
            entries["problem.yaml"] = (
                b"name: test\nlimits:\n  memory: 256\nvalidation: custom\n"
            )
            entries["output_validators/validate/validate.cpp"] = b"not c++\n"
            entries["output_validators/validate/testlib.h"] = b"header\n"
            self.write_zip(package, entries)

            def fake_run(command, **kwargs):
                kwargs["stdout_path"].write_bytes(b"")
                kwargs["stderr_path"].write_text("compile failed", encoding="utf-8")
                return {"reason": "completed", "returncode": 1, "message": ""}

            with mock.patch(
                "probhub.package_tools.run_managed_to_files",
                side_effect=fake_run,
            ):
                result = verify_package(package, require_pdf=True)
            self.assertFalse(result["ok"], result)
            self.assertIn(
                "package_output_validator_compile_failed",
                self.diagnostic_codes(result),
            )
            self.assertFalse(result["stats"]["output_validator_compiled"])

    def create_checker_problem(self, root):
        self.create_problem(root)
        code = root / "code"
        code.mkdir()
        (code / "checker.cpp").write_text('#include "testlib.h"\nint main(){}\n', encoding="utf-8")
        return {
            "name": "checker",
            "limits": {"time": 1, "memory": 256},
            "judge": {"type": "checker", "checker": "code/checker.cpp"},
        }

    def test_output_validator_rejects_unsafe_checker_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            config = self.create_checker_problem(problem)
            for source in (None, "", "   "):
                with self.subTest(source=source):
                    config["judge"]["checker"] = source
                    with self.assertRaises(ProbHubError) as raised:
                        validate_output_validator_source(problem, config)
                    self.assertEqual(raised.exception.code, "unsafe_package_source")
                    self.assertEqual(str(raised.exception), "judge.checker is required")

            for source in ("../outside.cpp", r"..\outside.cpp", r"C:relative.cpp"):
                with self.subTest(source=source):
                    config["judge"]["checker"] = source
                    with self.assertRaises(ProbHubError) as raised:
                        validate_output_validator_source(problem, config)
                    self.assertEqual(raised.exception.code, "unsafe_package_source")
                    self.assertIn(
                        "judge.checker must stay inside the problem directory",
                        str(raised.exception),
                    )

            checker_link = problem / "code/checker-link.cpp"
            try:
                checker_link.symlink_to(problem / "code/checker.cpp")
            except (NotImplementedError, OSError):
                return
            config["judge"]["checker"] = "code/checker-link.cpp"
            with self.assertRaises(ProbHubError) as raised:
                validate_output_validator_source(problem, config)
            self.assertEqual(raised.exception.code, "unsafe_package_source")
            self.assertEqual(
                str(raised.exception),
                "judge.checker must be a regular file: code/checker-link.cpp",
            )

    def test_output_validator_compile_uses_managed_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            config = self.create_checker_problem(problem)
            calls = []

            def fake_run(command, **kwargs):
                calls.append(kwargs)
                kwargs["stdout_path"].write_bytes(b"")
                kwargs["stderr_path"].write_bytes(b"")
                return {"reason": "completed", "returncode": 0, "message": ""}

            with mock.patch("probhub.package_tools.run_managed_to_files", side_effect=fake_run):
                validate_dir = validate_output_validator_source(problem, config)

            self.assertEqual(validate_dir, problem / "output_validators" / "validate")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["timeout"], 90.0)
            self.assertEqual(calls[0]["memory_limit_mb"], 2048)
            self.assertEqual(calls[0]["output_limit_bytes"], 8 * 1024 * 1024)

    def test_output_validator_compile_timeout_raises_probhub_error(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            config = self.create_checker_problem(problem)

            def fake_run(command, **kwargs):
                kwargs["stdout_path"].write_bytes(b"")
                kwargs["stderr_path"].write_bytes(b"")
                return {"reason": "time_limit", "returncode": None, "message": "time limit exceeded"}

            with mock.patch("probhub.package_tools.run_managed_to_files", side_effect=fake_run):
                with self.assertRaisesRegex(
                    ProbHubError, "output validator failed to compile: time limit exceeded"
                ):
                    validate_output_validator_source(problem, config)

    def test_output_validator_compile_error_reports_stderr(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            config = self.create_checker_problem(problem)

            def fake_run(command, **kwargs):
                kwargs["stdout_path"].write_bytes(b"")
                kwargs["stderr_path"].write_text("error: expected ';'", encoding="utf-8")
                return {"reason": "completed", "returncode": 1, "message": ""}

            with mock.patch("probhub.package_tools.run_managed_to_files", side_effect=fake_run):
                with self.assertRaisesRegex(ProbHubError, "error: expected ';'"):
                    validate_output_validator_source(problem, config)

    def test_output_validator_gpp_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            config = self.create_checker_problem(problem)

            with mock.patch(
                "probhub.package_tools.run_managed_to_files",
                side_effect=OSError("no g++"),
            ):
                with self.assertRaisesRegex(ProbHubError, "failed to run g\\+\\+ for output validator"):
                    validate_output_validator_source(problem, config)

    def test_invalid_staged_package_does_not_replace_existing_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            problem = temp / "A"
            self.create_problem(problem)
            output = temp / "A.zip"
            output.write_bytes(b"last known good package")
            invalid = {
                "ok": False,
                "errors": ["invalid fixture"],
                "warnings": [],
                "stats": {},
            }

            with mock.patch(
                "probhub.package_tools.verify_package",
                return_value=invalid,
            ):
                _, verification = build_verified_package(
                    problem,
                    output,
                    require_pdf=True,
                )

            self.assertEqual(verification, invalid)
            self.assertEqual(output.read_bytes(), b"last known good package")
            self.assertEqual(list(temp.glob(".A-package-*")), [])


if __name__ == "__main__":
    unittest.main()
