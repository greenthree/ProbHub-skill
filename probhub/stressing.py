import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .build_lock import workspace_build_lock
from .errors import ProbHubError
from .io import normalize_newlines, read_yaml, write_json, write_yaml
from .output_compare import compare_standard_output
from .process_control import DEFAULT_PROCESS_LIMIT, run_managed_to_files
from .transactions import (
    TRANSACTION_PHASE_COMMITTED,
    TRANSACTION_PHASE_PREPARED,
    read_transaction_journal,
    recover_workspace_transactions,
    resolve_journal_target,
    transaction_child,
    validate_transaction_directory,
)
from .workspace import load_workspace


DEFAULT_ROUNDS = 1000
# Keep in sync with datagen.CASE_NAME_PATTERN (importing datagen here would cycle).
_CASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DEFAULT_TOOL_TIMEOUT = 5.0


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbHubError(f"{label} must be a positive finite number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ProbHubError(f"{label} must be a positive finite number")
    return converted


def _entry_file(entry):
    return entry.get("file") if isinstance(entry, dict) else entry


def _first_solution(config, key):
    value = ((config.get("solutions") or {}).get(key))
    entries = value if isinstance(value, list) else [value] if value else []
    for entry in entries:
        path = _entry_file(entry)
        if path:
            return str(path)
    return None


def _judge_type(config):
    value = str(((config.get("judge") or {}).get("type", "standard"))).strip().lower()
    return "custom" if value == "checker" else value


def _resolve_problem_file(problem_dir, value, label):
    if not value:
        raise ProbHubError(f"stress.{label} is required")
    problem_dir = Path(problem_dir).resolve()
    path = (problem_dir / str(value)).resolve()
    try:
        path.relative_to(problem_dir)
    except ValueError as exc:
        raise ProbHubError(f"stress.{label} must stay inside the problem directory: {value}") from exc
    if not path.is_file():
        raise ProbHubError(f"stress.{label} not found: {value}")
    return path


def _stress_config(problem_dir, config, against=None):
    stress = config.get("stress")
    if not isinstance(stress, dict):
        raise ProbHubError("stress configuration is required in probhub.yaml")
    judge_type = _judge_type(config)
    if judge_type == "interactive":
        raise ProbHubError("probhub stress does not support interactive judging")
    generator_rel = stress.get("generator")
    accepted_rel = stress.get("accepted") or _first_solution(config, "accepted")
    if against is not None:
        # Killer hunt: the accepted solution is compared against the target
        # instead of the brute; a mismatch is the desired outcome.
        brute_rel = against
    else:
        brute_rel = stress.get("brute") or _first_solution(config, "brute")
    validator_rel = ((config.get("judge") or {}).get("validator"))
    checker_rel = ((config.get("judge") or {}).get("checker")) if judge_type == "custom" else None
    if judge_type == "custom" and not checker_rel:
        raise ProbHubError(
            "judge.type custom requires judge.checker before running stress"
        )
    args = stress.get("args", ["{seed}"])
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list):
        raise ProbHubError("stress.args must be a string or list")
    configured_rounds = stress.get("rounds", DEFAULT_ROUNDS)
    if not _is_int(configured_rounds) or configured_rounds <= 0:
        raise ProbHubError("stress.rounds must be a positive integer")
    configured_time_limit = (config.get("limits") or {}).get("time", 1)
    try:
        default_time_limit = max(float(configured_time_limit) * 2, 5.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProbHubError("limits.time must be numeric before running stress") from exc
    time_limit = _positive_number(
        stress.get("time_limit", default_time_limit), "stress.time_limit"
    )
    tool_timeout = _positive_number(
        stress.get("tool_timeout", DEFAULT_TOOL_TIMEOUT), "stress.tool_timeout"
    )
    limits = config.get("limits") or {}
    output_limit = limits.get("output", 64)
    process_limit = limits.get("processes", DEFAULT_PROCESS_LIMIT)
    memory_limit = limits.get("memory", 256)
    if not _is_int(output_limit) or output_limit <= 0:
        raise ProbHubError("limits.output must be a positive integer before running stress")
    if not _is_int(process_limit) or process_limit <= 0:
        raise ProbHubError("limits.processes must be a positive integer before running stress")
    if not _is_int(memory_limit) or memory_limit <= 0:
        raise ProbHubError("limits.memory must be a positive integer before running stress")
    return {
        "judge_type": judge_type,
        "generator_rel": str(generator_rel) if generator_rel else None,
        "accepted_rel": str(accepted_rel) if accepted_rel else None,
        "brute_rel": str(brute_rel) if brute_rel else None,
        "validator_rel": str(validator_rel) if validator_rel else None,
        "checker_rel": str(checker_rel) if checker_rel else None,
        "generator": _resolve_problem_file(problem_dir, generator_rel, "generator"),
        "accepted": _resolve_problem_file(problem_dir, accepted_rel, "accepted"),
        "brute": _resolve_problem_file(
            problem_dir, brute_rel, "against" if against is not None else "brute"
        ),
        "validator": _resolve_problem_file(problem_dir, validator_rel, "validator"),
        "checker": _resolve_problem_file(problem_dir, checker_rel, "checker") if checker_rel else None,
        "args": [str(item) for item in args],
        "rounds": configured_rounds,
        "time_limit": time_limit,
        "tool_timeout": tool_timeout,
        "memory_limit": memory_limit,
        "output_limit": output_limit,
        "process_limit": process_limit,
    }


def expand_generator_args(patterns, seed, round_number):
    values = {"seed": seed, "round": round_number}
    result = []
    for pattern in patterns:
        try:
            result.append(str(pattern).format(**values))
        except (IndexError, KeyError, ValueError) as exc:
            raise ProbHubError(f"invalid stress.args template: {pattern}") from exc
    return result


def _compile_cpp(source, output, role):
    references = Path(__file__).resolve().parents[1] / "references"
    command = [
        "g++", str(source), "-o", str(output), "-O2", "-std=c++17",
        "-I", str(references), "-I", str(source.parent),
    ]
    if platform.system() == "Windows":
        command.append("-static")
        if role == "validator":
            command.append("-DFOR_LINUX")
    with tempfile.TemporaryDirectory(prefix="probhub-stress-compile-") as temp:
        stdout_path = Path(temp) / "compiler.out"
        stderr_path = Path(temp) / "compiler.stderr"
        result = run_managed_to_files(
            command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=60.0,
            memory_limit_mb=2048,
            output_limit_bytes=8 * 1024 * 1024,
            process_limit=DEFAULT_PROCESS_LIMIT,
        )
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    if result["reason"] != "completed" or result["returncode"] != 0:
        detail = stderr.strip() or result["message"] or result["reason"]
        raise ProbHubError(
            f"{role} failed to compile: {source}\n{detail}",
            code="compile_failed",
        )
    return command


def _prepare_program(source, build_dir, role):
    source = Path(source)
    if source.suffix.lower() == ".cpp":
        output = Path(build_dir) / (f"{role}.exe" if os.name == "nt" else role)
        compile_command = _compile_cpp(source, output, role)
        return [str(output)], {
            "type": "compile",
            "role": role,
            "source": str(source),
            "command": compile_command,
            "ok": True,
        }
    if source.suffix.lower() == ".py":
        return [sys.executable, str(source)], {
            "type": "compile",
            "role": role,
            "source": str(source),
            "command": None,
            "interpreted": True,
            "ok": True,
        }
    if os.name != "nt" and not os.access(source, os.X_OK):
        raise ProbHubError(f"stress {role} is not executable: {source}")
    return [str(source)], {
        "type": "compile",
        "role": role,
        "source": str(source),
        "command": None,
        "prebuilt": True,
        "ok": True,
    }


def _run(
    command,
    input_data,
    timeout,
    cwd,
    memory_limit=256,
    output_limit=64,
    process_limit=DEFAULT_PROCESS_LIMIT,
):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with tempfile.TemporaryDirectory(prefix="probhub-stress-run-") as temp:
            stdout_path = Path(temp) / "stdout"
            stderr_path = Path(temp) / "stderr"
            result = run_managed_to_files(
                command,
                input_data=input_data,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=timeout,
                memory_limit_mb=memory_limit,
                output_limit_bytes=int(output_limit * 1024 * 1024),
                process_limit=process_limit,
                cwd=cwd,
                env=env,
            )
            stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
            stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
        reason = result["reason"]
        status_by_reason = {
            "time_limit": "TLE",
            "output_limit": "OLE",
            "memory_limit": "MLE",
            "process_limit": "RE",
        }
        status = status_by_reason.get(reason)
        if status is None:
            if result["returncode"] == 0:
                status = "AC"
            elif (
                result["memory_enforced"]
                and result["memory"] is not None
                and result["memory"] >= memory_limit * 0.98
            ):
                status = "MLE"
            else:
                status = "RE"
        message = result["message"]
        if not message and status == "RE":
            decoded = stderr.decode("utf-8", errors="replace").strip()
            message = decoded or f"process exited with code {result['returncode']}"
        return {
            "status": status,
            "reason": reason,
            "returncode": result["returncode"],
            "time": result["time"],
            "memory": result["memory"],
            "memory_enforced": result["memory_enforced"],
            "process_limit_enforced": result["process_limit_enforced"],
            "stdout": stdout,
            "stderr": stderr,
            "message": message or "",
        }
    except OSError as exc:
        return {
            "status": "RE",
            "reason": "start_error",
            "returncode": None,
            "time": 0.0,
            "memory": None,
            "memory_enforced": False,
            "process_limit_enforced": False,
            "stdout": b"",
            "stderr": str(exc).encode("utf-8", errors="replace"),
            "message": str(exc),
        }


def _feedback_message(feedback_dir, fallback=""):
    for name in ("judgemessage.txt", "teammessage.txt"):
        path = Path(feedback_dir) / name
        if path.is_file():
            message = path.read_text(encoding="utf-8", errors="replace").strip()
            if message:
                return message
    return (fallback or "").strip()


def _compare_custom(
    checker_command,
    input_path,
    answer_path,
    brute_output,
    timeout,
    cwd,
    memory_limit,
    output_limit,
    process_limit,
):
    feedback_dir = Path(tempfile.mkdtemp(prefix="feedback-", dir=cwd))
    try:
        result = _run(
            [*checker_command, str(input_path), str(answer_path), str(feedback_dir)],
            brute_output,
            timeout,
            cwd,
            memory_limit,
            min(output_limit, 8),
            process_limit,
        )
        stderr = result.get("stderr") or b""
        message = _feedback_message(
            feedback_dir, stderr.decode("utf-8", errors="replace")
        )
        if result.get("reason") != "completed":
            return {
                "status": "FAIL",
                "execution_status": result.get("status"),
                "match": False,
                "message": message or f"checker {result.get('message') or result.get('reason')}",
                "stderr": stderr,
            }
        if result["returncode"] in {0, 42}:
            return {"status": "AC", "match": True, "message": message, "stderr": stderr}
        if result["returncode"] in {1, 2, 43}:
            return {"status": "WA", "match": False, "message": message, "stderr": stderr}
        return {
            "status": "FAIL",
            "match": False,
            "message": message or f"checker exited with code {result['returncode']}",
            "stderr": stderr,
        }
    finally:
        shutil.rmtree(feedback_dir, ignore_errors=True)


def _comparison(configured, commands, round_dir, input_data, accepted_output, brute_output):
    if configured["judge_type"] == "custom":
        input_path = Path(round_dir) / "input.in"
        answer_path = Path(round_dir) / "accepted.out"
        input_path.write_bytes(input_data)
        answer_path.write_bytes(accepted_output)
        return _compare_custom(
            commands["checker"],
            input_path,
            answer_path,
            brute_output,
            configured["tool_timeout"],
            round_dir,
            configured["memory_limit"],
            configured["output_limit"],
            configured["process_limit"],
        )
    matched, message = compare_standard_output(
        accepted_output.decode("utf-8", errors="replace"),
        brute_output.decode("utf-8", errors="replace"),
    )
    return {
        "status": "AC" if matched else "WA",
        "match": matched,
        "message": message,
        "stderr": b"",
    }


def _round_once(problem_dir, configured, commands, seed, round_number, round_dir, input_data=None):
    generator_args = expand_generator_args(configured["args"], seed, round_number)
    generator = None
    if input_data is None:
        generator = _run(
            [*commands["generator"], *generator_args],
            None,
            configured["tool_timeout"],
            problem_dir,
            configured["memory_limit"],
            configured["output_limit"],
            configured["process_limit"],
        )
        # Windows text-mode generators emit CRLF; validators are compiled with
        # Linux line-ending semantics, so normalise like `gen` does.
        input_data = normalize_newlines(generator["stdout"])
        if generator["status"] != "AC":
            return {
                "ok": False,
                "kind": "infrastructure",
                "reason": f"generator_{generator['status'].lower()}",
                "message": generator["message"],
                "input": input_data,
                "generator": generator,
                "generator_args": generator_args,
            }

    validator = _run(
        commands["validator"], input_data, configured["tool_timeout"], problem_dir,
        configured["memory_limit"], configured["output_limit"], configured["process_limit"]
    )
    if validator["status"] != "AC":
        return {
            "ok": False,
            "kind": "infrastructure",
            "reason": "validator_rejected",
            "message": validator["message"] or "generated input was rejected by validator",
            "input": input_data,
            "generator": generator,
            "validator": validator,
            "generator_args": generator_args,
        }

    accepted = _run(
        commands["accepted"], input_data, configured["time_limit"], problem_dir,
        configured["memory_limit"], configured["output_limit"], configured["process_limit"]
    )
    if accepted["status"] != "AC":
        return {
            "ok": False,
            "kind": "counterexample",
            "reason": f"accepted_{accepted['status'].lower()}",
            "message": accepted["message"],
            "input": input_data,
            "generator": generator,
            "validator": validator,
            "accepted": accepted,
            "generator_args": generator_args,
        }

    brute = _run(
        commands["brute"], input_data, configured["time_limit"], problem_dir,
        configured["memory_limit"], configured["output_limit"], configured["process_limit"]
    )
    if brute["status"] != "AC":
        return {
            "ok": False,
            "kind": "counterexample",
            "reason": f"brute_{brute['status'].lower()}",
            "message": brute["message"],
            "input": input_data,
            "generator": generator,
            "validator": validator,
            "accepted": accepted,
            "brute": brute,
            "generator_args": generator_args,
        }

    comparison = _comparison(
        configured, commands, round_dir, input_data, accepted["stdout"], brute["stdout"]
    )
    if comparison["status"] == "FAIL":
        return {
            "ok": False,
            "kind": "infrastructure",
            "reason": "checker_failed",
            "message": comparison["message"],
            "input": input_data,
            "generator": generator,
            "validator": validator,
            "accepted": accepted,
            "brute": brute,
            "comparison": comparison,
            "generator_args": generator_args,
        }
    if not comparison["match"]:
        return {
            "ok": False,
            "kind": "counterexample",
            "reason": "output_mismatch",
            "message": comparison["message"],
            "input": input_data,
            "generator": generator,
            "validator": validator,
            "accepted": accepted,
            "brute": brute,
            "comparison": comparison,
            "generator_args": generator_args,
        }
    return {
        "ok": True,
        "kind": "match",
        "reason": "outputs_match",
        "message": comparison["message"],
        "input": input_data,
        "generator": generator,
        "validator": validator,
        "accepted": accepted,
        "brute": brute,
        "comparison": comparison,
        "generator_args": generator_args,
    }


def _write_optional_bytes(directory, name, value):
    if value is not None:
        (Path(directory) / name).write_bytes(value)


def _save_counterexample(root, problem_dir, problem_id, configured, master_seed, seed, round_number, outcome):
    stress_root = Path(problem_dir) / ".probhub" / "stress"
    stress_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    final = stress_root / f"{stamp}-r{round_number}-s{seed}"
    if final.exists():
        final = stress_root / f"{stamp}-r{round_number}-s{seed}-{uuid.uuid4().hex[:8]}"
    temporary = stress_root / f".tmp-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        _write_optional_bytes(temporary, "input.in", outcome.get("input", b""))
        for role, output_name, stderr_name in (
            ("generator", "generator.out", "generator.stderr"),
            ("validator", "validator.out", "validator.stderr"),
            ("accepted", "accepted.out", "accepted.stderr"),
            ("brute", "brute.out", "brute.stderr"),
        ):
            result = outcome.get(role)
            if result:
                _write_optional_bytes(temporary, output_name, result.get("stdout", b""))
                _write_optional_bytes(temporary, stderr_name, result.get("stderr", b""))
        comparison = outcome.get("comparison") or {}
        _write_optional_bytes(temporary, "checker.stderr", comparison.get("stderr", b""))
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "schema_version": 1,
            "problem_id": problem_id,
            "created_at": created_at,
            "kind": outcome.get("kind"),
            "reason": outcome.get("reason"),
            "message": outcome.get("message", ""),
            "master_seed": master_seed,
            "seed": seed,
            "round": round_number,
            "generator": configured["generator_rel"],
            "generator_args": outcome.get("generator_args", []),
            "validator": configured["validator_rel"],
            "accepted": configured["accepted_rel"],
            "brute": configured["brute_rel"],
            "checker": configured["checker_rel"],
            "judge_type": configured["judge_type"],
            "statuses": {
                name: (outcome.get(name) or {}).get("status")
                for name in ("generator", "validator", "accepted", "brute", "comparison")
                if outcome.get(name)
            },
        }
        write_json(temporary / "metadata.json", metadata)
        try:
            temporary.replace(final)
        except OSError:
            # Same-second collision with another process on the same seed/round:
            # fall back to a unique suffix instead of losing the counterexample.
            final = stress_root / f"{stamp}-r{round_number}-s{seed}-{uuid.uuid4().hex[:8]}"
            temporary.replace(final)
        relative = final.resolve().relative_to(Path(root).resolve()).as_posix()
        latest_temporary = stress_root / f".latest-{uuid.uuid4().hex}.tmp"
        write_json(latest_temporary, {
            "schema_version": 1,
            "problem_id": problem_id,
            "artifact": relative,
            "created_at": created_at,
        })
        latest_temporary.replace(stress_root / "latest.json")
        return final, metadata
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _read_json_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbHubError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProbHubError(f"invalid {label}: expected a JSON object: {path}")
    return value


