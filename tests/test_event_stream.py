import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from probhub import cli
from probhub.events import (
    EventSink,
    PROTOCOL_SCHEMA_VERSION,
    parse_event_stream,
    parse_event_stream_line,
)
from probhub.io import write_yaml
from probhub.process_control import ProcessCancelled
from probhub.stressing import stress_problem


class EventStreamProtocolTests(unittest.TestCase):
    def test_started_frame_schema(self):
        buf = io.StringIO()
        sink = EventSink(buf)
        sink.emit_started("stress", "L01", total=100, unit="rounds", metadata={"seed": 42})
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        frame = json.loads(lines[0])
        self.assertEqual(frame["protocol_schema_version"], PROTOCOL_SCHEMA_VERSION)
        self.assertEqual(frame["type"], "started")
        self.assertEqual(frame["operation"], "stress")
        self.assertEqual(frame["problem_id"], "L01")
        self.assertEqual(frame["total"], 100)
        self.assertEqual(frame["unit"], "rounds")
        self.assertEqual(frame["metadata"], {"seed": 42})
        self.assertIsInstance(frame["timestamp"], float)

    def test_progress_throttling(self):
        buf = io.StringIO()
        sink = EventSink(buf, min_interval_seconds=0.05)
        emitted_count = 0
        for i in range(1, 50):
            if sink.emit_progress("stress", "L01", completed=i, total=100, unit="rounds"):
                emitted_count += 1
        self.assertLess(emitted_count, 10)
        self.assertGreaterEqual(emitted_count, 1)

        # Force emits regardless of time
        forced = sink.emit_progress("stress", "L01", completed=50, total=100, unit="rounds", force=True)
        self.assertTrue(forced)

        # Completion emits regardless of time
        completed_emitted = sink.emit_progress("stress", "L01", completed=100, total=100, unit="rounds")
        self.assertTrue(completed_emitted)

    def test_final_frame_schema(self):
        buf = io.StringIO()
        sink = EventSink(buf)
        sink.emit_final("stress", "L01", ok=True, status="passed", result={"passed": True})
        self.assertTrue(sink.has_emitted_final)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        frame = json.loads(lines[0])
        self.assertEqual(frame["protocol_schema_version"], 1)
        self.assertEqual(frame["type"], "final")
        self.assertEqual(frame["operation"], "stress")
        self.assertEqual(frame["problem_id"], "L01")
        self.assertTrue(frame["ok"])
        self.assertEqual(frame["status"], "passed")
        self.assertEqual(frame["result"], {"passed": True})

    def test_cancelled_frame_schema(self):
        buf = io.StringIO()
        sink = EventSink(buf)
        sink.emit_cancelled("stress", "L01", completed=45, total=100, reason="user_interrupted")
        self.assertTrue(sink.has_emitted_cancelled)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        frame = json.loads(lines[0])
        self.assertEqual(frame["protocol_schema_version"], 1)
        self.assertEqual(frame["type"], "cancelled")
        self.assertEqual(frame["operation"], "stress")
        self.assertEqual(frame["problem_id"], "L01")
        self.assertEqual(frame["completed"], 45)
        self.assertEqual(frame["total"], 100)
        self.assertEqual(frame["reason"], "user_interrupted")

    def test_parse_event_stream_line(self):
        self.assertIsNone(parse_event_stream_line(""))
        self.assertIsNone(parse_event_stream_line("   \n"))
        self.assertIsNone(parse_event_stream_line("non json text"))
        self.assertIsNone(parse_event_stream_line("[1, 2, 3]"))
        self.assertIsNone(parse_event_stream_line('{"foo": "bar"}'))
        self.assertIsNone(parse_event_stream_line('{"protocol_schema_version": 2, "type": "started"}'))
        self.assertIsNone(parse_event_stream_line('{"protocol_schema_version": 1}'))

        valid = '{"protocol_schema_version": 1, "type": "started", "operation": "stress"}'
        parsed = parse_event_stream_line(valid)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["type"], "started")

    def test_parse_event_stream_iterable(self):
        lines = [
            "starting compiler...",
            '{"protocol_schema_version": 1, "type": "started", "operation": "stress"}',
            "random log line",
            '{"protocol_schema_version": 1, "type": "progress", "completed": 1}',
            "another log line",
            '{"protocol_schema_version": 1, "type": "final", "ok": true}',
        ]
        frames = list(parse_event_stream(lines))
        self.assertEqual(len(frames), 3)
        self.assertEqual([f["type"] for f in frames], ["started", "progress", "final"])


