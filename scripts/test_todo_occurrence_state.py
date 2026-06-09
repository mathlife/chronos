#!/usr/bin/env python3
"""Regression checks for todo.py FIN occurrence state transitions."""
from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import todo
from scripts.test_helpers import make_case_dir


BASE_SCHEMA = """
CREATE TABLE periodic_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    cycle_type TEXT,
    time_of_day TEXT,
    timezone TEXT,
    is_active INTEGER,
    count_current_month INTEGER,
    task_kind TEXT,
    source TEXT,
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
    is_auto_completed INTEGER DEFAULT 0,
    completed_at TEXT,
    completion_mode TEXT,
    special_handler_result TEXT,
    scheduled_time TEXT,
    scheduled_at TEXT,
    legacy_entry_id INTEGER
);
"""

OPTIONAL_COLUMNS = """
ALTER TABLE periodic_occurrences ADD COLUMN completion_source TEXT;
ALTER TABLE periodic_occurrences ADD COLUMN trigger_label TEXT;
ALTER TABLE periodic_occurrences ADD COLUMN trigger_command TEXT;
"""


def prepare_db(*, with_optional_columns: bool) -> Path:
    db_path = make_case_dir("todo-occ-state") / "todo.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(BASE_SCHEMA)
    if with_optional_columns:
        conn.executescript(OPTIONAL_COLUMNS)
    conn.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'Daily task', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (1, 1, '2026-06-09', 'pending', 'reminder-1', 'execute-1', '09:00', '2026-06-09T09:00:00')
        """
    )
    conn.commit()
    conn.close()
    return db_path


def set_todo_db(db_path: Path) -> None:
    todo.TODO_DB = db_path


def fetch_occurrence(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM periodic_occurrences WHERE id = 1").fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def run_cmd_skip(identifier: str) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        todo.cmd_skip(identifier)
    return buffer.getvalue()


def test_complete_periodic_occurrence_records_metadata_and_clears_jobs() -> None:
    db_path = prepare_db(with_optional_columns=True)
    set_todo_db(db_path)

    ok, message = todo.complete_periodic_occurrence(
        1,
        completion_mode="manual",
        special_handler_result="done",
        completion_source="telegram:FIN",
        trigger_label="manual_complete",
        trigger_command="telegram_status_sync",
        allow_auto_for_scheduled=True,
    )

    assert ok is True
    assert "✅ 已完成 FIN-1" in message
    row = fetch_occurrence(db_path)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["completion_mode"] == "manual"
    assert row["special_handler_result"] == "done"
    assert row["completion_source"] == "telegram:FIN"
    assert row["trigger_label"] == "manual_complete"
    assert row["trigger_command"] == "telegram_status_sync"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None


def test_complete_periodic_occurrence_supports_old_schema() -> None:
    db_path = prepare_db(with_optional_columns=False)
    set_todo_db(db_path)

    ok, message = todo.complete_periodic_occurrence(
        1,
        completion_mode="manual",
        completion_source="telegram:FIN",
        trigger_label="manual_complete",
        trigger_command="telegram_status_sync",
        allow_auto_for_scheduled=True,
    )

    assert ok is True
    assert "✅ 已完成 FIN-1" in message
    row = fetch_occurrence(db_path)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["completion_mode"] == "manual"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None


def test_cmd_skip_fin_supports_old_occurrence_schema_without_optional_metadata_columns() -> None:
    db_path = prepare_db(with_optional_columns=False)
    set_todo_db(db_path)

    output = run_cmd_skip("FIN-1")

    assert "✅ 已跳过 FIN-1" in output
    row = fetch_occurrence(db_path)
    assert row["status"] == "skipped"
    assert row["completed_at"] is not None
    assert row["completion_mode"] == "manual"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None


def test_cmd_skip_fin_records_standard_metadata_when_columns_exist() -> None:
    db_path = prepare_db(with_optional_columns=True)
    set_todo_db(db_path)

    output = run_cmd_skip("FIN-1")

    assert "✅ 已跳过 FIN-1" in output
    row = fetch_occurrence(db_path)
    assert row["status"] == "skipped"
    assert row["completed_at"] is not None
    assert row["completion_mode"] == "manual"
    assert row["completion_source"] == "manual_cli"
    assert row["trigger_label"] == "manual_skip"
    assert row["trigger_command"] == "todo.py skip"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None


def test_complete_periodic_occurrence_supports_schema_without_execution_job_id() -> None:
    db_path = prepare_db(with_optional_columns=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE periodic_occurrences_old AS SELECT id, task_id, date, status, reminder_job_id, is_auto_completed, completed_at, completion_mode, special_handler_result, scheduled_time, scheduled_at, legacy_entry_id, completion_source, trigger_label, trigger_command FROM periodic_occurrences")
    conn.execute("DROP TABLE periodic_occurrences")
    conn.execute("ALTER TABLE periodic_occurrences_old RENAME TO periodic_occurrences")
    conn.commit()
    conn.close()
    set_todo_db(db_path)

    ok, message = todo.complete_periodic_occurrence(1)

    assert ok is True
    assert "✅ 已完成 FIN-1" in message
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, reminder_job_id FROM periodic_occurrences WHERE id = 1").fetchone()
    conn.close()
    assert row["status"] == "completed"
    assert row["reminder_job_id"] is None


def test_cmd_skip_fin_supports_schema_without_execution_job_id() -> None:
    db_path = prepare_db(with_optional_columns=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE periodic_occurrences_old AS SELECT id, task_id, date, status, reminder_job_id, is_auto_completed, completed_at, completion_mode, special_handler_result, scheduled_time, scheduled_at, legacy_entry_id, completion_source, trigger_label, trigger_command FROM periodic_occurrences")
    conn.execute("DROP TABLE periodic_occurrences")
    conn.execute("ALTER TABLE periodic_occurrences_old RENAME TO periodic_occurrences")
    conn.commit()
    conn.close()
    set_todo_db(db_path)

    output = run_cmd_skip("FIN-1")

    assert "✅ 已跳过 FIN-1" in output
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, reminder_job_id FROM periodic_occurrences WHERE id = 1").fetchone()
    conn.close()
    assert row["status"] == "skipped"
    assert row["reminder_job_id"] is None


def test_complete_periodic_occurrence_job_cleanup_uses_shared_job_ref_iterator() -> None:
    source = (PROJECT_ROOT / "scripts" / "todo.py").read_text()
    body = source.split("def complete_periodic_occurrence", 1)[1].split("def get_entry_archive_state", 1)[0]

    assert "iter_job_refs_from_pair(" in body
    assert "remove_job(reminder_job_id)" not in body
    assert "remove_job(execution_job_id)" not in body


def test_cmd_skip_fin_job_cleanup_uses_shared_job_ref_iterator() -> None:
    source = (PROJECT_ROOT / "scripts" / "todo.py").read_text()
    body = source.split("def cmd_skip", 1)[1].split("def cmd_show", 1)[0]

    assert "iter_job_refs_from_pair(" in body
    assert "if job_name" not in body
    assert "if execution_job_id" not in body
    assert "remove_job(execution_job_id)" not in body


def test_complete_periodic_occurrence_monthly_quota_cleanup_uses_state_store() -> None:
    source = (PROJECT_ROOT / "scripts" / "todo.py").read_text()
    body = source.split("def complete_periodic_occurrence", 1)[1].split("def get_entry_archive_state", 1)[0]

    assert "OccurrenceStateStore" in body
    assert "clear_jobs_for_ids(" in body
    assert "find_ids_with_jobs(" in body or "find_completed_ids_with_jobs_in_date_window(" in body
    assert "SET reminder_job_id = NULL, execution_job_id = NULL" not in body
    assert "o.reminder_job_id IS NOT NULL OR o.execution_job_id IS NOT NULL" not in body


def test_todo_job_pair_cleanup_uses_shared_pair_helper() -> None:
    source = (PROJECT_ROOT / "scripts" / "todo.py").read_text()

    assert "iter_job_refs_from_pair(" in source
    assert '{"reminder_job_id": reminder_job_id, "execution_job_id": execution_job_id}' not in source
    assert '{"reminder_job_id": reminder_ref, "execution_job_id": execution_ref}' not in source
    assert '{"reminder_job_id": job_name, "execution_job_id": execution_job_id}' not in source


if __name__ == "__main__":
    test_complete_periodic_occurrence_records_metadata_and_clears_jobs()
    test_complete_periodic_occurrence_supports_old_schema()
    test_cmd_skip_fin_supports_old_occurrence_schema_without_optional_metadata_columns()
    test_cmd_skip_fin_records_standard_metadata_when_columns_exist()
    print("[ok] todo occurrence state regression checks passed")