def _resolve_replay(root, problem_dir, replay, problem_id=None):
    root = Path(root).resolve()
    problem_dir = Path(problem_dir).resolve()
    stress_root = (problem_dir / ".probhub" / "stress").resolve()
    if str(replay).lower() == "latest":
        latest = stress_root / "latest.json"
        if not latest.is_file():
            raise ProbHubError(f"stress replay metadata not found: {latest}")
        data = _read_json_object(latest, "stress replay index")
        artifact_value = data.get("artifact")
        if not isinstance(artifact_value, str) or not artifact_value.strip():
            raise ProbHubError(f"invalid stress replay index: missing artifact: {latest}")
        candidate = root / artifact_value
    else:
        candidate = Path(replay)
        if not candidate.is_absolute():
            root_candidate = root / candidate
            problem_candidate = problem_dir / candidate
            candidate = root_candidate if root_candidate.exists() else problem_candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(stress_root)
    except ValueError as exc:
        raise ProbHubError(
            f"stress replay path must stay inside {stress_root}: {candidate}"
        ) from exc
    artifact = candidate.parent if candidate.is_file() else candidate
    input_path = (
        candidate
        if candidate.is_file() and candidate.name != "metadata.json"
        else artifact / "input.in"
    )
    metadata_path = artifact / "metadata.json"
    if not input_path.is_file():
        raise ProbHubError(f"stress replay input not found: {input_path}")
    metadata = _read_json_object(metadata_path, "stress replay metadata") if metadata_path.is_file() else {}
    metadata_problem_id = metadata.get("problem_id")
    if problem_id and metadata_problem_id and str(metadata_problem_id) != str(problem_id):
        raise ProbHubError(
            f"stress replay belongs to problem {metadata_problem_id}, not {problem_id}"
        )
    return artifact, input_path, metadata


