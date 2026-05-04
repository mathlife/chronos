#!/usr/bin/env python3
"""Compact Chronos DB after legacy merge.

This script is intentionally conservative and only targets post-migration
residues. Default mode is dry-run.

What it can do:
1) delete archived+linked legacy entries that already have canonical tasks
2) clear periodic_tasks.legacy_entry_id on legacy-sourced tasks
3) normalize periodic_tasks.source from legacy markers to "chronos"
4) clear periodic_occurrences.legacy_entry_id
5) prune scheduler_operation_log older than retention days
6) optional VACUUM/ANALYZE for file compaction/stat refresh
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LEGACY_SOURCES = ("legacy_entries_linked", "legacy_entries_migrated")


@dataclass
class PlanItem:
    action: str
    count: int
    note: str


@dataclass
class AppliedItem:
    action: str
    affected: int
    note: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact Chronos DB after canonical merge")
    parser.add_argument("--db", required=True, help="Path to todo.db")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default dry-run)")
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=30,
        help="Keep scheduler_operation_log rows within this many days (default: 30)",
    )
    parser.add_argument(
        "--skip-vacuum",
        action="store_true",
        help="Skip VACUUM/ANALYZE in apply mode",
    )
    return parser.parse_args()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def get_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def build_archive_predicate(entry_columns: set[str]) -> str:
    checks = ["e.status = 'archived'"]
    if "chronos_archived_at" in entry_columns:
        checks.append("e.chronos_archived_at IS NOT NULL")
    if "chronos_archive_reason" in entry_columns:
        checks.append("e.chronos_archive_reason LIKE 'Chronos legacy archive:%'")
    if "chronos_archived_from_status" in entry_columns:
        checks.append("e.chronos_archived_from_status IS NOT NULL")
    return "(" + " OR ".join(checks) + ")"


def count_legacy_archived_entries(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "entries") or not table_exists(conn, "periodic_tasks"):
        return 0
    entry_columns = get_columns(conn, "entries")
    archive_predicate = build_archive_predicate(entry_columns)
    source_placeholders = ",".join("?" for _ in LEGACY_SOURCES)
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT e.id)
        FROM entries e
        JOIN periodic_tasks t ON t.legacy_entry_id = e.id
        WHERE t.source IN ({source_placeholders})
          AND {archive_predicate}
        """,
        LEGACY_SOURCES,
    ).fetchone()
    return int(row[0] if row else 0)


def count_tasks_with_legacy_ref(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "periodic_tasks"):
        return 0
    source_placeholders = ",".join("?" for _ in LEGACY_SOURCES)
    row = conn.execute(
        f"""
        SELECT COUNT(1)
        FROM periodic_tasks
        WHERE source IN ({source_placeholders})
          AND legacy_entry_id IS NOT NULL
        """,
        LEGACY_SOURCES,
    ).fetchone()
    return int(row[0] if row else 0)


def count_tasks_need_source_normalize(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "periodic_tasks"):
        return 0
    source_placeholders = ",".join("?" for _ in LEGACY_SOURCES)
    row = conn.execute(
        f"""
        SELECT COUNT(1)
        FROM periodic_tasks
        WHERE source IN ({source_placeholders})
        """,
        LEGACY_SOURCES,
    ).fetchone()
    return int(row[0] if row else 0)


def count_occurrences_with_legacy_ref(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "periodic_occurrences"):
        return 0
    columns = get_columns(conn, "periodic_occurrences")
    if "legacy_entry_id" not in columns:
        return 0
    row = conn.execute(
        "SELECT COUNT(1) FROM periodic_occurrences WHERE legacy_entry_id IS NOT NULL",
    ).fetchone()
    return int(row[0] if row else 0)


def count_scheduler_log_prunable(conn: sqlite3.Connection, retention_days: int) -> int:
    if retention_days < 0 or not table_exists(conn, "scheduler_operation_log"):
        return 0
    row = conn.execute(
        "SELECT COUNT(1) FROM scheduler_operation_log WHERE created_at < datetime('now', ?)",
        (f"-{retention_days} day",),
    ).fetchone()
    return int(row[0] if row else 0)


def build_plan(conn: sqlite3.Connection, retention_days: int, skip_vacuum: bool) -> list[PlanItem]:
    plan = [
        PlanItem(
            action="delete_legacy_archived_entries",
            count=count_legacy_archived_entries(conn),
            note="Delete legacy entries already archived and linked to canonical tasks",
        ),
        PlanItem(
            action="clear_task_legacy_entry_id",
            count=count_tasks_with_legacy_ref(conn),
            note="Set periodic_tasks.legacy_entry_id=NULL for legacy-sourced tasks",
        ),
        PlanItem(
            action="normalize_task_source",
            count=count_tasks_need_source_normalize(conn),
            note="Set periodic_tasks.source='chronos' where source is legacy marker",
        ),
        PlanItem(
            action="clear_occurrence_legacy_entry_id",
            count=count_occurrences_with_legacy_ref(conn),
            note="Set periodic_occurrences.legacy_entry_id=NULL",
        ),
        PlanItem(
            action="prune_scheduler_operation_log",
            count=count_scheduler_log_prunable(conn, retention_days),
            note=f"Delete scheduler_operation_log rows older than {retention_days} days",
        ),
        PlanItem(
            action="vacuum_analyze",
            count=0 if skip_vacuum else 1,
            note="Run VACUUM and ANALYZE for compaction and planner stats",
        ),
    ]
    return plan


