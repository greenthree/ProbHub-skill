import io
import json
import shutil
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from probhub.cli import main as cli_main
from probhub.calibration import SANDBOX_CACHE_SCHEMA_VERSION
from probhub.io import read_yaml, write_yaml
from probhub.judging import check_sample_answers, judge_problem


def _source(body):
    return textwrap.dedent(body).lstrip()


def _write_problem(
    root,
    *,
    judge_type="standard",
    accepted_source=None,
    answer=b"2\n",
    checker_source=None,
    second_accepted_source=None,
    with_sample=True,
):
    problem = Path(root) / "A"
    code = problem / "code"
    code.mkdir(parents=True)
    (problem / "problem.md").write_text(
        "# A\n\n## 题目描述\nA\n\n## 输入格式\nA\n\n## 输出格式\nA\n",
        encoding="utf-8",
    )
    (code / "validator.cpp").write_text(
        "int main(){return 0;}\n", encoding="utf-8"
    )
    (code / "std.cpp").write_text(
        accepted_source
        or _source(
            r"""
            #include <iostream>
            int main(){ long long x; std::cin >> x; std::cout << x << '\n'; }
            """
        ),
        encoding="utf-8",
    )
    accepted = ["code/std.cpp"]
    if second_accepted_source is not None:
        (code / "std2.cpp").write_text(
            second_accepted_source, encoding="utf-8"
        )
        accepted.append("code/std2.cpp")
    judge = {"type": judge_type, "validator": "code/validator.cpp"}
    if judge_type == "custom":
        (code / "checker.cpp").write_text(
            checker_source or "int main(){return 42;}\n", encoding="utf-8"
        )
        judge["checker"] = "code/checker.cpp"
    elif judge_type == "interactive":
        judge["interactor"] = "code/interactor.cpp"
    if with_sample:
        sample = problem / "data/sample"
        sample.mkdir(parents=True)
        (sample / "1.in").write_bytes(b"2\n")
        (sample / "1.ans").write_bytes(answer)
    config = {
        "schema_version": 1,
        "id": "A",
        "name": "A",
        "limits": {"time": 2, "memory": 256, "output": 1, "processes": 8},
        "statement": {"source": "problem.md"},
        "judge": judge,
        "solutions": {"accepted": accepted, "brute": [], "wrong": []},
        "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
    }
    write_yaml(problem / "probhub.yaml", config)
    return problem