def _public_run(result):
    if not result:
        return None
    public = {
        "status": result.get("status"),
        "message": result.get("message", ""),
    }
    if "returncode" in result:
        public["returncode"] = result.get("returncode")
    if "time" in result:
        public["time"] = round(float(result.get("time", 0)), 6)
    if "match" in result:
        public["match"] = bool(result.get("match"))
    for key in ("reason", "memory", "memory_enforced", "process_limit_enforced", "execution_status"):
        if key in result:
            public[key] = result.get(key)
    return public


def _is_killer_outcome(outcome):
    if outcome.get("kind") != "counterexample":
        return False
    if (outcome.get("brute") or {}).get("reason") == "start_error":
        # The target never executed (CreateProcess/exec failure); that is an
        # infrastructure problem, not a separation.
        return False
    return (
        outcome.get("reason") == "output_mismatch"
        or str(outcome.get("reason", "")).startswith("brute_")
    )


def _contained_data_dir(problem_dir, relative, label):
    # Keep in sync with datagen.resolve_data_dir (importing datagen would cycle).
    if not isinstance(relative, str) or not relative.strip():
        raise ProbHubError(f"{label} must be a non-empty path string", code="fixate_invalid")
    problem_dir = Path(problem_dir).resolve()
    candidate = (problem_dir / relative).resolve()
    try:
        candidate.relative_to(problem_dir)
    except ValueError as exc:
        raise ProbHubError(
            f"{label} must stay inside the problem directory: {relative}",
            code="fixate_invalid",
        ) from exc
    return candidate


