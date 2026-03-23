#!/usr/bin/env python3
"""Preflight checks for Chronos runtime DB schema health."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.paths import TODO_DB, WORKSPACE

ALLOWED_OCCURRENCE_STATUSES = {"pending", "completed", "skipped", "reminded"}
REQUIRED_TABLES = {"periodic_tasks", "periodic_occurrences"}


def get_table_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cur.fetchall()}


def get_table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    cur = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    row = cur.fetchone()
    return row[0] or "" if row else ""


def count_duplicate_occurrences(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT task_id, date, COUNT(*) AS n
            FROM periodic_occurrences
            GROUP BY task_id, date
            HAVING COUNT(*) > 1
        )
        """
    )
    return int(cur.fetchone()[0])


def get_invalid_statuses(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT status, COUNT(*)
        FROM periodic_occurrences
        GROUP BY status
        ORDER BY status
        """
    )
    invalid = []
    for status, count in cur.fetchall():
        if status not in ALLOWED_OCCURRENCE_STATUSES:
            invalid.append({"status": status, "count": count})
    return invalid


def inspect_schema() -> dict:
    info = {
        "workspace": str(WORKSPACE),
        "runtime_db": str(TODO_DB),
        "db_exists": TODO_DB.exists(),
        "status": "ok",
        "errors": [],
        "checks": {},
    }

    if not TODO_DB.exists():
        info["status"] = "error"
        info["errors"].append("Runtime todo.db does not exist")
        return info

    conn = sqlite3.connect(str(TODO_DB))
    try:
        tables = get_table_names(conn)
        missing_tables = sorted(REQUIRED_TABLES - tables)
        if missing_tables:
            info["status"] = "error"
            info["errors"].append(f"Missing required tables: {', '.join(missing_tables)}")
            return info

        occurrences_sql = get_table_sql(conn, "periodic_occurrences")
        tasks_sql = get_table_sql(conn, "periodic_tasks")
        duplicate_count = count_duplicate_occurrences(conn)
        invalid_statuses = get_invalid_statuses(conn)

        info["checks"] = {
            "tables_present": sorted(REQUIRED_TABLES),
            "periodic_occurrences_unique_task_date": "UNIQUE(task_id, date)" in occurrences_sql,
            "periodic_occurrences_fk_task_id": "FOREIGN KEY (task_id) REFERENCES periodic_tasks(id) ON DELETE CASCADE" in occurrences_sql,
            "periodic_tasks_name_unique": "name TEXT NOT NULL UNIQUE" in tasks_sql,
            "duplicate_occurrence_groups": duplicate_count,
            "invalid_statuses": invalid_statuses,
        }

        if not info["checks"]["periodic_occurrences_unique_task_date"]:
            info["status"] = "warn"
            info["errors"].append("Missing UNIQUE(task_id, date) on periodic_occurrences")
        if not info["checks"]["periodic_occurrences_fk_task_id"]:
            info["status"] = "warn"
            info["errors"].append("Missing FK(task_id -> periodic_tasks.id ON DELETE CASCADE)")
        if duplicate_count > 0:
            info["status"] = "warn"
            info["errors"].append(f"Found {duplicate_count} duplicate occurrence groups")
        if invalid_statuses:
            info["status"] = "warn"
            info["errors"].append("Found invalid periodic_occurrences.status values")

        return info
    finally:
        conn.close()


def main() -> int:
    info = inspect_schema()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
