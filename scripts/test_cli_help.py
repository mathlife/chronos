#!/usr/bin/env python3
"""Regression checks for Chronos CLI help output and aliases."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cli import periodic_cli
from scripts import chronos_api, todo


def _render_help(parser) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        parser.print_help()
    return buffer.getvalue()


def test_todo_help_includes_examples_and_aliases() -> None:
    help_text = _render_help(todo.build_parser())
    assert "Examples:" in help_text
    assert "monthly_fixed" in help_text
    assert "monthly_n_times" in help_text
    assert "natural-language instructions" in help_text


def test_chronos_api_help_includes_json_shape() -> None:
    help_text = _render_help(chronos_api.build_parser())
    assert "Output is always JSON" in help_text
    assert "task create" in help_text
    assert "channel replace" in help_text
    assert "occurrence complete" in help_text


def test_fire_reminder_cli_dispatches_only_one_occurrence() -> None:
    manager = mock.Mock()
    manager.fire_reminder_occurrence.return_value = True

    with mock.patch.object(periodic_cli, "PeriodicTaskManager", return_value=manager):
        code = periodic_cli.run_cli(["--fire-reminder", "--occurrence-id", "42"])

    assert code == 0
    manager.fire_reminder_occurrence.assert_called_once_with(42)
    manager.run_daily.assert_not_called()
    manager.db.close.assert_called_once_with()


if __name__ == "__main__":
    test_todo_help_includes_examples_and_aliases()
    print("[ok] todo help includes examples and alias notes")
    test_chronos_api_help_includes_json_shape()
    print("[ok] chronos_api help includes JSON shape notes")
    test_fire_reminder_cli_dispatches_only_one_occurrence()
    print("[ok] fire-reminder dispatches one occurrence without daily snapshot")
