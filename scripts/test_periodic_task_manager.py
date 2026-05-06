#!/usr/bin/env python3
"""Focused regression checks for Chronos periodic task manager."""
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import db as db_module
from core import paths as paths_module
from scripts import periodic_task_manager as ptm_module
from scripts.test_helpers import make_case_dir, reset_db_singleton


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


def prepare_temp_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()

def test_fire_reminder_occurrence() -> None:
    db_path = make_case_dir("fire-reminder") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

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
    reset_db_singleton(db_module)


def test_run_system_occurrence_marks_completed() -> None:
    db_path = make_case_dir("run-system") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, special_handler, handler_payload, created_at, updated_at)
        VALUES (1, 'Refresh cache', 'System', 'daily', '09:30', 'Asia/Shanghai', 1, 0, 'system', 'chronos', 'run_command', '{"command_id":"echo","args":["ok"]}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
    with mock.patch("core.system_command_runner.subprocess.run", return_value=completed_process):
        assert manager.run_system_occurrence(1) is True

    row = manager.db.execute(
        "SELECT status, completion_mode, special_handler_result, reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert row[0] == "completed"
    assert row[1] == "system_scheduler"
    assert "命令ID：echo" in (row[2] or "")
    assert "退出码：0" in (row[2] or "")
    assert row[3] is None
    assert row[4] is None
    manager.db.close()
    reset_db_singleton(db_module)


def test_run_system_occurrence_blocks_legacy_shell_payload() -> None:
    db_path = make_case_dir("run-system-block-legacy") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, special_handler, handler_payload, created_at, updated_at)
        VALUES (1, 'Legacy shell', 'System', 'daily', '09:30', 'Asia/Shanghai', 1, 0, 'system', 'chronos', 'run_command', '{"command":"echo legacy"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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

    assert manager.run_system_occurrence(1) is True
    row = manager.db.execute(
        "SELECT status, completion_mode, special_handler_result FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert row[0] == "skipped"
    assert row[1] == "blocked_policy"
    assert "blocked=" in (row[2] or "")
    manager.db.close()
    reset_db_singleton(db_module)


def test_system_schedule_creates_execute_job_only() -> None:
    db_path = make_case_dir("run-system-schedule-only-execute") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    with mock.patch("service.periodic_service.supports_system_scheduler", return_value=True), mock.patch(
        "service.periodic_service.create_once_job"
    ) as mocked_create_once_job:
        reminder_job_name, execution_job_name = manager._schedule_system_occurrence_jobs(123, date(2026, 5, 6), "12:30")

    assert reminder_job_name is None
    assert execution_job_name == "chronos_execute_123"
    assert mocked_create_once_job.call_count == 1
    kwargs = mocked_create_once_job.call_args.kwargs
    assert kwargs["job_name"] == "chronos_execute_123"
    assert "--run-system-task" in kwargs["command"]
    assert "--fire-reminder" not in kwargs["command"]
    manager.db.close()
    reset_db_singleton(db_module)


if __name__ == "__main__":
    test_fire_reminder_occurrence()
    print("[ok] fire_reminder_occurrence marks pending occurrence as reminded")
    test_run_system_occurrence_marks_completed()
    print("[ok] run_system_occurrence marks system command occurrence completed")
    test_run_system_occurrence_blocks_legacy_shell_payload()
    print("[ok] run_system_occurrence blocks legacy shell payload")
    test_system_schedule_creates_execute_job_only()
    print("[ok] system scheduling creates execute job only (no pre-reminder)")
