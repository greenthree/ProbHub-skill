import subprocess
import sys
import time
from pathlib import Path


def _record(task, classification, code):
    return {
        **task.mutation.as_dict(),
        "classification": classification,
        "hit_cases": [],
        "hit_cases_total": 0,
        "hit_cases_truncated": False,
        "diagnostic": {"code": code},
    }


def controlled_mutation_worker(task, cancel_event, connection):
    """Picklable spawn target for production scheduler failure tests."""
    mode = task.config["_scheduler_test_mode"]
    ready = Path(task.baseline).parent / f"{mode}-peer.pid"
    child = None
    try:
        if mode == "external":
            ready.write_text(str(task.index), encoding="utf-8")
            while not cancel_event.is_set():
                time.sleep(0.01)
            connection.send(_record(task, "cancelled", "mutation_cancelled"))
            return

        if task.index == 0:
            deadline = time.monotonic() + 10.0
            while not ready.is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            connection.send(_record(
                task,
                "infrastructure-failed",
                "fixture_infrastructure_failure",
            ))
            return

        if task.index == 1 and mode == "cooperative":
            ready.write_text(str(task.index), encoding="utf-8")
            while not cancel_event.is_set():
                time.sleep(0.01)
            connection.send(_record(task, "cancelled", "mutation_cancelled"))
            return

        if task.index == 1 and mode == "stubborn":
            child = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", "import time; time.sleep(300)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready.write_text(str(child.pid), encoding="utf-8")
            while True:
                time.sleep(1.0)

        connection.send(_record(task, "survived", "fixture_survived"))
    finally:
        connection.close()
