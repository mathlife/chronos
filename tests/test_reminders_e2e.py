import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANAGER_SCRIPT = PROJECT_ROOT / "scripts" / "periodic_task_manager.py"
MODULE_NAME = "chronos_periodic_task_manager_e2e"
CORE_MODULES = [
    "core.paths",
    "core.db",
    "core.scheduler",
    "core.config",
    "core.learning",
    "core.models",
]


def load_manager_module(db_path: Path):
    for name in CORE_MODULES + [MODULE_NAME]:
        sys.modules.pop(name, None)

    os.environ["CHRONOS_DB_PATH"] = str(db_path)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MANAGER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_test_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE periodic_tasks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'Inbox',
            cycle_type TEXT NOT NULL,
            weekday INTEGER,
            day_of_month INTEGER,
            range_start INTEGER,
            range_end INTEGER,
            n_per_month INTEGER,
            time_of_day TEXT NOT NULL,
            event_time TEXT,
            timezone TEXT DEFAULT 'Asia/Shanghai',
            is_active INTEGER DEFAULT 1,
            count_current_month INTEGER DEFAULT 0,
            end_date TEXT,
            reminder_template TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reminder_job_id TEXT,
            is_auto_completed INTEGER DEFAULT 0,
            completed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


class ReminderEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "todo.db"
        create_test_db(self.db_path)
        self.module = load_manager_module(self.db_path)
        self.manager = self.module.PeriodicTaskManager()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            INSERT INTO periodic_tasks (
                id, name, category, cycle_type, weekday, time_of_day, event_time, timezone,
                is_active, count_current_month, end_date, reminder_template, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (1, "测试提醒", "Inbox", "daily", None, "10:00", "10:00", "Asia/Shanghai", 1, 0, None, None),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.manager.db.close()
        self.tempdir.cleanup()
        os.environ.pop("CHRONOS_DB_PATH", None)

    def test_generate_reminders_for_today_persists_reminder_job_id(self):
        with patch.object(self.module, "to_shanghai_date", return_value=date(2026, 3, 23)), \
             patch.object(self.module, "datetime", wraps=self.module.datetime) as mock_datetime, \
             patch.object(self.module, "get_chat_id", return_value="123456"), \
             patch.object(self.module.subprocess, "run") as mock_run:
            mock_datetime.now.return_value = self.module.datetime(2026, 3, 23, 0, 0, tzinfo=self.module.ZoneInfo("UTC"))
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            scheduled = self.manager.generate_reminders_for_today()

            self.assertEqual(scheduled, 1)
            row = self.conn.execute(
                "SELECT status, reminder_job_id FROM periodic_occurrences WHERE task_id = 1 AND date = ?",
                ("2026-03-23",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["reminder_job_id"], "task_reminder_1_20260323")

            cmd = mock_run.call_args.args[0]
            self.assertIn("--announce", cmd)
            self.assertIn("--to", cmd)
            self.assertEqual(cmd[cmd.index("--to") + 1], "123456")
            self.assertEqual(cmd[cmd.index("--session") + 1], "isolated")

    def test_generate_reminders_for_today_uses_immediate_branch_without_persisting_job(self):
        with patch.object(self.module, "to_shanghai_date", return_value=date(2026, 3, 23)), \
             patch.object(self.module, "datetime", wraps=self.module.datetime) as mock_datetime, \
             patch.object(self.module, "get_chat_id", return_value="123456"), \
             patch.object(self.module.subprocess, "run") as mock_run:
            mock_datetime.now.return_value = self.module.datetime(2026, 3, 23, 5, 0, tzinfo=self.module.ZoneInfo("UTC"))
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            scheduled = self.manager.generate_reminders_for_today()

            self.assertEqual(scheduled, 0)
            row = self.conn.execute(
                "SELECT status, reminder_job_id FROM periodic_occurrences WHERE task_id = 1 AND date = ?",
                ("2026-03-23",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["reminder_job_id"])

            cmd = mock_run.call_args.args[0]
            self.assertIn("reminder_immediate_1_202603230000", cmd)
            self.assertIn("--to", cmd)
            self.assertEqual(cmd[cmd.index("--to") + 1], "123456")

    def test_generate_reminders_for_today_skips_when_chat_id_missing(self):
        with patch.object(self.module, "to_shanghai_date", return_value=date(2026, 3, 23)), \
             patch.object(self.module, "get_chat_id", side_effect=ValueError("missing chat id")), \
             patch.object(self.module.subprocess, "run") as mock_run:
            scheduled = self.manager.generate_reminders_for_today()

            self.assertEqual(scheduled, 0)
            row = self.conn.execute(
                "SELECT status, reminder_job_id FROM periodic_occurrences WHERE task_id = 1 AND date = ?",
                ("2026-03-23",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["reminder_job_id"])
            mock_run.assert_not_called()

    def test_cleanup_old_jobs_clears_db_reference_after_cron_remove(self):
        self.conn.execute(
            "INSERT INTO periodic_occurrences (task_id, date, status, reminder_job_id) VALUES (?, ?, ?, ?)",
            (1, "2026-03-22", "pending", "task_reminder_1_20260322"),
        )
        self.conn.commit()

        with patch.object(self.module.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            cleaned = self.manager.cleanup_old_jobs(date(2026, 3, 22))

            self.assertEqual(cleaned, 1)
            row = self.conn.execute(
                "SELECT reminder_job_id FROM periodic_occurrences WHERE task_id = 1 AND date = ?",
                ("2026-03-22",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["reminder_job_id"])
            cmd = mock_run.call_args.args[0]
            self.assertEqual(cmd[:3], [self.module.OPENCLAW_BIN, "cron", "remove"])
            self.assertEqual(cmd[3], "task_reminder_1_20260322")


if __name__ == "__main__":
    unittest.main()
