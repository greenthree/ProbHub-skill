#!/usr/bin/env python3
"""Measure managed short-process startup latency with and without a memory limit."""

import argparse
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.process_control import spawn_managed


def _target_command():
    if platform.system() == "Windows":
        return [sys.executable, "-I", "-S", "-c", "pass"]
    return ["/bin/true"]


def _percentile(values, percentile):
    index = max(0, min(len(values) - 1, math.ceil(len(values) * percentile) - 1))
    return values[index]


def _measure(command, *, rounds, warmup, memory_limit_mb):
    values = []
    for index in range(rounds + warmup):
        started = time.perf_counter()
        managed = spawn_managed(
            command,
            memory_limit_mb=memory_limit_mb,
            process_limit=8,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            returncode = managed.proc.wait(timeout=10)
        finally:
            try:
                if managed.proc.poll() is None:
                    managed.terminate()
                    managed.proc.wait(timeout=2)
            finally:
                managed.close()
        if returncode != 0:
            raise RuntimeError(f"benchmark target exited with {returncode}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        if index >= warmup:
            values.append(elapsed_ms)
    values.sort()
    return {
        "rounds": len(values),
        "mean_ms": statistics.mean(values),
        "p50_ms": statistics.median(values),
        "p95_ms": _percentile(values, 0.95),
        "min_ms": values[0],
        "max_ms": values[-1],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--memory-limit-mb", type=int, default=256)
    args = parser.parse_args(argv)
    if args.rounds <= 0 or args.warmup < 0 or args.memory_limit_mb <= 0:
        parser.error("rounds and memory limit must be positive; warmup must be non-negative")

    command = _target_command()
    unlimited = _measure(
        command,
        rounds=args.rounds,
        warmup=args.warmup,
        memory_limit_mb=None,
    )
    limited = _measure(
        command,
        rounds=args.rounds,
        warmup=args.warmup,
        memory_limit_mb=args.memory_limit_mb,
    )
    print(json.dumps({
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "command": command,
        "memory_limit_mb": args.memory_limit_mb,
        "unlimited": unlimited,
        "memory_limited": limited,
        "incremental_p50_ms": limited["p50_ms"] - unlimited["p50_ms"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
