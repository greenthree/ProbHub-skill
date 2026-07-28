import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .calibration import build_judge_evidence, write_calibration_evidence
from .errors import ProbHubError
from .io import read_yaml
from .linting import compute_data_hash, compute_source_hash
from .process_control import run_managed_to_files


JUDGE_TIMEOUT_SECONDS = 3600.0
JUDGE_OUTPUT_LIMIT_BYTES = 128 * 1024 * 1024


def judge_problem(root, problem_dir, use_cache=True, timeout=JUDGE_TIMEOUT_SECONDS, output_limit_bytes=JUDGE_OUTPUT_LIMIT_BYTES):
    root = Path(root)
    problem_dir = Path(problem_dir)
    initial_identity = None
    try:
        config = read_yaml(problem_dir / "probhub.yaml")
        initial_identity = (
            compute_source_hash(problem_dir, config),
            compute_data_hash(problem_dir, config),
        )
    except (FileNotFoundError, ProbHubError, OSError, ValueError, TypeError):
        config = None
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
    summaries = [event for event in events if event.get("type") == "summary"]
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
        return {
            "ok": False,
            "returncode": run["returncode"],
            "final": final,
            "cache": cache,
            "summaries": summaries,
            "calibration": None,
            "events": events,
        }
    final = events[-1] if events else {}
    ok = run["returncode"] == 0 and final.get("type") == "final" and final.get("status") == "passed" and final.get("code") == "all_expectations_met"
    evidence = None
    evidence_error = None
    evidence_published = None
    if ok and config is not None and initial_identity is not None:
        try:
            current_identity = (
                compute_source_hash(problem_dir, config),
                compute_data_hash(problem_dir, config),
            )
            if current_identity != initial_identity:
                evidence_error = "inputs_changed"
            else:
                evidence = build_judge_evidence(
                    events,
                    source_hash=initial_identity[0],
                    data_hash=initial_identity[1],
                    measured_at=datetime.now(timezone.utc).isoformat(),
                )
                if evidence is not None:
                    evidence_published = write_calibration_evidence(
                        problem_dir, evidence
                    )
        except (OSError, UnicodeError, ValueError, TypeError, ProbHubError) as exc:
            evidence = None
            evidence_error = str(exc)
    return {
        "ok": ok,
        "returncode": run["returncode"],
        "final": final,
        "cache": cache,
        "summaries": summaries,
        "calibration": evidence,
        "calibration_published": evidence_published,
        **({"calibration_error": evidence_error} if evidence_error else {}),
        "events": events,
    }
