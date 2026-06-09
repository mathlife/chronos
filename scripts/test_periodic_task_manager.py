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


def prepare_temp_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def test_periodic_service_occurrence_reads_support_schema_without_execution_job_id() -> None:
    db_path = make_case_dir("periodic-service-old-exec-schema") / "todo.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
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
            is_auto_completed BOOLEAN DEFAULT 0,
            completed_at TEXT,
            completion_mode TEXT,
            special_handler_result TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT,
            legacy_entry_id INTEGER
        );
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'old service', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'pending', 'reminder-old-service', '09:00', '2026-05-03T09:00:00+08:00');
        """
    )
    conn.commit()
    conn.close()

    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)
    manager = ptm_module.PeriodicTaskManager()

    row = manager._get_occurrence_row(1)

    assert row is not None
    assert row[5] is None

    manager.db.close()
    reset_db_singleton(db_module)


def test_fire_reminder_occurrence() -> None:
    db_path = make_case_dir("fire-reminder") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT
        )
        """
    )
    manager.db.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            status TEXT,
            group_id INTEGER
        )
        """
    )
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
    with mock.patch("core.system_command_runner.subprocess.run", return_value=completed_process), mock.patch.object(
        manager, "_send_message_now", return_value=True
    ) as mocked_send_message:
        assert manager.run_system_occurrence(1) is True
    mocked_send_message.assert_called_once()

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

    with mock.patch.object(manager, "_send_message_now", return_value=True) as mocked_send_message:
        assert manager.run_system_occurrence(1) is True
    mocked_send_message.assert_called_once()
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


def test_today_snapshot_puts_system_task_under_other_todo() -> None:
    db_path = make_case_dir("snapshot-system-under-other") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'System Sync', '系统任务', 'daily', '12:30', 'Asia/Shanghai', 1, 0, 'system', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-06', 'pending', '12:30', '2026-05-06T12:30:00')
        """
    )
    db_module.db_commit()

    snapshot = manager._build_today_todo_snapshot(date(2026, 5, 6))
    assert "【今日周期任务】\n- 无" in snapshot
    assert "【其他待办】" in snapshot
    assert "FIN-1 | 系统任务 | System Sync | 开始时间 12:30 | 待处理" in snapshot

    manager.db.close()
    reset_db_singleton(db_module)


