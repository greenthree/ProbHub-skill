import importlib.util
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
