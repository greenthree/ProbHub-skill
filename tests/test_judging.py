import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probhub.judging import judge_problem


class JudgingEncodingTests(unittest.TestCase):
    def test_local_judge_subprocess_is_forced_to_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "scripts" / "local_judge.py"
            script.parent.mkdir()
            script.write_text("", encoding="utf-8")
            message = "[+] 恭喜！所有代码均符合预期宿命"
            event = {
                "type": "final",
                "status": "passed",
                "code": "all_expectations_met",
                "message": message,
            }
            completed = type("Completed", (), {
                "returncode": 0,
                "stdout": json.dumps(event, ensure_ascii=False) + "\n",
            })()

            with patch("probhub.judging.subprocess.run", return_value=completed) as run:
                result = judge_problem(root, root / "A")

            self.assertTrue(result["ok"])
            self.assertEqual(result["final"]["message"], message)
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")


if __name__ == "__main__":
    unittest.main()