def _declared_solution_files(config):
    files = {}
    solutions = config.get("solutions") or {}
    for kind in ("accepted", "brute", "wrong"):
        entries = solutions.get(kind) or []
        entries = entries if isinstance(entries, list) else [entries]
        for entry in entries:
            value = _entry_file(entry)
            if value:
                normalized = str(value).replace("\\", "/")
                folded = normalized.casefold()
                previous = files.get(folded)
                if previous is not None and previous != normalized:
                    raise ProbHubError(
                        f"solution paths differ only by case: {previous}, {normalized}",
                        code="fixate_invalid",
                    )
                files[folded] = normalized
    return files


def _casefold_entry(directory, name):
    directory = Path(directory)
    if not directory.is_dir():
        return None
    folded = str(name).casefold()
    for entry in directory.iterdir():
        if entry.name.casefold() == folded:
            return entry
    return None


def _recipe_case_exists(recipes, case_name):
    folded = str(case_name).casefold()
    return any(
        isinstance(item, dict)
        and str(item.get("case", "")).casefold() == folded
        for item in recipes
    )


def _fixate_precheck(problem_dir, config, case_name, against_rel):
    """Fail fast before the hunt; `_fixate_killer` re-checks under the lock."""
    if not _CASE_NAME_PATTERN.match(case_name or ""):
        raise ProbHubError(f"invalid fixate case name: {case_name!r}", code="fixate_invalid")
    requested_target = str(against_rel).replace("\\", "/")
    normalized_target = _declared_solution_files(config).get(requested_target.casefold())
    if normalized_target is None:
        # A group target that is not a declared solution fails lint afterwards,
        # breaking the one-step fixation loop.
        raise ProbHubError(
            f"--fixate requires the --against target to be declared in solutions.*: "
            f"{requested_target} (declare it, e.g. under solutions.wrong with an "
            "expected entry, then fixate)",
            code="fixate_undeclared",
        )
    data_config = config.get("data") or {}
    secret_dir = _contained_data_dir(
        problem_dir, data_config.get("secret_dir") or "data/secret", "data.secret_dir"
    )
    if (
        _casefold_entry(secret_dir, f"{case_name}.in") is not None
        or _casefold_entry(secret_dir, f"{case_name}.ans") is not None
    ):
        raise ProbHubError(f"fixate target already exists: {case_name}", code="fixate_exists")
    recipes = data_config.get("recipes") or []
    if isinstance(recipes, list) and _recipe_case_exists(recipes, case_name):
        raise ProbHubError(f"a recipe already exists for case: {case_name}", code="fixate_exists")
    return normalized_target


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fixate_input_snapshot(problem_dir, config, configured):
    # Imported lazily because linting imports stress argument helpers.
    from .linting import compute_source_hash

    paths = {
        str(path.resolve()): _file_digest(path)
        for path in (
            configured.get("generator"),
            configured.get("accepted"),
            configured.get("brute"),
            configured.get("validator"),
            configured.get("checker"),
        )
        if path is not None
    }
    first_rel = _first_solution(config, "accepted")
    if first_rel is not None:
        first_path = _resolve_problem_file(problem_dir, first_rel, "accepted")
        paths[str(first_path.resolve())] = _file_digest(first_path)
    return {
        "config": json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "source_hash": compute_source_hash(problem_dir, config),
        "files": paths,
    }


