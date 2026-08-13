#!/usr/bin/env python3
"""Run a test-only, spawn-based mutation scheduling benchmark on fixture M01.

The production mutation CLI and evidence writer are intentionally not used.
The parent captures one immutable baseline and mutation plan, while spawned
workers copy that baseline into independent directories and call the current
``judge_problem`` boundary.  Results are observations, not performance gates.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import multiprocessing
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import median


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.errors import ProbHubError
from probhub.io import read_yaml, write_yaml
from probhub.judging import judge_problem
from probhub.linting import compute_data_hash, compute_source_hash
from probhub.mutation import (
    MAX_MUTANTS,
    MUTATION_OPERATORS,
    _classify_judge_result,
    _mutation_execution_profile,
    apply_mutation,
    plan_mutations,
)
from probhub.problem_snapshot import copy_problem_consistently
from probhub.process_control import (
    ProcessCancelled,
    process_alive,
    run_managed_to_files,
    snapshot_process_tree,
    terminate_external_process_tree,
)
from probhub.solutions import normalize_solution_entries, resolve_solution_source
from probhub.workspace import load_workspace, problem_entries, resolve_problem_dir


SCHEMA_VERSION = 1
BENCHMARK = "mutation-shadow-spawn-v1"
CANCEL_GRACE_SECONDS = 2.0
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "workspaces" / "mutation"
PROBLEM_ID = "M01"
CLASSIFICATIONS = (
    "killed",
    "survived",
    "compile-invalid",
    "infrastructure-failed",
    "cancelled",
)
_FORMAL_RELATIVE_PATHS = (
    "meta.json",
    "problem.pdf",
    "problem.yaml",
    "domjudge-problem.ini",
    ".probhub/build-manifest.json",
)
_LOCAL_MUTATION_ARTIFACTS = (
    ".probhub/mutation-evidence-v1.json",
    ".probhub/mutation-evidence-v2.json",
    ".probhub/mutation.lock",
)


@dataclass(frozen=True)
class WorkerTask:
    index: int
    mutation_id: str
    mutation: object | None
    baseline: str
    worker_root: str
    source: str
    source_relative: str
    config: dict
    execution_profile: dict
    mutant_timeout: float
    use_cache: bool
    synthetic: dict | None = None


@dataclass(frozen=True)
class BenchmarkPlan:
    live_root: Path
    live_problem: Path
    baseline: Path
    workers_root: Path
    source: str
    source_relative: str
    config: dict
    source_hash: str
    data_hash: str
    plan_hash: str
    baseline_hash: str
    snapshot_seconds: float
    planning_seconds: float
    execution_profile: dict
    tasks: tuple[WorkerTask, ...]


def _hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _hash_tree(root):
    root = Path(root)
    digest = hashlib.sha256()
    entries = []

    def visit(directory):
        with os.scandir(directory) as stream:
            for entry in sorted(stream, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                info = entry.stat(follow_symlinks=False)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                is_reparse = bool(
                    reparse_flag
                    and getattr(info, "st_file_attributes", 0) & reparse_flag
                )
                if stat.S_ISLNK(info.st_mode) or is_reparse:
                    try:
                        payload = os.readlink(path).encode("utf-8")
                    except OSError:
                        payload = b""
                    entries.append((relative, b"link", payload))
                elif stat.S_ISDIR(info.st_mode):
                    entries.append((relative, b"directory", b""))
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    entries.append((relative, b"file", path.read_bytes()))
                else:
                    entries.append((relative, b"special", str(info.st_mode).encode("ascii")))

    visit(root)
    for relative_text, kind, content in entries:
        relative = relative_text.encode("utf-8")
        digest.update(len(kind).to_bytes(8, "big"))
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_text(command, *, timeout=10.0, limit=64 * 1024):
    with tempfile.TemporaryDirectory(prefix="mutation-shadow-identity-") as temp:
        stdout = Path(temp) / "stdout"
        stderr = Path(temp) / "stderr"
        try:
            result = run_managed_to_files(
                command,
                stdout_path=stdout,
                stderr_path=stderr,
                timeout=timeout,
                memory_limit_mb=256,
                output_limit_bytes=limit,
                process_limit=8,
            )
        except OSError:
            return None
        if result.get("reason") != "completed" or result.get("returncode") != 0:
            return None
        return stdout.read_text(encoding="utf-8", errors="replace")[:limit]


def _environment_identity():
    compiler_output = _read_text(["g++", "--version"])
    compiler = next(
        (line.strip() for line in (compiler_output or "").splitlines() if line.strip()),
        None,
    )
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_executable": str(Path(sys.executable).resolve()),
        "cpu_count": os.cpu_count(),
        "compiler": compiler,
    }


def _live_input_hashes(problem):
    config = read_yaml(Path(problem) / "probhub.yaml")
    return {
        "source": compute_source_hash(problem, config),
        "data": compute_data_hash(problem, config),
    }


def _formal_artifact_hashes(root, problem):
    root = Path(root)
    problem = Path(problem)
    candidates = [problem / relative for relative in _FORMAL_RELATIVE_PATHS]
    candidates.extend((root / f"{PROBLEM_ID}.zip",))
    try:
        _, workspace = load_workspace(root)
        typst = workspace.get("typst") if isinstance(workspace, dict) else None
        if isinstance(typst, dict):
            for key in ("main", "pdf", "problems_json"):
                value = typst.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(root / value)
    except (OSError, ValueError, TypeError, ProbHubError):
        pass
    snapshot = {}
    for path in sorted(set(item.resolve() for item in candidates)):
        try:
            relative = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if path.is_file() and not path.is_symlink():
            snapshot[relative] = _hash_bytes(path.read_bytes())
        elif path.exists() or path.is_symlink():
            snapshot[relative] = "non-regular"
    return snapshot


def _local_mutation_artifact_hashes(problem):
    problem = Path(problem)
    snapshot = {}
    for relative in _LOCAL_MUTATION_ARTIFACTS:
        path = problem / relative
        if path.is_file() and not path.is_symlink():
            snapshot[relative] = _hash_bytes(path.read_bytes())
        elif path.exists() or path.is_symlink():
            snapshot[relative] = "non-regular"
    return snapshot


def _failure_result(task, code, message, *, classification="infrastructure-failed"):
    return {
        "index": task.index,
        "id": task.mutation_id,
        "classification": classification,
        "diagnostic": {
            "code": str(code)[:256],
            "message": str(message)[:2048],
        },
    }


def _run_synthetic(task, cancel_event):
    spec = dict(task.synthetic or {})
    child_pid_path = Path(task.worker_root) / "synthetic-child.pid"
    if spec.get("hard_kill_descendant"):
        external_pid_path = (
            Path(task.baseline).parent / f"hard-child-{task.index}.pid"
        )
        child_code = (
            "import os,time,pathlib; "
            f"pathlib.Path({str(external_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(300)"
        )
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", child_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
        ready_deadline = time.monotonic() + 10.0
        while not external_pid_path.is_file() and time.monotonic() < ready_deadline:
            time.sleep(0.01)
        if not external_pid_path.is_file():
            raise RuntimeError("synthetic detached child did not become ready")
        cancel_event.set()
        while True:
            time.sleep(1.0)
    if spec.get("spawn_descendant"):
        Path(task.worker_root).mkdir(parents=True, exist_ok=True)
        child_code = (
            "import os,time; "
            f"open({str(child_pid_path)!r},'w').write(str(os.getpid())); "
            "time.sleep(300)"
        )
        supervisor_code = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([{sys.executable!r},'-I','-S','-c',{child_code!r}]); "
            "time.sleep(300)"
        )
        with tempfile.TemporaryDirectory(prefix="mutation-shadow-synthetic-") as temp:
            result = run_managed_to_files(
                [sys.executable, "-I", "-S", "-c", supervisor_code],
                stdout_path=Path(temp) / "stdout",
                stderr_path=Path(temp) / "stderr",
                timeout=max(float(spec.get("managed_timeout", 0.1)), 0.01),
                memory_limit_mb=256,
                output_limit_bytes=64 * 1024,
                process_limit=8,
                cancel_check=cancel_event.is_set,
            )
        child_pid = None
        ready_deadline = time.monotonic() + 10.0
        while child_pid is None and time.monotonic() < ready_deadline:
            try:
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, UnicodeError, ValueError):
                time.sleep(0.01)
        if result.get("reason") not in {"time_limit", "cancelled"}:
            raise RuntimeError(f"synthetic managed tree ended as {result.get('reason')}")
        if child_pid is not None:
            cleanup_deadline = time.monotonic() + 10.0
            while process_alive(child_pid) and time.monotonic() < cleanup_deadline:
                time.sleep(0.05)
            if process_alive(child_pid):
                raise RuntimeError(
                    f"synthetic descendant {child_pid} survived cleanup"
                )
        classification = str(spec.get("classification", "infrastructure-failed"))
        return {
            "index": task.index,
            "id": task.mutation_id,
            "classification": classification,
            "diagnostic": {"code": f"synthetic-{result.get('reason')}"},
            "descendant_pid": child_pid,
            "descendants_cleaned": True,
        }
    if spec.get("request_cancel"):
        cancel_event.set()
    deadline = time.monotonic() + max(float(spec.get("delay", 0)), 0.0)
    while time.monotonic() < deadline:
        if cancel_event.is_set() and not spec.get("ignore_cancel"):
            raise ProcessCancelled("synthetic peer cancellation")
        time.sleep(min(0.01, max(deadline - time.monotonic(), 0.0)))
    if spec.get("raise"):
        raise RuntimeError(str(spec["raise"]))
    classification = str(spec.get("classification", "survived"))
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"invalid synthetic classification: {classification}")
    return {
        "index": task.index,
        "id": task.mutation_id,
        "classification": classification,
        "diagnostic": {"code": "synthetic"},
        "descendants_cleaned": True,
    }


def _run_judge_task(task, cancel_event):
    worker_root = Path(task.worker_root)
    problem = worker_root / "problem"
    shutil.copytree(task.baseline, problem)
    source_relative = Path(*PurePosixPath(task.source_relative).parts)
    (problem / source_relative).write_text(
        apply_mutation(task.source, task.mutation),
        encoding="utf-8",
        newline="",
    )
    config = copy.deepcopy(task.config)
    config["solutions"] = {
        "accepted": [{
            "file": task.source_relative,
            "expected": {"status": "AC", "all": True},
        }],
        "brute": [],
        "wrong": [],
    }
    write_yaml(problem / "probhub.yaml", config)
    judged = judge_problem(
        worker_root,
        problem,
        use_cache=task.use_cache,
        timeout=task.mutant_timeout,
        output_limit_bytes=task.execution_profile["judge_output_limit_bytes"],
        memory_limit_mb=task.execution_profile["judge_memory_limit_mb"],
        process_limit=task.execution_profile["judge_process_limit"],
        cancel_check=cancel_event.is_set,
    )
    if cancel_event.is_set() or (judged.get("final") or {}).get("code") == "judge_cancelled":
        raise ProcessCancelled("judge cancelled by shadow scheduler")
    classification, hits, diagnostic = _classify_judge_result(
        judged, task.source_relative
    )
    events = judged.get("events") or []
    case_events = [
        event for event in events
        if event.get("type") == "case" and event.get("program") == task.source_relative
    ]
    times = [
        float(event["time"])
        for event in case_events
        if isinstance(event.get("time"), (int, float))
    ]
    memories = [
        float(event["memory"])
        for event in case_events
        if isinstance(event.get("memory"), (int, float))
    ]
    hit_signature = [
        {
            "case": hit.get("case"),
            "status": hit.get("status"),
            "termination_reason": hit.get("termination_reason"),
            "failure_kind": hit.get("failure_kind"),
        }
        for hit in hits
    ]
    return {
        "index": task.index,
        "id": task.mutation_id,
        "classification": classification,
        "diagnostic": {"code": str(diagnostic or "unknown")[:256]},
        "final_code": (judged.get("final") or {}).get("code"),
        "hit_cases": hit_signature,
        "hit_cases_total": len(hits),
        "case_events": len(case_events),
        "max_case_seconds": round(max(times), 6) if times else None,
        "max_case_memory_mb": round(max(memories), 6) if memories else None,
    }


def _worker_entry(task, cancel_event, connection):
    started = time.perf_counter()
    result = None
    try:
        if cancel_event.is_set():
            raise ProcessCancelled("cancelled before worker start")
        result = (
            _run_synthetic(task, cancel_event)
            if task.synthetic is not None
            else _run_judge_task(task, cancel_event)
        )
    except ProcessCancelled as exc:
        result = _failure_result(
            task, "cancelled", exc, classification="cancelled"
        )
    except BaseException as exc:
        result = _failure_result(task, type(exc).__name__, exc)
    if result.get("classification") == "infrastructure-failed":
        cancel_event.set()
    result.setdefault("descendants_cleaned", True)
    cleanup_error = None
    try:
        if (task.synthetic or {}).get("cleanup_failure"):
            Path(task.worker_root).mkdir(parents=True, exist_ok=True)
            raise OSError("synthetic cleanup failure")
        shutil.rmtree(task.worker_root, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_error = exc
        result = _failure_result(task, "worker_cleanup_failed", exc)
        cancel_event.set()
        delay = (task.synthetic or {}).get("cleanup_failure_send_delay", 0)
        if delay:
            time.sleep(max(float(delay), 0.0))
    result["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    result["worker_cleaned"] = cleanup_error is None and not Path(task.worker_root).exists()
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


def _cancelled_record(task, code, message, *, descendants_cleaned=True):
    result = _failure_result(task, code, message, classification="cancelled")
    result["elapsed_seconds"] = None
    result["worker_cleaned"] = not Path(task.worker_root).exists()
    result["descendants_cleaned"] = bool(descendants_cleaned)
    return result


def _parent_cleanup_worker(task):
    try:
        shutil.rmtree(task.worker_root)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return not Path(task.worker_root).exists()


def _synthetic_registered_pids(task):
    if not (task.synthetic or {}).get("hard_kill_descendant"):
        return set()
    pid_path = Path(task.baseline).parent / f"hard-child-{task.index}.pid"
    try:
        return {int(pid_path.read_text(encoding="utf-8"))}
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return set()


def _can_claim_descendant_cleanup(task):
    return task.synthetic is not None and not (task.synthetic or {}).get(
        "real_like"
    )


class _MultiprocessingProcessAdapter:
    def __init__(self, process):
        self._process = process
        self.pid = process.pid

    def wait(self, timeout=None):
        self._process.join(timeout=timeout)
        if self._process.is_alive():
            raise subprocess.TimeoutExpired("multiprocessing worker", timeout)
        return self._process.exitcode

    def kill(self):
        self._process.kill()


def _stop_worker(process, known_pids=(), *, allow_descendant_claim=True):
    known_pids = set(known_pids or ())
    if process.pid:
        known_pids.update(snapshot_process_tree(process.pid))
    if process.is_alive():
        terminate_external_process_tree(
            _MultiprocessingProcessAdapter(process), known_pids
        )
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)
    cleanup_deadline = time.monotonic() + 10.0
    external_pids = {pid for pid in known_pids if pid != process.pid}
    while (
        any(process_alive(pid) for pid in external_pids)
        and time.monotonic() < cleanup_deadline
    ):
        time.sleep(0.05)
    worker_stopped = not process.is_alive()
    descendants_cleaned = (
        not process.is_alive()
        and not any(process_alive(pid) for pid in external_pids)
    )
    return {
        "worker_stopped": worker_stopped,
        "descendants_cleaned": (
            descendants_cleaned if allow_descendant_claim else False
        ),
    }


def run_spawn_scheduler(
    tasks,
    jobs,
    *,
    timeout,
    cancel_grace=CANCEL_GRACE_SECONDS,
):
    """Execute a bounded set of tasks with multiprocessing ``spawn``."""
    tasks = tuple(tasks)
    if jobs not in {1, 2, 4}:
        raise ValueError("jobs must be one of 1, 2, or 4")
    if timeout <= 0 or cancel_grace < 0:
        raise ValueError("timeout must be positive and cancel_grace non-negative")
    if len({task.index for task in tasks}) != len(tasks):
        raise ValueError("task indices must be unique")

    context = multiprocessing.get_context("spawn")
    cancel_event = context.Event()
    active = {}
    results = {}
    next_task = 0
    max_active = 0
    stop_code = None
    started = time.perf_counter()
    deadline = started + float(timeout)
    cancellation_started = None
    processes = []

    def launch(task):
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_worker_entry,
            args=(task, cancel_event, child),
            name=f"mutation-shadow-{task.index}",
        )
        process.start()
        child.close()
        processes.append(process)
        active[task.index] = (process, parent, task, {process.pid})

    try:
        while active or (next_task < len(tasks) and stop_code is None):
            made_progress = False
            for index in sorted(tuple(active)):
                process, connection, task, known_pids = active[index]
                if process.is_alive():
                    known_pids.update(snapshot_process_tree(process.pid))
                if connection.poll():
                    try:
                        result = connection.recv()
                    except (EOFError, OSError) as exc:
                        result = _failure_result(task, "worker_result_failed", exc)
                        result["elapsed_seconds"] = None
                        result["worker_cleaned"] = not Path(task.worker_root).exists()
                        result["descendants_cleaned"] = False
                    connection.close()
                    process.join(timeout=0.5)
                    if process.is_alive():
                        stopped = _stop_worker(
                            process,
                            known_pids,
                            allow_descendant_claim=_can_claim_descendant_cleanup(task),
                        )
                        result.update(stopped)
                    if not result.get("worker_cleaned", False):
                        result["worker_cleaned"] = _parent_cleanup_worker(task)
                    results[index] = result
                    del active[index]
                    made_progress = True
                    if result.get("classification") == "infrastructure-failed" and stop_code is None:
                        stop_code = "infrastructure-failed"
                        cancellation_started = time.monotonic()
                        cancel_event.set()
                elif not process.is_alive():
                    connection.close()
                    process.join(timeout=0.5)
                    result = _failure_result(
                        task,
                        "worker_exited",
                        f"worker exited with code {process.exitcode} without a result",
                    )
                    result["elapsed_seconds"] = None
                    result["worker_cleaned"] = _parent_cleanup_worker(task)
                    stopped = _stop_worker(
                        process,
                        known_pids,
                        allow_descendant_claim=_can_claim_descendant_cleanup(task),
                    )
                    result.update(stopped)
                    results[index] = result
                    del active[index]
                    made_progress = True
                    if stop_code is None:
                        stop_code = "infrastructure-failed"
                        cancellation_started = time.monotonic()
                        cancel_event.set()

            now = time.monotonic()
            if cancel_event.is_set() and stop_code is None:
                stop_code = "infrastructure-failed"
                cancellation_started = now
            if now >= deadline and stop_code is None:
                stop_code = "scheduler-timeout"
                cancellation_started = now
                cancel_event.set()
            if (
                stop_code is not None
                and active
                and cancellation_started is not None
                and now - cancellation_started >= cancel_grace
            ):
                for index in sorted(tuple(active)):
                    process, connection, task, known_pids = active[index]
                    stop_after = cancel_grace
                    if not _can_claim_descendant_cleanup(task):
                        stop_after += task.mutant_timeout
                    if now - cancellation_started < stop_after:
                        continue
                    del active[index]
                    known_pids.update(_synthetic_registered_pids(task))
                    connection.close()
                    stopped = _stop_worker(
                        process,
                        known_pids,
                        allow_descendant_claim=_can_claim_descendant_cleanup(task),
                    )
                    _parent_cleanup_worker(task)
                    results[index] = _cancelled_record(
                        task,
                        stop_code,
                        "worker terminated after scheduler cancellation",
                        descendants_cleaned=stopped["descendants_cleaned"],
                    )
                    results[index]["worker_stopped"] = stopped["worker_stopped"]
                    made_progress = True
            while (
                stop_code is None
                and not cancel_event.is_set()
                and len(active) < jobs
                and next_task < len(tasks)
            ):
                launch(tasks[next_task])
                next_task += 1
                max_active = max(max_active, len(active))
                made_progress = True
            if not made_progress and (active or next_task < len(tasks)):
                time.sleep(0.01)
    finally:
        cancel_event.set()
        for process, connection, task, known_pids in active.values():
            connection.close()
            _stop_worker(
                process,
                known_pids,
                allow_descendant_claim=_can_claim_descendant_cleanup(task),
            )
            _parent_cleanup_worker(task)

    for task in tasks[next_task:]:
        results[task.index] = _cancelled_record(
            task,
            stop_code or "not-started",
            "task was not started after scheduler cancellation",
        )
    ordered = [results[task.index] for task in sorted(tasks, key=lambda item: item.index)]
    elapsed = time.perf_counter() - started
    classifications = {
        name: sum(result["classification"] == name for result in ordered)
        for name in CLASSIFICATIONS
    }
    started_tasks = next_task
    completed = sum(
        result.get("classification") not in {"cancelled", "infrastructure-failed"}
        for result in ordered
    )
    return {
        "planned": len(tasks),
        "started": started_tasks,
        "completed": completed,
        "executed": started_tasks,
        "classifications": classifications,
        "mutations": ordered,
        "scheduler_wall_seconds": round(elapsed, 6),
        "scheduler_mutants_per_second": (
            round(completed / elapsed, 6) if elapsed > 0 else None
        ),
        "max_active": max_active,
        "stop_code": stop_code,
        "workers_cleaned": (
            all(not Path(task.worker_root).exists() for task in tasks)
            and all(not process.is_alive() for process in processes)
        ),
        "descendants_cleaned": all(
            result.get("descendants_cleaned", True) for result in ordered
        ),
    }


def prepare_plan(
    temporary_root,
    *,
    max_mutants,
    operators,
    mutant_timeout,
    use_cache=False,
):
    live_root, workspace = load_workspace(FIXTURE_ROOT)
    matches = [entry for entry in problem_entries(workspace) if entry["id"] == PROBLEM_ID]
    if len(matches) != 1:
        raise ProbHubError("fixed mutation fixture M01 is unavailable")
    entry = matches[0]
    live_problem = resolve_problem_dir(live_root, entry)
    baseline = Path(temporary_root) / "baseline"
    workers_root = Path(temporary_root) / "workers"
    workers_root.mkdir()
    snapshot_started = time.perf_counter()
    config, source_hash, data_hash = copy_problem_consistently(
        live_root,
        entry,
        baseline,
        operation="shadow mutation benchmark snapshot",
    )
    snapshot_seconds = time.perf_counter() - snapshot_started
    accepted = normalize_solution_entries(config)["std"]
    if not accepted or not accepted[0].get("file"):
        raise ProbHubError("M01 requires an accepted C++ source")
    source_path, source_error, source_message = resolve_solution_source(
        baseline, accepted[0]["file"]
    )
    if source_path is None or source_path.suffix.lower() != ".cpp":
        raise ProbHubError(
            f"M01 accepted source is invalid: {source_message or source_error}"
        )
    source = source_path.read_text(encoding="utf-8")
    selected_operators = list(MUTATION_OPERATORS if operators is None else operators)
    planning_started = time.perf_counter()
    mutation_plan = plan_mutations(
        source,
        operators=selected_operators,
        max_mutants=max_mutants,
    )
    planning_seconds = time.perf_counter() - planning_started
    source_relative = PurePosixPath(
        str(accepted[0]["file"]).replace("\\", "/")
    ).as_posix()
    execution_profile = _mutation_execution_profile(config)
    tasks = tuple(
        WorkerTask(
            index=index,
            mutation_id=mutation.id,
            mutation=mutation,
            baseline=str(baseline),
            worker_root=str(workers_root / f"worker-{index:04d}"),
            source=source,
            source_relative=source_relative,
            config=config,
            execution_profile=execution_profile,
            mutant_timeout=float(mutant_timeout),
            use_cache=bool(use_cache),
        )
        for index, mutation in enumerate(mutation_plan["mutations"])
    )
    return BenchmarkPlan(
        live_root=Path(live_root),
        live_problem=Path(live_problem),
        baseline=baseline,
        workers_root=workers_root,
        source=source,
        source_relative=source_relative,
        config=config,
        source_hash=source_hash,
        data_hash=data_hash,
        plan_hash=mutation_plan["plan_hash"],
        baseline_hash=_hash_tree(baseline),
        snapshot_seconds=round(snapshot_seconds, 6),
        planning_seconds=round(planning_seconds, 6),
        execution_profile=execution_profile,
        tasks=tasks,
    )


def run_benchmark(
    *,
    jobs,
    timeout,
    max_mutants,
    operators,
    mutant_timeout,
    use_cache=False,
):
    total_started = time.perf_counter()
    live_root, workspace = load_workspace(FIXTURE_ROOT)
    entry = next(item for item in problem_entries(workspace) if item["id"] == PROBLEM_ID)
    live_problem = resolve_problem_dir(live_root, entry)
    live_before = _live_input_hashes(live_problem)
    formal_before = _formal_artifact_hashes(live_root, live_problem)
    mutation_artifacts_before = _local_mutation_artifact_hashes(live_problem)
    fixture_before = _hash_tree(live_root)

    with tempfile.TemporaryDirectory(prefix="probhub-mutation-shadow-") as temp:
        plan = prepare_plan(
            Path(temp),
            max_mutants=max_mutants,
            operators=operators,
            mutant_timeout=mutant_timeout,
            use_cache=use_cache,
        )
        if not plan.tasks:
            raise ProbHubError("M01 mutation plan is empty")
        scheduled = run_spawn_scheduler(
            plan.tasks,
            jobs,
            timeout=timeout,
            cancel_grace=CANCEL_GRACE_SECONDS,
        )
        workers_cleaned = scheduled["workers_cleaned"] and not any(
            plan.workers_root.iterdir()
        )

    live_after = _live_input_hashes(live_problem)
    formal_after = _formal_artifact_hashes(live_root, live_problem)
    mutation_artifacts_after = _local_mutation_artifact_hashes(live_problem)
    fixture_after = _hash_tree(live_root)
    wall_seconds = time.perf_counter() - total_started
    report = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK,
        "jobs": jobs,
        "use_cache": bool(use_cache),
        "environment": _environment_identity(),
        "planned": scheduled["planned"],
        "started": scheduled["started"],
        "completed": scheduled["completed"],
        "executed": scheduled["executed"],
        "classifications": scheduled["classifications"],
        "mutations": scheduled["mutations"],
        "wall_seconds": round(wall_seconds, 6),
        "mutants_per_second": (
            round(scheduled["completed"] / wall_seconds, 6) if wall_seconds > 0 else None
        ),
        "scheduler_wall_seconds": scheduled["scheduler_wall_seconds"],
        "scheduler_mutants_per_second": scheduled["scheduler_mutants_per_second"],
        "max_active": scheduled["max_active"],
        "phase_seconds": {
            "snapshot": plan.snapshot_seconds,
            "planning": plan.planning_seconds,
            "scheduler": scheduled["scheduler_wall_seconds"],
        },
        "stop_code": scheduled["stop_code"],
        "hashes": {
            "source": plan.source_hash,
            "data": plan.data_hash,
            "plan": plan.plan_hash,
            "baseline": plan.baseline_hash,
            "formal_before": formal_before,
            "formal_after": formal_after,
            "mutation_artifacts_before": mutation_artifacts_before,
            "mutation_artifacts_after": mutation_artifacts_after,
            "fixture_before": fixture_before,
            "fixture_after": fixture_after,
        },
        "live_inputs_unchanged": live_before == live_after == {
            "source": plan.source_hash,
            "data": plan.data_hash,
        },
        "formal_artifacts_unchanged": formal_before == formal_after,
        "mutation_evidence_unchanged": (
            mutation_artifacts_before == mutation_artifacts_after
        ),
        "fixture_unchanged": fixture_before == fixture_after,
        "workers_cleaned": workers_cleaned,
        "descendants_cleaned": scheduled["descendants_cleaned"],
        "execution_profile": {
            "schema_version": 1,
            "scheduler": "spawn-bounded-shadow-v1",
            "jobs": jobs,
            "snapshot": "immutable-problem-v1",
            "mutant_timeout_seconds": float(mutant_timeout),
            "cooperative_cancel_grace_seconds": CANCEL_GRACE_SECONDS,
            "real_worker_forced_stop_after_cancel_seconds": (
                CANCEL_GRACE_SECONDS + float(mutant_timeout)
            ),
            "forced_real_worker_descendant_cleanup_claim": False,
            "judge_output_limit_bytes": plan.execution_profile[
                "judge_output_limit_bytes"
            ],
            "judge_memory_limit_mb": plan.execution_profile[
                "judge_memory_limit_mb"
            ],
            "judge_process_limit": plan.execution_profile[
                "judge_process_limit"
            ],
        },
        "performance_gate": False,
    }
    return report


def _comparison_signature(report):
    return [
        {
            "id": item.get("id"),
            "classification": item.get("classification"),
            "final_code": item.get("final_code"),
            "hit_cases": item.get("hit_cases"),
            "hit_cases_total": item.get("hit_cases_total"),
        }
        for item in report.get("mutations", [])
    ]


def _comparison_identity(report):
    hashes = report.get("hashes") or {}
    return {
        key: hashes.get(key)
        for key in ("source", "data", "plan", "baseline")
    }


def run_matrix(
    *,
    jobs_values,
    repetitions,
    timeout,
    max_mutants,
    operators,
    mutant_timeout,
    use_cache=False,
):
    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    jobs_values = tuple(jobs_values)
    if not jobs_values or any(value not in {1, 2, 4} for value in jobs_values):
        raise ValueError("jobs matrix must contain only 1, 2, or 4")
    if len(set(jobs_values)) != len(jobs_values):
        raise ValueError("jobs matrix values must be unique")
    runs = []
    reference = None
    reference_identity = None
    equivalent = True
    identities_equivalent = True
    for repetition in range(repetitions):
        offset = repetition % len(jobs_values)
        run_order = jobs_values[offset:] + jobs_values[:offset]
        for jobs in run_order:
            report = run_benchmark(
                jobs=jobs,
                timeout=timeout,
                max_mutants=max_mutants,
                operators=operators,
                mutant_timeout=mutant_timeout,
                use_cache=use_cache,
            )
            signature = _comparison_signature(report)
            identity = _comparison_identity(report)
            if reference is None:
                reference = signature
                reference_identity = identity
            equivalent = equivalent and signature == reference
            identities_equivalent = (
                identities_equivalent and identity == reference_identity
            )
            runs.append({
                "repetition": repetition + 1,
                "jobs": jobs,
                "wall_seconds": report["wall_seconds"],
                "mutants_per_second": report["mutants_per_second"],
                "report": report,
            })
    by_jobs = {}
    for jobs in jobs_values:
        values = [item["wall_seconds"] for item in runs if item["jobs"] == jobs]
        values.sort()
        lower = values[: max(1, len(values) // 2)]
        upper = values[(len(values) + 1) // 2 :] or values[-1:]
        p95_index = max(0, min(len(values) - 1, int(math.ceil(len(values) * 0.95)) - 1))
        distribution_statistics_meaningful = len(values) >= 5
        tail_statistics_meaningful = len(values) >= 20
        by_jobs[str(jobs)] = {
            "runs": len(values),
            "wall_seconds": values,
            "median_wall_seconds": median(values),
            "p95_wall_seconds": values[p95_index] if tail_statistics_meaningful else None,
            "iqr_wall_seconds": (
                median(upper) - median(lower)
                if distribution_statistics_meaningful else None
            ),
            "statistics_meaningful": (
                distribution_statistics_meaningful
                and tail_statistics_meaningful
            ),
            "distribution_statistics_meaningful": distribution_statistics_meaningful,
            "tail_statistics_meaningful": tail_statistics_meaningful,
        }
    baseline = by_jobs.get("1", {}).get("median_wall_seconds")
    if baseline:
        for summary in by_jobs.values():
            median_seconds = summary["median_wall_seconds"]
            summary["speedup_vs_jobs_1"] = round(
                baseline / median_seconds, 6
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK + "-matrix",
        "jobs": list(jobs_values),
        "repetitions": repetitions,
        "use_cache": bool(use_cache),
        "results_equivalent": equivalent and identities_equivalent,
        "classifications_equivalent": equivalent,
        "identities_equivalent": identities_equivalent,
        "reference_signature": reference or [],
        "reference_identity": reference_identity or {},
        "summary": by_jobs,
        "runs": runs,
        "performance_gate": False,
    }


class _StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _write_stdout(payload):
    stream = sys.stdout
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(payload.encode("utf-8", errors="backslashreplace"))
        binary.flush()
        return
    stream.write(payload)
    stream.flush()


def build_parser():
    parser = _StructuredArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="run jobs=1,2,4 sequentially and compare structured results",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--mutant-timeout", type=float, default=120.0)
    parser.add_argument("--max-mutants", type=int, default=3)
    parser.add_argument("--operator", action="append", choices=MUTATION_OPERATORS)
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="allow each isolated Judge worker to use its own temporary cache",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv=None):
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    output = None
    for index, value in enumerate(raw_argv):
        if value == "--output" and index + 1 < len(raw_argv):
            output = Path(raw_argv[index + 1])
        elif value.startswith("--output="):
            output = Path(value.split("=", 1)[1])
    try:
        args = parser.parse_args(raw_argv)
        output = args.output
        if args.timeout <= 0 or args.mutant_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if args.repetitions <= 0:
            raise ValueError("repetitions must be positive")
        if args.max_mutants <= 0 or args.max_mutants > MAX_MUTANTS:
            raise ValueError(f"max-mutants must be in 1..{MAX_MUTANTS}")
        report = (
            run_matrix(
                jobs_values=(1, 2, 4),
                repetitions=args.repetitions,
                timeout=args.timeout,
                max_mutants=args.max_mutants,
                operators=args.operator,
                mutant_timeout=args.mutant_timeout,
                use_cache=args.use_cache,
            )
            if args.matrix
            else run_benchmark(
                jobs=args.jobs,
                timeout=args.timeout,
                max_mutants=args.max_mutants,
                operators=args.operator,
                mutant_timeout=args.mutant_timeout,
                use_cache=args.use_cache,
            )
        )
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        TypeError,
        ProbHubError,
    ) as exc:
        failure = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK,
            "ok": False,
            "error": getattr(exc, "code", None) or type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False, indent=2) + "\n"
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(failure, encoding="utf-8", newline="")
        _write_stdout(failure)
        return 1
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8", newline="")
    _write_stdout(payload)
    if args.matrix:
        return 0 if (
            report["results_equivalent"]
            and all(
                item["report"]["classifications"]["infrastructure-failed"] == 0
                and item["report"]["classifications"]["cancelled"] == 0
                and item["report"]["live_inputs_unchanged"]
                and item["report"]["formal_artifacts_unchanged"]
                and item["report"]["mutation_evidence_unchanged"]
                and item["report"]["fixture_unchanged"]
                and item["report"]["workers_cleaned"]
                and item["report"]["descendants_cleaned"]
                for item in report["runs"]
            )
        ) else 1
    return 0 if (
        report["classifications"]["infrastructure-failed"] == 0
        and report["classifications"]["cancelled"] == 0
        and report["live_inputs_unchanged"]
        and report["formal_artifacts_unchanged"]
        and report["mutation_evidence_unchanged"]
        and report["fixture_unchanged"]
        and report["workers_cleaned"]
        and report["descendants_cleaned"]
    ) else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
