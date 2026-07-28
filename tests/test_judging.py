import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from probhub.calibration import evidence_path
from probhub.judging import (
    JUDGE_OUTPUT_LIMIT_BYTES,
    JUDGE_TIMEOUT_SECONDS,
    check_sample_answers,
    judge_problem,
)
from probhub.process_control import process_alive


def _fake_run(stdout_text, reason="completed", returncode=0, message=""):
    def side_effect(command, **kwargs):
        Path(kwargs["stdout_path"]).write_text(stdout_text, encoding="utf-8")
        Path(kwargs["stderr_path"]).write_text("", encoding="utf-8")
        return {
            "reason": reason,
            "message": message,
            "returncode": returncode,
            "time": 0.01,
            "memory": None,
            "memory_enforced": False,
            "process_limit_enforced": False,
        }

    return side_effect


class JudgingTests(unittest.TestCase):
    def make_root(self, temp, script_body=""):
        root = Path(temp)
        script = root / "scripts" / "local_judge.py"
        script.parent.mkdir()
        script.write_text(script_body, encoding="utf-8")
        return root

    def make_problem(self, root):
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# A\n\n## 题目描述\nA\n\n## 输入格式\nA\n\n## 输出格式\nA\n",
            encoding="utf-8",
        )
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "probhub.yaml").write_text(
            "schema_version: 1\nid: A\nname: A\n"
            "limits:\n  time: 1\n  memory: 256\n"
            "statement:\n  source: problem.md\n"
            "judge:\n  type: standard\n  validator: code/std.cpp\n"
            "solutions:\n  accepted: [code/std.cpp]\n"
            "data:\n  sample_dir: data/sample\n  secret_dir: data/secret\n",
            encoding="utf-8",
        )
        return problem

    def test_local_judge_subprocess_is_forced_to_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            message = "[+] 恭喜！所有代码均符合预期宿命"
            event = {
                "type": "final",
                "status": "passed",
                "code": "all_expectations_met",
                "message": message,
            }
            side_effect = _fake_run(json.dumps(event, ensure_ascii=False) + "\n")

            with patch("probhub.judging.run_managed_to_files", side_effect=side_effect) as run:
                result = judge_problem(root, root / "A")

            self.assertTrue(result["ok"])
            self.assertEqual(result["final"]["message"], message)
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(run.call_args.kwargs["timeout"], JUDGE_TIMEOUT_SECONDS)
            self.assertEqual(run.call_args.kwargs["output_limit_bytes"], JUDGE_OUTPUT_LIMIT_BYTES)

    def test_time_limit_produces_structured_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            events = [
                {"type": "case", "case": "1", "status": "passed"},
                {"type": "case", "case": "2", "status": "passed"},
            ]
            stdout_text = "".join(json.dumps(event) + "\n" for event in events)
            side_effect = _fake_run(
                stdout_text,
                reason="time_limit",
                returncode=1,
                message="time limit exceeded",
            )

            with patch("probhub.judging.run_managed_to_files", side_effect=side_effect):
                result = judge_problem(root, root / "A")

            self.assertFalse(result["ok"])
            self.assertEqual(result["final"]["code"], "judge_timeout")
            self.assertEqual(result["final"]["status"], "failed")
            self.assertEqual(result["events"], events)

    def test_output_limit_with_truncated_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            stdout_text = json.dumps({"type": "case", "case": "1", "status": "passed"}) + "\n" + '{"type": "cas'
            side_effect = _fake_run(
                stdout_text,
                reason="output_limit",
                returncode=1,
                message="output limit exceeded",
            )

            with patch("probhub.judging.run_managed_to_files", side_effect=side_effect):
                result = judge_problem(root, root / "A")

            self.assertFalse(result["ok"])
            self.assertEqual(result["final"]["code"], "judge_output_limit")
            self.assertEqual(len(result["events"]), 1)

    def test_successful_complete_judge_atomically_publishes_calibration_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            problem = self.make_problem(root)
            events = [
                {
                    "type": "limits",
                    "time_limit": 1,
                    "memory_limit": 256,
                    "output_limit": 64,
                    "process_limit": 32,
                    "platform": "Windows",
                    "machine": "AMD64",
                    "compiler": "g++ test",
                },
                {
                    "type": "summary",
                    "kind": "std",
                    "program": "code/std.cpp",
                    "stats": {"AC": 2},
                    "expectation": {"expected_statuses": ["AC"], "ok": True},
                    "calibration": {
                        "max_time": 0.1,
                        "headroom_factor": 10,
                        "resource_kills": [],
                    },
                },
                {
                    "type": "final",
                    "status": "passed",
                    "code": "all_expectations_met",
                },
            ]
            side_effect = _fake_run(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            with patch("probhub.judging.run_managed_to_files", side_effect=side_effect):
                result = judge_problem(root, problem)

            self.assertTrue(result["ok"])
            self.assertEqual(len(result["summaries"]), 1)
            self.assertIsNotNone(result["calibration"])
            self.assertTrue(evidence_path(problem).is_file())

    def test_failed_judge_preserves_last_successful_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            problem = self.make_problem(root)
            evidence = evidence_path(problem)
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"previous":true}\n', encoding="utf-8")
            before = evidence.read_bytes()
            event = {"type": "final", "status": "failed", "code": "expectation_not_met"}
            with patch(
                "probhub.judging.run_managed_to_files",
                side_effect=_fake_run(json.dumps(event) + "\n", returncode=1),
            ):
                result = judge_problem(root, problem)
            self.assertFalse(result["ok"])
            self.assertEqual(evidence.read_bytes(), before)

    def test_sample_check_uses_dedicated_mode_and_never_publishes_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            problem = self.make_problem(root)
            evidence = evidence_path(problem)
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"previous":true}\n', encoding="utf-8")
            before = evidence.read_bytes()
            events = [
                {
                    "type": "sample_check",
                    "applicable": True,
                    "ok": True,
                    "case": "sample/1",
                    "matches": True,
                },
                {
                    "type": "final",
                    "status": "passed",
                    "code": "sample_answers_match",
                    "applicable": True,
                },
            ]
            side_effect = _fake_run(
                "".join(json.dumps(event) + "\n" for event in events)
            )

            with patch(
                "probhub.judging.run_managed_to_files", side_effect=side_effect
            ) as run:
                result = check_sample_answers(root, problem, use_cache=False)

            self.assertTrue(result["ok"])
            self.assertTrue(result["applicable"])
            command = run.call_args.args[0]
            self.assertIn("--sample-check", command)
            self.assertIn("--no-cache", command)
            self.assertEqual(evidence.read_bytes(), before)

    def test_hanging_judge_is_killed_after_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(
                temp,
                "import os, pathlib, sys, time\n"
                "pathlib.Path(sys.argv[1], 'judge.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
                "time.sleep(60)\n",
            )
            problem_dir = root / "A"
            problem_dir.mkdir()

            started = time.time()
            result = judge_problem(root, problem_dir, timeout=1)
            elapsed = time.time() - started

            self.assertFalse(result["ok"])
            self.assertEqual(result["final"]["code"], "judge_timeout")
            self.assertLess(elapsed, 10)
            pid_file = problem_dir / "judge.pid"
            self.assertTrue(pid_file.is_file(), "judge stub did not start")
            pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while process_alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_alive(pid), f"judge process {pid} survived")


if __name__ == "__main__":
    unittest.main()