def test_today_snapshot_uses_6am_to_next_6am_window() -> None:
    db_path = make_case_dir("snapshot-window-6am") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Today 07:00', 'Inbox', 'daily', '07:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (2, 'Next day 01:30', 'Inbox', 'daily', '01:30', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (3, 'Next day 06:30', 'Inbox', 'daily', '06:30', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences (id, task_id, date, status, scheduled_time, scheduled_at)
        VALUES (11, 1, '2026-05-06', 'pending', '07:00', '2026-05-06T07:00:00')
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences (id, task_id, date, status, scheduled_time, scheduled_at)
        VALUES (12, 2, '2026-05-07', 'pending', '01:30', '2026-05-07T01:30:00')
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences (id, task_id, date, status, scheduled_time, scheduled_at)
        VALUES (13, 3, '2026-05-07', 'pending', '06:30', '2026-05-07T06:30:00')
        """
    )
    db_module.db_commit()

    snapshot = manager._build_today_todo_snapshot(date(2026, 5, 6))
    assert "Today 07:00" in snapshot
    assert "Next day 01:30" in snapshot
    assert "Next day 06:30" not in snapshot

    manager.db.close()
    reset_db_singleton(db_module)


def test_clear_day_reminder_jobs_supports_schema_without_execution_job_id() -> None:
    db_path = make_case_dir("clear-day-old-schema") / "todo.db"
    legacy_schema = SCHEMA_SQL.replace("    execution_job_id TEXT,\n", "")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(legacy_schema)
    conn.commit()
    conn.close()
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Old schema daily', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'pending', 'reminder-old', '09:00', '2026-05-03T09:00:00')
        """
    )
    db_module.db_commit()

    with mock.patch("service.periodic_service.remove_job", return_value=True) as mocked_remove_job:
        manager._clear_day_reminder_jobs(task_id=1, occ_day=date(2026, 5, 3))

    row = manager.db.execute("SELECT reminder_job_id FROM periodic_occurrences WHERE id = 1").fetchone()
    assert row[0] is None
    mocked_remove_job.assert_called_once_with("reminder-old")
    manager.db.close()
    reset_db_singleton(db_module)


def test_monthly_quota_completion_clears_jobs_via_shared_helper() -> None:
    db_path = make_case_dir("monthly-quota-clears-jobs") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, range_start, range_end, n_per_month, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Monthly quota', 'Inbox', 'monthly_n_times', 1, 31, 1, '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'completed', 'reminder-quota', 'execute-quota', '09:00', '2026-05-03T09:00:00')
        """
    )
    db_module.db_commit()
    task_row = manager.db.execute(
        "SELECT cycle_type, n_per_month, count_current_month, range_start, range_end FROM periodic_tasks WHERE id = 1"
    ).fetchone()

    with mock.patch.object(manager, "_clear_occurrence_jobs", wraps=manager._clear_occurrence_jobs) as mocked_clear_jobs, mock.patch(
        "service.periodic_service.remove_job", return_value=True
    ) as mocked_remove_job:
        manager._apply_monthly_quota_completion(task_id=1, occurrence_date=date(2026, 5, 3), task_row=task_row)

    row = manager.db.execute(
        "SELECT reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
    mocked_clear_jobs.assert_called_once_with(1)
    assert mocked_remove_job.call_args_list == [mock.call("reminder-quota"), mock.call("execute-quota")]
    manager.db.close()
    reset_db_singleton(db_module)


def test_run_system_occurrence_clears_jobs_via_shared_helper() -> None:
    db_path = make_case_dir("run-system-clear-helper") / "todo.db"
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
        VALUES (1, 1, '2026-05-03', 'pending', 'reminder-system', 'execute-system', '09:30', '2026-05-03T09:30:00')
        """
    )
    db_module.db_commit()

    completed_process = mock.Mock(returncode=0, stdout="ok", stderr="")
    with mock.patch("core.system_command_runner.subprocess.run", return_value=completed_process), mock.patch.object(
        manager, "_send_message_now", return_value=True
    ), mock.patch.object(manager, "_clear_occurrence_jobs", wraps=manager._clear_occurrence_jobs) as mocked_clear_jobs, mock.patch(
        "service.periodic_service.remove_job", return_value=True
    ):
        assert manager.run_system_occurrence(1) is True

    row = manager.db.execute(
        "SELECT reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
    mocked_clear_jobs.assert_called_once_with(1)
    manager.db.close()
    reset_db_singleton(db_module)


def test_cleanup_old_jobs_clears_jobs_via_shared_helper() -> None:
    db_path = make_case_dir("cleanup-old-helper") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Old job task', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-01', 'pending', 'reminder-old-cleanup', 'execute-old-cleanup', '09:00', '2026-05-01T09:00:00')
        """
    )
    db_module.db_commit()

    with mock.patch.object(manager, "_clear_occurrence_jobs", wraps=manager._clear_occurrence_jobs) as mocked_clear_jobs, mock.patch(
        "service.periodic_service.remove_job", return_value=True
    ) as mocked_remove_job:
        cleaned = manager.cleanup_old_jobs(date(2026, 5, 3))

    row = manager.db.execute(
        "SELECT reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert cleaned == 1
    assert row[0] is None
    assert row[1] is None
    mocked_clear_jobs.assert_called_once_with(1)
    assert mocked_remove_job.call_args_list == [mock.call("reminder-old-cleanup"), mock.call("execute-old-cleanup")]
    manager.db.close()
    reset_db_singleton(db_module)



def test_clear_occurrence_jobs_preserves_db_refs_when_scheduler_remove_fails() -> None:
    db_path = make_case_dir("clear-jobs-preserve-on-failure") / "todo.db"
    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)
    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'cleanup fail', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'pending', 'reminder-fail', 'execute-fail', '09:00', '2026-05-03T09:00:00+08:00')
        """
    )
    db_module.db_commit()

    with mock.patch("service.periodic_service.remove_job", side_effect=RuntimeError("boom")):
        try:
            manager._clear_occurrence_jobs(1)
            raise AssertionError("expected cleanup failure")
        except RuntimeError:
            pass

    row = manager.db.execute(
        "SELECT reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = 1"
    ).fetchone()
    assert tuple(row) == ("reminder-fail", "execute-fail")
    manager.db.close()
    reset_db_singleton(db_module)


