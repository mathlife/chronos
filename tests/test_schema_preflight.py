import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "schema_preflight.py"
MODULE_NAME = "chronos_schema_preflight"


def load_module(db_path: Path):
    for name in [MODULE_NAME, "core.paths"]:
        sys.modules.pop(name, None)

    os.environ["CHRONOS_DB_PATH"] = str(db_path)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_db(db_path: Path, *, include_unique: bool = True, statuses: list[str] | None = None):
    statuses = statuses or ["pending"]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE periodic_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT 'Inbox'
        )
        """
    )
    if include_unique:
        cur.execute(
            """
            CREATE TABLE periodic_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                reminder_job_id TEXT,
                FOREIGN KEY (task_id) REFERENCES periodic_tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, date)
            )
            """
        )
    else:
        cur.execute(
            """
            CREATE TABLE periodic_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                reminder_job_id TEXT,
                FOREIGN KEY (task_id) REFERENCES periodic_tasks(id) ON DELETE CASCADE
            )
            """
        )
    cur.execute("INSERT INTO periodic_tasks (name) VALUES ('test-task')")
    for index, status in enumerate(statuses, start=1):
        cur.execute(
            "INSERT INTO periodic_occurrences (task_id, date, status) VALUES (1, ?, ?)",
            (f"2026-03-{index:02d}", status),
        )
    conn.commit()
    conn.close()


class SchemaPreflightTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("CHRONOS_DB_PATH", None)

    def test_inspect_schema_reports_ok_for_expected_runtime_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "todo.db"
            create_db(db_path)
            module = load_module(db_path)

            info = module.inspect_schema()

            self.assertEqual(info["status"], "ok")
            self.assertTrue(info["checks"]["periodic_occurrences_unique_task_date"])
            self.assertEqual(info["checks"]["duplicate_occurrence_groups"], 0)
            self.assertEqual(info["checks"]["invalid_statuses"], [])

    def test_inspect_schema_warns_when_unique_constraint_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "todo.db"
            create_db(db_path, include_unique=False)
            module = load_module(db_path)

            info = module.inspect_schema()

            self.assertEqual(info["status"], "warn")
            self.assertFalse(info["checks"]["periodic_occurrences_unique_task_date"])

    def test_inspect_schema_warns_on_invalid_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "todo.db"
            create_db(db_path, statuses=["pending", "bogus"])
            module = load_module(db_path)

            info = module.inspect_schema()

            self.assertEqual(info["status"], "warn")
            self.assertEqual(info["checks"]["invalid_statuses"], [{"status": "bogus", "count": 1}])


if __name__ == "__main__":
    unittest.main()
