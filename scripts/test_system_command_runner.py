#!/usr/bin/env python3
"""Regression checks for whitelist command rendering/execution."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.system_command_runner import execute_system_handler


def test_execute_python3_whitelist_command() -> None:
    completed_process = mock.Mock(returncode=0, stdout="Python 3.x", stderr="")
    with mock.patch("core.system_command_runner.subprocess.run", return_value=completed_process) as mocked_run:
        result = execute_system_handler(json.dumps({"command_id": "python3", "args": ["-V"]}, ensure_ascii=False))
    assert result["ok"] is True
    assert result["command_id"] == "python3"
    assert result["argv"] == ["python3", "-V"]
    mocked_run.assert_called_once_with(
        ["python3", "-V"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_execute_bash_whitelist_command() -> None:
    completed_process = mock.Mock(returncode=0, stdout="ok", stderr="")
    with mock.patch("core.system_command_runner.subprocess.run", return_value=completed_process) as mocked_run:
        result = execute_system_handler(json.dumps({"command_id": "bash", "args": ["-lc", "echo ok"]}, ensure_ascii=False))
    assert result["ok"] is True
    assert result["command_id"] == "bash"
    assert result["argv"] == ["bash", "-lc", "echo ok"]
    mocked_run.assert_called_once_with(
        ["bash", "-lc", "echo ok"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
    )


if __name__ == "__main__":
    test_execute_python3_whitelist_command()
    print("[ok] execute_system_handler supports python3 whitelist command")
    test_execute_bash_whitelist_command()
    print("[ok] execute_system_handler supports bash whitelist command")
