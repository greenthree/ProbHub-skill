import importlib.util
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

import probhub.special_judges as SPECIAL_JUDGES
from probhub.process_control import (
    process_alive,
    spawn_managed,
)

ROOT = Path(__file__).resolve().parents[1]
LOCAL_JUDGE = ROOT / "scripts" / "local_judge.py"

SPEC = importlib.util.spec_from_file_location("special_local_judge", LOCAL_JUDGE)
JUDGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JUDGE_MODULE)


class InteractiveJudgeUnitTests(unittest.TestCase):
    class _ChunkedSource:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            pass

    class _NonClosingBytesIO(io.BytesIO):
        def close(self):
            pass

    def test_output_budget_enforcement_failure_is_infrastructure_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            output = str(Path(temp) / "output.txt")
            with mock.patch.object(
                JUDGE_MODULE,
                "run_managed_to_files",
                side_effect=JUDGE_MODULE.OutputBudgetError("cannot enforce output budget"),
            ):
                status, _, _, _, message, details = JUDGE_MODULE.run_program_to_file(
                    "unused", "unused", output
                )
            self.assertEqual(status, "FAIL")
            self.assertIn("cannot enforce", message)
            self.assertEqual(details["termination_reason"], "output_control_error")

    def test_custom_checker_output_control_failure_has_precise_reason(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            JUDGE_MODULE,
            "run_program_to_file",
            return_value=("AC", 0.01, 1, True, "", {"output_bytes": 0}),
        ), mock.patch.object(
            SPECIAL_JUDGES,
            "run_managed_to_files",
            side_effect=JUDGE_MODULE.OutputBudgetError("feedback is not regular"),
        ):
            root = Path(temp)
            status, _, _, _, message, details = JUDGE_MODULE.run_custom_testcase(
                str(root / "solution"),
                str(root / "checker"),
                str(root / "case.in"),
                str(root / "case.ans"),
            )
        self.assertEqual(status, "FAIL")
        self.assertIn("output control failed", message)
        self.assertNotIn("failed to start", message)
        self.assertEqual(details["checker_termination_reason"], "output_control_error")

    def test_custom_contestant_failure_fields_are_normalized_before_checker(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        cases = (
            (
                ("MLE", 0.01, 255, True, "", {
                    "output_bytes": 0,
                    "termination_reason": "inferred_memory_limit",
                }),
                "memory_limit",
                "resource_limit",
                "inferred_memory_limit",
            ),
            (
                ("RE", 0.0, None, False, "missing", {"output_bytes": 0}),
                "start_error",
                "startup_failure",
                "start_error",
            ),
        )
        for program_result, execution_status, failure_kind, termination_reason in cases:
            with self.subTest(execution_status=execution_status), mock.patch.object(
                JUDGE_MODULE, "run_program_to_file", return_value=program_result
            ), mock.patch.object(
                JUDGE_MODULE, "run_checker_to_files"
            ) as checker:
                result = JUDGE_MODULE.run_custom_testcase(
                    str(root / "solution"),
                    str(root / "checker"),
                    str(root / "case.in"),
                    str(root / "case.ans"),
                )
            details = result[5]
            self.assertEqual(details["execution_status"], execution_status)
            self.assertEqual(details["failure_kind"], failure_kind)
            self.assertEqual(details["termination_reason"], termination_reason)
            self.assertEqual(details["actor"], "contestant")
            checker.assert_not_called()

    def test_interactive_final_classification_follows_tree_termination(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.returncode = 0

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_enforced = True
            peak_memory_mb = 1

            def __init__(self, stderr, flood):
                self.proc = FakeProcess()
                self.stderr = stderr
                self.flood = flood
                self.terminated = False

            def sample(self):
                return 1, 1

            def terminate(self):
                if not self.terminated and self.flood:
                    self.stderr.write(b"x" * (2 * 1024 * 1024))
                    self.stderr.flush()
                self.terminated = True

        spawned = []

        def fake_spawn(*_args, **kwargs):
            managed = FakeManaged(kwargs["stderr"], flood=not spawned)
            spawned.append(managed)
            return managed

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES, "spawn_managed", side_effect=fake_spawn
        ):
            root = Path(temp)
            result = JUDGE_MODULE.run_interactive_testcase(
                str(root / "solution"),
                str(root / "interactor"),
                str(root / "case.in"),
                str(root / "case.ans"),
                output_limit=1,
            )

        self.assertEqual(result[0], "OLE", result)
        self.assertTrue(all(item.terminated for item in spawned))

    def test_interactive_transcript_decodes_utf8_across_chunks(self):
        lock = threading.RLock()
        transcript = {
            "entries": [],
            "bytes": 0,
            "limit": 32,
            "truncated": False,
            "_lock": lock,
        }
        activity = {"last": 0.0}
        traffic = {"limit": 1024, "_lock": lock}
        destination = self._NonClosingBytesIO()

        SPECIAL_JUDGES._pump_interactive_stream(
            self._ChunkedSource([b"\xe4\xbd", b"\xa0\n"]),
            destination,
            "interactor_to_solution",
            transcript,
            activity,
            traffic,
        )

        recorded = "".join(entry["data"] for entry in transcript["entries"])
        self.assertEqual(recorded, "你\n")
        self.assertNotIn("\ufffd", recorded)
        self.assertEqual(destination.getvalue(), "你\n".encode("utf-8"))

    def test_interactive_transcript_budget_is_shared_atomically(self):
        class RacingTranscript(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._barriers = [
                    threading.Barrier(2),
                    threading.Barrier(2),
                ]
                self._reads = {}

            def __getitem__(self, key):
                if key == "bytes":
                    thread_id = threading.get_ident()
                    count = self._reads.get(thread_id, 0)
                    self._reads[thread_id] = count + 1
                    if count < len(self._barriers):
                        try:
                            self._barriers[count].wait(timeout=0.1)
                        except threading.BrokenBarrierError:
                            pass
                return super().__getitem__(key)

        lock = threading.RLock()
        transcript = RacingTranscript(
            entries=[], bytes=0, limit=8, truncated=False, _lock=lock
        )
        activity = {"last": 0.0}
        traffic = {"limit": 1024, "_lock": lock}
        destinations = [self._NonClosingBytesIO(), self._NonClosingBytesIO()]
        threads = [
            threading.Thread(
                target=SPECIAL_JUDGES._pump_interactive_stream,
                args=(
                    self._ChunkedSource([b"abcdefgh"]),
                    destinations[index],
                    direction,
                    transcript,
                    activity,
                    traffic,
                ),
            )
            for index, direction in enumerate(
                ("solution_to_interactor", "interactor_to_solution")
            )
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(transcript["bytes"], transcript["limit"])
        self.assertTrue(transcript["truncated"])

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
            SPECIAL_JUDGES, "spawn_managed", side_effect=spawn
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
            evidence = SPECIAL_JUDGES._interactive_evidence(
                traffic, solution_stderr, interactor_stderr
            )
            outcome = SPECIAL_JUDGES._interactive_output_classification(
                evidence, SPECIAL_JUDGES.MAX_CHECKER_DIAGNOSTIC_BYTES
            )
            self.assertEqual(outcome["status"], "OLE")
            self.assertIn("output limit", outcome["message"])

    def test_interactive_feedback_uses_bounded_diagnostic_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            solution_stderr = root / "solution.stderr"
            interactor_stderr = root / "interactor.stderr"
            feedback = root / "feedback"
            feedback.mkdir()
            solution_stderr.write_bytes(b"")
            interactor_stderr.write_bytes(b"e" * 400)
            (feedback / "judgemessage.txt").write_bytes(b"f" * 700)
            traffic = {
                "limit": 2000,
                "solution_to_interactor": 0,
                "interactor_to_solution": 0,
            }
            evidence = SPECIAL_JUDGES._interactive_evidence(
                traffic,
                solution_stderr,
                interactor_stderr,
                feedback,
            )
            outcome = SPECIAL_JUDGES._interactive_output_classification(evidence, 1000)
            self.assertEqual(outcome["status"], "FAIL")
            self.assertIn("diagnostic output limit", outcome["message"])

            (feedback / "judgemessage.txt").unlink()
            (feedback / "judgemessage.txt").mkdir()
            with self.assertRaisesRegex(
                JUDGE_MODULE.OutputBudgetError, "not a regular file"
            ):
                SPECIAL_JUDGES._interactive_evidence(
                    traffic,
                    solution_stderr,
                    interactor_stderr,
                    feedback,
                )

    def test_interactive_non_regular_feedback_is_structured_control_failure(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.returncode = 0

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_enforced = True
            process_limit_enforced = True
            peak_memory_mb = 1

            def __init__(self):
                self.proc = FakeProcess()

            def sample(self):
                return 1, 1

            def terminate(self):
                pass

        spawned = []

        def fake_spawn(command, **_kwargs):
            managed = FakeManaged()
            if spawned:
                Path(command[-1], "judgemessage.txt").mkdir()
            spawned.append(managed)
            return managed

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES, "spawn_managed", side_effect=fake_spawn
        ):
            root = Path(temp)
            result = SPECIAL_JUDGES.execute_interactive_session(
                [str(root / "solution")],
                [str(root / "interactor")],
                root / "case.in",
                root / "case.ans",
                work_dir=root,
            )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["execution_status"], "output_control_error")
        self.assertEqual(result["failure_kind"], "control_failure")
        self.assertEqual(result["actor"], "supervisor")
        self.assertTrue(result["cleanup"]["ok"])

    def test_interactive_temp_cleanup_retries_permission_error(self):
        with mock.patch.object(
            SPECIAL_JUDGES.shutil,
            "rmtree",
            side_effect=[PermissionError("busy"), None],
        ) as rmtree, mock.patch.object(SPECIAL_JUDGES.time, "sleep"):
            removed, attempts, error = SPECIAL_JUDGES._remove_runtime_directory("temporary")
        self.assertTrue(removed)
        self.assertEqual(attempts, 2)
        self.assertIsNone(error)
        self.assertEqual(rmtree.call_count, 2)

    def test_checker_cleanup_failure_is_structured_infrastructure_failure(self):
        execution = {
            "reason": "completed",
            "returncode": 0,
            "time": 0.01,
            "memory": 1,
            "memory_enforced": True,
            "process_limit_enforced": True,
            "output_bytes": 0,
            "retained_output_bytes": 0,
            "stdout_retained_bytes": 0,
            "stderr_retained_bytes": 0,
            "output_truncated": False,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("case.in", "case.ans", "contestant.out"):
                (root / name).write_bytes(b"")
            with mock.patch.object(
                SPECIAL_JUDGES, "run_managed_to_files", return_value=execution
            ), mock.patch.object(
                SPECIAL_JUDGES.shutil,
                "rmtree",
                side_effect=PermissionError("runtime busy"),
            ) as rmtree, mock.patch.object(SPECIAL_JUDGES.time, "sleep"):
                result = SPECIAL_JUDGES.run_checker_to_files(
                    ["checker"],
                    root / "case.in",
                    root / "case.ans",
                    root / "contestant.out",
                    timeout=1,
                    cwd=root,
                )
        self.assertIsNone(result["verdict"])
        self.assertEqual(result["actor"], "supervisor")
        self.assertEqual(result["execution_status"], "cleanup_error")
        self.assertEqual(result["failure_kind"], "cleanup_failure")
        self.assertFalse(result["cleanup"]["ok"])
        self.assertEqual(result["cleanup"]["runtime_remove_attempts"], 20)
        self.assertEqual(rmtree.call_count, 20)

    def test_checker_cancel_cleanup_failure_overrides_cancellation(self):
        execution = {
            "reason": "cancelled",
            "message": "execution cancelled",
            "returncode": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("case.in", "case.ans", "contestant.out"):
                (root / name).write_bytes(b"")
            with mock.patch.object(
                SPECIAL_JUDGES, "run_managed_to_files", return_value=execution
            ), mock.patch.object(
                SPECIAL_JUDGES.shutil,
                "rmtree",
                side_effect=PermissionError("runtime busy"),
            ), mock.patch.object(SPECIAL_JUDGES.time, "sleep"):
                result = SPECIAL_JUDGES.run_checker_to_files(
                    ["checker"],
                    root / "case.in",
                    root / "case.ans",
                    root / "contestant.out",
                    timeout=1,
                    cwd=root,
                )
        self.assertEqual(result["execution_status"], "cleanup_error")
        self.assertEqual(result["failure_kind"], "cleanup_failure")
        self.assertEqual(
            result["pre_cleanup_result"]["execution_status"], "cancelled"
        )

    def test_checker_cancel_with_successful_cleanup_propagates(self):
        execution = {
            "reason": "cancelled",
            "message": "execution cancelled",
            "returncode": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("case.in", "case.ans", "contestant.out"):
                (root / name).write_bytes(b"")
            with mock.patch.object(
                SPECIAL_JUDGES, "run_managed_to_files", return_value=execution
            ):
                with self.assertRaisesRegex(
                    JUDGE_MODULE.ProcessCancelled, "execution cancelled"
                ):
                    SPECIAL_JUDGES.run_checker_to_files(
                        ["checker"],
                        root / "case.in",
                        root / "case.ans",
                        root / "contestant.out",
                        timeout=1,
                        cwd=root,
                    )

    def test_interactive_cleanup_failure_does_not_skip_other_cleanup(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.returncode = 0

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_enforced = True
            process_limit_enforced = True
            peak_memory_mb = 1

            def __init__(self, fail=False):
                self.proc = FakeProcess()
                self.fail = fail
                self.terminate_calls = 0

            def sample(self):
                return 1, 1

            def terminate(self):
                self.terminate_calls += 1
                if self.fail:
                    raise OSError("tree cleanup failed")

        managed = [FakeManaged(fail=True), FakeManaged()]
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES, "spawn_managed", side_effect=managed
        ):
            root = Path(temp)
            result = SPECIAL_JUDGES.execute_interactive_session(
                [str(root / "solution")],
                [str(root / "interactor")],
                root / "case.in",
                root / "case.ans",
                work_dir=root,
                time_limit=1,
            )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["actor"], "supervisor")
        self.assertEqual(result["execution_status"], "cleanup_error")
        self.assertEqual(result["failure_kind"], "cleanup_failure")
        self.assertFalse(result["cleanup"]["ok"])
        self.assertEqual(managed[0].terminate_calls, 1)
        self.assertEqual(managed[1].terminate_calls, 1)
        self.assertTrue(result["cleanup"]["runtime_removed"])

    def test_interactive_cancel_cleanup_failure_overrides_cancellation(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.returncode = None

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_enforced = True
            process_limit_enforced = True
            peak_memory_mb = 1

            def __init__(self):
                self.proc = FakeProcess()

            def sample(self):
                return 1, 1

            def terminate(self):
                self.proc.returncode = -1

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES, "spawn_managed", side_effect=[FakeManaged(), FakeManaged()]
        ), mock.patch.object(
            SPECIAL_JUDGES, "cancellation_requested", return_value=True
        ), mock.patch.object(
            SPECIAL_JUDGES.shutil,
            "rmtree",
            side_effect=PermissionError("runtime busy"),
        ), mock.patch.object(SPECIAL_JUDGES.time, "sleep"):
            root = Path(temp)
            result = SPECIAL_JUDGES.execute_interactive_session(
                [str(root / "solution")],
                [str(root / "interactor")],
                root / "case.in",
                root / "case.ans",
                work_dir=root,
            )
        self.assertEqual(result["execution_status"], "cleanup_error")
        self.assertEqual(result["failure_kind"], "cleanup_failure")
        self.assertEqual(
            result["pre_cleanup_result"]["execution_status"], "cancelled"
        )

    def test_interactive_preparation_failure_is_structured_start_error(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES.Path,
            "mkdir",
            side_effect=OSError("feedback directory unavailable"),
        ):
            root = Path(temp)
            result = SPECIAL_JUDGES.execute_interactive_session(
                [str(root / "solution")],
                [str(root / "interactor")],
                root / "case.in",
                root / "case.ans",
                work_dir=root,
            )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["execution_status"], "start_error")
        self.assertEqual(result["failure_kind"], "startup_failure")
        self.assertEqual(result["actor"], "supervisor")
        self.assertTrue(result["cleanup"]["ok"])

    def test_interactor_memory_failure_reports_interactor_memory(self):
        class FakeProcess:
            def __init__(self, returncode):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.returncode = returncode

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_enforced = True
            process_limit_enforced = True

            def __init__(self, memory, returncode):
                self.peak_memory_mb = memory
                self.proc = FakeProcess(returncode)

            def sample(self):
                return 1, self.peak_memory_mb

            def terminate(self):
                pass

        managed = [FakeManaged(5, 0), FakeManaged(100, 1)]
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES, "spawn_managed", side_effect=managed
        ):
            root = Path(temp)
            result = SPECIAL_JUDGES.execute_interactive_session(
                [str(root / "solution")],
                [str(root / "interactor")],
                root / "case.in",
                root / "case.ans",
                work_dir=root,
                memory_limit_mb=50,
            )
        self.assertEqual(result["actor"], "interactor")
        self.assertEqual(result["execution_status"], "memory_limit")
        self.assertEqual(result["memory"], 100)
        self.assertEqual(result["resources"]["contestant"]["memory"], 5)
        self.assertEqual(result["resources"]["interactor"]["memory"], 100)

    def test_interactive_deadlines_exclude_process_setup(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.returncode = None

            def poll(self):
                return self.returncode

        class FakeManaged:
            memory_enforced = True
            peak_memory_mb = 1

            def __init__(self):
                self.proc = FakeProcess()

            def sample(self):
                return 1, 1

            def terminate(self):
                self.proc.returncode = -1

        def slow_spawn(*_args, **_kwargs):
            time.sleep(0.15)
            return FakeManaged()

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            SPECIAL_JUDGES, "spawn_managed", side_effect=slow_spawn
        ):
            root = Path(temp)
            result = JUDGE_MODULE.run_interactive_testcase(
                str(root / "solution"),
                str(root / "interactor"),
                str(root / "case.in"),
                str(root / "case.ans"),
                time_limit=0.2,
                idle_limit=0.1,
            )

        self.assertEqual(result[0], "TLE", result)
        self.assertEqual(result[5]["timeout_kind"], "idle", result)


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

    def run_judge(self, problem, *, no_cache=True):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        command = [sys.executable, str(LOCAL_JUDGE), str(problem), "--jsonl"]
        if no_cache:
            command.append("--no-cache")
        result = subprocess.run(
            command,
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
                int main(){
                    long long x;
                    std::cin >> x;
                    std::cout << (x == 2 ? x : -x) << '\\n';
                }
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
            self.assertTrue(all(event["actor"] == "session" for event in std_cases))
            self.assertTrue(all(event["execution_status"] == "completed" for event in std_cases))
            self.assertTrue(all(event["failure_kind"] is None for event in std_cases))
            self.assertTrue(all(event["actor"] == "contestant" for event in wrong_cases))
            self.assertTrue(all(event["failure_kind"] == "wrong_answer" for event in wrong_cases))
            self.assertTrue(all(event["cleanup"]["ok"] for event in std_cases + wrong_cases))
            sample_check = next(
                event for event in events if event.get("type") == "sample_check"
            )
            self.assertTrue(sample_check["matches"], sample_check)

            cached_result, cached_events = self.run_judge(problem, no_cache=False)
            self.assertEqual(cached_result.returncode, 0, cached_result.stderr + cached_result.stdout)
            cached_cases = [
                event for event in cached_events if event.get("type") == "case"
            ]
            self.assertTrue(cached_cases and all(event["cached"] for event in cached_cases))
            fields = (
                "status", "judge_type", "verdict", "actor", "execution_status",
                "failure_kind", "termination_reason", "cleanup",
            )
            first_by_key = {
                (event["kind"], event["case"]): event
                for event in std_cases + wrong_cases
            }
            for event in cached_cases:
                original = first_by_key[(event["kind"], event["case"])]
                self.assertEqual(
                    {field: event.get(field) for field in fields},
                    {field: original.get(field) for field in fields},
                )

    def test_custom_checker_feedback_flood_is_infrastructure_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = self.write_problem(
                Path(temp),
                {"type": "custom", "checker": "code/checker.cpp"},
                "int main(){ return 0; }",
                "int main(){ return 0; }",
                {
                    "checker.cpp": r"""
                    #include <fstream>
                    #include <string>
                    int main(int argc, char** argv) {
                        std::ofstream out(std::string(argv[3]) + "/judgemessage.txt", std::ios::binary);
                        out << std::string(2000000, 'x');
                        out.close();
                        return 42;
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            case = next(
                event
                for event in events
                if event.get("type") == "case" and event.get("kind") == "std"
            )
            self.assertEqual(case["status"], "FAIL", case)
            self.assertIn("output limit", case["message"])
            self.assertEqual(case["actor"], "checker", case)
            self.assertEqual(case["execution_status"], "output_limit", case)
            self.assertEqual(case["failure_kind"], "resource_limit", case)
            self.assertEqual(case["termination_reason"], "output_limit", case)

    def test_custom_checker_ac_cannot_hide_sample_answer_mismatch(self):
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
                        if (std::llabs(actual) == std::llabs(expected))
                            quitf(_ok, "accepted absolute value");
                        quitf(_wa, "wrong absolute value");
                    }
                    """,
                },
            )
            result, events = self.run_judge(problem)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(events[-1]["code"], "sample_answer_mismatch")
            sample_case = next(
                event
                for event in events
                if event.get("type") == "case"
                and event.get("kind") == "std"
                and event.get("case") == "sample/1"
            )
            self.assertEqual(sample_case["status"], "AC", sample_case)
            sample_check = next(
                event for event in events if event.get("type") == "sample_check"
            )
            self.assertFalse(sample_check["matches"], sample_check)

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
            self.assertTrue(all(event["actor"] == "session" for event in std_cases))
            self.assertTrue(all(event["failure_kind"] is None for event in std_cases))
            self.assertTrue(all(event["actor"] == "contestant" for event in wrong_cases))
            self.assertTrue(all(event["failure_kind"] == "wrong_answer" for event in wrong_cases))
            self.assertTrue(all(event["cleanup"]["ok"] for event in std_cases + wrong_cases))
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
            self.assertEqual(case["actor"], "contestant", case)
            self.assertEqual(case["execution_status"], "completed", case)
            self.assertEqual(case["failure_kind"], "runtime_error", case)
            self.assertEqual(case["termination_reason"], "completed", case)

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
            self.assertEqual(case["actor"], "contestant", case)
            self.assertEqual(case["execution_status"], "output_limit", case)
            self.assertEqual(case["failure_kind"], "resource_limit", case)
            self.assertEqual(case["termination_reason"], "output_limit", case)

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
            self.assertEqual(case["actor"], "interactor", case)
            self.assertEqual(case["execution_status"], "memory_limit", case)
            self.assertEqual(case["failure_kind"], "resource_limit", case)
            self.assertEqual(case["termination_reason"], "memory_limit", case)

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
            self.assertEqual(case["actor"], "session", case)
            self.assertEqual(case["execution_status"], "time_limit", case)
            self.assertEqual(case["failure_kind"], "resource_limit", case)
            self.assertEqual(case["termination_reason"], "time_limit", case)
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
                while process_alive(pid) and time.time() < deadline:
                    time.sleep(0.05)
                self.assertFalse(process_alive(pid), f"interactive process {pid} survived cancellation")

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
            managed = spawn_managed([sys.executable, str(parent_script)])
            deadline = time.time() + 5
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "child process did not start")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(process_alive(child_pid))
            managed.terminate()
            deadline = time.time() + 5
            while process_alive(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_alive(child_pid), f"child process {child_pid} survived")


if __name__ == "__main__":
    unittest.main()
