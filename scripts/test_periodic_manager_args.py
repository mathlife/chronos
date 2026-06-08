#!/usr/bin/env python3
"""Regression checks for periodic manager argv construction."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.todo import build_periodic_manager_args


def test_build_periodic_manager_args_includes_ranges_and_flags() -> None:
    args = build_periodic_manager_args(
        "周会",
        category="Work",
        cycle_type="weekly",
        kwargs={
            "time": "10:00",
            "weekday": 1,
            "range_start": 3,
            "range_end": 5,
            "task_kind": "scheduled",
            "special_handler": "foo",
            "handler_payload": '{"x":1}',
        },
    )
    assert args[:6] == [sys.executable, str(PROJECT_ROOT / "scripts" / "periodic_task_manager.py"), "--add", "--name", "周会", "--category"]
    assert "Work" in args
    assert "--cycle-type" in args and "weekly" in args
    assert "--time" in args and "10:00" in args
    assert "--weekday" in args and "1" in args
    assert "--range-start" in args and "3" in args
    assert "--range-end" in args and "5" in args
    assert "--task-kind" in args and "scheduled" in args
    assert "--special-handler" in args and "foo" in args
    assert "--handler-payload" in args and '{"x":1}' in args


def test_build_periodic_manager_args_omits_empty_optional_values() -> None:
    args = build_periodic_manager_args(
        "提醒",
        category="Inbox",
        cycle_type="daily",
        kwargs={"time": "09:00", "special_handler": "", "handler_payload": None, "system_command": ""},
    )
    assert "--special-handler" not in args
    assert "--handler-payload" not in args
    assert "--system-command" not in args


if __name__ == "__main__":
    test_build_periodic_manager_args_includes_ranges_and_flags()
    test_build_periodic_manager_args_omits_empty_optional_values()
    print("[ok] build_periodic_manager_args regression checks passed")
