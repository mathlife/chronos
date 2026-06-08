#!/usr/bin/env python3
"""Regression checks for the add-kwargs helper."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.todo import build_add_kwargs


def test_build_add_kwargs_cli_like() -> None:
    kwargs = build_add_kwargs(
        category="Inbox",
        cycle_type="weekly",
        time_of_day="10:00",
        task_kind="scheduled",
        weekday=1,
        reminder_template="hello",
    )
    assert kwargs["category"] == "Inbox"
    assert kwargs["cycle_type"] == "weekly"
    assert kwargs["time"] == "10:00"
    assert kwargs["task_kind"] == "scheduled"
    assert kwargs["weekday"] == 1
    assert kwargs["reminder_template"] == "hello"


def test_build_add_kwargs_omits_none_values() -> None:
    kwargs = build_add_kwargs(category="Inbox", cycle_type="daily", time_of_day="09:00")
    assert kwargs == {"category": "Inbox", "cycle_type": "daily", "time": "09:00"}


if __name__ == "__main__":
    test_build_add_kwargs_cli_like()
    test_build_add_kwargs_omits_none_values()
    print("[ok] build_add_kwargs regression checks passed")
