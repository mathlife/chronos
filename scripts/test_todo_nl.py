#!/usr/bin/env python3
"""Regression checks for extracted todo natural-language parsing."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.todo import parse_compact_end_date
from scripts.todo_nl import parse_natural_language


def _parse(text: str) -> dict[str, object]:
    return parse_natural_language(text, parse_compact_end_date=parse_compact_end_date)


def test_nl_complete_overdue() -> None:
    result = _parse("自动完成逾期待办")
    assert result["cmd"] == "complete-overdue"


def test_nl_skip_fin_identifier() -> None:
    result = _parse("跳过 FIN-123")
    assert result["cmd"] == "skip"
    assert result["identifier"] == "FIN-123"


def test_nl_show_identifier() -> None:
    result = _parse("查看 FIN-456 详情")
    assert result["cmd"] == "show"
    assert result["identifier"] == "FIN-456"


def test_nl_add_weekly_task() -> None:
    result = _parse("添加待办，每周一10:00 写周报")
    assert result["cmd"] == "add"
    assert result["cycle_type"] == "weekly"
    assert result["weekday"] == 0
    assert result["time_of_day"] == "10:00"


def test_nl_add_with_compact_end_date() -> None:
    result = _parse("添加任务 叫测试任务 每天 9点 结束日期260630")
    assert result["cmd"] == "add"
    assert result["name"] == "测试任务"
    assert result["cycle_type"] == "daily"
    assert result["end_date"] == "2026-06-30"


if __name__ == "__main__":
    test_nl_complete_overdue()
    test_nl_skip_fin_identifier()
    test_nl_show_identifier()
    test_nl_add_weekly_task()
    test_nl_add_with_compact_end_date()
    print("[ok] todo_nl regression checks passed")
