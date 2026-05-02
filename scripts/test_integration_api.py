#!/usr/bin/env python3
"""Regression checks for integration API."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
TMP_ROOT = PROJECT_ROOT / ".tmp_tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

from core import db as db_module
from core import paths as paths_module
from core.integration_api import create_task, delete_channel, list_channels, put_channel, remove_task, replace_channels, update_task


SCHEMA_SQL = """
CREATE TABLE periodic_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT,
    cycle_type TEXT,
    weekday INTEGER,
    day_of_month INTEGER,
    range_start INTEGER,
    range_end INTEGER,
    n_per_month INTEGER,
    interval_hours INTEGER,
    time_of_day TEXT,
    event_time TEXT,
    timezone TEXT,
    is_active INTEGER,
    count_current_month INTEGER,
    end_date TEXT,
    reminder_template TEXT,
    dates_list TEXT,
    task_kind TEXT,
    source TEXT,
    legacy_entry_id INTEGER,
    special_handler TEXT,
    handler_payload TEXT,
    start_date TEXT,
    delivery_target TEXT,
    delivery_mode TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE periodic_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    reminder_job_id TEXT,
    execution_job_id TEXT,
    is_auto_completed BOOLEAN DEFAULT 0,
    completed_at TEXT,
    completion_mode TEXT,
    special_handler_result TEXT,
    scheduled_time TEXT,
    scheduled_at TEXT,
    legacy_entry_id INTEGER,
    FOREIGN KEY (task_id) REFERENCES periodic_tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, date, scheduled_time)
);
"""


def reset_db_singleton() -> None:
    if db_module.DB._conn is not None:
        db_module.DB._conn.close()
    db_module.DB._conn = None
    db_module.DB._instance = None
    db_module.clear_task_cache()


def prepare_temp_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def make_case_dir(case_name: str) -> Path:
    case_dir = TMP_ROOT / f"{case_name}-{uuid.uuid4().hex}"
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


def test_task_flow() -> None:
    db_path = make_case_dir("integration-task") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton()

    created = create_task(
        {
            "name": "Integration API task",
            "cycle_type": "once",
            "start_date": "2026-05-03",
            "time_of_day": "10:30",
            "task_kind": "scheduled",
        }
    )
    assert created["id"] > 0
    assert created["name"] == "Integration API task"
    assert created["cycle_type"] == "once"

    updated = update_task(created["id"], {"cycle_type": "daily"})
    assert updated["cycle_type"] == "daily"

    removed = remove_task(created["id"], hard=False)
    assert removed is True
    row = db_module.DB().execute("SELECT is_active FROM periodic_tasks WHERE id = ?", (created["id"],)).fetchone()
    assert row[0] == 0

    db_module.DB().close()
    reset_db_singleton()


def test_channel_flow() -> None:
    config_path = make_case_dir("integration-channel") / "chronos-config.json"
    original_config_path = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)
    try:
        replaced = replace_channels(
            [
                {
                    "id": "tg-main",
                    "type": "telegram",
                    "enabled": True,
                    "config": {"bot_token": "t1", "chat_id": "100"},
                }
            ]
        )
        assert len(replaced) == 1
        assert replaced[0]["id"] == "tg-main"

        put_channel(
            {
                "id": "hook-main",
                "type": "webhook",
                "enabled": True,
                "config": {"url": "https://example.com/hook"},
            }
        )
        channels = list_channels()
        assert len(channels) == 2
        assert {c["id"] for c in channels} == {"tg-main", "hook-main"}

        removed = delete_channel("tg-main")
        assert removed is True
        channels_after = list_channels()
        assert len(channels_after) == 1
        assert channels_after[0]["id"] == "hook-main"

        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        assert isinstance(config_data.get("channels"), list)
    finally:
        if original_config_path is None:
            os.environ.pop("CHRONOS_CONFIG_PATH", None)
        else:
            os.environ["CHRONOS_CONFIG_PATH"] = original_config_path


if __name__ == "__main__":
    test_task_flow()
    print("[ok] integration API task flow")
    test_channel_flow()
    print("[ok] integration API channel flow")