def _verify_fixate_snapshot(problem_dir, live, live_configured, expected):
    actual = _fixate_input_snapshot(problem_dir, live, live_configured)
    if actual != expected:
        raise ProbHubError(
            "problem configuration or stress inputs changed while hunting the killer; rerun stress",
            code="fixate_inputs_changed",
        )


def _fixate_reproduce_input(problem_dir, configured, generator_args, expected_input):
    with tempfile.TemporaryDirectory(prefix="probhub-fixate-gen-") as temp:
        command, _ = _prepare_program(configured["generator"], temp, "generator")
        rerun = _run(
            [*command, *generator_args],
            None,
            configured["tool_timeout"],
            problem_dir,
            configured["memory_limit"],
            configured["output_limit"],
            configured["process_limit"],
        )
    if rerun["status"] != "AC":
        raise ProbHubError(
            f"generator failed while verifying the fixated killer: {rerun['status']}",
            code="fixate_reproduce_failed",
        )
    reproduced = normalize_newlines(rerun["stdout"])
    if reproduced != expected_input:
        raise ProbHubError(
            "stress generator did not byte-reproduce the killer input from its recorded arguments",
            code="fixate_nondeterministic",
        )
    return reproduced


def _fixate_revalidate_killer(problem_dir, configured, input_bytes):
    """Re-run live Validator, accepted, target and Checker before publication."""
    with tempfile.TemporaryDirectory(prefix="probhub-fixate-recheck-") as temp:
        commands = {}
        for role in ("validator", "accepted", "brute"):
            commands[role], _ = _prepare_program(configured[role], temp, role)
        if configured.get("checker") is not None:
            commands["checker"], _ = _prepare_program(
                configured["checker"], temp, "checker"
            )
        round_dir = Path(temp) / "round"
        round_dir.mkdir()
        outcome = _round_once(
            problem_dir,
            configured,
            commands,
            0,
            1,
            round_dir,
            input_data=input_bytes,
        )
    if outcome.get("kind") == "infrastructure":
        raise ProbHubError(
            "killer revalidation failed in problem infrastructure: "
            f"{outcome.get('reason')}",
            code="fixate_revalidate_failed",
        )
    if outcome.get("ok") or not _is_killer_outcome(outcome):
        raise ProbHubError(
            "the reproduced input no longer separates the target solution",
            code="fixate_not_killer",
        )


def _fixate_answer_bytes(problem_dir, config, configured, input_bytes):
    """Run the same first accepted solution used by `gen` on reproduced input."""
    first_rel = _first_solution(config, "accepted")
    if first_rel is None:
        raise ProbHubError(
            "solutions.accepted must declare a solution before fixating a killer",
            code="fixate_answer_failed",
        )
    source = _resolve_problem_file(problem_dir, first_rel, "accepted")
    limits = config.get("limits") or {}
    raw_time = limits.get("time")
    time_limit = raw_time if _is_int(raw_time) and raw_time > 0 else 1
    with tempfile.TemporaryDirectory(prefix="probhub-fixate-ans-") as temp:
        command, _ = _prepare_program(source, temp, "accepted")
        rerun = _run(
            command,
            input_bytes,
            min(600.0, max(30.0, 10.0 * float(time_limit))),
            problem_dir,
            memory_limit=max(2048, 2 * configured["memory_limit"]),
            output_limit=configured["output_limit"],
            process_limit=configured["process_limit"],
        )
        if rerun["status"] != "AC":
            raise ProbHubError(
                "first accepted solution failed on the reproduced killer input while producing "
                f"the fixated answer: {rerun['status']}",
                code="fixate_answer_failed",
            )
        answer = normalize_newlines(rerun["stdout"])
        if configured.get("checker") is not None:
            checker, _ = _prepare_program(configured["checker"], temp, "checker")
            checker_dir = Path(temp) / "answer-check"
            checker_dir.mkdir()
            input_path = checker_dir / "input.in"
            answer_path = checker_dir / "answer.ans"
            input_path.write_bytes(input_bytes)
            answer_path.write_bytes(answer)
            checked = _compare_custom(
                checker,
                input_path,
                answer_path,
                answer,
                configured["tool_timeout"],
                checker_dir,
                configured["memory_limit"],
                configured["output_limit"],
                configured["process_limit"],
            )
            if checked["status"] != "AC":
                detail = checked.get("execution_status") or checked["status"]
                raise ProbHubError(
                    "Checker rejected the first accepted solution while producing "
                    f"the fixated answer: {detail}",
                    code="fixate_answer_failed",
                )
        return answer


