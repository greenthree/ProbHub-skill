import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from probhub.errors import ProbHubError
from probhub.build_lock import workspace_file_lock
from probhub.io import write_yaml
from probhub.judge_qa import judge_qa_problem
from probhub.process_control import process_alive
import probhub.judge_qa_runtime as runtime
from tests.fixture_support import FIXTURE_ROOT, copy_workspace_fixture


class JudgeQAExecutionTests(unittest.TestCase):
    def copy_fixture(self, name):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return copy_workspace_fixture(name, temporary.name)

    @staticmethod
    def evidence_path(fixture):
        return fixture.problem / ".probhub" / runtime.JUDGE_QA_EVIDENCE_FILENAME

    def seed_old_evidence(self, fixture):
        path = self.evidence_path(fixture)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b'{"sentinel":"last-known-good"}\n'
        path.write_bytes(content)
        return path, content

    @staticmethod
    def seed_formal_artifacts(fixture):
        sentinels = {
            fixture.problem / "meta.json": b"old meta\n",
            fixture.problem / "problem.yaml": b"old problem config\n",
            fixture.problem / "domjudge-problem.ini": b"old domjudge config\n",
            fixture.problem / "problem.pdf": b"old problem pdf\n",
            fixture.problem / f"{fixture.problem.name}.zip": b"old package\n",
            fixture.problem / ".probhub" / "build-manifest.json": b"old manifest\n",
            fixture.root / "main.pdf": b"old contest pdf\n",
        }
        for path, content in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    @staticmethod
    def formal_workspace_bytes(fixture):
        ignored = {
            runtime.JUDGE_QA_EVIDENCE_FILENAME,
            runtime.JUDGE_QA_EVIDENCE_LOCK_FILENAME,
            "judge.lock",
        }
        return {
            path.relative_to(fixture.root).as_posix(): path.read_bytes()
            for path in fixture.root.rglob("*")
            if path.is_file()
            and not (
                path.parent.name == ".probhub"
                and (
                    path.name in ignored
                    or path.name.startswith(
                        runtime.JUDGE_QA_EVIDENCE_FILENAME + "."
                    )
                )
            )
        }

    @staticmethod
    def fake_success_execution(config):
        limits = runtime._runtime_limits(config)
        return limits, [], [], [], [], "passed"

    def assert_old_evidence_preserved(self, fixture, expected):
        self.assertEqual(self.evidence_path(fixture).read_bytes(), expected)

    def assert_evidence_has_no_stream_content(self, evidence):
        def visit(value):
            if isinstance(value, dict):
                self.assertNotIn("stdout", value)
                self.assertNotIn("stderr", value)
                self.assertNotIn("entries", value)
                if "transcript" in value:
                    self.assertLessEqual(
                        set(value["transcript"]),
                        {"bytes", "limit", "truncated"},
                    )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(evidence)
        serialized = json.dumps(evidence, ensure_ascii=False).lower()
        self.assertNotIn("transcript_entries", serialized)
        self.assertNotIn("stdout_text", serialized)
        self.assertNotIn("stderr_text", serialized)

    def run_with_compile_count(self, fixture):
        calls = []
        original = runtime._compile_program

        def counted(source, build_dir, role, cancellation):
            calls.append((Path(source).name, role))
            return original(source, build_dir, role, cancellation)

        with mock.patch.object(runtime, "_compile_program", side_effect=counted):
            result = judge_qa_problem(fixture.root, fixture.problem)
        return result, calls

    def assert_successful_evidence(self, fixture, result):
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["code"], "judge_qa_passed", result)
        self.assertTrue(result["evidence_published"], result)
        evidence_path = self.evidence_path(fixture)
        self.assertEqual(Path(result["evidence_path"]), evidence_path)
        self.assertEqual(
            json.loads(evidence_path.read_text(encoding="utf-8")),
            result["evidence"],
        )
        self.assertTrue(result["evidence"]["cleanup"]["snapshot_removed"])
        self.assert_evidence_has_no_stream_content(result["evidence"])
        self.assertEqual(
            list(evidence_path.parent.glob(f"{evidence_path.name}.*.tmp")),
            [],
        )

    def test_checker_fixture_executes_and_publishes_bounded_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        self.seed_formal_artifacts(fixture)
        before = self.formal_workspace_bytes(fixture)
        result, compile_calls = self.run_with_compile_count(fixture)

        self.assert_successful_evidence(fixture, result)
        self.assertEqual(
            Counter(role for _, role in compile_calls),
            Counter({"validator": 1, "checker": 1}),
        )
        self.assertEqual(len(compile_calls), len(set(compile_calls)), compile_calls)
        self.assertEqual(
            {case["id"] for case in result["cases"]},
            {"accepts-alternative", "rejects-extra-token"},
        )
        self.assertTrue(all(case["matched"] for case in result["cases"]), result)
        self.assertEqual(
            {probe["id"] for probe in result["probes"]},
            {"empty", "truncated", "extra-token", "oversized"},
        )
        self.assertEqual(
            result["evidence"]["limits"]["checker_timeout"],
            runtime.MIN_CHECKER_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            result["evidence"]["limits"]["validator"],
            {
                "timeout": runtime.VALIDATOR_TIMEOUT_SECONDS,
                "memory": runtime.VALIDATOR_MEMORY_LIMIT_MB,
                "output_bytes": runtime.VALIDATOR_OUTPUT_LIMIT_BYTES,
                "processes": runtime.DEFAULT_PROCESS_LIMIT,
            },
        )
        self.assertTrue(all(probe["matched"] for probe in result["probes"]), result)
        self.assertTrue(all(
            "manual_review_required" in probe for probe in result["probes"]
        ))
        self.assertEqual(self.formal_workspace_bytes(fixture), before)

    def test_cpp_checker_qa_runs_from_unicode_and_space_path(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "中文 workspace"
            fixture = copy_workspace_fixture("checker-qa", workspace)
            temporary_root = fixture.root / "中文 snapshot root"
            original_mkdtemp = tempfile.mkdtemp
            calls = 0

            def make_temporary(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    temporary_root.mkdir()
                    return str(temporary_root)
                return original_mkdtemp(*args, **kwargs)

            with mock.patch.object(
                runtime.tempfile,
                "mkdtemp",
                side_effect=make_temporary,
            ):
                result = judge_qa_problem(fixture.root, fixture.problem)

            self.assert_successful_evidence(fixture, result)
            self.assertFalse(temporary_root.exists())

    def test_probe_ac_is_preserved_as_manual_review_not_automatic_safety(self):
        fixture = self.copy_fixture("checker-qa")
        report = runtime.inspect_judge_qa(fixture.problem, fixture.config())
        for case in report["cases"]:
            case["expected"]["status"] = "AC"
        checker_ac = {
            "verdict": "AC",
            "execution_status": "completed",
            "failure_kind": None,
            "actor": "session",
            "termination_reason": "completed",
            "returncode": 0,
            "time": 0.001,
            "memory": 1.0,
            "memory_enforced": True,
            "process_limit_enforced": True,
            "output_bytes": 0,
            "retained_output_bytes": 0,
            "output_truncated": False,
            "cleanup": {"ok": True, "errors": []},
        }
        cancellation = runtime._RunCancellation(10, None)
        with mock.patch.object(
            runtime, "run_checker_to_files", return_value=checker_ac
        ):
            cases, probes, status = runtime._run_checker_qa(
                fixture.problem,
                report,
                ["checker"],
                runtime._runtime_limits(fixture.config()),
                cancellation,
            )
        self.assertEqual(status, "passed", (cases, probes))
        self.assertTrue(probes)
        self.assertTrue(all(item["manual_review_required"] for item in probes))

    def test_resource_and_control_failures_cannot_satisfy_checker_expectations(self):
        case = {"id": "fault", "purpose": "fault", "expected": {"status": "WA"}}
        for execution_status, failure_kind in (
            ("time_limit", "resource_limit"),
            ("memory_limit", "resource_limit"),
            ("output_limit", "resource_limit"),
            ("process_limit", "resource_limit"),
            ("completed", "judge_failure"),
            ("start_error", "startup_failure"),
            ("output_control_error", "control_failure"),
        ):
            summary = runtime._checker_summary(case, {
                "verdict": None,
                "execution_status": execution_status,
                "failure_kind": failure_kind,
                "actor": "checker",
                "termination_reason": execution_status,
                "cleanup": {"ok": True, "errors": []},
            })
            self.assertTrue(summary["infrastructure_failed"], summary)
            self.assertFalse(summary["matched"], summary)

    def test_checker_timeout_matches_formal_judge_policy(self):
        fixture = self.copy_fixture("checker-qa")
        report = runtime.inspect_judge_qa(fixture.problem, fixture.config())
        report["cases"] = [report["cases"][0]]
        report["robustness"] = None
        checker_ac = {
            "verdict": "AC",
            "execution_status": "completed",
            "failure_kind": None,
            "actor": "session",
            "termination_reason": "completed",
            "cleanup": {"ok": True, "errors": []},
        }

        for configured_time, expected_timeout in ((1, 5.0), (20, 20.0)):
            with self.subTest(configured_time=configured_time):
                config = fixture.config()
                config["limits"]["time"] = configured_time
                limits = runtime._runtime_limits(config)
                with mock.patch.object(
                    runtime,
                    "run_checker_to_files",
                    return_value=checker_ac,
                ) as checker:
                    cases, probes, status = runtime._run_checker_qa(
                        fixture.problem,
                        report,
                        ["checker"],
                        limits,
                        runtime._RunCancellation(60, None),
                    )
                self.assertEqual(status, "passed", (cases, probes))
                actual_timeout = checker.call_args.kwargs["timeout"]
                self.assertGreater(actual_timeout, expected_timeout - 0.1)
                self.assertLessEqual(actual_timeout, expected_timeout)
                self.assertEqual(limits["checker_timeout"], expected_timeout)

    def test_interactor_failures_remain_infrastructure_but_contestant_limits_do_not(self):
        case = {"id": "fault", "purpose": "fault", "expected": {"status": "OLE"}}
        for actor in ("interactor", "supervisor"):
            summary = runtime._interactor_summary(case, {
                "status": "FAIL",
                "actor": actor,
                "execution_status": "output_limit",
                "failure_kind": "resource_limit",
                "termination_reason": "output_limit",
                "cleanup": {"ok": True, "errors": []},
            })
            self.assertTrue(summary["infrastructure_failed"], summary)
            self.assertFalse(summary["matched"], summary)
        contestant = runtime._interactor_summary(case, {
            "status": "OLE",
            "actor": "contestant",
            "execution_status": "output_limit",
            "failure_kind": "resource_limit",
            "termination_reason": "output_limit",
            "cleanup": {"ok": True, "errors": []},
        })
        self.assertFalse(contestant["infrastructure_failed"], contestant)
        self.assertTrue(contestant["matched"], contestant)

    def test_interactor_fixture_executes_and_compiles_unique_contestant_once(self):
        fixture = self.copy_fixture("interactor-qa")
        result, compile_calls = self.run_with_compile_count(fixture)

        self.assert_successful_evidence(fixture, result)
        self.assertEqual(
            Counter(role for _, role in compile_calls),
            Counter({"validator": 1, "interactor": 1, "contestant": 1}),
        )
        self.assertEqual(len(compile_calls), len(set(compile_calls)), compile_calls)
        self.assertEqual(
            {case["id"] for case in result["cases"]},
            {
                "normal-protocol",
                "early-eof-player",
                "idle-player",
                "output-flood-player",
            },
        )
        self.assertTrue(all(case["matched"] for case in result["cases"]), result)
        self.assertEqual(
            result["evidence"]["limits"]["output_bytes"],
            1024 * 1024,
        )
        self.assertTrue(all(
            case["transcript"]["limit"] <= runtime.MAX_JUDGE_QA_TRANSCRIPT_BYTES
            for case in result["cases"]
        ))

    def test_interactor_fixture_executes_python_contestant_source(self):
        fixture = self.copy_fixture("interactor-qa")
        python_source = fixture.problem / "code" / "judge-qa" / "normal.py"
        python_source.write_text(
            "import sys\n"
            "value = int(sys.stdin.readline())\n"
            "print(2 * value, flush=True)\n",
            encoding="utf-8",
        )
        config = fixture.config()
        config["judge"]["qa"]["cases"] = [config["judge"]["qa"]["cases"][0]]
        config["judge"]["qa"]["cases"][0]["contestant"]["source"] = (
            "code/judge-qa/normal.py"
        )
        write_yaml(fixture.config_path, config)

        result, compile_calls = self.run_with_compile_count(fixture)

        self.assert_successful_evidence(fixture, result)
        self.assertIn(("normal.py", "contestant"), compile_calls)
        contestant = next(
            item for item in result["evidence"]["compilers"]
            if item["role"] == "contestant"
        )
        self.assertEqual(contestant["kind"], "python", contestant)

    def test_runtime_caps_output_and_transcript_without_changing_problem_limits(self):
        fixture = self.copy_fixture("interactor-qa")
        config = fixture.config()
        config["limits"]["output"] = 1024
        config["judge"]["interactive"]["idle_limit"] = 0.01
        config["judge"]["interactive"]["transcript_limit"] = 1024 * 1024 * 1024
        limits = runtime._runtime_limits(config)
        self.assertEqual(limits["output_bytes"], runtime.MAX_JUDGE_QA_OUTPUT_BYTES)
        self.assertEqual(limits["configured_output_bytes"], 1024 * 1024 * 1024)

        captured = []

        def fake_session(*_args, **kwargs):
            captured.append(kwargs)
            return {
                "status": "AC",
                "actor": "session",
                "execution_status": "completed",
                "failure_kind": None,
                "termination_reason": "completed",
                "cleanup": {"ok": True, "errors": []},
                "transcript_bytes": 0,
                "transcript_truncated": False,
            }

        report = runtime.inspect_judge_qa(fixture.problem, fixture.config())
        report["cases"] = [report["cases"][0]]
        with mock.patch.object(
            runtime, "execute_interactive_session", side_effect=fake_session
        ):
            cases, status = runtime._run_interactor_qa(
                fixture.problem,
                report,
                ["interactor"],
                {"code/judge-qa/normal.cpp": ["contestant"]},
                config,
                limits,
                runtime._RunCancellation(10, None),
            )
        self.assertEqual(status, "passed", cases)
        self.assertEqual(captured[0]["output_limit_bytes"], runtime.MAX_JUDGE_QA_OUTPUT_BYTES)
        self.assertEqual(
            captured[0]["transcript_limit"], runtime.MAX_JUDGE_QA_TRANSCRIPT_BYTES
        )
        self.assertEqual(captured[0]["idle_limit"], runtime.MIN_JUDGE_QA_IDLE_SECONDS)
        effective_limits = dict(limits)
        effective_limits.update(runtime._interactive_limits(config, limits))
        evidence = runtime._build_evidence(
            ("source", "data", "fixture"),
            effective_limits,
            [],
            [],
            cases,
            [],
            0.1,
            12.5,
        )
        self.assertEqual(evidence["limits"]["configured_idle_limit"], 0.01)
        self.assertEqual(
            evidence["limits"]["idle_limit"], runtime.MIN_JUDGE_QA_IDLE_SECONDS
        )
        self.assertEqual(
            evidence["limits"]["configured_transcript_limit"],
            1024 * 1024 * 1024,
        )
        self.assertEqual(
            evidence["limits"]["transcript_limit"],
            runtime.MAX_JUDGE_QA_TRANSCRIPT_BYTES,
        )
        self.assertEqual(evidence["limits"]["overall_timeout"], 12.5)

    def test_direct_api_rejects_invalid_runtime_limits_without_publishing(self):
        mutations = (
            ("checker-qa", lambda config: config["limits"].update(time=-1)),
            ("checker-qa", lambda config: config["limits"].update(memory=128)),
            ("checker-qa", lambda config: config["limits"].update(output=0)),
            ("checker-qa", lambda config: config["limits"].update(processes=0)),
            (
                "interactor-qa",
                lambda config: config["judge"]["interactive"].update(idle_limit=-1),
            ),
            (
                "interactor-qa",
                lambda config: config["judge"]["interactive"].update(
                    transcript_limit=-1
                ),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name, mutation=mutate):
                fixture = self.copy_fixture(name)
                config = fixture.config()
                mutate(config)
                write_yaml(fixture.config_path, config)
                _, old = self.seed_old_evidence(fixture)
                result = judge_qa_problem(fixture.root, fixture.problem)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["code"], "judge_qa_limits_invalid", result)
                self.assert_old_evidence_preserved(fixture, old)

    def test_not_configured_is_successful_and_does_not_publish(self):
        fixture = self.copy_fixture("custom")
        result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["applicable"], result)
        self.assertEqual(result["status"], "not-configured", result)
        self.assertFalse(self.evidence_path(fixture).exists())

    def test_expectation_mismatch_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        config["judge"]["qa"]["cases"][1]["expected"]["status"] = "AC"
        write_yaml(fixture.config_path, config)
        _, old = self.seed_old_evidence(fixture)
        self.seed_formal_artifacts(fixture)
        before = self.formal_workspace_bytes(fixture)

        result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "expectation-failed", result)
        self.assertEqual(result["code"], "judge_qa_expectation_failed", result)
        mismatched = {case["id"]: case for case in result["cases"]}
        self.assertFalse(mismatched["rejects-extra-token"]["matched"], result)
        self.assert_old_evidence_preserved(fixture, old)
        self.assertEqual(self.formal_workspace_bytes(fixture), before)

    def test_validator_rejection_is_infrastructure_failure_and_preserves_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        rejected = fixture.problem / "code" / "validator-reject.cpp"
        shutil.copy2(FIXTURE_ROOT / "faults" / "validator-reject.cpp", rejected)
        config = fixture.config()
        config["judge"]["validator"] = "code/validator-reject.cpp"
        write_yaml(fixture.config_path, config)
        _, old = self.seed_old_evidence(fixture)

        result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_validator_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_validator_execution_matches_formal_judge_policy(self):
        fixture = self.copy_fixture("checker-qa")
        report = runtime.inspect_judge_qa(fixture.problem, fixture.config())
        validator_ok = {
            "reason": "completed",
            "returncode": 0,
            "time": 0.001,
            "message": "",
        }

        with mock.patch.object(
            runtime,
            "run_managed_to_files",
            return_value=validator_ok,
        ) as execute:
            results = runtime._validate_inputs(
                ["validator"],
                fixture.problem,
                [report["cases"][0]],
                runtime._RunCancellation(60, None),
            )

        self.assertTrue(results[0]["ok"], results)
        call = execute.call_args.kwargs
        self.assertGreater(
            call["timeout"], runtime.VALIDATOR_TIMEOUT_SECONDS - 0.1
        )
        self.assertLessEqual(call["timeout"], runtime.VALIDATOR_TIMEOUT_SECONDS)
        self.assertEqual(call["memory_limit_mb"], runtime.VALIDATOR_MEMORY_LIMIT_MB)
        self.assertEqual(
            call["output_limit_bytes"], runtime.VALIDATOR_OUTPUT_LIMIT_BYTES
        )
        self.assertEqual(call["process_limit"], runtime.DEFAULT_PROCESS_LIMIT)

    def test_checker_fail_is_infrastructure_failure_and_preserves_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        failing = fixture.problem / "code" / "checker-fail.cpp"
        shutil.copy2(FIXTURE_ROOT / "faults" / "checker-fail.cpp", failing)
        config = fixture.config()
        config["judge"]["checker"] = "code/checker-fail.cpp"
        write_yaml(fixture.config_path, config)
        _, old = self.seed_old_evidence(fixture)

        result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_infrastructure_failed", result)
        self.assertTrue(result["cases"][0]["infrastructure_failed"], result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_cancel_before_execution_preserves_old_evidence(self):
        fixture = self.copy_fixture("interactor-qa")
        _, old = self.seed_old_evidence(fixture)
        self.seed_formal_artifacts(fixture)
        before = self.formal_workspace_bytes(fixture)

        result = judge_qa_problem(
            fixture.root,
            fixture.problem,
            cancel_check=lambda: True,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["code"], "cancelled", result)
        self.assert_old_evidence_preserved(fixture, old)
        self.assertEqual(self.formal_workspace_bytes(fixture), before)

    def test_running_cpp_checker_cancel_file_cleans_tree_and_snapshot(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)
        parent_pid = fixture.root / "checker-parent.pid"
        child_pid = fixture.root / "checker-child.pid"
        cancel_file = fixture.root / "cancel.requested"
        checker = fixture.problem / "code" / "checker-cancellable.cpp"
        checker.write_text(
            '#include "testlib.h"\n'
            "#include <chrono>\n"
            "#include <cstdlib>\n"
            "#include <fstream>\n"
            "#include <string>\n"
            "#include <thread>\n"
            "#ifdef _WIN32\n"
            "#include <process.h>\n"
            "#define GETPID _getpid\n"
            "#else\n"
            "#include <sys/wait.h>\n"
            "#include <unistd.h>\n"
            "#define GETPID getpid\n"
            "#endif\n"
            f'const char* parent_path = R"PID({parent_pid.as_posix()})PID";\n'
            f'const char* child_path = R"PID({child_pid.as_posix()})PID";\n'
            "int main(int argc, char** argv) {\n"
            '    if (argc == 2 && std::string(argv[1]) == "--child") {\n'
            "        std::ofstream(child_path) << GETPID();\n"
            "        std::this_thread::sleep_for(std::chrono::seconds(30));\n"
            "        return 0;\n"
            "    }\n"
            "    std::ofstream(parent_path) << GETPID();\n"
            "    registerTestlibCmd(argc, argv);\n"
            "#ifdef _WIN32\n"
            "    return static_cast<int>(_spawnl(_P_WAIT, argv[0], argv[0], \"--child\", nullptr));\n"
            "#else\n"
            "    pid_t child = fork();\n"
            "    if (child == 0) {\n"
            "        execl(argv[0], argv[0], \"--child\", nullptr);\n"
            "        _exit(127);\n"
            "    }\n"
            "    if (child < 0) return 1;\n"
            "    int status = 0;\n"
            "    return waitpid(child, &status, 0) < 0 ? 1 : status;\n"
            "#endif\n"
            "}\n",
            encoding="utf-8",
        )
        config = fixture.config()
        config["judge"]["checker"] = "code/checker-cancellable.cpp"
        write_yaml(fixture.config_path, config)
        temporary_root = fixture.root / "controlled-running-cancel-snapshot"
        original_mkdtemp = tempfile.mkdtemp
        calls = 0

        def make_temporary(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                temporary_root.mkdir()
                return str(temporary_root)
            return original_mkdtemp(*args, **kwargs)

        def request_cancel():
            deadline = time.time() + 60
            while time.time() < deadline:
                if parent_pid.is_file():
                    child_deadline = time.time() + 3
                    while time.time() < child_deadline and not child_pid.is_file():
                        time.sleep(0.02)
                    cancel_file.write_text("cancel\n", encoding="utf-8")
                    return
                time.sleep(0.02)

        requester = threading.Thread(target=request_cancel, daemon=True)
        requester.start()
        with (
            mock.patch.dict(
                os.environ,
                {runtime.CANCEL_FILE_ENV: str(cancel_file)},
            ),
            mock.patch.object(
                runtime.tempfile,
                "mkdtemp",
                side_effect=make_temporary,
            ),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)
        requester.join(timeout=60)

        self.assertFalse(requester.is_alive(), "cancel requester did not finish")
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["code"], "cancelled", result)
        self.assertTrue(parent_pid.is_file(), "checker did not start")
        self.assertTrue(child_pid.is_file(), "checker descendant did not start")
        for path in (parent_pid, child_pid):
            pid = int(path.read_text(encoding="utf-8"))
            deadline = time.time() + 10
            while time.time() < deadline and process_alive(pid):
                time.sleep(0.05)
            self.assertFalse(process_alive(pid), f"process {pid} survived cancellation")
        self.assertFalse(temporary_root.exists())
        self.assert_old_evidence_preserved(fixture, old)

    def test_judge_and_evidence_locks_fail_closed_without_publishing(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)

        with workspace_file_lock(fixture.problem, ".probhub/judge.lock"):
            judge_busy = judge_qa_problem(fixture.root, fixture.problem)
        self.assertEqual(judge_busy["code"], "judge_busy", judge_busy)
        self.assert_old_evidence_preserved(fixture, old)

        config = fixture.config()
        with (
            workspace_file_lock(
                fixture.problem,
                Path(".probhub") / runtime.JUDGE_QA_EVIDENCE_LOCK_FILENAME,
                no_follow=True,
            ),
            mock.patch.object(
                runtime,
                "_execute_snapshot",
                return_value=self.fake_success_execution(config),
            ),
        ):
            evidence_busy = judge_qa_problem(fixture.root, fixture.problem)
        self.assertEqual(
            evidence_busy["code"], "judge_qa_evidence_busy", evidence_busy
        )
        self.assert_old_evidence_preserved(fixture, old)

    def test_default_cancel_file_is_honored_and_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)
        cancel_file = fixture.root / "cancel.requested"
        cancel_file.write_text("cancel\n", encoding="utf-8")

        with mock.patch.dict(
            os.environ,
            {runtime.CANCEL_FILE_ENV: str(cancel_file)},
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["code"], "cancelled", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_cancel_callback_failure_is_structured_and_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)

        def fail_cancel_check():
            raise RuntimeError("injected cancellation callback failure")

        result = judge_qa_problem(
            fixture.root,
            fixture.problem,
            cancel_check=fail_cancel_check,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_cancel_check_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_cancel_after_execution_but_before_publish_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        _, old = self.seed_old_evidence(fixture)
        checks = 0

        def cancel_at_publish():
            nonlocal checks
            checks += 1
            return checks >= 3

        with mock.patch.object(
            runtime,
            "_execute_snapshot",
            return_value=self.fake_success_execution(config),
        ):
            result = judge_qa_problem(
                fixture.root,
                fixture.problem,
                cancel_check=cancel_at_publish,
            )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertGreaterEqual(checks, 3)
        self.assert_old_evidence_preserved(fixture, old)

    def test_overall_timeout_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)

        result = judge_qa_problem(fixture.root, fixture.problem, timeout=0)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_overall_timeout", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_running_overall_timeout_cleans_descendants_and_snapshot(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)
        parent_pid = fixture.root / "timeout-parent.pid"
        child_pid = fixture.root / "timeout-child.pid"
        checker_script = fixture.root / "timeout-checker.py"
        child_code = (
            "import os,pathlib,sys,time;"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8');"
            "time.sleep(30)"
        )
        checker_script.write_text(
            "import os, pathlib, subprocess, sys, time\n"
            f"pathlib.Path({str(parent_pid)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            f"subprocess.Popen([sys.executable, '-I', '-c', {child_code!r}, {str(child_pid)!r}])\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        controlled_snapshot = fixture.root / "controlled-timeout-snapshot"
        original_mkdtemp = tempfile.mkdtemp
        calls = 0

        def make_temporary(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                controlled_snapshot.mkdir()
                return str(controlled_snapshot)
            return original_mkdtemp(*args, **kwargs)

        def fake_compile(_source, _build_dir, role, _cancellation):
            if role == "validator":
                return [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"], {
                    "role": role,
                    "source": "test-validator.cpp",
                    "kind": "test-double",
                }
            if role == "checker":
                return [sys.executable, "-I", str(checker_script)], {
                    "role": role,
                    "source": "test-checker.cpp",
                    "kind": "test-double",
                }
            self.fail(f"unexpected compile role: {role}")

        with (
            mock.patch.object(runtime, "_compile_program", side_effect=fake_compile),
            mock.patch.object(runtime.tempfile, "mkdtemp", side_effect=make_temporary),
        ):
            result = judge_qa_problem(
                fixture.root,
                fixture.problem,
                timeout=1.5,
            )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_overall_timeout", result)
        self.assertTrue(parent_pid.is_file(), "checker did not start before deadline")
        self.assertTrue(child_pid.is_file(), "checker descendant did not start before deadline")
        for path in (parent_pid, child_pid):
            pid = int(path.read_text(encoding="utf-8"))
            deadline = time.time() + 10
            while time.time() < deadline and process_alive(pid):
                time.sleep(0.05)
            self.assertFalse(process_alive(pid), f"process {pid} survived overall timeout")
        self.assertFalse(controlled_snapshot.exists())
        self.assert_old_evidence_preserved(fixture, old)

    def test_timeout_above_supported_maximum_is_rejected(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)

        result = judge_qa_problem(
            fixture.root,
            fixture.problem,
            timeout=runtime.MAX_JUDGE_QA_TIMEOUT_SECONDS + 1,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_timeout_invalid", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_snapshot_is_removed_after_success(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        temporary_root = fixture.root / "controlled-judge-qa-tmp"

        def make_temporary(*_args, **_kwargs):
            temporary_root.mkdir()
            return str(temporary_root)

        with (
            mock.patch.object(runtime.tempfile, "mkdtemp", side_effect=make_temporary),
            mock.patch.object(
                runtime,
                "_execute_snapshot",
                return_value=self.fake_success_execution(config),
            ),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertTrue(result["ok"], result)
        self.assertFalse(temporary_root.exists())

    def test_snapshot_cleanup_failure_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        _, old = self.seed_old_evidence(fixture)
        original_remove = runtime._remove_snapshot

        def remove_then_fail(path):
            original_remove(path)
            raise OSError("injected cleanup failure")

        with (
            mock.patch.object(
                runtime,
                "_execute_snapshot",
                return_value=self.fake_success_execution(config),
            ),
            mock.patch.object(runtime, "_remove_snapshot", side_effect=remove_then_fail),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_snapshot_cleanup_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_snapshot_cleanup_failure_overrides_execution_cancellation(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)
        original_remove = runtime._remove_snapshot

        def remove_then_fail(path):
            original_remove(path)
            raise OSError("injected cleanup failure")

        with (
            mock.patch.object(
                runtime,
                "_create_snapshot",
                side_effect=runtime.ProcessCancelled("injected cancellation"),
            ),
            mock.patch.object(runtime, "_remove_snapshot", side_effect=remove_then_fail),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_snapshot_cleanup_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_snapshot_includes_qa_files_excluded_from_general_code_hash(self):
        fixture = self.copy_fixture("interactor-qa")
        ignored_suffix = fixture.problem / "code" / "judge-qa" / "helper.exe"
        ignored_suffix.write_bytes(b"prebuilt identity sentinel")
        config = fixture.config()
        report = runtime.inspect_judge_qa(fixture.problem, config)
        identity = (
            runtime.compute_source_hash(fixture.problem, config),
            runtime.compute_data_hash(fixture.problem, config),
            report["fixture_hash"],
        )
        destination = fixture.root / "snapshot-with-ignored-suffix"
        self.addCleanup(lambda: shutil.rmtree(destination, ignore_errors=True))
        runtime._create_snapshot(
            fixture.problem,
            destination,
            config,
            report,
            identity,
            runtime._RunCancellation(10, None),
        )
        copied = destination / "code" / "judge-qa" / "helper.exe"
        self.assertEqual(copied.read_bytes(), b"prebuilt identity sentinel")

    def test_snapshot_copy_checks_cancellation_between_chunks(self):
        fixture = self.copy_fixture("checker-qa")
        source = fixture.problem / "judge-fixtures" / "checker" / "large.bin"
        source.write_bytes(b"x" * (2 * 1024 * 1024))
        destination = fixture.root / "cancelled-copy.bin"

        class CancelDuringCopy:
            calls = 0

            def remaining(self):
                self.calls += 1
                if self.calls >= 2:
                    raise runtime.ProcessCancelled("cancelled during copy")
                return 1.0

        cancellation = CancelDuringCopy()
        with self.assertRaises(runtime.ProcessCancelled):
            runtime._copy_regular_file(
                source,
                destination,
                fixture.problem.resolve(),
                cancellation,
            )
        self.assertGreaterEqual(cancellation.calls, 2)

    def test_snapshot_failure_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)

        with mock.patch.object(
            runtime,
            "_create_snapshot",
            side_effect=ProbHubError("injected snapshot failure", code="snapshot_failed"),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["code"], "snapshot_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_compile_failure_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)
        with mock.patch.object(
            runtime,
            "_compile_program",
            side_effect=ProbHubError("injected compile failure", code="compile_failed"),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["code"], "compile_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_compiler_identity_failure_is_fail_closed(self):
        cancellation = runtime._RunCancellation(10, None)
        with mock.patch.object(runtime, "run_managed_to_files", return_value={
            "reason": "start_error",
            "returncode": None,
            "message": "injected identity failure",
        }):
            with self.assertRaises(ProbHubError) as caught:
                runtime._compiler_identity(cancellation)
        self.assertEqual(caught.exception.code, "compiler_identity_failed")

    def test_hash_fence_rejects_live_change_and_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        _, old = self.seed_old_evidence(fixture)

        def mutate_live_then_succeed(*_args, **_kwargs):
            statement = fixture.problem / "problem.md"
            statement.write_text(
                statement.read_text(encoding="utf-8") + "\nchanged during Judge QA\n",
                encoding="utf-8",
            )
            return self.fake_success_execution(config)

        with mock.patch.object(
            runtime,
            "_execute_snapshot",
            side_effect=mutate_live_then_succeed,
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "inputs_changed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_hash_fence_overrides_snapshot_expectation_failure(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        _, old = self.seed_old_evidence(fixture)

        def mutate_live_then_fail(*_args, **_kwargs):
            statement = fixture.problem / "problem.md"
            statement.write_text(
                statement.read_text(encoding="utf-8") + "\nchanged during failure\n",
                encoding="utf-8",
            )
            limits = runtime._runtime_limits(config)
            return limits, [], [], [{"id": "failed"}], [], "expectation-failed"

        with mock.patch.object(
            runtime,
            "_execute_snapshot",
            side_effect=mutate_live_then_fail,
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "inputs_changed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_hash_fence_overrides_snapshot_execution_error(self):
        fixture = self.copy_fixture("checker-qa")
        _, old = self.seed_old_evidence(fixture)

        def mutate_live_then_raise(*_args, **_kwargs):
            statement = fixture.problem / "problem.md"
            statement.write_text(
                statement.read_text(encoding="utf-8") + "\nchanged before error\n",
                encoding="utf-8",
            )
            raise ProbHubError("old snapshot failed", code="compile_failed")

        with mock.patch.object(
            runtime,
            "_execute_snapshot",
            side_effect=mutate_live_then_raise,
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "inputs_changed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_publish_failure_preserves_old_evidence(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        _, old = self.seed_old_evidence(fixture)

        with (
            mock.patch.object(
                runtime,
                "_execute_snapshot",
                return_value=self.fake_success_execution(config),
            ),
            mock.patch.object(
                runtime,
                "atomic_write_json",
                side_effect=OSError("injected publish failure"),
            ),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "infrastructure-failed", result)
        self.assertEqual(result["code"], "judge_qa_infrastructure_failed", result)
        self.assert_old_evidence_preserved(fixture, old)

    def test_publish_lock_hash_fence_rechecks_live_identity(self):
        fixture = self.copy_fixture("checker-qa")
        config = fixture.config()
        _, old = self.seed_old_evidence(fixture)
        original_identity = runtime._live_identity
        live_checks = 0

        def change_only_at_publish(path, cancellation=None):
            nonlocal live_checks
            result = original_identity(path, cancellation)
            if Path(path).resolve() == fixture.problem.resolve():
                live_checks += 1
                if live_checks == 2:
                    identity = result[2]
                    return result[0], result[1], ("changed", identity[1], identity[2])
            return result

        with (
            mock.patch.object(
                runtime,
                "_execute_snapshot",
                return_value=self.fake_success_execution(config),
            ),
            mock.patch.object(
                runtime, "_live_identity", side_effect=change_only_at_publish
            ),
        ):
            result = judge_qa_problem(fixture.root, fixture.problem)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["code"], "inputs_changed", result)
        self.assertEqual(live_checks, 2)
        self.assert_old_evidence_preserved(fixture, old)


if __name__ == "__main__":
    unittest.main()