@unittest.skipUnless(shutil.which("g++"), "g++ is required for sample-check integration tests")
class SampleCheckIntegrationTests(unittest.TestCase):
    def test_sample_check_rejects_accepted_paths_outside_the_problem_before_compile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(root)
            outside = root / "outside.cpp"
            outside.write_text("int main(){return 0;}\n", encoding="utf-8")
            config_path = problem / "probhub.yaml"
            config = read_yaml(config_path)

            for program in ("../outside.cpp", outside.as_posix(), "C:/outside/std.cpp"):
                with self.subTest(program=program):
                    config["solutions"]["accepted"] = [program]
                    write_yaml(config_path, config)
                    result = check_sample_answers(root, problem, use_cache=False)
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["final"]["code"], "solution_source_invalid")
                    self.assertFalse(
                        any(event.get("type") == "compile" for event in result["events"])
                    )

    def test_standard_check_normalizes_newlines_reuses_cache_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(root, answer=b"2\r\n")
            evidence = problem / ".probhub/judge-evidence-v2.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(b'{"previous":true}\n')
            before = evidence.read_bytes()

            first = check_sample_answers(root, problem)
            second = check_sample_answers(root, problem)

            self.assertTrue(first["ok"], first)
            self.assertTrue(first["applicable"])
            self.assertFalse(first["checks"][0]["cached"])
            self.assertTrue(second["ok"], second)
            self.assertTrue(second["checks"][0]["cached"])
            self.assertEqual(second["cache"]["case_hits"], 1)
            self.assertEqual(
                second["checks"][0]["actual"]["sha256"],
                second["checks"][0]["expected"]["sha256"],
            )
            self.assertEqual(second["checks"][0]["actual"]["bytes"], 2)
            self.assertEqual(evidence.read_bytes(), before)

    def test_standard_check_uses_first_configured_accepted_and_is_byte_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(
                root,
                accepted_source=_source(
                    r"""
                    #include <iostream>
                    int main(){ std::cout << "2   \n"; }
                    """
                ),
                second_accepted_source=_source(
                    r"""
                    #include <iostream>
                    int main(){ std::cout << "2\n"; }
                    """
                ),
            )

            result = check_sample_answers(root, problem)

            self.assertFalse(result["ok"], result)
            self.assertEqual(result["final"]["code"], "sample_answer_mismatch")
            check = result["checks"][0]
            self.assertEqual(check["program"], "code/std.cpp")
            self.assertEqual(check["run_status"], "AC")
            self.assertFalse(check["matches"])
            self.assertIsNotNone(check["first_difference"])

    def test_custom_checker_ac_cannot_replace_sample_byte_consistency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(
                root,
                judge_type="custom",
                accepted_source=_source(
                    r"""
                    #include <iostream>
                    int main(){ long long x; std::cin >> x; std::cout << -x << '\n'; }
                    """
                ),
                checker_source="int main(){return 42;}\n",
            )

            result = check_sample_answers(root, problem)

            self.assertFalse(result["ok"], result)
            self.assertEqual(result["final"]["code"], "sample_answer_mismatch")
            self.assertEqual(result["checks"][0]["run_status"], "AC")
            self.assertFalse(result["checks"][0]["matches"])

    def test_custom_sample_check_runs_no_checker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(
                root,
                judge_type="custom",
                checker_source="this is intentionally not valid C++\n",
            )

            result = check_sample_answers(root, problem, use_cache=False)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["final"]["code"], "sample_answers_match")
            self.assertTrue(result["checks"][0]["matches"])
            self.assertFalse(any(
                event.get("kind") == "checker"
                for event in result["events"]
            ))

    def test_execution_failure_is_not_misreported_as_answer_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(
                root,
                accepted_source=_source(
                    r'''
                    #include <iostream>
                    int main(){ std::cout << "9\n"; return 7; }
                    '''
                ),
            )

            quick = check_sample_answers(root, problem, use_cache=False)
            full = judge_problem(root, problem, use_cache=False)

            self.assertFalse(quick["ok"], quick)
            self.assertEqual(quick["final"]["code"], "sample_check_failed")
            self.assertEqual(quick["checks"][0]["run_status"], "RE")
            self.assertFalse(quick["checks"][0]["mismatch"])
            self.assertFalse(full["ok"], full)
            self.assertNotEqual(full["final"]["code"], "sample_answer_mismatch")

    def test_custom_checker_failure_is_not_misreported_as_answer_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(
                root,
                judge_type="custom",
                accepted_source=_source(
                    r'''
                    #include <iostream>
                    int main(){ std::cout << "9\n"; }
                    '''
                ),
                checker_source="int main(){return 99;}\n",
            )

            result = judge_problem(root, problem, use_cache=False)

            self.assertFalse(result["ok"], result)
            self.assertNotEqual(result["final"]["code"], "sample_answer_mismatch")
            check = next(
                event for event in result["events"]
                if event.get("type") == "sample_check"
            )
            self.assertEqual(check["run_status"], "FAIL")
            self.assertFalse(check["mismatch"])

    def test_sample_check_no_cache_preserves_unrelated_cache_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(root)
            cache_path = problem / ".probhub/sandbox-cache-v1.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(json.dumps({
                "schema_version": SANDBOX_CACHE_SCHEMA_VERSION,
                "compile": {"unrelated-compile": {"ok": True}},
                "validator": {"unrelated-validator": {"ok": True}},
                "case": {"unrelated-case": {"status": "AC"}},
                "probe": {"unrelated-probe": {"status": "TLE"}},
            }), encoding="utf-8")

            result = check_sample_answers(root, problem, use_cache=False)
            saved = json.loads(cache_path.read_text(encoding="utf-8"))

            self.assertTrue(result["ok"], result)
            self.assertIn("unrelated-compile", saved["compile"])
            self.assertIn("unrelated-validator", saved["validator"])
            self.assertIn("unrelated-case", saved["case"])
            self.assertIn("unrelated-probe", saved["probe"])

    def test_missing_samples_have_stable_error_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(root, with_sample=False)

            result = check_sample_answers(root, problem, use_cache=False)

            self.assertFalse(result["ok"], result)
            self.assertTrue(result["applicable"])
            self.assertEqual(result["final"]["code"], "sample_cases_missing")

    def test_full_judge_blocks_standard_sample_mismatch_before_evidence_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = _write_problem(
                root,
                accepted_source=_source(
                    r"""
                    #include <iostream>
                    int main(){ std::cout << "2   \n"; }
                    """
                ),
            )
            evidence = problem / ".probhub/judge-evidence-v2.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(b'{"previous":true}\n')
            before = evidence.read_bytes()

            result = judge_problem(root, problem)

            self.assertFalse(result["ok"], result)
            self.assertEqual(result["final"]["code"], "sample_answer_mismatch")
            self.assertEqual(evidence.read_bytes(), before)


class SampleCheckCliTests(unittest.TestCase):
    def test_interactive_cli_is_not_applicable_and_does_not_touch_gitignore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".probhub").mkdir()
            write_yaml(
                root / ".probhub/workspace.yaml",
                {
                    "schema_version": 1,
                    "contest": {"title": "Contest"},
                    "typst": {"directory": "typst"},
                    "problems": [{"id": "A", "directory": "A"}],
                },
            )
            _write_problem(root, judge_type="interactive", with_sample=False)
            gitignore = root / ".gitignore"
            gitignore.write_text("*.exe\n", encoding="utf-8")
            before = gitignore.read_bytes()
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                returncode = cli_main(
                    [
                        "--workspace",
                        str(root),
                        "--json",
                        "sample-check",
                        "A",
                        "--no-cache",
                    ]
                )

            result = json.loads(stdout.getvalue())
            self.assertEqual(returncode, 0, result)
            self.assertTrue(result["ok"])
            self.assertFalse(result["problems"]["A"]["applicable"])
            self.assertEqual(
                result["problems"]["A"]["final"]["code"],
                "sample_check_not_applicable",
            )
            self.assertEqual(gitignore.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