def _replace_fixate_path(source, target):
    os.replace(source, target)


def _recover_fixate_transactions(problem_dir):
    problem_dir = Path(problem_dir).resolve()
    for staging in sorted(problem_dir.glob(".probhub-fixate-*")):
        try:
            staging = validate_transaction_directory(
                staging,
                problem_dir,
                ".probhub-fixate-",
            )
            journal_path = staging / "journal.json"
            if not journal_path.is_file():
                shutil.rmtree(staging)
                continue
            _, phase, entries = read_transaction_journal(staging)
            if phase == TRANSACTION_PHASE_COMMITTED:
                shutil.rmtree(staging)
                continue
            for reverse_index, entry in enumerate(reversed(entries)):
                index = len(entries) - reverse_index - 1
                if not isinstance(entry, dict):
                    raise ValueError("journal entry must be an object")
                target = resolve_journal_target(problem_dir, entry["target"])
                backup_name = entry.get("backup")
                if backup_name:
                    backup = transaction_child(
                        staging,
                        backup_name,
                        expected_prefix="backup",
                        index=index,
                    )
                    if backup.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(backup, target)
                    elif not target.is_file():
                        raise FileNotFoundError(
                            f"missing recovery backup and target: {backup} -> {target}"
                        )
                elif not entry.get("existed"):
                    target.unlink(missing_ok=True)
            shutil.rmtree(staging)
        except BaseException as exc:
            raise ProbHubError(
                f"failed to recover interrupted fixate transaction {staging}: {exc}",
                code="fixate_recovery_failed",
            ) from exc


def _publish_fixation(problem_dir, input_path, answer_path, config_path, input_bytes, answer_bytes, live):
    """Publish the two data files and YAML as one rollback-capable transaction."""
    problem_dir = Path(problem_dir)
    staging = Path(tempfile.mkdtemp(prefix=".probhub-fixate-", dir=problem_dir))
    staged_input = staging / "case.in"
    staged_answer = staging / "case.ans"
    staged_config = staging / "probhub.yaml"
    secret_dir_existed = input_path.parent.is_dir()
    cleanup_staging = True
    cleanup_context = "staging"
    try:
        staged_input.write_bytes(input_bytes)
        staged_answer.write_bytes(answer_bytes)
        write_yaml(staged_config, live)
        input_path.parent.mkdir(parents=True, exist_ok=True)
        targets = (
            (staged_input, input_path),
            (staged_answer, answer_path),
            (staged_config, config_path),
        )
        backups = {}
        journal_entries = []
        for index, (_, target) in enumerate(targets):
            relative = target.resolve().relative_to(problem_dir.resolve()).as_posix()
            backup_name = None
            if target.exists():
                backup = staging / f"backup-{index}"
                shutil.copyfile(target, backup)
                backups[target] = backup
                backup_name = backup.name
            journal_entries.append({
                "target": relative,
                "existed": target.exists(),
                "backup": backup_name,
            })
        write_json(staging / "journal.json", {
            "schema_version": 1,
            "phase": TRANSACTION_PHASE_PREPARED,
            "entries": journal_entries,
        })
        cleanup_staging = False
        try:
            for source, target in targets:
                _replace_fixate_path(source, target)
        except BaseException as exc:
            rollback_errors = []
            for _, target in reversed(targets):
                backup = backups.get(target)
                try:
                    if backup is not None:
                        os.replace(backup, target)
                    else:
                        target.unlink(missing_ok=True)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"{target}: {rollback_exc}")
            if not secret_dir_existed:
                try:
                    input_path.parent.rmdir()
                except OSError:
                    pass
            if rollback_errors:
                raise ProbHubError(
                    "failed to publish fixated killer and rollback was incomplete; "
                    f"recovery files remain in {staging}: {', '.join(rollback_errors)}",
                    code="fixate_rollback_failed",
                ) from exc
            cleanup_staging = True
            cleanup_context = "rollback"
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ProbHubError(
                f"failed to publish fixated killer transaction: {exc}",
                code="fixate_publish_failed",
            ) from exc
        write_json(staging / "journal.json", {
            "schema_version": 1,
            "phase": TRANSACTION_PHASE_COMMITTED,
            "entries": journal_entries,
        })
        cleanup_staging = True
        cleanup_context = "committed"
    except Exception as exc:
        if not secret_dir_existed:
            try:
                input_path.parent.rmdir()
            except OSError:
                pass
        if isinstance(exc, ProbHubError):
            raise
        raise ProbHubError(
            f"failed to publish fixated killer transaction: {exc}",
            code="fixate_publish_failed",
        ) from exc
    finally:
        if cleanup_staging:
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                detail = {
                    "committed": "fixated data was committed",
                    "rollback": "fixated data publication was rolled back",
                    "staging": "fixated data staging failed",
                }[cleanup_context]
                raise ProbHubError(
                    f"{detail} but transaction cleanup failed; "
                    f"the next writer will retry cleanup: {staging}: {exc}",
                    code="fixate_cleanup_failed",
                ) from exc


