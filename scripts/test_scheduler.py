#!/usr/bin/env python3
"""Regression checks for monthly scheduler unification."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.models import PeriodicTask
from core.scheduler import TaskScheduler, resolve_monthly_quota_window


def test_cross_month_quota_window() -> None:
    start, end = resolve_monthly_quota_window(
        cycle_type="monthly_range",
        target_day=date(2026, 5, 12),
        range_start=10,
        range_end=3,
    ) or (None, None)
    assert start == date(2026, 5, 10)
    assert end == date(2026, 6, 3)

    start2, end2 = resolve_monthly_quota_window(
        cycle_type="monthly_range",
        target_day=date(2026, 6, 2),
        range_start=10,
        range_end=3,
    ) or (None, None)
    assert start2 == date(2026, 5, 10)
    assert end2 == date(2026, 6, 3)


def test_monthly_range_cross_month_membership() -> None:
    task = PeriodicTask(
        id=1,
        name="cross-month",
        cycle_type="monthly_range",
        range_start=10,
        range_end=3,
        n_per_month=3,
    )
    assert TaskScheduler(task, date(2026, 5, 9)).should_remind_today() is False
    assert TaskScheduler(task, date(2026, 5, 10)).should_remind_today() is True
    assert TaskScheduler(task, date(2026, 6, 3)).should_remind_today() is True
    assert TaskScheduler(task, date(2026, 6, 4)).should_remind_today() is False


if __name__ == "__main__":
    test_cross_month_quota_window()
    print("[ok] resolve_monthly_quota_window handles cross-month range")
    test_monthly_range_cross_month_membership()
    print("[ok] TaskScheduler monthly_range supports cross-month windows")
