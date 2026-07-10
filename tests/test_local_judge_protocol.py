import json
import subprocess
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
