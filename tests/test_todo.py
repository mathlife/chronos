import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TODO_SCRIPT = PROJECT_ROOT / "scripts" / "todo.py"

spec = importlib.util.spec_from_file_location("chronos_todo", TODO_SCRIPT)
todo_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(todo_module)


class TodoHelpersTests(unittest.TestCase):
    def test_parse_entry_identifier_accepts_prefixed_ids(self):
        self.assertEqual(todo_module.parse_entry_identifier("ID45"), 45)
        self.assertEqual(todo_module.parse_entry_identifier("45"), 45)

    def test_parse_compact_end_date_supports_yymmdd(self):
        self.assertEqual(todo_module.parse_compact_end_date("260630"), "2026-06-30")
        self.assertEqual(todo_module.parse_compact_end_date("20260630"), "2026-06-30")
        self.assertIsNone(todo_module.parse_compact_end_date("20261340"))

    def test_natural_language_parser_extracts_compact_end_date(self):
        parsed = todo_module.parse_natural_language("添加任务 每周三 10:00 周三抢券 结束日期260630")

        self.assertEqual(parsed["cmd"], "add")
        self.assertEqual(parsed["cycle_type"], "weekly")
        self.assertEqual(parsed["weekday"], 2)
        self.assertEqual(parsed["time_of_day"], "10:00")
        self.assertEqual(parsed["end_date"], "2026-06-30")

    def test_natural_language_parser_detects_complete_overdue(self):
        parsed = todo_module.parse_natural_language("自动完成逾期待办")
        self.assertEqual(parsed["cmd"], "complete-overdue")


class TodoOverdueCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "todo.db"
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "memory").mkdir()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE periodic_tasks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                cycle_type TEXT NOT NULL,
                time_of_day TEXT,
                count_current_month INTEGER DEFAULT 0,
                special_handler TEXT,
                handler_payload TEXT,
                legacy_entry_id INTEGER,
                start_date TEXT,
                task_kind TEXT DEFAULT 'scheduled',
                source TEXT DEFAULT 'chronos',
                delivery_target TEXT,
                delivery_mode TEXT
            );
            CREATE TABLE periodic_occurrences (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL,
                reminder_job_id TEXT,
                completed_at TEXT,
                is_auto_completed INTEGER DEFAULT 0,
                completion_mode TEXT,
                special_handler_result TEXT,
                scheduled_time TEXT,
                scheduled_at TEXT,
                legacy_entry_id INTEGER
            );
            """
        )
        conn.execute("INSERT INTO groups (id, name) VALUES (1, 'System')")
        conn.execute(
            "INSERT INTO periodic_tasks (id, name, category, cycle_type, time_of_day, count_current_month) VALUES (1, '周期测试任务', 'System', 'daily', '09:00', 0)"
        )
        conn.execute(
            "INSERT INTO periodic_occurrences (id, task_id, date, status) VALUES (101, 1, '2026-03-25', 'pending')"
        )
        conn.execute(
            "INSERT INTO periodic_tasks (id, name, category, cycle_type, time_of_day, count_current_month, special_handler, task_kind, source, start_date) VALUES (2, 'Meta-Review fallback', 'System', 'daily', '02:00', 0, 'meta_review_fallback', 'system', 'system_seeded', '2026-03-01')"
        )
        conn.execute(
            "INSERT INTO periodic_occurrences (id, task_id, date, status) VALUES (202, 2, '2026-03-25', 'pending')"
        )
        conn.execute(
            "INSERT INTO entries (id, text, status, group_id) VALUES (16, 'Meta-Review (daily 02:00): Run meta_auditor.py analyze --days 1 and apply high-confidence suggestions', 'pending', 1)"
        )
        conn.execute(
            "INSERT INTO entries (id, text, status, group_id) VALUES (17, '每 4 小时 08:00：同步 subagent 记忆 (memory_manager.py sync)', 'pending', 1)"
        )
        conn.execute(
            "INSERT INTO entries (id, text, status, group_id) VALUES (18, '给朋友发消息 21:00', 'pending', 1)"
        )
        conn.commit()
        conn.close()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def test_get_overdue_legacy_entries_filters_to_recurring_and_due(self):
        with patch.object(todo_module, 'TODO_DB', self.db_path):
            entries = todo_module.get_overdue_legacy_entries(datetime(2026, 3, 25, 11, 30))

        identifiers = [entry['identifier'] for entry in entries]
        self.assertEqual(identifiers, ['ID16', 'ID17'])
        self.assertEqual(entries[0]['special_handler'], 'meta_review_fallback')
        self.assertIsNone(entries[1]['special_handler'])

    def test_complete_overdue_tasks_runs_special_handler_from_periodic_metadata(self):
        completed_activity_calls = []

        def fake_run(args, capture_output=True, text=True, timeout=None):
            if '--complete-activity' in args:
                completed_activity_calls.append(args[-1])

            class Result:
                returncode = 0
                stdout = ''
                stderr = ''

            return Result()

        with patch.object(todo_module, 'TODO_DB', self.db_path), \
             patch.object(todo_module, 'WORKSPACE', self.workspace), \
             patch.object(todo_module, 'subprocess') as mock_subprocess:
            mock_subprocess.run.side_effect = fake_run
            result = todo_module.complete_overdue_tasks(now=datetime(2026, 3, 25, 11, 30))

        self.assertFalse(result['errors'])
        self.assertEqual(result['handled'], ['FIN-202', 'FIN-101', 'ID16', 'ID17'])
        self.assertIn('2', completed_activity_calls)
        self.assertIn('1', completed_activity_calls)

        conn = self._connect()
        special_row = conn.execute(
            "SELECT status, completion_mode, special_handler_result FROM periodic_occurrences WHERE id = 202"
        ).fetchone()
        occ_status = conn.execute("SELECT status FROM periodic_occurrences WHERE id = 101").fetchone()[0]
        meta_status = conn.execute("SELECT status FROM entries WHERE id = 16").fetchone()[0]
        recurring_status = conn.execute("SELECT status FROM entries WHERE id = 17").fetchone()[0]
        future_status = conn.execute("SELECT status FROM entries WHERE id = 18").fetchone()[0]
        conn.close()

        self.assertEqual(special_row[0], 'completed')
        self.assertEqual(special_row[1], 'fallback_handler')
        self.assertIn('Meta-Review fallback completed via direct PREDICTIONS.md/FRICTION.md inspection', special_row[2])
        self.assertEqual(occ_status, 'completed')
        self.assertEqual(meta_status, 'done')
        self.assertEqual(recurring_status, 'done')
        self.assertEqual(future_status, 'pending')

        memory_log = (self.workspace / 'memory' / '2026-03-25.md').read_text(encoding='utf-8')
        self.assertIn('Meta-Review fallback completed via direct PREDICTIONS.md/FRICTION.md inspection', memory_log)

    def test_complete_overdue_tasks_dry_run_does_not_change_state(self):
        with patch.object(todo_module, 'TODO_DB', self.db_path), \
             patch.object(todo_module, 'WORKSPACE', self.workspace), \
             patch.object(todo_module, 'subprocess') as mock_subprocess:
            mock_subprocess.run.return_value = type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
            result = todo_module.complete_overdue_tasks(now=datetime(2026, 3, 25, 11, 30), dry_run=True)

        self.assertEqual(result['handled'], ['FIN-202', 'FIN-101', 'ID16', 'ID17'])
        self.assertEqual(len(result['simulated']), 4)

        conn = self._connect()
        occ_status = conn.execute("SELECT status FROM periodic_occurrences WHERE id = 101").fetchone()[0]
        meta_status = conn.execute("SELECT status FROM entries WHERE id = 16").fetchone()[0]
        special_status = conn.execute("SELECT status FROM periodic_occurrences WHERE id = 202").fetchone()[0]
        conn.close()

        self.assertEqual(occ_status, 'pending')
        self.assertEqual(meta_status, 'pending')
        self.assertEqual(special_status, 'pending')
        self.assertFalse((self.workspace / 'memory' / '2026-03-25.md').exists())


if __name__ == "__main__":
    unittest.main()
