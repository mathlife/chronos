"""Shared monthly-quota completion rules.

The scheduler, service layer, CLI, and integration API must use the same quota
window and completion semantics. This module only mutates occurrence state; the
caller remains responsible for scheduler-job cleanup and committing the DB.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .scheduler import resolve_monthly_quota_window


QUOTA_CYCLE_TYPES = {"monthly_range", "monthly_n_times", "monthly_dates"}


def _value(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index]


def quota_window(task_row: Any, target_day: date) -> tuple[date, date] | None:
    cycle_type = _value(task_row, "cycle_type", 0)
    if cycle_type not in QUOTA_CYCLE_TYPES:
        return None
    return resolve_monthly_quota_window(
        cycle_type=cycle_type,
        target_day=target_day,
        range_start=_value(task_row, "range_start", 3),
        range_end=_value(task_row, "range_end", 4),
    )


def complete_remaining_quota_occurrences(
    db: Any,
    *,
    task_id: int,
    occurrence_date: date,
    task_row: Any,
) -> list[int]:
    """Auto-complete remaining occurrences when a monthly quota is reached.

    Returns occurrence IDs transitioned by this call. The operation is safe to
    call from every completion entrypoint because it only changes pending/
    reminded rows after counting completed rows in the canonical window.
    """
    raw_limit = _value(task_row, "n_per_month", 2)
    try:
        limit = int(raw_limit or 0)
    except (TypeError, ValueError):
        limit = 0
    window = quota_window(task_row, occurrence_date)
    if limit <= 0 or window is None:
        return []
    start_day, end_day = window
    count_row = db.execute(
        """
        SELECT COUNT(1) AS completed_count
        FROM periodic_occurrences
        WHERE task_id = ? AND status = 'completed'
          AND date >= ? AND date <= ?
        """,
        (task_id, start_day.isoformat(), end_day.isoformat()),
    ).fetchone()
    completed_count = int(_value(count_row, "completed_count", 0) or 0)
    if completed_count < limit:
        return []

    # Keep the legacy counter as a derived compatibility field rather than
    # incrementing it at multiple completion entrypoints.
    db.execute(
        "UPDATE periodic_tasks SET count_current_month = ? WHERE id = ?",
        (completed_count, task_id),
    )

    rows = db.execute(
        """
        SELECT id FROM periodic_occurrences
        WHERE task_id = ? AND status IN ('pending', 'reminded')
          AND date >= ? AND date <= ?
        """,
        (task_id, start_day.isoformat(), end_day.isoformat()),
    ).fetchall()
    ids = [int(_value(row, "id", 0)) for row in rows]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        "PRAGMA table_info(periodic_occurrences)"
    ).fetchall()
    columns = {_value(row, "name", 1) for row in rows}
    assignments = ["status = 'completed'", "is_auto_completed = 1", "completion_mode = COALESCE(completion_mode, 'auto_quota')"]
    if "completion_source" in columns:
        assignments.append("completion_source = COALESCE(completion_source, 'quota')")
    if "trigger_label" in columns:
        assignments.append("trigger_label = COALESCE(trigger_label, 'monthly_quota')")
    if "trigger_command" in columns:
        assignments.append("trigger_command = COALESCE(trigger_command, 'complete_periodic_occurrence')")
    db.execute(
        f"""
        UPDATE periodic_occurrences
        SET {', '.join(assignments)}
        WHERE id IN ({placeholders})
        """,
        ids,
    )
    return ids
