#!/usr/bin/env python3
"""Fallback entry point for Skill installations without the npm bin."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probhub.cli import main

if __name__ == "__main__":
    sys.exit(main())
