#!/usr/bin/env python3
"""Regression checks for centralized periodic occurrence state transitions."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.occurrence_state import OccurrenceStateStore


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reminder_job_id TEXT,
            is_auto_completed INTEGER DEFAULT 0,
            completed_at TIMESTAMP,
            completion_mode TEXT,
            special_handler_result TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT,
            legacy_entry_id INTEGER,
            completion_source TEXT,
            trigger_label TEXT,
            trigger_command TEXT,
            execution_job_id TEXT
        );
        """
    )
    return conn


def insert_occurrence(conn: sqlite3.Connection, *, status: str = "pending") -> None:
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (1, 10, '2026-06-09', ?, 'reminder-1', 'execute-1', '09:00', '2026-06-09T09:00:00')
        """,
        (status,),
    )
    conn.commit()


def get_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM periodic_occurrences WHERE id = 1").fetchone()
    assert row is not None
    return row


def test_mark_reminded_only_moves_pending_and_keeps_terminal_states() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    store = OccurrenceStateStore(conn)

    assert store.mark_reminded(1, reminder_job_id="reminder-new") is True
    row = get_row(conn)
    assert row["status"] == "reminded"
    assert row["reminder_job_id"] == "reminder-new"

    assert store.complete(1, completion_mode="manual") is True
    assert store.mark_reminded(1, reminder_job_id="late-reminder") is False
    row = get_row(conn)
    assert row["status"] == "completed"
    assert row["reminder_job_id"] is None


def test_complete_sets_metadata_and_clears_jobs() -> None:
    conn = make_db()
    insert_occurrence(conn, status="reminded")
    store = OccurrenceStateStore(conn)

    assert store.complete(
        1,
        completion_mode="manual",
        special_handler_result="ok",
        completion_source="telegram:FIN",
        trigger_label="manual_complete",
        trigger_command="telegram_status_sync",
    ) is True

    row = get_row(conn)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["completion_mode"] == "manual"
    assert row["special_handler_result"] == "ok"
    assert row["completion_source"] == "telegram:FIN"
    assert row["trigger_label"] == "manual_complete"
    assert row["trigger_command"] == "telegram_status_sync"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None


def test_complete_is_idempotent_for_already_completed() -> None:
    conn = make_db()
    insert_occurrence(conn, status="completed")
    store = OccurrenceStateStore(conn)

    assert store.complete(1, completion_mode="manual") is False
    row = get_row(conn)
    assert row["status"] == "completed"
    assert row["completion_mode"] is None


def test_skip_only_non_terminal_and_clears_jobs() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    store = OccurrenceStateStore(conn)

    assert store.skip(
        1,
        completion_mode="manual",
        special_handler_result="skip reason",
        completion_source="telegram:FIN",
        trigger_label="manual_skip",
        trigger_command="telegram_status_sync",
    ) is True

    row = get_row(conn)
    assert row["status"] == "skipped"
    assert row["completed_at"] is not None
    assert row["completion_mode"] == "manual"
    assert row["special_handler_result"] == "skip reason"
    assert row["completion_source"] == "telegram:FIN"
    assert row["trigger_label"] == "manual_skip"
    assert row["trigger_command"] == "telegram_status_sync"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None

    assert store.complete(1, completion_mode="manual") is False
    row = get_row(conn)
    assert row["status"] == "skipped"


def test_find_ids_with_jobs_filters_with_schema_aware_predicate() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (2, 10, '2026-06-10', 'pending', NULL, NULL, '09:00', '2026-06-10T09:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (3, 11, '2026-06-09', 'completed', NULL, 'execute-3', '09:00', '2026-06-09T09:00:00')
        """
    )
    conn.commit()
    store = OccurrenceStateStore(conn)

    assert store.find_ids_with_jobs("task_id = ? AND date = ?", (10, "2026-06-09")) == [1]
    assert store.find_ids_with_jobs("status = ?", ("completed",)) == [3]
    assert store.find_ids_with_jobs("task_id = ?", (99,)) == []
    assert store.find_ids_with_jobs_for_task(10) == [1]
    assert store.find_ids_with_jobs_for_task_on_date(10, "2026-06-09") == [1]
    assert store.find_completed_ids_with_jobs_in_date_window(11, "2026-06-09", "2026-06-10") == [3]
    assert store.find_ids_with_jobs_before_or_on("2026-06-09") == [1, 3]


