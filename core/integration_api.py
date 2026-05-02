"""Integration-facing API for task and channel management."""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from .config import get_raw_config, remove_channel, set_channels, upsert_channel
from .db import DB, clear_task_cache, db_commit
from .models import ALLOWED_CYCLE_TYPES
from .system_scheduler import remove_job

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

TASK_MUTABLE_FIELDS = {
    "name",
    "category",
    "cycle_type",
    "weekday",
    "day_of_month",
    "range_start",
    "range_end",
    "n_per_month",
    "interval_hours",
    "time_of_day",
    "end_date",
    "start_date",
    "reminder_template",
    "task_kind",
    "source",
    "legacy_entry_id",
    "special_handler",
    "handler_payload",
    "delivery_target",
    "delivery_mode",
    "dates_list",
    "is_active",
}


def _parse_iso_date(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
    return raw


def _normalize_time_of_day(value: Any) -> str:
    if value is None:
        return "09:00"
    match = _TIME_RE.match(str(value).strip())
    if not match:
        raise ValueError("time_of_day must be HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time_of_day must be HH:MM (00:00-23:59)")
    return f"{hour:02d}:{minute:02d}"


def _normalize_dates_list(value: Any) -> str | None:
    if value is None:
        return None
    chunks = [chunk.strip() for chunk in str(value).split(",") if chunk.strip()]
    if not chunks:
        return None
    parsed: list[int] = []
    for chunk in chunks:
        try:
            day = int(chunk)
        except ValueError as exc:
            raise ValueError("dates_list must contain comma-separated day numbers") from exc
        if day < 1 or day > 31:
            raise ValueError("dates_list day must be 1-31")
        parsed.append(day)
    return ",".join(str(day) for day in sorted(set(parsed)))


def _normalize_task_payload(payload: dict, *, partial: bool) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("task payload must be an object")
    unknown = sorted(set(payload.keys()) - TASK_MUTABLE_FIELDS - {"system_command"})
    if unknown:
        raise ValueError(f"unknown task fields: {', '.join(unknown)}")

    normalized = dict(payload)
    if "cycle_type" in normalized:
        cycle_type = str(normalized.get("cycle_type") or "").strip()
        if cycle_type not in ALLOWED_CYCLE_TYPES:
            raise ValueError(f"cycle_type must be one of: {', '.join(ALLOWED_CYCLE_TYPES)}")
        normalized["cycle_type"] = cycle_type

    if "time_of_day" in normalized:
        normalized["time_of_day"] = _normalize_time_of_day(normalized.get("time_of_day"))
    elif not partial:
        normalized["time_of_day"] = "09:00"

    if "weekday" in normalized and normalized["weekday"] is not None:
        weekday = int(normalized["weekday"])
        if weekday < 0 or weekday > 6:
            raise ValueError("weekday must be 0-6 (Mon=0)")
        normalized["weekday"] = weekday
    if "day_of_month" in normalized and normalized["day_of_month"] is not None:
        day_of_month = int(normalized["day_of_month"])
        if day_of_month < 1 or day_of_month > 31:
            raise ValueError("day_of_month must be 1-31")
        normalized["day_of_month"] = day_of_month
    if "range_start" in normalized and normalized["range_start"] is not None:
        range_start = int(normalized["range_start"])
        if range_start < 1 or range_start > 31:
            raise ValueError("range_start must be 1-31")
        normalized["range_start"] = range_start
    if "range_end" in normalized and normalized["range_end"] is not None:
        range_end = int(normalized["range_end"])
        if range_end < 1 or range_end > 31:
            raise ValueError("range_end must be 1-31")
        normalized["range_end"] = range_end
    if "n_per_month" in normalized and normalized["n_per_month"] is not None:
        n_per_month = int(normalized["n_per_month"])
        if n_per_month <= 0:
            raise ValueError("n_per_month must be > 0")
        normalized["n_per_month"] = n_per_month
    if "interval_hours" in normalized and normalized["interval_hours"] is not None:
        interval_hours = int(normalized["interval_hours"])
        if interval_hours <= 0 or interval_hours > 24:
            raise ValueError("interval_hours must be 1-24")
        normalized["interval_hours"] = interval_hours
    if "start_date" in normalized:
        normalized["start_date"] = _parse_iso_date(normalized.get("start_date"), "start_date")
    if "end_date" in normalized:
        normalized["end_date"] = _parse_iso_date(normalized.get("end_date"), "end_date")
    if "dates_list" in normalized:
        normalized["dates_list"] = _normalize_dates_list(normalized.get("dates_list"))
    if "is_active" in normalized and normalized["is_active"] is not None:
        normalized["is_active"] = 1 if bool(normalized["is_active"]) else 0

    system_command = normalized.pop("system_command", None)
    if system_command is not None:
        command = str(system_command).strip()
        if command:
            normalized["special_handler"] = "run_command"
            normalized["handler_payload"] = json.dumps({"command": command}, ensure_ascii=False)
        else:
            normalized["special_handler"] = None
            normalized["handler_payload"] = None

    return normalized


def _validate_cycle_requirements(task_data: dict) -> None:
    cycle_type = task_data.get("cycle_type")
    if cycle_type == "once" and not task_data.get("start_date"):
        raise ValueError("once tasks require start_date")
    if cycle_type == "hourly" and not task_data.get("interval_hours"):
        raise ValueError("hourly tasks require interval_hours")
    if cycle_type == "weekly" and task_data.get("weekday") is None:
        raise ValueError("weekly tasks require weekday")
    if cycle_type == "monthly_fixed" and task_data.get("day_of_month") is None:
        raise ValueError("monthly_fixed tasks require day_of_month")
    if cycle_type == "monthly_range":
        if task_data.get("range_start") is None or task_data.get("range_end") is None:
            raise ValueError("monthly_range tasks require range_start and range_end")
    if cycle_type == "monthly_n_times" and task_data.get("n_per_month") is None:
        raise ValueError("monthly_n_times tasks require n_per_month")
    if cycle_type == "monthly_dates" and not task_data.get("dates_list"):
        raise ValueError("monthly_dates tasks require dates_list")


def list_tasks(*, active_only: bool | None = None) -> list[dict]:
    db = DB()
    query = "SELECT * FROM periodic_tasks"
    params: tuple[Any, ...] = ()
    if active_only is True:
        query += " WHERE is_active = 1"
    elif active_only is False:
        query += " WHERE is_active = 0"
    query += " ORDER BY id"
    rows = db.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_task(task_id: int) -> dict | None:
    row = DB().execute("SELECT * FROM periodic_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def create_task(payload: dict) -> dict:
    normalized = _normalize_task_payload(payload, partial=False)
    name = str(normalized.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    normalized["name"] = name
    normalized.setdefault("category", "Inbox")
    normalized.setdefault("cycle_type", "once")
    normalized.setdefault("task_kind", "scheduled")
    normalized.setdefault("source", "integration_api")
    normalized.setdefault("is_active", 1)
    _validate_cycle_requirements(normalized)

    db = DB()
    cur = db.execute(
        """
        INSERT INTO periodic_tasks
        (name, category, cycle_type, weekday, day_of_month, range_start, range_end, n_per_month,
         interval_hours, time_of_day, event_time, timezone, is_active, count_current_month, end_date,
         reminder_template, dates_list, task_kind, source, legacy_entry_id, special_handler, handler_payload,
         start_date, delivery_target, delivery_mode, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            normalized["name"],
            normalized.get("category", "Inbox"),
            normalized.get("cycle_type", "once"),
            normalized.get("weekday"),
            normalized.get("day_of_month"),
            normalized.get("range_start"),
            normalized.get("range_end"),
            normalized.get("n_per_month"),
            normalized.get("interval_hours"),
            normalized.get("time_of_day", "09:00"),
            normalized.get("time_of_day", "09:00"),
            normalized.get("is_active", 1),
            normalized.get("end_date"),
            normalized.get("reminder_template"),
            normalized.get("dates_list"),
            normalized.get("task_kind", "scheduled"),
            normalized.get("source", "integration_api"),
            normalized.get("legacy_entry_id"),
            normalized.get("special_handler"),
            normalized.get("handler_payload"),
            normalized.get("start_date"),
            normalized.get("delivery_target"),
            normalized.get("delivery_mode"),
        ),
    )
    db_commit()
    clear_task_cache()
    created = get_task(cur.lastrowid)
    if not created:
        raise RuntimeError("failed to load created task")
    return created


def update_task(task_id: int, patch: dict) -> dict:
    current = get_task(task_id)
    if not current:
        raise ValueError(f"task {task_id} not found")
    normalized_patch = _normalize_task_payload(patch, partial=True)
    if not normalized_patch:
        return current

    merged = dict(current)
    merged.update(normalized_patch)
    _validate_cycle_requirements(merged)

    assignments: list[str] = []
    params: list[Any] = []
    for key, value in normalized_patch.items():
        assignments.append(f"{key} = ?")
        params.append(value)
    assignments.append("updated_at = CURRENT_TIMESTAMP")
    params.append(task_id)
    DB().execute(
        f"UPDATE periodic_tasks SET {', '.join(assignments)} WHERE id = ?",
        tuple(params),
    )
    db_commit()
    clear_task_cache()
    updated = get_task(task_id)
    if not updated:
        raise RuntimeError(f"task {task_id} not found after update")
    return updated


def remove_task(task_id: int, *, hard: bool = False) -> bool:
    current = get_task(task_id)
    if not current:
        return False
    db = DB()

    rows = db.execute(
        """
        SELECT reminder_job_id, execution_job_id
        FROM periodic_occurrences
        WHERE task_id = ? AND (reminder_job_id IS NOT NULL OR execution_job_id IS NOT NULL)
        """,
        (task_id,),
    ).fetchall()
    for row in rows:
        if row["reminder_job_id"]:
            remove_job(row["reminder_job_id"])
        if row["execution_job_id"]:
            remove_job(row["execution_job_id"])
    if hard:
        db.execute(
            "UPDATE periodic_occurrences SET reminder_job_id = NULL, execution_job_id = NULL WHERE task_id = ?",
            (task_id,),
        )
    else:
        db.execute(
            """
            UPDATE periodic_occurrences
            SET reminder_job_id = NULL,
                execution_job_id = NULL,
                status = CASE WHEN status IN ('pending', 'reminded') THEN 'skipped' ELSE status END
            WHERE task_id = ?
            """,
            (task_id,),
        )

    if hard:
        db.execute("DELETE FROM periodic_tasks WHERE id = ?", (task_id,))
    else:
        db.execute("UPDATE periodic_tasks SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (task_id,))
    db_commit()
    clear_task_cache()
    return True


def list_channels() -> list[dict]:
    channels = get_raw_config().get("channels")
    if not isinstance(channels, list):
        return []
    return [dict(channel) for channel in channels if isinstance(channel, dict)]


def replace_channels(channels: list[dict]) -> list[dict]:
    return set_channels(channels)


def put_channel(channel: dict) -> dict:
    return upsert_channel(channel)


def delete_channel(channel_id: str) -> bool:
    return remove_channel(channel_id)
