import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCAL_JUDGE = ROOT / "scripts" / "local_judge.py"

SPEC = importlib.util.spec_from_file_location("special_local_judge", LOCAL_JUDGE)
JUDGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JUDGE_MODULE)


class InteractiveJudgeUnitTests(unittest.TestCase):
    def test_interactor_spawn_uses_memory_limit_and_containment_failure_is_fail(self):
        class FakeManaged:
            memory_enforced = True
            proc = object()

            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        managed = FakeManaged()
        calls = []

        def spawn(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                return managed
            raise OSError("interactor containment failed")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            JUDGE_MODULE, "spawn_managed", side_effect=spawn
        ):
            root = Path(temp)
            result = JUDGE_MODULE.run_interactive_testcase(
                str(root / "solution"),
                str(root / "interactor"),
                str(root / "case.in"),
                str(root / "case.ans"),
                memory_limit=73,
            )

        self.assertEqual(result[0], "FAIL", result)
        self.assertIn("containment failed", result[4])
        self.assertEqual([call[1]["memory_limit_mb"] for call in calls], [73, 73])
        self.assertTrue(managed.terminated)

    def test_interactive_output_status_combines_protocol_and_stderr(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            solution_stderr = root / "solution.stderr"
            interactor_stderr = root / "interactor.stderr"
            solution_stderr.write_bytes(b"s" * 400)
            interactor_stderr.write_bytes(b"")
            traffic = {
                "limit": 1000,
                "solution_to_interactor": 700,
                "interactor_to_solution": 0,
            }
            status, message = JUDGE_MODULE._interactive_output_status(
                traffic, solution_stderr, interactor_stderr
            )
            self.assertEqual(status, "OLE")
            self.assertIn("output limit", message)

    def test_interactive_temp_cleanup_retries_permission_error(self):
        with mock.patch.object(
            JUDGE_MODULE.shutil,
            "rmtree",
            side_effect=[PermissionError("busy"), None],
        ) as rmtree, mock.patch.object(JUDGE_MODULE.time, "sleep"):
            JUDGE_MODULE._remove_interactive_temp("temporary")
        self.assertEqual(rmtree.call_count, 2)


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
            "limits": {"time": 2, "memory": 256, "output": 1, "processes": 8},
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


    def test_standard_output_limit_returns_ole(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "standard"},
                """
                #include <iostream>
                int main(){ for (int i = 0; i < 2000000; ++i) std::cout << 'x'; }
                """,
                """
                #include <iostream>
                int main(){ std::cout << 0 << '\n'; }
                """,
                {},
            )
            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            case = next(
                event for event in events
                if event.get("type") == "case" and event.get("kind") == "std"
            )
            self.assertEqual(case["status"], "OLE", case)
            limits = next(event for event in events if event.get("type") == "limits")
            self.assertEqual(limits["output_limit"], 1)
            self.assertEqual(limits["process_limit"], 8)

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

    def test_interactive_fast_exit_is_submission_re_not_infrastructure_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "interactive", "interactor": "code/interactor.cpp"},
                "int main(){ return 7; }",
                "int main(){ return 7; }",
                {
                    "interactor.cpp": """
                    #include "testlib.h"
                    int main(int argc, char** argv) {
                        registerInteraction(argc, argv);
                        ouf.readLong();
                        quitf(_ok, "received response");
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            case = next(
                event for event in events
                if event.get("type") == "case" and event.get("kind") == "std"
            )
            self.assertEqual(case["status"], "RE", case)

    def test_interactive_fast_output_flood_is_ole_after_process_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "interactive", "interactor": "code/interactor.cpp"},
                """
                #include <iostream>
                int main(){ std::cout << std::string(2000000, 'x') << std::flush; }
                """,
                "int main(){ return 0; }",
                {
                    "interactor.cpp": """
                    #include "testlib.h"
                    #include <iostream>
                    int main(int argc, char** argv) {
                        registerInteraction(argc, argv);
                        std::string value;
                        while (std::cin >> value) {}
                        quitf(_wa, "unexpected output");
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            case = next(
                event for event in events
                if event.get("type") == "case" and event.get("kind") == "std"
            )
            self.assertEqual(case["status"], "OLE", case)

    def test_interactor_memory_limit_is_infrastructure_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {
                    "type": "interactive",
                    "interactor": "code/interactor.cpp",
                    "interactive": {"idle_limit": 10},
                },
                "int main(){ return 0; }",
                "int main(){ return 0; }",
                {
                    "interactor.cpp": """
                    #include "testlib.h"
                    #include <chrono>
                    #include <thread>
                    #include <vector>
                    int main(int argc, char** argv) {
                        registerInteraction(argc, argv);
                        std::vector<char> memory(128 * 1024 * 1024, 1);
                        std::this_thread::sleep_for(std::chrono::seconds(30));
                        quitf(_ok, "unexpectedly survived memory limit");
                    }
                    """,
                },
            )
            config_path = problem / "probhub.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["limits"]["time"] = 4
            config["limits"]["memory"] = 32
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            case = next(
                event for event in events
                if event.get("type") == "case" and event.get("kind") == "std"
            )
            self.assertEqual(case["status"], "FAIL", case)

    @unittest.skipUnless(platform.system() == "Linux", "Linux /proc peak-memory regression")
    def test_interactive_linux_peak_memory_survives_process_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "interactive", "interactor": "code/interactor.cpp"},
                """
                #include <chrono>
                #include <iostream>
                #include <thread>
                #include <vector>
                int main(){
                    long long x;
                    if (!(std::cin >> x)) return 1;
                    std::vector<char> memory(32 * 1024 * 1024, 1);
                    std::this_thread::sleep_for(std::chrono::milliseconds(200));
                    std::cout << 2 * x << std::endl;
                }
                """,
                "int main(){ return 1; }",
                {
                    "interactor.cpp": """
                    #include "testlib.h"
                    #include <iostream>
                    int main(int argc, char** argv) {
                        registerInteraction(argc, argv);
                        long long secret = inf.readLong();
                        std::cout << secret << std::endl;
                        if (ouf.readLong() == 2 * secret) quitf(_ok, "accepted");
                        quitf(_wa, "wrong answer");
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            case = next(
                event for event in events
                if event.get("type") == "case" and event.get("kind") == "std"
            )
            self.assertEqual(case["status"], "AC", case)
            self.assertIsNotNone(case["memory"], case)
            self.assertGreater(case["memory"], 16, case)

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

    def test_interactive_judge_cancellation_cleans_both_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            solution_pid_file = temp / "solution.pid"
            interactor_pid_file = temp / "interactor.pid"
            problem = self.write_problem(
                temp,
                {
                    "type": "interactive",
                    "interactor": "code/interactor.cpp",
                    "interactive": {"idle_limit": 30, "transcript_limit": 4096},
                },
                f'''
                #include <chrono>
                #include <fstream>
                #include <thread>
                #ifdef _WIN32
                #include <process.h>
                #define PROBHUB_GETPID _getpid
                #else
                #include <unistd.h>
                #define PROBHUB_GETPID getpid
                #endif
                int main() {{
                    std::ofstream("{solution_pid_file.as_posix()}") << PROBHUB_GETPID() << std::flush;
                    std::this_thread::sleep_for(std::chrono::seconds(30));
                }}
                ''',
                "int main(){return 0;}",
                {
                    "interactor.cpp": f'''
                    #include "testlib.h"
                    #include <chrono>
                    #include <fstream>
                    #include <thread>
                    #ifdef _WIN32
                    #include <process.h>
                    #define PROBHUB_GETPID _getpid
                    #else
                    #include <unistd.h>
                    #define PROBHUB_GETPID getpid
                    #endif
                    int main(int argc, char** argv) {{
                        registerInteraction(argc, argv);
                        std::ofstream("{interactor_pid_file.as_posix()}") << PROBHUB_GETPID() << std::flush;
                        std::this_thread::sleep_for(std::chrono::seconds(30));
                    }}
                    ''',
                },
            )
            config_path = problem / "probhub.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["limits"]["time"] = 30
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            cancel_file = temp / "cancel.requested"
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
            proc = subprocess.Popen(
                [sys.executable, str(LOCAL_JUDGE), str(problem), "--jsonl", "--no-cache", "--cancellable"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            output = ""
            try:
                deadline = time.time() + 30
                while time.time() < deadline and not (solution_pid_file.is_file() and interactor_pid_file.is_file()):
                    time.sleep(0.05)
                self.assertTrue(solution_pid_file.is_file(), "interactive solution did not start")
                self.assertTrue(interactor_pid_file.is_file(), "interactor did not start")
                solution_pid = int(solution_pid_file.read_text(encoding="utf-8"))
                interactor_pid = int(interactor_pid_file.read_text(encoding="utf-8"))
                cancel_file.touch()
                output, _ = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            self.assertEqual(proc.returncode, 130, output)
            self.assertEqual(events[-1].get("status"), "cancelled", events[-1])
            for pid in (solution_pid, interactor_pid):
                deadline = time.time() + 5
                while JUDGE_MODULE._process_alive(pid) and time.time() < deadline:
                    time.sleep(0.05)
                self.assertFalse(JUDGE_MODULE._process_alive(pid), f"interactive process {pid} survived cancellation")

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