def _replay_command(problem_id, relative, against=None):
    command = f"probhub stress {problem_id}"
    if against is not None:
        command += f" --against {json.dumps(str(against), ensure_ascii=False)}"
    return command + f" --replay {json.dumps(str(relative), ensure_ascii=False)}"


def _fixate_killer(
    root,
    problem_dir,
    config,
    configured,
    outcome,
    case_name,
    group_name,
    against_rel,
    input_snapshot,
):
    """Persist a found killer: secret data + gen recipe + targeted data group."""
    input_bytes = normalize_newlines(outcome.get("input") or b"")
    generator_args = [str(item) for item in outcome.get("generator_args") or []]
    requested_group = group_name or case_name
    config_path = problem_dir / "probhub.yaml"
    with workspace_build_lock(root):
        _, live_workspace = load_workspace(root)
        recover_workspace_transactions(root, live_workspace)
        live = read_yaml(config_path)
        try:
            live_configured = _stress_config(problem_dir, live, against=against_rel)
        except ProbHubError as exc:
            raise ProbHubError(
                "problem configuration or stress inputs changed while hunting the killer; rerun stress",
                code="fixate_inputs_changed",
            ) from exc
        _verify_fixate_snapshot(problem_dir, live, live_configured, input_snapshot)
        target = _fixate_precheck(problem_dir, live, case_name, against_rel)
        reproduced_input = _fixate_reproduce_input(
            problem_dir, live_configured, generator_args, input_bytes
        )
        _fixate_revalidate_killer(problem_dir, live_configured, reproduced_input)
        answer_bytes = _fixate_answer_bytes(
            problem_dir, live, live_configured, reproduced_input
        )
        data = live.setdefault("data", {})
        if not isinstance(data, dict):
            raise ProbHubError("data must be a mapping to fixate", code="fixate_invalid")
        secret_dir = _contained_data_dir(
            problem_dir, data.get("secret_dir") or "data/secret", "data.secret_dir"
        )
        input_path = secret_dir / f"{case_name}.in"
        answer_path = secret_dir / f"{case_name}.ans"
        recipes = data.setdefault("recipes", [])
        if not isinstance(recipes, list):
            raise ProbHubError("data.recipes must be a list to fixate", code="fixate_invalid")
        recipes.append({
            "case": case_name,
            "generator": live_configured["generator_rel"],
            "args": generator_args,
        })
        groups = data.setdefault("groups", [])
        if not isinstance(groups, list):
            raise ProbHubError("data.groups must be a list to fixate", code="fixate_invalid")
        folded_group = str(requested_group).casefold()
        existing = next(
            (
                item
                for item in groups
                if isinstance(item, dict)
                and str(item.get("name", "")).casefold() == folded_group
            ),
            None,
        )
        group = str(existing.get("name")) if existing is not None else requested_group
        pattern = f"secret/{case_name}"
        if existing is None:
            groups.append({
                "name": group,
                "role": "wrong-solution-killer",
                "patterns": [pattern],
                "targets": [target],
            })
        else:
            existing.setdefault("role", "wrong-solution-killer")
            patterns = existing.get("patterns") or existing.get("cases") or []
            patterns = [patterns] if isinstance(patterns, str) else list(patterns)
            matching_pattern = next(
                (index for index, item in enumerate(patterns) if str(item).casefold() == pattern.casefold()),
                None,
            )
            if matching_pattern is None:
                patterns.append(pattern)
            else:
                patterns[matching_pattern] = pattern
            existing["patterns"] = patterns
            existing.pop("cases", None)
            targets = existing.get("targets") or []
            targets = [targets] if isinstance(targets, str) else list(targets)
            matching_target = next(
                (
                    index
                    for index, item in enumerate(targets)
                    if str(item).replace("\\", "/").casefold() == target.casefold()
                ),
                None,
            )
            if matching_target is None:
                targets.append(target)
            else:
                targets[matching_target] = target
            existing["targets"] = targets
        disk_config = read_yaml(config_path)
        try:
            disk_configured = _stress_config(problem_dir, disk_config, against=against_rel)
        except ProbHubError as exc:
            raise ProbHubError(
                "problem configuration or stress inputs changed before publishing the killer",
                code="fixate_inputs_changed",
            ) from exc
        _verify_fixate_snapshot(
            problem_dir,
            disk_config,
            disk_configured,
            input_snapshot,
        )
        _publish_fixation(
            problem_dir,
            input_path,
            answer_path,
            config_path,
            reproduced_input,
            answer_bytes,
            live,
        )
    return {
        "case": case_name,
        "group": group,
        "generator": live_configured["generator_rel"],
        "args": generator_args,
        "input": str(input_path.relative_to(problem_dir).as_posix()),
        "answer": str(answer_path.relative_to(problem_dir).as_posix()),
        "target": target,
    }


