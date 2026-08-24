"""Validate and run the repository's explicit unittest shard manifest.

The manifest is intentionally explicit: a newly added ``tests/test_*.py``
file must be assigned to a shard before CI can run it.
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
MANIFEST_PATH = TESTS_ROOT / "test-shards.json"
FAST_MANIFEST_PATH = TESTS_ROOT / "test-fast.json"
SCHEMA_VERSION = 1


class TestShardError(ValueError):
    """Raised when a test manifest is invalid or incomplete."""


def _read_manifest(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TestShardError(f"cannot read test manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise TestShardError(f"{path.name} must use schema_version {SCHEMA_VERSION}")
    return payload


def _validate_test_names(names: list[object], *, label: str) -> list[str]:
    if not isinstance(names, list) or not names:
        raise TestShardError(f"{label}.tests must be a non-empty list")
    result = []
    for name in names:
        if not isinstance(name, str) or not name.startswith("test_") or not name.endswith(".py"):
            raise TestShardError(f"{label} contains an invalid test filename: {name!r}")
        path = Path(name)
        if path.name != name or path.parent != Path("."):
            raise TestShardError(f"{label} contains a non-root test path: {name!r}")
        result.append(name)
    if len(set(result)) != len(result):
        raise TestShardError(f"{label} contains duplicate test filenames")
    return result


def _is_regular_test_file(path: Path) -> bool:
    """Return whether path is a non-linked regular test source file."""

    return path.is_file() and not path.is_symlink()


def _repository_test_names() -> list[str]:
    return sorted(
        path.name
        for path in TESTS_ROOT.glob("test_*.py")
        if _is_regular_test_file(path)
    )


def load_shards(path: Path = MANIFEST_PATH) -> dict[str, list[str]]:
    """Load and fully validate the explicit shard manifest."""

    payload = _read_manifest(path)
    raw_shards = payload.get("shards")
    if not isinstance(raw_shards, list) or not raw_shards:
        raise TestShardError("test-shards.json.shards must be a non-empty list")
    shards: dict[str, list[str]] = {}
    assigned: list[str] = []
    for index, raw in enumerate(raw_shards):
        if not isinstance(raw, dict):
            raise TestShardError(f"shards[{index}] must be an object")
        shard_id = raw.get("id")
        if not isinstance(shard_id, str) or not shard_id or shard_id in shards:
            raise TestShardError(f"shards[{index}] has an invalid or duplicate id")
        names = _validate_test_names(raw.get("tests"), label=f"shards[{index}]")
        shards[shard_id] = names
        assigned.extend(names)

    if len(set(assigned)) != len(assigned):
        duplicates = sorted({name for name in assigned if assigned.count(name) > 1})
        raise TestShardError(f"test files assigned to multiple shards: {duplicates}")
    actual = _repository_test_names()
    listed = sorted(assigned)
    if listed != actual:
        missing = sorted(set(actual) - set(listed))
        unknown = sorted(set(listed) - set(actual))
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise TestShardError("test shard manifest does not match test files: " + ", ".join(details))
    return shards


def load_fast_tests(path: Path = FAST_MANIFEST_PATH) -> list[str]:
    """Load the deliberately small, explicit local fast-feedback test set."""

    payload = _read_manifest(path)
    names = _validate_test_names(payload.get("tests"), label=path.name)
    actual = set(_repository_test_names())
    unknown = sorted(set(names) - actual)
    if unknown:
        raise TestShardError(f"fast test manifest contains unknown files: {unknown}")
    return names


def _suite_for(names: list[str]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        path = TESTS_ROOT / name
        if not _is_regular_test_file(path):
            raise TestShardError(f"test source is not a regular file: {name}")
        discovered = loader.discover(str(TESTS_ROOT), pattern=name)
        if discovered.countTestCases() == 0:
            raise TestShardError(f"test source contains no test cases: {name}")
        suite.addTests(discovered)
    return suite


def run_tests(names: list[str], *, verbosity: int = 2) -> int:
    """Run explicit files in one process and return a shell exit code."""

    tests_path = str(TESTS_ROOT)
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)
    result = unittest.TextTestRunner(verbosity=verbosity).run(_suite_for(names))
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--shard", help="run one shard from test-shards.json")
    group.add_argument("--fast", action="store_true", help="run the explicit local fast set")
    parser.add_argument("--validate", action="store_true", help="validate manifests without running tests")
    parser.add_argument("--quiet", action="store_true", help="reduce unittest output")
    args = parser.parse_args(argv)
    if not args.validate and not args.shard and not args.fast:
        parser.error("one of --shard, --fast or --validate is required")
    try:
        shards = load_shards()
        names = load_fast_tests() if args.fast else shards.get(args.shard)
        if args.shard and names is None:
            raise TestShardError(f"unknown test shard: {args.shard}")
        if args.validate:
            print(json.dumps({"ok": True, "shards": shards, "fast": load_fast_tests()}, ensure_ascii=False))
            return 0
        return run_tests(names, verbosity=1 if args.quiet else 2)
    except TestShardError as exc:
        print(f"test shard manifest error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
