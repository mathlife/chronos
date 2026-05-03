#!/usr/bin/env python3
"""Regression checks for migrate_system_command_payloads script logic."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.migrate_system_command_payloads import apply_plans, build_plans
from scripts.test_helpers import make_case_dir


SCHEMA_SQL = """
CREATE TABLE periodic_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    special_handler TEXT,
    handler_payload TEXT,
    updated_at TEXT
);
"""


def prepare_temp_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def test_migration_plan_and_apply() -> None:
    db_path = make_case_dir("migrate-system-command") / "todo.db"
    prepare_temp_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO periodic_tasks (id, name, special_handler, handler_payload) VALUES (1, 'legacy-a', 'run_command', ?)",
            ('{"command":"echo hello"}',),
        )
        conn.execute(
            "INSERT INTO periodic_tasks (id, name, special_handler, handler_payload) VALUES (2, 'already-new', 'run_command', ?)",
            ('{"command_id":"echo","args":["ok"]}',),
        )
        conn.execute(
            "INSERT INTO periodic_tasks (id, name, special_handler, handler_payload) VALUES (3, 'legacy-string', 'run_command', ?)",
            ("echo world",),
        )
        conn.execute(
            "INSERT INTO periodic_tasks (id, name, special_handler, handler_payload) VALUES (4, 'non-system', 'other', ?)",
            ('{"command":"echo ignore"}',),
        )
        conn.commit()

        plans = build_plans(conn)
        assert sum(1 for p in plans if p.action == "update") == 2
        updated = apply_plans(conn, plans)
        assert updated == 2

        row1 = conn.execute("SELECT handler_payload FROM periodic_tasks WHERE id = 1").fetchone()
        row3 = conn.execute("SELECT handler_payload FROM periodic_tasks WHERE id = 3").fetchone()
        assert '"command_id": "echo"' in row1[0]
        assert '"command_id": "echo"' in row3[0]
    finally:
        conn.close()


if __name__ == "__main__":
    test_migration_plan_and_apply()
    print("[ok] migrate_system_command_payloads converts legacy payloads")
