import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCAL_JUDGE = ROOT / "scripts" / "local_judge.py"

SPEC = importlib.util.spec_from_file_location("special_local_judge", LOCAL_JUDGE)
JUDGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JUDGE_MODULE)


@unittest.skipUnless(shutil.which("g++"), "g++ is required for special-judge integration tests")
class SpecialJudgeIntegrationTests(unittest.TestCase):
    def write_problem(self, root, judge, accepted_source, wrong_source, extra_sources):
        problem = root / "A"
        code = problem / "code"
        sample = problem / "data" / "sample"
        secret = problem / "data" / "secret"
        code.mkdir(parents=True)
        sample.mkdir(parents=True)
        secret.mkdir(parents=True)
        (code / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (code / "std.cpp").write_text(textwrap.dedent(accepted_source), encoding="utf-8")
        (code / "wrong.cpp").write_text(textwrap.dedent(wrong_source), encoding="utf-8")
        for name, source in extra_sources.items():
            (code / name).write_text(textwrap.dedent(source), encoding="utf-8")
        for directory, value in ((sample, 2), (secret, 5)):
            (directory / "1.in").write_text(f"{value}\n", encoding="utf-8")
            (directory / "1.ans").write_text(f"{value}\n", encoding="utf-8")
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "Special",
            "limits": {"time": 2, "memory": 256},
            "judge": {"validator": "code/validator.cpp", **judge},
            "solutions": {
                "accepted": ["code/std.cpp"],
                "brute": [],
                "wrong": ["code/wrong.cpp"],
            },
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        }
        (problem / "probhub.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return problem

    def run_judge(self, problem):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(LOCAL_JUDGE), str(problem), "--jsonl", "--no-cache"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60,
        )
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, events

    def test_custom_checker_accepts_non_unique_output_and_kills_wrong_solution(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "custom", "checker": "code/checker.cpp"},
                """
                #include <iostream>
                int main(){ long long x; std::cin >> x; std::cout << -x << '\\n'; }
                """,
                """
                #include <iostream>
                int main(){ long long x; std::cin >> x; std::cout << 0 << '\\n'; }
                """,
                {
                    "checker.cpp": """
                    #include "testlib.h"
                    #include <cstdlib>
                    int main(int argc, char** argv) {
                        registerTestlibCmd(argc, argv);
                        long long actual = ouf.readLong();
                        long long expected = ans.readLong();
                        if (!ouf.seekEof()) quitf(_wa, "extra output");
                        if (std::llabs(actual) == std::llabs(expected))
                            quitf(_ok, "accepted absolute value");
                        quitf(_wa, "expected absolute value %lld, found %lld", expected, actual);
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(events[-1]["code"], "all_expectations_met")
            compile_kinds = {event.get("kind") for event in events if event.get("type") == "compile"}
            self.assertIn("checker", compile_kinds)
            std_cases = [event for event in events if event.get("type") == "case" and event.get("kind") == "std"]
            wrong_cases = [event for event in events if event.get("type") == "case" and event.get("kind") == "wrong"]
            self.assertTrue(std_cases and all(event["status"] == "AC" for event in std_cases))
            self.assertTrue(wrong_cases and all(event["status"] == "WA" for event in wrong_cases))
            self.assertTrue(all(event["judge_type"] == "custom" for event in std_cases + wrong_cases))

    def test_interactor_runs_bidirectional_protocol_and_kills_wrong_solution(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "interactive", "interactor": "code/interactor.cpp"},
                """
                #include <iostream>
                int main(){ long long x; if (!(std::cin >> x)) return 1; std::cout << 2 * x << std::endl; }
                """,
                """
                #include <iostream>
                int main(){ long long x; if (!(std::cin >> x)) return 1; std::cout << x << std::endl; }
                """,
                {
                    "interactor.cpp": """
                    #include "testlib.h"
                    #include <iostream>
                    int main(int argc, char** argv) {
                        registerInteraction(argc, argv);
                        long long secret = inf.readLong();
                        std::cout << secret << std::endl;
                        long long answer = ouf.readLong();
                        if (answer == 2 * secret) quitf(_ok, "correct response");
                        quitf(_wa, "expected %lld, found %lld", 2 * secret, answer);
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(events[-1]["code"], "all_expectations_met")
            compile_kinds = {event.get("kind") for event in events if event.get("type") == "compile"}
            self.assertIn("interactor", compile_kinds)
            std_cases = [event for event in events if event.get("type") == "case" and event.get("kind") == "std"]
            wrong_cases = [event for event in events if event.get("type") == "case" and event.get("kind") == "wrong"]
            self.assertTrue(std_cases and all(event["status"] == "AC" for event in std_cases))
            self.assertTrue(wrong_cases and all(event["status"] == "WA" for event in wrong_cases))
            self.assertTrue(all(event["judge_type"] == "interactive" for event in std_cases + wrong_cases))
            transcripts = [event for event in events if event.get("type") == "transcript"]
            self.assertTrue(transcripts)
            directions = {
                entry["direction"]
                for event in transcripts
                for entry in event.get("entries", [])
            }
            self.assertEqual(
                directions,
                {"interactor_to_solution", "solution_to_interactor"},
            )

    def test_interactor_idle_timeout_is_structured_and_preserves_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {
                    "type": "interactive",
                    "interactor": "code/interactor.cpp",
                    "interactive": {"idle_limit": 0.2, "transcript_limit": 4096},
                },
                """
                #include <chrono>
                #include <thread>
                int main(){ std::this_thread::sleep_for(std::chrono::seconds(5)); }
                """,
                """
                #include <chrono>
                #include <thread>
                int main(){ std::this_thread::sleep_for(std::chrono::seconds(5)); }
                """,
                {
                    "interactor.cpp": """
                    #include "testlib.h"
                    #include <chrono>
                    #include <iostream>
                    #include <thread>
                    int main(int argc, char** argv) {
                        registerInteraction(argc, argv);
                        std::cout << inf.readLong() << std::endl;
                        std::this_thread::sleep_for(std::chrono::seconds(5));
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            case = next(event for event in events if event.get("type") == "case")
            self.assertEqual(case["status"], "TLE")
            self.assertEqual(case["timeout_kind"], "idle")
            transcript = next(event for event in events if event.get("type") == "transcript")
            self.assertTrue(transcript["entries"])
            self.assertEqual(transcript["entries"][0]["direction"], "interactor_to_solution")

    def test_process_tree_termination_kills_spawned_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            pid_file = temp / "child.pid"
            parent_script = temp / "parent.py"
            child_script = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8');"
                "time.sleep(30)"
            )
            parent_script.write_text(
                "import subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            kwargs = {}
            if os.name == "nt":
                proc = subprocess.Popen([sys.executable, str(parent_script)])
                job = JUDGE_MODULE._windows_assign_job(proc)
            else:
                kwargs["start_new_session"] = True
                proc = subprocess.Popen([sys.executable, str(parent_script)], **kwargs)
                job = None
            deadline = time.time() + 5
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "child process did not start")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(JUDGE_MODULE._process_alive(child_pid))
            JUDGE_MODULE._terminate_process(proc, job)
            deadline = time.time() + 5
            while JUDGE_MODULE._process_alive(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(JUDGE_MODULE._process_alive(child_pid), f"child process {child_pid} survived")


if __name__ == "__main__":
    unittest.main()
