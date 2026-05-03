"""Compatibility wrapper for periodic task manager CLI/service."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cli.periodic_cli import build_parser, main, parse_time_of_day, run_cli, validate_add_params
from service.periodic_service import PeriodicTaskManager

__all__ = [
    "PeriodicTaskManager",
    "parse_time_of_day",
    "validate_add_params",
    "build_parser",
    "run_cli",
    "main",
]


if __name__ == "__main__":
    main()
