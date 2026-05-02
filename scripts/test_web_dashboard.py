#!/usr/bin/env python3
"""Regression checks for web_dashboard."""
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
from scripts.web_dashboard import build_snapshot, handle_mutation


SCHEMA_SQL = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    group_id INTEGER
);

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


def test_snapshot_excludes_linked_legacy_entries() -> None:
    case_dir = make_case_dir("web-snapshot")
    db_path = case_dir / "todo.db"
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps({"channels": []}, ensure_ascii=False), encoding="utf-8")
    original_config = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)

    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton()

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("INSERT INTO groups (id, name) VALUES (1, 'Inbox')")
    cur.execute("INSERT INTO entries (id, text, status, group_id) VALUES (10, 'legacy linked', 'pending', 1)")
    cur.execute("INSERT INTO entries (id, text, status, group_id) VALUES (11, 'legacy standalone', 'pending', 1)")
    cur.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, legacy_entry_id, created_at, updated_at)
        VALUES (1, 'task-linked', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.commit()
    conn.close()

    snapshot = build_snapshot(db_path)
    identifiers = {item["identifier"] for item in snapshot["today_tasks"]}
    assert "ID10" not in identifiers
    assert "ID11" in identifiers

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton()


def test_mutation_ops() -> None:
    case_dir = make_case_dir("web-mutation")
    db_path = case_dir / "todo.db"
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps({"channels": []}, ensure_ascii=False), encoding="utf-8")
    original_config = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)

    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton()

    created = handle_mutation(
        "/api/task/create",
        {"payload": {"name": "web-created", "cycle_type": "once", "start_date": "2026-05-03", "time_of_day": "09:00"}},
    )
    task_id = int(created["id"])
    assert created["name"] == "web-created"

    updated = handle_mutation("/api/task/update", {"id": task_id, "patch": {"cycle_type": "daily"}})
    assert updated["cycle_type"] == "daily"

    removed = handle_mutation("/api/task/remove", {"id": task_id, "hard": False})
    assert removed["id"] == task_id

    channel = handle_mutation(
        "/api/channel/put",
        {"channel": {"id": "hook-main", "type": "webhook", "enabled": True, "config": {"url": "https://example.com"}}},
    )
    assert channel["id"] == "hook-main"
    handle_mutation("/api/channel/remove", {"id": "hook-main"})

    settings = handle_mutation("/api/settings/update", {"chat_id": "12345"})
    assert settings["chat_id"] == "12345"

    config_data = json.loads(config_path.read_text(encoding="utf-8"))
    assert config_data.get("chat_id") == "12345"

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton()


if __name__ == "__main__":
    test_snapshot_excludes_linked_legacy_entries()
    print("[ok] snapshot excludes linked legacy entries")
    test_mutation_ops()
    print("[ok] mutation ops for task/channel/settings")
