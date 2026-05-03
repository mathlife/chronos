#!/usr/bin/env python3
"""Migrate legacy system command payloads to whitelist command_id+args format."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import TODO_DB
from core.system_command_runner import build_handler_payload_from_legacy_command


@dataclass
class Plan:
    task_id: int
    action: str  # update|skip|invalid
    reason: str
    before: str
    after: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy handler_payload for run_command tasks")
    parser.add_argument("--db", default=str(TODO_DB), help="SQLite DB path")
    parser.add_argument("--apply", action="store_true", help="Apply updates (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    return parser.parse_args()


def _extract_legacy_command(raw_payload: str) -> str | None:
    raw_payload = str(raw_payload or "").strip()
    if not raw_payload:
        return None
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return raw_payload

    if isinstance(decoded, dict):
        command_id = str(decoded.get("command_id") or "").strip()
        if command_id:
            return None
        command = decoded.get("command") or decoded.get("system_command")
        if isinstance(command, str) and command.strip():
            return command.strip()
        return None
    if isinstance(decoded, str) and decoded.strip():
        return decoded.strip()
    return None


def build_plans(conn: sqlite3.Connection) -> list[Plan]:
    rows = conn.execute(
        """
        SELECT id, COALESCE(handler_payload, '') AS handler_payload
        FROM periodic_tasks
        WHERE COALESCE(special_handler, '') = 'run_command'
        ORDER BY id
        """
    ).fetchall()
    plans: list[Plan] = []
    for row in rows:
        task_id = int(row[0])
        raw_payload = str(row[1] or "")
        if not raw_payload.strip():
            plans.append(Plan(task_id=task_id, action="skip", reason="empty handler_payload", before=raw_payload, after=None))
            continue
        command = _extract_legacy_command(raw_payload)
        if command is None:
            plans.append(
                Plan(
                    task_id=task_id,
                    action="skip",
                    reason="already migrated or unsupported payload shape",
                    before=raw_payload,
                    after=None,
                )
            )
            continue
        try:
            migrated = build_handler_payload_from_legacy_command(command)
        except ValueError as exc:
            plans.append(
                Plan(
                    task_id=task_id,
                    action="invalid",
                    reason=f"cannot parse legacy command: {exc}",
                    before=raw_payload,
                    after=None,
                )
            )
            continue
        if migrated == raw_payload:
            plans.append(Plan(task_id=task_id, action="skip", reason="no change", before=raw_payload, after=None))
            continue
        plans.append(
            Plan(
                task_id=task_id,
                action="update",
                reason="legacy payload converted to command_id+args",
                before=raw_payload,
                after=migrated,
            )
        )
    return plans


def apply_plans(conn: sqlite3.Connection, plans: list[Plan]) -> int:
    updates = [plan for plan in plans if plan.action == "update" and plan.after is not None]
    if not updates:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for plan in updates:
            conn.execute(
                """
                UPDATE periodic_tasks
                SET handler_payload = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (plan.after, plan.task_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(updates)


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        plans = build_plans(conn)
        updated = 0
        if args.apply:
            updated = apply_plans(conn, plans)

        summary: dict[str, Any] = {
            "mode": "apply" if args.apply else "dry-run",
            "db": str(db_path),
            "updated": updated,
            "counts": {
                "update": sum(1 for p in plans if p.action == "update"),
                "skip": sum(1 for p in plans if p.action == "skip"),
                "invalid": sum(1 for p in plans if p.action == "invalid"),
            },
            "plans": [asdict(plan) for plan in plans],
        }

        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"System command payload migration ({summary['mode']})")
            print(f"db: {summary['db']}")
            print(f"update candidates: {summary['counts']['update']}")
            print(f"skip: {summary['counts']['skip']}")
            print(f"invalid: {summary['counts']['invalid']}")
            if args.apply:
                print(f"updated: {updated}")
            for plan in plans:
                print(f"- [task {plan.task_id}] {plan.action}: {plan.reason}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