def apply_plan(conn: sqlite3.Connection, retention_days: int, skip_vacuum: bool) -> list[AppliedItem]:
    applied: list[AppliedItem] = []
    source_placeholders = ",".join("?" for _ in LEGACY_SOURCES)
    entry_columns = get_columns(conn, "entries") if table_exists(conn, "entries") else set()
    archive_predicate = build_archive_predicate(entry_columns) if entry_columns else ""

    conn.execute("BEGIN IMMEDIATE")
    try:
        if table_exists(conn, "entries") and table_exists(conn, "periodic_tasks"):
            cur = conn.execute(
                f"""
                DELETE FROM entries
                WHERE id IN (
                    SELECT e.id
                    FROM entries e
                    JOIN periodic_tasks t ON t.legacy_entry_id = e.id
                    WHERE t.source IN ({source_placeholders})
                      AND {archive_predicate}
                )
                """,
                LEGACY_SOURCES,
            )
            applied.append(
                AppliedItem(
                    action="delete_legacy_archived_entries",
                    affected=cur.rowcount,
                    note="Deleted archived legacy entries linked to canonical tasks",
                )
            )

        if table_exists(conn, "periodic_tasks"):
            cur = conn.execute(
                f"""
                UPDATE periodic_tasks
                SET legacy_entry_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE source IN ({source_placeholders})
                  AND legacy_entry_id IS NOT NULL
                """,
                LEGACY_SOURCES,
            )
            applied.append(
                AppliedItem(
                    action="clear_task_legacy_entry_id",
                    affected=cur.rowcount,
                    note="Cleared periodic_tasks.legacy_entry_id",
                )
            )

            cur = conn.execute(
                f"""
                UPDATE periodic_tasks
                SET source = 'chronos', updated_at = CURRENT_TIMESTAMP
                WHERE source IN ({source_placeholders})
                """,
                LEGACY_SOURCES,
            )
            applied.append(
                AppliedItem(
                    action="normalize_task_source",
                    affected=cur.rowcount,
                    note="Normalized periodic_tasks.source to chronos",
                )
            )

        if table_exists(conn, "periodic_occurrences"):
            occ_cols = get_columns(conn, "periodic_occurrences")
            if "legacy_entry_id" in occ_cols:
                cur = conn.execute(
                    """
                    UPDATE periodic_occurrences
                    SET legacy_entry_id = NULL
                    WHERE legacy_entry_id IS NOT NULL
                    """
                )
                applied.append(
                    AppliedItem(
                        action="clear_occurrence_legacy_entry_id",
                        affected=cur.rowcount,
                        note="Cleared periodic_occurrences.legacy_entry_id",
                    )
                )

        if retention_days >= 0 and table_exists(conn, "scheduler_operation_log"):
            cur = conn.execute(
                """
                DELETE FROM scheduler_operation_log
                WHERE created_at < datetime('now', ?)
                """,
                (f"-{retention_days} day",),
            )
            applied.append(
                AppliedItem(
                    action="prune_scheduler_operation_log",
                    affected=cur.rowcount,
                    note=f"Pruned scheduler_operation_log older than {retention_days} days",
                )
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if not skip_vacuum:
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        applied.append(
            AppliedItem(
                action="vacuum_analyze",
                affected=1,
                note="Executed VACUUM and ANALYZE",
            )
        )
    return applied


def print_human(summary: dict[str, Any]) -> None:
    print(f"Chronos canonical compaction ({summary['mode']})")
    print(f"db={summary['db']}")
    for item in summary["plan"]:
        print(f"- {item['action']}: {item['count']} | {item['note']}")
    if summary.get("applied"):
        print()
        print("Applied:")
        for item in summary["applied"]:
            print(f"- {item['action']}: affected={item['affected']} | {item['note']}")


def main() -> int:
    args = parse_args()
    db_path = str(Path(args.db).expanduser())
    conn = connect(db_path)
    try:
        required = {"periodic_tasks", "periodic_occurrences"}
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = sorted(required - tables)
        if missing:
            raise SystemExit(f"Missing required tables: {', '.join(missing)}")

        plan = build_plan(conn, retention_days=args.retention_days, skip_vacuum=bool(args.skip_vacuum))
        applied: list[AppliedItem] = []
        if args.apply:
            applied = apply_plan(conn, retention_days=args.retention_days, skip_vacuum=bool(args.skip_vacuum))
        summary = {
            "mode": "apply" if args.apply else "dry-run",
            "db": db_path,
            "plan": [asdict(item) for item in plan],
            "applied": [asdict(item) for item in applied],
        }
    finally:
        conn.close()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_human(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
