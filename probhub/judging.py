import json
import os
import sys
import tempfile
from pathlib import Path

from .errors import ProbHubError
from .process_control import run_managed_to_files


JUDGE_TIMEOUT_SECONDS = 3600.0
JUDGE_OUTPUT_LIMIT_BYTES = 128 * 1024 * 1024


def judge_problem(root, problem_dir, use_cache=True, timeout=JUDGE_TIMEOUT_SECONDS, output_limit_bytes=JUDGE_OUTPUT_LIMIT_BYTES):
    candidates = [root / "scripts/local_judge.py", Path(__file__).resolve().parents[1] / "scripts/local_judge.py"]
    script = next((path for path in candidates if path.is_file()), None)
    if not script:
        raise ProbHubError("local_judge.py not found")
    command = [sys.executable, str(script), str(problem_dir), "--jsonl"]
    if not use_cache:
        command.append("--no-cache")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="probhub-judge-") as temp:
        stdout_path = Path(temp) / "stdout"
        stderr_path = Path(temp) / "stderr"
        try:
            run = run_managed_to_files(
                command,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=timeout,
                memory_limit_mb=None,
                output_limit_bytes=output_limit_bytes,
                process_limit=None,
                cwd=root,
                env=env,
            )
        except OSError as error:
            raise ProbHubError(f"failed to launch local_judge.py: {error}", code="judge_spawn_failed")
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    events = []
    for line in stdout_text.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    cache = next((event for event in reversed(events) if event.get("type") == "cache"), {})
    if run["reason"] != "completed":
        codes = {"time_limit": "judge_timeout", "output_limit": "judge_output_limit"}
        message = run.get("message") or "sandbox supervisor failed"
        if stderr_text.strip():
            message += ": " + stderr_text.strip()[-4000:]
        final = {
            "type": "final",
            "status": "failed",
            "code": codes.get(run["reason"], "judge_" + run["reason"]),
            "message": message,
        }
        return {"ok": False, "returncode": run["returncode"], "final": final, "cache": cache, "events": events}
    final = events[-1] if events else {}
    ok = run["returncode"] == 0 and final.get("type") == "final" and final.get("status") == "passed" and final.get("code") == "all_expectations_met"
    return {"ok": ok, "returncode": run["returncode"], "final": final, "cache": cache, "events": events}
