import json
import subprocess
import sys
from pathlib import Path

from .errors import ProbHubError


def judge_problem(root, problem_dir):
    candidates = [root / "scripts/local_judge.py", Path(__file__).resolve().parents[1] / "scripts/local_judge.py"]
    script = next((path for path in candidates if path.is_file()), None)
    if not script:
        raise ProbHubError("local_judge.py not found")
    proc = subprocess.run(
        [sys.executable, str(script), str(problem_dir), "--jsonl"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    final = events[-1] if events else {}
    ok = proc.returncode == 0 and final.get("type") == "final" and final.get("status") == "passed" and final.get("code") == "all_expectations_met"
    return {"ok": ok, "returncode": proc.returncode, "final": final, "events": events}
