import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from probhub.build_lock import workspace_file_lock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_judge.py"


class LocalJudgeProtocolTests(unittest.TestCase):
    def test_invalid_arguments_emit_versioned_final_event(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--jsonl"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(proc.returncode, 0)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(events)
        final = events[-1]
        self.assertEqual(final["protocol"], "probhub.local_judge")
        self.assertEqual(final["protocol_version"], 1)
        self.assertEqual(final["type"], "final")
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["code"], "validation_failed")
        self.assertFalse(final["ok"])
        self.assertEqual(final["exit_code"], proc.returncode)

    def test_second_process_for_same_problem_reports_judge_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            with workspace_file_lock(problem, ".probhub/judge.lock"):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), str(problem), "--jsonl"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                )
        self.assertNotEqual(proc.returncode, 0)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["code"], "judge_busy")
        self.assertFalse(events[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