def stress_problem(
    root,
    problem_dir,
    config,
    rounds=None,
    master_seed=None,
    replay=None,
    against=None,
    fixate=None,
    fixate_group=None,
):
    root = Path(root).resolve()
    problem_dir = Path(problem_dir).resolve()
    problem_id = str(config.get("id") or problem_dir.name)
    if fixate is not None and against is None:
        raise ProbHubError("--fixate requires --against")
    if fixate is not None and replay is not None:
        raise ProbHubError("--fixate cannot be combined with --replay")
    fixate_snapshot = None
    if fixate is not None:
        # Recover any interrupted prior commit, then capture a current snapshot
        # under the same writer lock used by fixate publication.
        with workspace_build_lock(root):
            _, live_workspace = load_workspace(root)
            recover_workspace_transactions(root, live_workspace)
            config = read_yaml(problem_dir / "probhub.yaml")
            configured = _stress_config(problem_dir, config, against=against)
            _fixate_precheck(problem_dir, config, fixate, against)
            fixate_snapshot = _fixate_input_snapshot(problem_dir, config, configured)
    else:
        configured = _stress_config(problem_dir, config, against=against)
    requested_rounds = configured["rounds"] if rounds is None else rounds
    if not _is_int(requested_rounds) or requested_rounds <= 0:
        raise ProbHubError("--rounds must be a positive integer")
    master_seed = secrets.randbelow(2**31) if master_seed is None else master_seed
    if not _is_int(master_seed) or master_seed < 0:
        raise ProbHubError("--seed must be a non-negative integer")

    with tempfile.TemporaryDirectory(prefix="probhub-stress-build-") as build_temp:
        commands = {}
        compile_events = []
        roles = ("validator", "accepted", "brute") if replay is not None else (
            "generator", "validator", "accepted", "brute"
        )
        for role in roles:
            commands[role], event = _prepare_program(configured[role], build_temp, role)
            event["source"] = configured[f"{role}_rel"]
            compile_events.append(event)
        if configured["checker"]:
            commands["checker"], event = _prepare_program(
                configured["checker"], build_temp, "checker"
            )
            event["source"] = configured["checker_rel"]
            compile_events.append(event)

        if replay is not None:
            artifact, input_path, metadata = _resolve_replay(
                root, problem_dir, replay, problem_id=problem_id
            )
            try:
                seed = int(metadata.get("seed", master_seed))
                round_number = int(metadata.get("round", 1))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProbHubError("stress replay metadata has an invalid seed or round") from exc
            if seed < 0 or round_number <= 0:
                raise ProbHubError("stress replay metadata has an invalid seed or round")
            with tempfile.TemporaryDirectory(prefix="probhub-stress-replay-") as round_temp:
                outcome = _round_once(
                    problem_dir,
                    configured,
                    commands,
                    seed,
                    round_number,
                    round_temp,
                    # Same normalisation as the generation path: validators are
                    # compiled with Linux line-ending semantics.
                    input_data=normalize_newlines(input_path.read_bytes()),
                )
            ok = bool(outcome["ok"])
            status = "passed" if ok else outcome["kind"]
            if against is not None and not ok and _is_killer_outcome(outcome):
                ok = True
                status = "killer_confirmed"
            return {
                "ok": ok,
                "status": status,
                "problem_id": problem_id,
                "replay": True,
                "against": configured["brute_rel"] if against is not None else None,
                "artifact": str(artifact),
                "input": str(input_path),
                "seed": seed,
                "round": round_number,
                "reason": outcome["reason"],
                "message": outcome.get("message", ""),
                "accepted": _public_run(outcome.get("accepted")),
                "brute": _public_run(outcome.get("brute")),
                "comparison": _public_run(outcome.get("comparison")),
                "compile": compile_events,
            }

        for round_number in range(1, requested_rounds + 1):
            seed = master_seed + round_number - 1
            with tempfile.TemporaryDirectory(prefix="probhub-stress-round-") as round_temp:
                outcome = _round_once(
                    problem_dir,
                    configured,
                    commands,
                    seed,
                    round_number,
                    round_temp,
                )
            if not outcome["ok"]:
                artifact, metadata = _save_counterexample(
                    root,
                    problem_dir,
                    problem_id,
                    configured,
                    master_seed,
                    seed,
                    round_number,
                    outcome,
                )
                relative = artifact.relative_to(root).as_posix()
                result = {
                    "ok": False,
                    "status": outcome["kind"],
                    "problem_id": problem_id,
                    "judge_type": configured["judge_type"],
                    "rounds_requested": requested_rounds,
                    "rounds_completed": round_number - 1,
                    "master_seed": master_seed,
                    "seed": seed,
                    "round": round_number,
                    "reason": outcome["reason"],
                    "message": outcome.get("message", ""),
                    "counterexample": relative,
                    "replay_command": _replay_command(
                        problem_id,
                        relative,
                        configured["brute_rel"] if against is not None else None,
                    ),
                    "accepted": _public_run(outcome.get("accepted")),
                    "brute": _public_run(outcome.get("brute")),
                    "comparison": _public_run(outcome.get("comparison")),
                    "metadata": metadata,
                    "compile": compile_events,
                }
                if against is not None:
                    result["against"] = configured["brute_rel"]
                    if _is_killer_outcome(outcome):
                        # In a killer hunt a mismatch is the desired result.
                        result["ok"] = True
                        result["status"] = "killer_found"
                        if fixate is not None:
                            result["fixated"] = _fixate_killer(
                                root,
                                problem_dir,
                                config,
                                configured,
                                outcome,
                                fixate,
                                fixate_group,
                                configured["brute_rel"],
                                fixate_snapshot,
                            )
                return result

    if against is not None:
        return {
            "ok": True,
            "status": "not_separated",
            "problem_id": problem_id,
            "judge_type": configured["judge_type"],
            "against": configured["brute_rel"],
            "rounds_requested": requested_rounds,
            "rounds_completed": requested_rounds,
            "master_seed": master_seed,
            "counterexample": None,
            "message": (
                "target matched accepted on every round; it is not separated by "
                "the current generator distribution — strengthen the generator "
                "or drop the wrong-solution model"
            ),
            "compile": compile_events,
        }
    return {
        "ok": True,
        "status": "passed",
        "problem_id": problem_id,
        "judge_type": configured["judge_type"],
        "rounds_requested": requested_rounds,
        "rounds_completed": requested_rounds,
        "master_seed": master_seed,
        "counterexample": None,
        "compile": compile_events,
    }
