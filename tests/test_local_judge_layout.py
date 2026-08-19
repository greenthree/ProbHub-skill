import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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
            identity = MODULE.compile_cpp(str(source), str(output), kind="std")
            self.assertTrue(identity)
            self.assertTrue(output.is_file())
            self.assertEqual(identity["path"], str(output.absolute()))
            self.assertEqual(identity["size"], output.stat().st_size)
            self.assertEqual(identity["digest"], MODULE._file_digest(output))
            self.assertEqual(list((code / ".probhub/compile").iterdir()), [])

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_cached_and_refresh_compile_return_the_same_binary_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "缓存 身份"
            code = problem / "code"
            code.mkdir(parents=True)
            source = code / "std.cpp"
            output = code / ("std.exe" if MODULE.platform.system() == "Windows" else "std")
            source.write_text("int main() { return 0; }\n", encoding="utf-8")

            cache = MODULE.SandboxCache(problem)
            first = MODULE.compile_cpp(
                str(source), str(output), kind="std", display_file="code/std.cpp", cache=cache
            )
            cached = MODULE.compile_cpp(
                str(source), str(output), kind="std", display_file="code/std.cpp", cache=cache
            )
            refresh_cache = MODULE.SandboxCache(problem, enabled=True, read_enabled=False)
            refreshed = MODULE.compile_cpp(
                str(source), str(output), kind="std", display_file="code/std.cpp", cache=refresh_cache
            )

            self.assertEqual(cached, first)
            self.assertEqual(refreshed, MODULE.binary_identity(output))
            self.assertEqual(cache.stats["compile_hits"], 1)

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_changed_cached_binary_is_recompiled_before_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            code = problem / "code"
            code.mkdir(parents=True)
            source = code / "std.cpp"
            output = code / ("std.exe" if MODULE.platform.system() == "Windows" else "std")
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            cache = MODULE.SandboxCache(problem)
            expected = MODULE.compile_cpp(
                str(source), str(output), kind="std", display_file="code/std.cpp", cache=cache
            )
            output.write_bytes(b"replaced binary")
            replaced_digest = MODULE.binary_identity(output)["digest"]

            actual = MODULE.compile_cpp(
                str(source), str(output), kind="std", display_file="code/std.cpp", cache=cache
            )

            self.assertEqual(actual, MODULE.binary_identity(output))
            self.assertNotEqual(actual["digest"], replaced_digest)
            self.assertEqual(cache.stats["compile_hits"], 0)
            self.assertEqual(cache.stats["compile_misses"], 2)

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
            self.assertTrue(all(results))
            self.assertTrue(all(isinstance(result, dict) for result in results))
            self.assertTrue(all(output.is_file() for output in outputs))
            self.assertEqual(list((code / ".probhub/compile").iterdir()), [])

            previous = outputs[0].read_bytes()
            source.write_text("int main( {\n", encoding="utf-8")
            self.assertFalse(MODULE.compile_cpp(str(source), str(outputs[0]), kind="std"))
            self.assertEqual(outputs[0].read_bytes(), previous)

    def test_publish_identity_mismatch_restores_previous_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged = root / "staged.exe"
            output = root / "program.exe"
            staged.write_bytes(b"new binary")
            output.write_bytes(b"last known good")
            staged_identity = MODULE.binary_identity(staged)
            mismatched = {
                "path": str(output.absolute()),
                "size": len(b"tampered"),
                "digest": "0" * 64,
            }

            with patch.object(MODULE, "binary_identity", return_value=mismatched):
                with self.assertRaises(MODULE.BinaryChangedError):
                    MODULE.publish_compiled_binary(
                        str(staged), str(output), staged_identity
                    )

            self.assertEqual(output.read_bytes(), b"last known good")

    def test_publish_failure_preserves_previous_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged = root / "staged.exe"
            output = root / "program.exe"
            staged.write_bytes(b"new binary")
            output.write_bytes(b"locked binary")
            staged_identity = MODULE.binary_identity(staged)
            real_replace = os.replace

            def deny_publish(source, destination):
                if Path(destination) == output:
                    raise PermissionError("injected file-in-use failure")
                return real_replace(source, destination)

            with patch.object(MODULE.os, "replace", side_effect=deny_publish):
                with self.assertRaises(PermissionError):
                    MODULE.publish_compiled_binary(
                        str(staged), str(output), staged_identity
                    )

            self.assertEqual(output.read_bytes(), b"locked binary")

    def test_binary_identity_fence_reports_each_judge_role_as_infrastructure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for role in ("validator", "checker", "interactor", "std", "brute", "wrong"):
                with self.subTest(role=role):
                    binary = root / f"{role}.exe"
                    binary.write_bytes(b"expected")
                    identity = MODULE.binary_identity(binary)
                    binary.write_bytes(b"changed!")
                    failure = MODULE.verify_binary_identities([{
                        "role": role,
                        "program": f"code/{role}.cpp",
                        "identity": identity,
                    }])
                    self.assertEqual(failure["code"], "binary_changed")
                    self.assertEqual(failure["failure_kind"], "infrastructure")
                    self.assertEqual(failure["role"], role)
                    self.assertIsNotNone(failure["actual_digest"])

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
                "schema_version": 1,
                "id": "A",
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

    def test_schema_execution_paths_use_the_shared_problem_fence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = root / "A"
            code = problem / "code"
            code.mkdir(parents=True)
            outside = root / "outside.cpp"
            outside.write_text("int main(){}\n", encoding="utf-8")
            (code / "checker.cpp").write_text("int main(){}\n", encoding="utf-8")

            self.assertIsNone(MODULE.resolve_problem_path(problem, "../outside.cpp"))
            self.assertIsNone(MODULE.resolve_problem_path(problem, str(outside)))
            self.assertEqual(
                Path(MODULE.resolve_problem_path(problem, "code/checker.cpp")),
                (code / "checker.cpp").absolute(),
            )

            link = code / "linked-checker.cpp"
            try:
                link.symlink_to(outside)
            except OSError:
                return
            self.assertIsNone(MODULE.resolve_problem_path(problem, "code/linked-checker.cpp"))


    def test_legacy_workspace_is_rejected_by_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            code = problem / "code"
            code.mkdir(parents=True)
            (code / "std.cpp").write_text("int main(){}\n", encoding="utf-8")
            run = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "local_judge.py"), str(problem), "--jsonl"],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(run.returncode, 0)
            events = [json.loads(line) for line in run.stdout.splitlines() if line.strip().startswith("{")]
            final = next(event for event in events if event.get("type") == "final")
            self.assertEqual(final["code"], "migration_required")

    def test_unsupported_problem_schema_is_rejected_by_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir(parents=True)
            write_yaml(problem / "probhub.yaml", {
                "schema_version": 2,
                "id": "A",
            })
            run = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "local_judge.py"), str(problem), "--jsonl"],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(run.returncode, 0)
            events = [json.loads(line) for line in run.stdout.splitlines() if line.strip().startswith("{")]
            final = next(event for event in events if event.get("type") == "final")
            self.assertEqual(final["code"], "unsupported_schema")

    def test_failed_status_requires_peak_memory_evidence_for_mle(self):
        self.assertEqual(MODULE._failed_status(-11, b"", True, 10.0, 256), "RE")
        self.assertEqual(MODULE._failed_status(1, b"", True, None, 256), "RE")
        self.assertEqual(MODULE._failed_status(-9, b"", True, 255.0, 256), "MLE")
        self.assertEqual(MODULE._failed_status(-11, b"", False, 300.0, 256), "RE")


if __name__ == "__main__":
    unittest.main()
