import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from probhub.package_tools import (
    build_verified_package,
    generate_domjudge_config,
    validate_output_validator_source,
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
            self.assertEqual(result["stats"]["sample_cases"], 1)
            self.assertEqual(result["stats"]["secret_cases"], 1)

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
                    config = {
                        "name": judge_type,
                        "limits": {"time": 1, "memory": 256},
                        "judge": {
                            "type": judge_type,
                            source_key: f"code/{source_key}.cpp",
                        },
                    }
                    generate_domjudge_config(problem, config)
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


if __name__ == "__main__":
    unittest.main()