def test_job_payloads_for_task_include_scheduler_fields() -> None:
    conn = make_db()
    conn.executescript(
        """
        CREATE TABLE periodic_tasks (
            id INTEGER PRIMARY KEY,
            time_of_day TEXT
        );
        INSERT INTO periodic_tasks (id, time_of_day) VALUES (10, '09:00');
        """
    )
    insert_occurrence(conn, status="pending")
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (2, 10, '2026-06-10', 'pending', NULL, NULL, '09:00', '2026-06-10T09:00:00')
        """
    )
    conn.commit()
    store = OccurrenceStateStore(conn)

    assert store.job_payloads_for_task(10) == [
        {
            "occurrence_id": 1,
            "date": "2026-06-09",
            "scheduled_time": "09:00",
            "reminder_job_id": "reminder-1",
            "execution_job_id": "execute-1",
            "time_of_day": "09:00",
        }
    ]


def test_update_non_terminal_changes_active_status_clears_jobs_and_blocks_terminal_rows() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    store = OccurrenceStateStore(conn)

    job_refs = store.update_non_terminal(1, status="in_progress", scheduled_time="10:15")
    assert job_refs == ("reminder-1", "execute-1")
    row = get_row(conn)
    assert row["status"] == "in_progress"
    assert row["scheduled_time"] == "10:15"
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None

    assert store.complete(1, completion_mode="manual") is True
    job_refs = store.update_non_terminal(1, status="pending", scheduled_time="11:00")
    assert job_refs == (None, None)
    row = get_row(conn)
    assert row["status"] == "completed"
    assert row["scheduled_time"] == "10:15"


def test_set_jobs_updates_available_columns_and_preserves_existing_reminder_when_none() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    store = OccurrenceStateStore(conn)

    store.set_jobs(1, reminder_job_id=None, execution_job_id="execute-new")
    row = get_row(conn)
    assert row["reminder_job_id"] == "reminder-1"
    assert row["execution_job_id"] == "execute-new"

    store.set_jobs(1, reminder_job_id="reminder-new", execution_job_id=None)
    row = get_row(conn)
    assert row["reminder_job_id"] == "reminder-new"
    assert row["execution_job_id"] == "execute-new"


def test_clear_jobs_returns_existing_job_ids_before_clearing() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    store = OccurrenceStateStore(conn)

    job_refs = store.clear_jobs(1)
    assert job_refs == ("reminder-1", "execute-1")
    row = get_row(conn)
    assert row["reminder_job_id"] is None
    assert row["execution_job_id"] is None


def test_clear_jobs_for_ids_clears_each_occurrence_and_returns_job_refs() -> None:
    conn = make_db()
    insert_occurrence(conn, status="pending")
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (2, 10, '2026-06-10', 'pending', 'reminder-2', NULL, '09:00', '2026-06-10T09:00:00')
        """
    )
    conn.commit()
    store = OccurrenceStateStore(conn)

    assert store.clear_jobs_for_ids([1, 2]) == [("reminder-1", "execute-1"), ("reminder-2", None)]
    rows = conn.execute(
        "SELECT id, reminder_job_id, execution_job_id FROM periodic_occurrences ORDER BY id"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [(1, None, None), (2, None, None)]


def test_job_payloads_for_task_supports_schema_without_execution_job_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE periodic_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_of_day TEXT
        );
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reminder_job_id TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT
        );
        INSERT INTO periodic_tasks (id, time_of_day) VALUES (10, '09:00');
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (1, 10, '2026-06-09', 'pending', 'reminder-1', '09:00', '2026-06-09T09:00:00');
        """
    )

    payloads = OccurrenceStateStore(conn).job_payloads_for_task(10)

    assert payloads == [
        {
            "occurrence_id": 1,
            "date": "2026-06-09",
            "scheduled_time": "09:00",
            "time_of_day": "09:00",
            "reminder_job_id": "reminder-1",
            "execution_job_id": None,
        }
    ]


def test_clear_jobs_supports_schema_without_execution_job_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reminder_job_id TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT
        );
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (1, 10, '2026-06-09', 'pending', 'reminder-1', '09:00', '2026-06-09T09:00:00');
        """
    )
    store = OccurrenceStateStore(conn)

    job_refs = store.clear_jobs(1)
    assert job_refs == ("reminder-1", None)
    row = conn.execute("SELECT reminder_job_id FROM periodic_occurrences WHERE id = 1").fetchone()
    assert row is not None
    assert row["reminder_job_id"] is None


if __name__ == "__main__":
    test_mark_reminded_only_moves_pending_and_keeps_terminal_states()
    test_complete_sets_metadata_and_clears_jobs()
    test_complete_is_idempotent_for_already_completed()
    test_skip_only_non_terminal_and_clears_jobs()
    test_find_ids_with_jobs_filters_with_schema_aware_predicate()
    test_job_payloads_for_task_include_scheduler_fields()
    test_clear_jobs_returns_existing_job_ids_before_clearing()
    test_clear_jobs_for_ids_clears_each_occurrence_and_returns_job_refs()
    test_job_payloads_for_task_supports_schema_without_execution_job_id()
    test_clear_jobs_supports_schema_without_execution_job_id()
    print("[ok] occurrence state regression checks passed")