def test_periodic_service_batch_job_cleanup_uses_shared_helper() -> None:
    source = (PROJECT_ROOT / "service" / "periodic_service.py").read_text()
    helper_body = source.split("def _clear_occurrence_jobs", 1)[1].split("def _mark_occurrence_reminded", 1)[0]
    quota_body = source.split("def _apply_monthly_quota_completion", 1)[1].split("def create_occurrence_if_missing", 1)[0]
    clear_day_body = source.split("def _clear_day_reminder_jobs", 1)[1].split("def _complete_occurrence_internal", 1)[0]
    system_body = source.split("def run_system_occurrence", 1)[1].split("def schedule_reminder_job", 1)[0]
    cleanup_body = source.split("def cleanup_old_jobs", 1)[1].split("def complete_occurrence", 1)[0]
    complete_cycle_body = source.split("def complete_activity_cycle", 1)[1].split("def _format_reminder_message", 1)[0]

    assert "get_job_refs(" in helper_body
    assert "clear_jobs(" in helper_body
    assert "_clear_occurrence_jobs(" in quota_body
    assert "_clear_occurrence_jobs(" in clear_day_body
    assert "_clear_occurrence_jobs(" in system_body
    assert "_clear_occurrence_jobs(" in cleanup_body
    assert "_clear_occurrence_jobs(" in complete_cycle_body
    assert "UPDATE periodic_occurrences SET reminder_job_id = NULL" not in quota_body
    assert "UPDATE periodic_occurrences SET reminder_job_id = NULL" not in clear_day_body
    assert "UPDATE periodic_occurrences SET reminder_job_id = NULL" not in system_body
    assert "UPDATE periodic_occurrences SET reminder_job_id = NULL" not in cleanup_body
    assert "UPDATE periodic_occurrences SET reminder_job_id = NULL" not in complete_cycle_body
    service_batch_bodies = quota_body + clear_day_body + cleanup_body + complete_cycle_body
    assert "job_pointer_columns(" not in service_batch_bodies
    assert "not_null_predicate" not in service_batch_bodies
    assert "find_ids_with_jobs(" not in service_batch_bodies


def test_periodic_service_job_assignment_uses_occurrence_state_store() -> None:
    source = (PROJECT_ROOT / "service" / "periodic_service.py").read_text()
    generate_body = source.split("def generate_reminders_for_today", 1)[1].split("def cleanup_old_jobs", 1)[0]
    ensure_body = source.split("def ensure_today_occurrences", 1)[1].split("def _build_today_todo_snapshot", 1)[0]
    combined = generate_body + ensure_body

    assert "set_jobs(" in combined
    assert "UPDATE periodic_occurrences SET reminder_job_id" not in combined
    assert "execution_job_id = ? WHERE id = ?" not in combined


def test_complete_activity_cycle_supports_schema_without_execution_job_id() -> None:
    db_path = make_case_dir("complete-cycle-old-schema") / "todo.db"
    legacy_schema = SCHEMA_SQL.replace("    execution_job_id TEXT,\n", "")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(legacy_schema)
    conn.commit()
    conn.close()
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    manager = ptm_module.PeriodicTaskManager()
    manager.db.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Old schema cycle', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    manager.db.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-05-03', 'pending', 'reminder-cycle', '09:00', '2026-05-03T09:00:00')
        """
    )
    db_module.db_commit()

    with mock.patch("service.periodic_service.remove_job", return_value=True) as mocked_remove_job:
        affected = manager.complete_activity_cycle(1, as_of=date(2026, 5, 3))

    row = manager.db.execute("SELECT status, reminder_job_id FROM periodic_occurrences WHERE id = 1").fetchone()
    assert affected == 1
    assert row[0] == "completed"
    assert row[1] is None
    mocked_remove_job.assert_called_once_with("reminder-cycle")
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
    test_today_snapshot_puts_system_task_under_other_todo()
    print("[ok] today snapshot renders system tasks under other todo")
    test_today_snapshot_uses_6am_to_next_6am_window()
    print("[ok] today snapshot covers 06:00 -> next day 06:00 window")
    test_clear_day_reminder_jobs_supports_schema_without_execution_job_id()
    print("[ok] clear day reminder jobs supports schema without execution_job_id")
    test_monthly_quota_completion_clears_jobs_via_shared_helper()
    print("[ok] monthly quota completion clears jobs via shared helper")
    test_run_system_occurrence_clears_jobs_via_shared_helper()
    print("[ok] run system occurrence clears jobs via shared helper")
    test_cleanup_old_jobs_clears_jobs_via_shared_helper()
    print("[ok] cleanup old jobs clears jobs via shared helper")
    test_periodic_service_batch_job_cleanup_uses_shared_helper()
    print("[ok] periodic service batch job cleanup uses shared helper")
    test_complete_activity_cycle_supports_schema_without_execution_job_id()
    print("[ok] complete activity cycle supports schema without execution_job_id")
