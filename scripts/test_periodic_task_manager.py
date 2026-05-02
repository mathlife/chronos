#!/usr/bin/env python3
"""Focused regression checks for Chronos periodic task manager."""
import sqlite3
import sys
import uuid
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
TMP_ROOT = PROJECT_ROOT / ".tmp_tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

from core import db as db_module
from core import paths as paths_module
from scripts import periodic_task_manager as ptm_module


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
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def make_case_dir(case_name: str) -> Path:
    case_dir = TMP_ROOT / f"{case_name}-{uuid.uuid4().hex}"
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


def test_fire_reminder_occurrence() -> None:
    db_path = make_case_dir("fire-reminder") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton()

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Daily review', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'pending', 'job-1', '09:00', '2026-05-03T09:00:00')
        """
    )
    db_module.db_commit()

    with mock.patch.object(manager, "_send_message_now", return_value=True):
        assert manager.fire_reminder_occurrence(1) is True

    row = manager.db.execute("SELECT status FROM periodic_occurrences WHERE id = 1").fetchone()
    assert row[0] == "reminded"
    manager.db.close()
    reset_db_singleton()


def test_run_system_occurrence_marks_completed() -> None:
    db_path = make_case_dir("run-system") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton()

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, special_handler, handler_payload, created_at, updated_at)
        VALUES (1, 'Refresh cache', 'System', 'daily', '09:30', 'Asia/Shanghai', 1, 0, 'system', 'chronos', 'run_command', '{"command":"echo ok"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'pending', 'reminder-1', 'execute-1', '09:30', '2026-05-03T09:30:00')
        """
    )
    db_module.db_commit()

    completed_process = mock.Mock(returncode=0, stdout="ok", stderr="")
    with mock.patch.object(ptm_module.subprocess, "run", return_value=completed_process):
        assert manager.run_system_occurrence(1) is True

    row = manager.db.execute(
        "SELECT status, completion_mode, special_handler_result, reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert row[0] == "completed"
    assert row[1] == "system_scheduler"
    assert "exit_code=0" in (row[2] or "")
    assert row[3] is None
    assert row[4] is None
    manager.db.close()
    reset_db_singleton()


if __name__ == "__main__":
    test_fire_reminder_occurrence()
    print("[ok] fire_reminder_occurrence marks pending occurrence as reminded")
    test_run_system_occurrence_marks_completed()
    print("[ok] run_system_occurrence marks system command occurrence completed")
