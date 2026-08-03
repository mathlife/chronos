from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from cli.periodic_cli import build_parser, validate_update_params
from core import config as config_module
from core import db as db_module
from core.integration_api import _normalize_task_payload, update_task
from core.scheduler import resolve_monthly_quota_window


SCHEMA = """
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
    count_current_month INTEGER DEFAULT 0,
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


class RegressionTests(unittest.TestCase):
    def test_update_parser_has_no_create_defaults(self) -> None:
        args = build_parser().parse_args(["--update", "--task-id", "7", "--name", "renamed"])
        validate_update_params(args)
        self.assertEqual(args.task_id, 7)
        self.assertIsNone(args.cycle_type)
        self.assertIsNone(args.time_of_day)
        self.assertIsNone(args.category)

    def test_quota_is_canonicalized(self) -> None:
        normalized = _normalize_task_payload({"quota": 3}, partial=True)
        self.assertEqual(normalized, {"n_per_month": 3})
        with self.assertRaisesRegex(ValueError, "must match"):
            _normalize_task_payload({"quota": 3, "n_per_month": 4}, partial=True)

    def test_monthly_dates_quota_uses_calendar_month(self) -> None:
        self.assertEqual(
            resolve_monthly_quota_window(cycle_type="monthly_dates", target_day=date(2026, 8, 12)),
            (date(2026, 8, 1), date(2026, 8, 31)),
        )

    def test_update_replaces_pending_occurrence_and_resyncs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "todo.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(SCHEMA)
            conn.execute(
                """
                INSERT INTO periodic_tasks
                (id, name, category, cycle_type, time_of_day, event_time, timezone, is_active,
                 count_current_month, task_kind, source, created_at, updated_at)
                VALUES (1, 'old', 'Inbox', 'daily', '09:00', '09:00', 'Asia/Shanghai', 1,
                        0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
            conn.execute(
                """
                INSERT INTO periodic_occurrences
                (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
                VALUES (10, 1, '2026-08-03', 'pending', 'chronos_reminder_10', '09:00', '2026-08-03T09:00:00+08:00')
                """
            )
            conn.commit()
            conn.close()

            original_db_path = db_module.TODO_DB
            db_module.TODO_DB = db_path
            db_module.DB.reset_for_tests()
            manager = mock.Mock()
            manager.db = mock.Mock()
            try:
                with mock.patch("core.integration_api.supports_system_scheduler", return_value=True), mock.patch(
                    "core.integration_api.remove_job", return_value=True
                ) as remove_job, mock.patch(
                    "service.periodic_service.PeriodicTaskManager", return_value=manager
                ):
                    updated = update_task(1, {"time_of_day": "10:30"})
                self.assertEqual(updated["time_of_day"], "10:30")
                remove_job.assert_called_once_with("chronos_reminder_10")
                manager.generate_reminders_for_today.assert_called_once_with()
                row = db_module.DB().execute("SELECT id FROM periodic_occurrences WHERE id = 10").fetchone()
                self.assertIsNone(row)
            finally:
                db_module.DB.reset_for_tests()
                db_module.TODO_DB = original_db_path

    def test_config_write_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            config_module._write_config_file(path, {"channels": [{"id": "main"}]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["channels"][0]["id"], "main")

    def test_atomic_config_patch_preserves_unrelated_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps({"channels": [{"id": "main"}], "chat_id": "old"}), encoding="utf-8")
            with mock.patch.object(config_module, "get_config_path", return_value=path):
                updated = config_module.update_raw_config(updates={"chat_id": "new"})
            self.assertEqual(updated["chat_id"], "new")
            self.assertEqual(updated["channels"], [{"id": "main"}])


if __name__ == "__main__":
    unittest.main()
