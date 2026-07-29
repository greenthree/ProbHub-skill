#!/usr/bin/env python3
"""Compatibility wrapper for deterministic ProbHub Core packaging."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.package_tools import build_package, build_verified_package
from probhub.io import read_yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("problem_dir")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--require-pdf", action="store_true")
    args = parser.parse_args()
    problem_dir = Path(args.problem_dir).resolve()
    if not problem_dir.is_dir():
        parser.error(f"problem directory not found: {problem_dir}")
    output = Path(args.output).resolve() if args.output else problem_dir.with_suffix(".zip")
    verification_args = {}
    config_path = problem_dir / "probhub.yaml"
    if config_path.is_file():
        config = read_yaml(config_path)
        verification_args.update(
            expected_config=config,
            source_problem_dir=problem_dir,
            run_validator=True,
        )
    files, result = build_verified_package(
        problem_dir,
        output,
        require_pdf=args.require_pdf,
        **verification_args,
    )
    if not result["ok"]:
        for error in result["errors"]:
            print(f"[-] {error}", file=sys.stderr)
        return 1
    print(f"[+] Built {output} ({len(files)} files)")
    print(f"[+] Verified {result['stats']['sample_cases']} sample / {result['stats']['secret_cases']} secret cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
