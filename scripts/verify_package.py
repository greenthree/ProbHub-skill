#!/usr/bin/env python3
"""Compatibility wrapper for ProbHub Core package verification."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.package_tools import verify_package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path")
    parser.add_argument("--require-pdf", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    result = verify_package(args.zip_path, require_pdf=args.require_pdf)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[{'PASS' if result['ok'] else 'FAIL'}] {args.zip_path}")
        stats = result["stats"]
        print(f"  files={stats['files']} sample={stats['sample_cases']} secret={stats['secret_cases']}")
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        for error in result["errors"]:
            print(f"  error: {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