class StressEventStreamIntegrationTests(unittest.TestCase):
    def create_workspace(self, root):
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
        })
        problem = root / "A"
        code = problem / "code"
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        code.mkdir(parents=True)
        (problem / "problem.md").write_text("# Stress\n\n## 题目描述\n\nD.\n", encoding="utf-8")
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("2\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("2\n", encoding="utf-8")
        (code / "generator.py").write_text("import sys\nseed=int(sys.argv[1])\nprint(seed)\n", encoding="utf-8")
        (code / "validator.py").write_text("import sys\nint(sys.stdin.read().strip())\n", encoding="utf-8")
        (code / "std.py").write_text("import sys\nprint(int(sys.stdin.read()))\n", encoding="utf-8")
        (code / "brute.py").write_text("import sys\nprint(int(sys.stdin.read()))\n", encoding="utf-8")
        config = {
            "schema_version": 1,
            "id": "A",
            "name": "Stress",
            "limits": {"time": 1, "memory": 256},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.py"},
            "solutions": {"std": "code/std.py"},
            "stress": {
                "generator": "code/generator.py",
                "validator": "code/validator.py",
                "accepted": "code/std.py",
                "brute": "code/brute.py",
                "rounds": 5,
            },
        }
        write_yaml(problem / "probhub.yaml", config)
        return problem, config

    def test_stress_event_stream_direct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            problem_dir, config = self.create_workspace(root)
            buf = io.StringIO()
            sink = EventSink(buf, min_interval_seconds=0.0)
            res = stress_problem(root, problem_dir, config, rounds=5, master_seed=123, event_sink=sink)
            self.assertTrue(res["ok"])
            lines = [l for l in buf.getvalue().splitlines() if l.strip()]
            frames = [json.loads(l) for l in lines]
            self.assertGreaterEqual(len(frames), 2)
            self.assertEqual(frames[0]["type"], "started")
            self.assertEqual(frames[0]["total"], 5)
            self.assertEqual(frames[-1]["type"], "final")
            self.assertTrue(frames[-1]["ok"])
            self.assertEqual(frames[-1]["result"]["status"], "passed")

    def test_stress_event_stream_cli_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.create_workspace(root)

            # Test global flag position: probhub --event-stream stress A --rounds 3 --seed 10
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                parser = cli.build_parser()
                args = parser.parse_args([
                    "--workspace", str(root),
                    "--event-stream",
                    "stress", "A",
                    "--rounds", "3",
                    "--seed", "10",
                ])
                result = args.handler(args)
                cli.emit_result(result, args)

            lines = [l for l in captured.getvalue().splitlines() if l.strip()]
            self.assertGreaterEqual(len(lines), 2)
            for line in lines:
                parsed = parse_event_stream_line(line)
                self.assertIsNotNone(parsed, f"Line was not valid event frame: {line}")
            frames = [json.loads(l) for l in lines]
            self.assertEqual(frames[0]["type"], "started")
            self.assertEqual(frames[-1]["type"], "final")

            # Test subparser flag position: probhub stress A --rounds 3 --seed 10 --event-stream
            captured2 = io.StringIO()
            with mock.patch("sys.stdout", captured2):
                parser = cli.build_parser()
                args2 = parser.parse_args([
                    "--workspace", str(root),
                    "stress", "A",
                    "--rounds", "3",
                    "--seed", "10",
                    "--event-stream",
                ])
                result2 = args2.handler(args2)
                cli.emit_result(result2, args2)

            lines2 = [l for l in captured2.getvalue().splitlines() if l.strip()]
            self.assertGreaterEqual(len(lines2), 2)
            frames2 = [json.loads(l) for l in lines2]
            self.assertEqual(frames2[0]["type"], "started")
            self.assertEqual(frames2[-1]["type"], "final")

    def test_legacy_json_mode_remains_single_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.create_workspace(root)

            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                parser = cli.build_parser()
                args = parser.parse_args([
                    "--workspace", str(root),
                    "--json",
                    "stress", "A",
                    "--rounds", "3",
                    "--seed", "10",
                ])
                result = args.handler(args)
                cli.emit_result(result, args)

            output = captured.getvalue().strip()
            # Must parse as a single JSON dict, not NDJSON frames
            parsed = json.loads(output)
            self.assertIn("problems", parsed)
            self.assertNotIn("protocol_schema_version", parsed)

    def test_stream_emits_final_frame_on_cli_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.create_workspace(root)

            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                code = cli.main([
                    "--workspace", str(root),
                    "--format", "stream",
                    "judge", "DOES_NOT_EXIST",
                ])

            self.assertEqual(code, 1)
            lines = [l for l in captured.getvalue().splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            frame = parse_event_stream_line(lines[0])
            self.assertIsNotNone(frame)
            self.assertEqual(frame["type"], "final")
            self.assertFalse(frame["ok"])
            self.assertEqual(frame["problem_id"], "DOES_NOT_EXIST")
            self.assertEqual(frame["status"], "failed")
            self.assertIn("unknown problem", frame["result"]["error"])

    def test_stream_emits_final_frame_on_argument_error(self):
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured), mock.patch("sys.stderr", io.StringIO()):
            try:
                cli.main(["--format", "stream", "invalid_subcommand"])
            except SystemExit as exc:
                self.assertEqual(exc.code, 2)

        lines = [l for l in captured.getvalue().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1)
        frame = parse_event_stream_line(lines[0])
        self.assertIsNotNone(frame)
        self.assertEqual(frame["type"], "final")
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["code"], "invalid_arguments")

    def test_event_sink_callback_reentrancy_no_deadlock(self):
        reentrant_calls = []

        def callback(event):
            reentrant_calls.append(sink.has_emitted_terminal)

        sink = EventSink(stream=io.StringIO(), callback=callback)
        sink.emit_started("stress", "A")
        sink.emit_final("stress", "A", ok=True, status="passed")
        self.assertEqual(len(reentrant_calls), 2)
        self.assertFalse(reentrant_calls[0])
        self.assertTrue(reentrant_calls[1])


if __name__ == "__main__":
    unittest.main()
