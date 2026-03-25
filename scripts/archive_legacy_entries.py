#!/usr/bin/env python3
"""Archive migrated legacy entries after canonical periodic task migration.

This is a conservative final-step cleanup:
- only touches entries that are already linked from periodic_tasks via legacy_entry_id
- never deletes legacy rows
- marks them readonly + archived so they stop behaving like live legacy tasks
- preserves an audit trail on the legacy row itself

Run after migrate_legacy_entries.py has linked/created canonical tasks.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

ARCHIVE_STATUS = 'archived'
ARCHIVE_NOTE_PREFIX = 'Chronos legacy archive:'
ENTRY_ARCHIVE_COLUMNS = {
    'chronos_readonly': "ALTER TABLE entries ADD COLUMN chronos_readonly INTEGER NOT NULL DEFAULT 0",
    'chronos_archived_at': "ALTER TABLE entries ADD COLUMN chronos_archived_at TEXT",
    'chronos_archive_reason': "ALTER TABLE entries ADD COLUMN chronos_archive_reason TEXT",
    'chronos_archived_from_status': "ALTER TABLE entries ADD COLUMN chronos_archived_from_status TEXT",
    'chronos_linked_task_id': "ALTER TABLE entries ADD COLUMN chronos_linked_task_id INTEGER",
}


@dataclass
class ArchivePlan:
    entry_id: int
    text: str
    entry_status: str
    task_id: int
    task_name: str
    task_source: str
    action: str
    reason: str
    readonly: int = 0
    archived_at: str | None = None
    archived_from_status: str | None = None
    linked_task_id: int | None = None


@dataclass
class AppliedResult:
    entry_id: int
    task_id: int
    note: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Archive readonly legacy entries already linked to periodic_tasks')
    parser.add_argument('--db', required=True, help='Path to todo.db')
    parser.add_argument('--apply', action='store_true', help='Apply changes (default is dry-run)')
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON summary')
    return parser.parse_args()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_entry_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute('PRAGMA table_info(entries)').fetchall()}


def ensure_archive_columns(conn: sqlite3.Connection) -> None:
    columns = get_entry_columns(conn)
    for name, statement in ENTRY_ARCHIVE_COLUMNS.items():
        if name not in columns:
            conn.execute(statement)
    conn.commit()


def select_linked_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = get_entry_columns(conn)
    readonly_sql = 'COALESCE(e.chronos_readonly, 0) AS chronos_readonly' if 'chronos_readonly' in columns else '0 AS chronos_readonly'
    archived_at_sql = 'e.chronos_archived_at AS chronos_archived_at' if 'chronos_archived_at' in columns else 'NULL AS chronos_archived_at'
    archived_from_sql = 'e.chronos_archived_from_status AS chronos_archived_from_status' if 'chronos_archived_from_status' in columns else 'NULL AS chronos_archived_from_status'
    linked_task_sql = 'e.chronos_linked_task_id AS chronos_linked_task_id' if 'chronos_linked_task_id' in columns else 'NULL AS chronos_linked_task_id'

    query = f"""
        SELECT e.id, e.text, e.status,
               t.id AS task_id, t.name AS task_name, COALESCE(t.source, 'chronos') AS task_source,
               {readonly_sql},
               {archived_at_sql},
               {archived_from_sql},
               {linked_task_sql}
        FROM entries e
        JOIN periodic_tasks t ON t.legacy_entry_id = e.id
        ORDER BY e.id, t.id
    """
    return conn.execute(query).fetchall()


def classify_row(row: sqlite3.Row) -> ArchivePlan:
    status = row['status']
    readonly = int(row['chronos_readonly'] or 0)
    archived_at = row['chronos_archived_at']
    archived_from_status = row['chronos_archived_from_status']
    linked_task_id = row['chronos_linked_task_id']

    if status == ARCHIVE_STATUS and readonly == 1:
        return ArchivePlan(
            entry_id=row['id'],
            text=row['text'],
            entry_status=status,
            task_id=row['task_id'],
            task_name=row['task_name'],
            task_source=row['task_source'],
            action='already_archived',
            reason='legacy row is already archived and readonly',
            readonly=readonly,
            archived_at=archived_at,
            archived_from_status=archived_from_status,
            linked_task_id=linked_task_id,
        )

    if status == ARCHIVE_STATUS and readonly != 1:
        return ArchivePlan(
            entry_id=row['id'],
            text=row['text'],
            entry_status=status,
            task_id=row['task_id'],
            task_name=row['task_name'],
            task_source=row['task_source'],
            action='repair_archive_metadata',
            reason='status is archived but readonly metadata is incomplete',
            readonly=readonly,
            archived_at=archived_at,
            archived_from_status=archived_from_status,
            linked_task_id=linked_task_id,
        )

    return ArchivePlan(
        entry_id=row['id'],
        text=row['text'],
        entry_status=status,
        task_id=row['task_id'],
        task_name=row['task_name'],
        task_source=row['task_source'],
        action='archive',
        reason='linked canonical periodic task exists; legacy row should become readonly archive',
        readonly=readonly,
        archived_at=archived_at,
        archived_from_status=archived_from_status,
        linked_task_id=linked_task_id,
    )


def build_archive_reason(task_id: int, task_name: str, task_source: str) -> str:
    return f"{ARCHIVE_NOTE_PREFIX} linked to periodic_tasks.id={task_id} name={task_name} source={task_source}"


def apply_plan(conn: sqlite3.Connection, plan: ArchivePlan, archived_at: str) -> AppliedResult:
    previous_status = plan.archived_from_status or plan.entry_status
    reason = build_archive_reason(plan.task_id, plan.task_name, plan.task_source)
    conn.execute(
        """
        UPDATE entries
        SET status = ?,
            updated_at = CURRENT_TIMESTAMP,
            chronos_readonly = 1,
            chronos_archived_at = COALESCE(chronos_archived_at, ?),
            chronos_archive_reason = ?,
            chronos_archived_from_status = COALESCE(chronos_archived_from_status, ?),
            chronos_linked_task_id = ?
        WHERE id = ?
        """,
        (ARCHIVE_STATUS, archived_at, reason, previous_status, plan.task_id, plan.entry_id),
    )
    return AppliedResult(
        entry_id=plan.entry_id,
        task_id=plan.task_id,
        note=f"archived legacy entry {plan.entry_id} -> task {plan.task_id} ({plan.task_name})",
    )


def summarize(plans: list[ArchivePlan], applied: list[AppliedResult], apply_mode: bool) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.action] = counts.get(plan.action, 0) + 1
    return {
        'mode': 'apply' if apply_mode else 'dry-run',
        'counts': counts,
        'plans': [asdict(plan) for plan in plans],
        'applied': [asdict(item) for item in applied],
    }


def print_human(summary: dict[str, Any]) -> None:
    print(f"Chronos legacy archive ({summary['mode']})")
    for action, count in sorted(summary['counts'].items()):
        print(f"- {action}: {count}")
    print()
    for plan in summary['plans']:
        print(
            f"[{plan['action']}] entry {plan['entry_id']} -> task {plan['task_id']} "
            f"| status={plan['entry_status']} readonly={plan['readonly']} "
            f"| {plan['reason']}"
        )
    if summary['applied']:
        print()
        print('Applied:')
        for item in summary['applied']:
            print(f"- {item['note']}")


def main() -> int:
    args = parse_args()
    conn = connect(args.db)
    try:
        rows = select_linked_rows(conn)
        plans = [classify_row(row) for row in rows]
        applied: list[AppliedResult] = []
        if args.apply and plans:
            ensure_archive_columns(conn)
            archived_at = datetime.now().isoformat(timespec='seconds')
            for plan in plans:
                if plan.action in {'archive', 'repair_archive_metadata'}:
                    applied.append(apply_plan(conn, plan, archived_at))
            conn.commit()
        summary = summarize(plans, applied, args.apply)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_human(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
