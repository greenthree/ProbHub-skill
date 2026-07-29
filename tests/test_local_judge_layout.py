import importlib.util
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from probhub.io import write_yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_judge", ROOT / "scripts" / "local_judge.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalJudgeLayoutTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_compile_cpp_supports_space_and_unicode_problem_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            code = Path(temp) / "含 空格的题目" / "code"
            code.mkdir(parents=True)
            source = code / "std.cpp"
            output = code / ("std.exe" if MODULE.platform.system() == "Windows" else "std")
            source.write_text(
                '#include "testlib.h"\nint main() { return 0; }\n',
                encoding="utf-8",
            )
            self.assertTrue(MODULE.compile_cpp(str(source), str(output), kind="std"))
            self.assertTrue(output.is_file())
            self.assertEqual(list((code / ".probhub/compile").iterdir()), [])

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_compile_cpp_isolates_concurrent_runs_and_preserves_last_binary_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            code = Path(temp) / "并发 编译" / "code"
            code.mkdir(parents=True)
            source = code / "std.cpp"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            suffix = ".exe" if MODULE.platform.system() == "Windows" else ""
            outputs = [code / f"std-{index}{suffix}" for index in range(4)]
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(
                    lambda output: MODULE.compile_cpp(str(source), str(output), kind="std"),
                    outputs,
                ))
            self.assertEqual(results, [True] * len(outputs))
            self.assertTrue(all(output.is_file() for output in outputs))
            self.assertEqual(list((code / ".probhub/compile").iterdir()), [])

            previous = outputs[0].read_bytes()
            source.write_text("int main( {\n", encoding="utf-8")
            self.assertFalse(MODULE.compile_cpp(str(source), str(outputs[0]), kind="std"))
            self.assertEqual(outputs[0].read_bytes(), previous)

    def test_schema_paths_resolve_inside_code_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            (problem / "code").mkdir(parents=True)
            (problem / "data/sample").mkdir(parents=True)
            (problem / "data/secret").mkdir(parents=True)
            for name in ("validator.cpp", "std.cpp", "brute.cpp", "wrong.cpp"):
                (problem / "code" / name).write_text("int main(){}\n", encoding="utf-8")
            (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
            (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
            write_yaml(problem / "probhub.yaml", {
                "limits": {"time": 2, "memory": 512},
                "judge": {"validator": "code/validator.cpp"},
                "solutions": {
                    "accepted": [{"file": "code/std.cpp"}],
                    "brute": ["code/brute.cpp"],
                    "wrong": "code/wrong.cpp",
                },
                "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
            })

            config = MODULE.read_probhub_config(problem)
            sources = MODULE.discover_solution_sources(problem, config)
            self.assertEqual(MODULE.read_problem_limits(problem, config), (2.0, 512))
            self.assertEqual(
                {kind: [Path(path).relative_to(problem).as_posix() for path in paths] for kind, paths in sources.items()},
                {
                    "std": ["code/std.cpp"],
                    "brute": ["code/brute.cpp"],
                    "wrong": ["code/wrong.cpp"],
                },
            )
            self.assertEqual(MODULE.binary_path_for_source(sources["std"][0]), str(problem / "code/std.exe"))
            cases = MODULE.collect_testcases(problem, config)
            self.assertEqual([case["case"] for case in cases], ["sample/1"])


    def test_legacy_workspace_prefers_code_directory_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            code = problem / "code"
            code.mkdir(parents=True)
            for name in ("validator.cpp", "std.cpp", "brute.cpp", "wrong_case.cpp"):
                (code / name).write_text("int main(){}\n", encoding="utf-8")

            sources = MODULE.discover_solution_sources(problem)
            self.assertEqual(
                {kind: [Path(path).relative_to(problem).as_posix() for path in paths] for kind, paths in sources.items()},
                {
                    "std": ["code/std.cpp"],
                    "brute": ["code/brute.cpp"],
                    "wrong": ["code/wrong_case.cpp"],
                },
            )
            self.assertEqual(
                Path(MODULE.discover_validator_source(problem)).relative_to(problem).as_posix(),
                "code/validator.cpp",
            )

    def test_failed_status_requires_peak_memory_evidence_for_mle(self):
        self.assertEqual(MODULE._failed_status(-11, b"", True, 10.0, 256), "RE")
        self.assertEqual(MODULE._failed_status(1, b"", True, None, 256), "RE")
        self.assertEqual(MODULE._failed_status(-9, b"", True, 255.0, 256), "MLE")
        self.assertEqual(MODULE._failed_status(-11, b"", False, 300.0, 256), "RE")


if __name__ == "__main__":
    unittest.main()
