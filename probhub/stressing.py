import json
import math
import os
import platform
import secrets
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import ProbHubError
from .io import write_json
from .output_compare import compare_standard_output
from .process_control import DEFAULT_PROCESS_LIMIT, run_managed_to_files


DEFAULT_ROUNDS = 1000
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


def _stress_config(problem_dir, config):
    stress = config.get("stress")
    if not isinstance(stress, dict):
        raise ProbHubError("stress configuration is required in probhub.yaml")
    judge_type = _judge_type(config)
    if judge_type == "interactive":
        raise ProbHubError("probhub stress does not support interactive judging")
    generator_rel = stress.get("generator")
    accepted_rel = stress.get("accepted") or _first_solution(config, "accepted")
    brute_rel = stress.get("brute") or _first_solution(config, "brute")
    validator_rel = ((config.get("judge") or {}).get("validator"))
    checker_rel = ((config.get("judge") or {}).get("checker")) if judge_type == "custom" else None
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
        "brute": _resolve_problem_file(problem_dir, brute_rel, "brute"),
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
        except (KeyError, ValueError) as exc:
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
        raise ProbHubError(f"stress {role} failed to compile: {source}\n{detail}")
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
        input_data = generator["stdout"]
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


def stress_problem(root, problem_dir, config, rounds=None, master_seed=None, replay=None):
    root = Path(root).resolve()
    problem_dir = Path(problem_dir).resolve()
    problem_id = str(config.get("id") or problem_dir.name)
    configured = _stress_config(problem_dir, config)
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
                    input_data=input_path.read_bytes(),
                )
            return {
                "ok": bool(outcome["ok"]),
                "status": "passed" if outcome["ok"] else outcome["kind"],
                "problem_id": problem_id,
                "replay": True,
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
                return {
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
                    "replay_command": f'probhub stress {problem_id} --replay "{relative}"',
                    "accepted": _public_run(outcome.get("accepted")),
                    "brute": _public_run(outcome.get("brute")),
                    "comparison": _public_run(outcome.get("comparison")),
                    "metadata": metadata,
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
