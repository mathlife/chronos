from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from cli.periodic_cli import build_parser, run_cli, validate_update_params
from core import config as config_module
from core import db as db_module
from core import notifiers as notifier_module
from core.integration_api import _normalize_task_payload, reconcile_scheduler_operations, update_task
from core.notifiers import NotifyResult
from core.occurrence_state import OccurrenceStateStore
from core.scheduler import resolve_monthly_quota_window
from core.system_command_runner import _render_argv
from scripts.web_dashboard import _redact_config
from service.periodic_service import PeriodicTaskManager


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
    def setUp(self) -> None:
        db_module.DB.reset_for_tests()

    def tearDown(self) -> None:
        db_module.DB.reset_for_tests()

    def _use_temp_db(self, db_path: Path) -> Path:
        original = db_module.TODO_DB
        self.addCleanup(setattr, db_module, "TODO_DB", original)
        self.addCleanup(db_module.DB.reset_for_tests)
        db_module.TODO_DB = db_path
        db_module.DB.reset_for_tests()
        return db_path

    def _create_db(self, directory: str) -> Path:
        db_path = Path(directory) / "todo.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        return self._use_temp_db(db_path)

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

    def test_occurrence_execution_claim_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_db(temp_dir)
            db = db_module.DB()
            db.execute(
                "INSERT INTO periodic_tasks (id, name, cycle_type, is_active) VALUES (1, 'job', 'daily', 1)"
            )
            db.execute(
                "INSERT INTO periodic_occurrences (id, task_id, date, status) VALUES (10, 1, '2026-08-03', 'pending')"
            )
            db.commit()
            state = OccurrenceStateStore(db)
            self.assertTrue(state.claim_execution(10))
            self.assertFalse(state.claim_execution(10))
            row = db.execute(
                "SELECT status, execution_started_at FROM periodic_occurrences WHERE id = 10"
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertTrue(row["execution_started_at"])
            db.close()

    def test_system_failure_is_terminal_and_not_reexecuted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_db(temp_dir)
            db = db_module.DB()
            db.execute(
                """
                INSERT INTO periodic_tasks
                (id, name, cycle_type, time_of_day, is_active, task_kind, special_handler, handler_payload)
                VALUES (1, 'system job', 'daily', '12:30', 1, 'system', 'run_command', '{}')
                """
            )
            db.execute(
                """
                INSERT INTO periodic_occurrences
                (id, task_id, date, status, execution_job_id, scheduled_time)
                VALUES (10, 1, '2026-08-03', 'pending', 'chronos_execute_10', '12:30')
                """
            )
            db.commit()
            manager = PeriodicTaskManager()
            with mock.patch("service.periodic_service.execute_system_handler", return_value={
                "ok": False, "command_id": "python3", "exit_code": 2, "output": "boom"
            }) as execute, mock.patch.object(manager, "_clear_occurrence_jobs"), mock.patch.object(
                manager, "_send_message_now", return_value=True
            ) as send:
                self.assertFalse(manager.run_system_occurrence(10))
                self.assertFalse(manager.run_system_occurrence(10))
            self.assertEqual(execute.call_count, 1)
            self.assertIn("执行失败", send.call_args.args[0])
            row = db.execute("SELECT status, last_error, retry_count FROM periodic_occurrences WHERE id = 10").fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertIn("boom", row["last_error"])
            self.assertEqual(row["retry_count"], 1)
            manager.db.close()

    def test_generic_interpreters_require_approved_script_files(self) -> None:
        project_script = Path(__file__).resolve().parents[1] / "scripts" / "todo.py"
        with mock.patch("core.system_command_runner.get_raw_config", return_value={}):
            self.assertEqual(Path(_render_argv("python3", [str(project_script)])[1]), project_script)
            with self.assertRaisesRegex(ValueError, "inline/module execution is blocked"):
                _render_argv("python3", ["-c", "print(1)"])
            with self.assertRaisesRegex(ValueError, "inline/module execution is blocked"):
                _render_argv("bash", ["-c", "echo 1"])
            with tempfile.TemporaryDirectory() as temp_dir:
                outside = Path(temp_dir) / "outside.py"
                outside.write_text("print('x')\n", encoding="ascii")
                with self.assertRaisesRegex(ValueError, "outside system_command_allowed_roots"):
                    _render_argv("python3", [str(outside)])
                with mock.patch(
                    "core.system_command_runner.get_raw_config",
                    return_value={"system_command_allowed_roots": [temp_dir]},
                ):
                    self.assertEqual(Path(_render_argv("python3", [str(outside)])[1]), outside)

    def test_web_config_redaction_and_secret_preservation(self) -> None:
        redacted = _redact_config({"bot_token": "token", "secret": "value", "chat_id": "1"})
        self.assertEqual(redacted["bot_token"], "")
        self.assertEqual(redacted["secret"], "")
        self.assertTrue(redacted["has_bot_token"])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps({"channels": [{
                "id": "tg", "type": "telegram", "config": {"bot_token": "keep", "chat_id": "1"}
            }]}), encoding="utf-8")
            with mock.patch.object(config_module, "get_config_path", return_value=path):
                config_module.upsert_channel({
                    "id": "tg", "type": "telegram", "config": {"bot_token": "", "chat_id": "2"}
                })
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["channels"][0]["config"]["bot_token"], "keep")
            self.assertEqual(saved["channels"][0]["config"]["chat_id"], "2")

    def test_notification_retry_targets_failed_channel_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_db(temp_dir)
            db = db_module.DB()
            db.execute(
                "INSERT INTO periodic_tasks (id, name, cycle_type, is_active) VALUES (1, 'job', 'daily', 1)"
            )
            db.execute(
                "INSERT INTO periodic_occurrences (id, task_id, date, status) VALUES (10, 1, '2026-08-03', 'pending')"
            )
            db.commit()
            first_results = [
                NotifyResult(True, "ok", "telegram"),
                NotifyResult(False, "bad", "webhook", "offline"),
            ]
            meta = {"occurrence_id": 10, "task_id": 1, "task_kind": "scheduled"}
            with mock.patch.object(notifier_module, "dispatch_message", return_value=first_results), mock.patch.object(
                notifier_module, "schedule_delivery_retry", return_value=True
            ):
                notifier_module.dispatch_and_record(config={}, message="remind", meta=meta)
            db.execute("UPDATE notification_delivery SET next_retry_at = CURRENT_TIMESTAMP WHERE status = 'retry'")
            db.commit()
            with mock.patch.object(
                notifier_module,
                "dispatch_message",
                return_value=[NotifyResult(True, "bad", "webhook")],
            ) as dispatch, mock.patch.object(notifier_module, "schedule_delivery_retry"):
                self.assertEqual(notifier_module.retry_due_deliveries(config={}), 1)
            self.assertEqual(dispatch.call_args.kwargs["target_ids"], ["bad"])
            statuses = {
                row["channel_id"]: row["status"]
                for row in db.execute("SELECT channel_id, status FROM notification_delivery").fetchall()
            }
            self.assertEqual(statuses, {"ok": "sent", "bad": "sent"})
            self.assertEqual(db.execute("SELECT status FROM periodic_occurrences WHERE id = 10").fetchone()[0], "reminded")
            db.close()

    def test_schema_migration_preserves_optional_occurrence_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "todo.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(SCHEMA.replace("UNIQUE(task_id, date, scheduled_time)", "UNIQUE(task_id, date)"))
            conn.execute(
                "INSERT INTO periodic_tasks (id, name, cycle_type, is_active) VALUES (1, 'job', 'daily', 1)"
            )
            conn.execute(
                """
                INSERT INTO periodic_occurrences
                (id, task_id, date, status, completion_mode, special_handler_result)
                VALUES (10, 1, '2026-08-03', 'completed', 'manual', 'kept')
                """
            )
            conn.commit()
            conn.close()
            self._use_temp_db(db_path)
            db = db_module.DB()
            row = db.execute(
                "SELECT completion_mode, special_handler_result FROM periodic_occurrences WHERE id = 10"
            ).fetchone()
            self.assertEqual(tuple(row), ("manual", "kept"))
            self.assertEqual(db.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0], 3)
            delivery_columns = {row[1] for row in db.execute("PRAGMA table_info(notification_delivery)").fetchall()}
            self.assertIn("delivery_key", delivery_columns)
            db.close()

    def test_reconciliation_uses_current_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_db(temp_dir)
            db = db_module.DB()
            db.execute(
                "INSERT INTO periodic_tasks (id, name, cycle_type, time_of_day, is_active) VALUES (1, 'job', 'daily', '10:30', 1)"
            )
            payload = json.dumps({"task_patch": {"time_of_day": "10:30"}, "jobs": []})
            db.execute(
                """
                INSERT INTO scheduler_operation_log (id, operation, task_id, payload, status)
                VALUES ('op1', 'update_task', 1, ?, 'planned')
                """,
                (payload,),
            )
            db.commit()
            with mock.patch("core.integration_api._sync_today_schedule", return_value=None) as sync, mock.patch(
                "core.integration_api._recreate_removed_jobs"
            ) as restore:
                result = reconcile_scheduler_operations()
            self.assertEqual(result, {"recovered": 1, "failed": 0})
            sync.assert_called_once_with()
            restore.assert_not_called()
            db.close()

    def test_reconciliation_restores_previous_jobs_when_update_not_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_db(temp_dir)
            db = db_module.DB()
            db.execute(
                "INSERT INTO periodic_tasks (id, name, cycle_type, time_of_day, is_active) VALUES (1, 'job', 'daily', '09:00', 1)"
            )
            payload = json.dumps({
                "task_patch": {"time_of_day": "10:30"},
                "jobs": [{
                    "occurrence_id": 10,
                    "date": "2099-08-03",
                    "scheduled_time": "10:30",
                    "reminder_job_id": "chronos_reminder_10",
                }],
            })
            db.execute(
                """
                INSERT INTO scheduler_operation_log (id, operation, task_id, payload, status)
                VALUES ('op2', 'update_task', 1, ?, 'planned')
                """,
                (payload,),
            )
            db.commit()
            with mock.patch("core.integration_api._recreate_removed_jobs", return_value=[]) as restore, mock.patch(
                "core.integration_api._sync_today_schedule"
            ) as sync:
                result = reconcile_scheduler_operations()
            self.assertEqual(result, {"recovered": 1, "failed": 0})
            restore.assert_called_once()
            sync.assert_not_called()
            db.close()

    def test_retry_cli_removes_current_one_shot_before_processing(self) -> None:
        manager = mock.Mock()
        manager.db = mock.Mock()
        with mock.patch("cli.periodic_cli.PeriodicTaskManager", return_value=manager), mock.patch(
            "core.config.get_config", return_value={"channels": []}
        ), mock.patch("core.notifiers.retry_due_deliveries", return_value=2) as retry, mock.patch(
            "core.system_scheduler.remove_job", return_value=True
        ) as remove:
            self.assertEqual(run_cli(["--retry-deliveries"]), 0)
        remove.assert_called_once_with("chronos_delivery_retry")
        retry.assert_called_once_with(config={"channels": []})


if __name__ == "__main__":
    unittest.main()
