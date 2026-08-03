"""Integration-facing API for task and channel management."""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .config import get_raw_config, remove_channel, set_channels, upsert_channel
from .db import DB, clear_task_cache, db_commit
from .models import ALLOWED_CYCLE_TYPES
from .occurrence_state import OccurrenceStateStore, iter_job_refs
from .paths import PYTHON_BIN, SCRIPTS_DIR
from .system_command_runner import build_handler_payload_from_legacy_command
from .system_scheduler import build_job_command, create_once_job, remove_job, supports_system_scheduler
from .timezones import get_shanghai_tz

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
SHANGHAI_TZ = get_shanghai_tz()

TASK_MUTABLE_FIELDS = {
    "quota",

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

    # Canonicalize legacy monthly aliases to a unified internal model:
    # - monthly_fixed -> monthly_dates(single day)
    # - monthly_n_times -> monthly_range(full month) with n_per_month quota
    cycle_type = normalized.get("cycle_type")
    if cycle_type == "monthly_fixed":
        day = normalized.get("day_of_month")
        if day is not None and not normalized.get("dates_list"):
            normalized["dates_list"] = str(int(day))
        normalized["cycle_type"] = "monthly_dates"
    elif cycle_type == "monthly_n_times":
        normalized["cycle_type"] = "monthly_range"
        if normalized.get("range_start") is None:
            normalized["range_start"] = 1
        if normalized.get("range_end") is None:
            normalized["range_end"] = 31

    system_command = normalized.pop("system_command", None)
    if system_command is not None:
        command = str(system_command).strip()
        if command:
            normalized["special_handler"] = "run_command"
            normalized["handler_payload"] = build_handler_payload_from_legacy_command(command)
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
        # Legacy compatibility. New writes are normalized to monthly_dates.
        raise ValueError("monthly_fixed tasks require day_of_month")
    if cycle_type == "monthly_range":
        if task_data.get("range_start") is None or task_data.get("range_end") is None:
            raise ValueError("monthly_range tasks require range_start and range_end")
    if cycle_type == "monthly_n_times" and task_data.get("n_per_month") is None:
        # Legacy compatibility. New writes are normalized to monthly_range + n_per_month.
        raise ValueError("monthly_n_times tasks require n_per_month")
    if cycle_type == "monthly_dates" and not task_data.get("dates_list"):
        raise ValueError("monthly_dates tasks require dates_list")


def _begin_immediate(db: DB) -> None:
    db.execute("BEGIN IMMEDIATE")


def _record_scheduler_operation(db: DB, *, operation_id: str, task_id: int, operation: str, payload: dict) -> None:
    db.execute(
        """
        INSERT INTO scheduler_operation_log (id, operation, task_id, payload, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'planned', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (operation_id, operation, task_id, json.dumps(payload, ensure_ascii=False)),
    )


def _update_scheduler_operation(db: DB, *, operation_id: str, status: str, error: str | None = None, bump_attempt: bool = False) -> None:
    if bump_attempt:
        db.execute(
            """
            UPDATE scheduler_operation_log
            SET status = ?, error = ?, attempt_count = attempt_count + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, error, operation_id),
        )
        return
    db.execute(
        """
        UPDATE scheduler_operation_log
        SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, error, operation_id),
    )


def _normalize_row_dict(row: Any) -> dict:
    return dict(row) if hasattr(row, "keys") else dict(row or {})


def _remove_scheduled_jobs(rows: list[dict]) -> tuple[list[dict], list[str]]:
    removed: list[dict] = []
    warnings: list[str] = []
    scheduler_available = supports_system_scheduler()
    for row in rows:
        row_payload = dict(row)
        for kind, job_name in iter_job_refs(row_payload):
            if not scheduler_available:
                warnings.append(f"skipped removing {kind} job {job_name}: scheduler unavailable")
                continue
            ok = remove_job(job_name)
            if not ok:
                raise RuntimeError(f"failed to remove {kind} job {job_name}")
            removed.append({"kind": kind, "job_name": job_name, "row": row_payload})
    return removed, warnings


def _build_run_at(row: dict, *, kind: str) -> datetime | None:
    date_raw = str(row.get("date") or "").strip()
    time_raw = str(row.get("scheduled_time") or row.get("time_of_day") or "").strip()
    if not date_raw or not time_raw:
        return None
    try:
        occ_date = date.fromisoformat(date_raw)
    except ValueError:
        return None
    match = _TIME_RE.match(time_raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    execute_at = datetime(occ_date.year, occ_date.month, occ_date.day, hour, minute, tzinfo=SHANGHAI_TZ)
    return execute_at - timedelta(minutes=5) if kind == "reminder" else execute_at


def _recreate_removed_jobs(removed_jobs: list[dict]) -> list[str]:
    errors: list[str] = []
    script_path = SCRIPTS_DIR / "periodic_task_manager.py"
    now = datetime.now(SHANGHAI_TZ)
    for item in removed_jobs:
        kind = str(item.get("kind") or "")
        job_name = str(item.get("job_name") or "")
        row = item.get("row") if isinstance(item.get("row"), dict) else {}
        if not job_name:
            continue
        run_at = _build_run_at(row, kind=kind)
        if run_at is None or run_at <= now:
            # Past jobs cannot be restored meaningfully.
            continue
        occurrence_id = row.get("occurrence_id")
        if occurrence_id is None:
            continue
        action = "--fire-reminder" if kind == "reminder" else "--run-system-task"
        command = build_job_command(PYTHON_BIN, script_path, action, int(occurrence_id))
        try:
            create_once_job(job_name=job_name, command=command, run_at=run_at)
        except Exception as exc:  # pragma: no cover - relies on host scheduler
            errors.append(f"{job_name}:{exc}")
    return errors


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


def _sync_today_occurrences_after_create() -> None:
    try:
        from service.periodic_service import PeriodicTaskManager
    except Exception:
        return

    manager = PeriodicTaskManager()
    try:
        manager.ensure_today_occurrences()
    except Exception:
        # Keep task creation successful even if same-day occurrence sync fails.
        return
    finally:
        manager.db.close()


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
    _sync_today_occurrences_after_create()
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

    if "time_of_day" in normalized_patch and "event_time" not in normalized_patch:
        normalized_patch["event_time"] = normalized_patch.get("time_of_day")

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


def _row_to_dict(row: Any) -> dict | None:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def get_occurrence(occurrence_id: int) -> dict | None:
    row = DB().execute(
        """
        SELECT o.*, t.name AS task_name, t.cycle_type AS task_cycle_type, t.time_of_day AS task_time_of_day
        FROM periodic_occurrences o
        LEFT JOIN periodic_tasks t ON t.id = o.task_id
        WHERE o.id = ?
        """,
        (occurrence_id,),
    ).fetchone()
    return _row_to_dict(row)


def _normalize_occurrence_payload(payload: dict | None, *, action: str) -> dict:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("occurrence payload must be an object")
    allowed = {"completion_mode", "special_handler_result", "completion_source", "trigger_label", "trigger_command"}
    unknown = sorted(set(payload.keys()) - allowed)
    if unknown:
        raise ValueError(f"unknown occurrence fields: {', '.join(unknown)}")
    normalized = dict(payload)
    if action == "complete":
        normalized.setdefault("completion_mode", "manual")
        normalized.setdefault("completion_source", "assistant_sync")
    elif action == "skip":
        normalized.setdefault("completion_mode", "skipped")
        normalized.setdefault("completion_source", "assistant_skip")
    return normalized


def _remove_occurrence_jobs_if_possible(occurrence: dict) -> list[str]:
    warnings: list[str] = []
    job_refs = iter_job_refs(occurrence)
    if not job_refs:
        return warnings
    if not supports_system_scheduler():
        warnings.append("scheduler unavailable; cleared DB job pointers only")
        return warnings
    for kind, job_name in job_refs:
        ok = remove_job(job_name)
        if not ok:
            raise RuntimeError(f"failed to remove {kind} job {job_name}")
    return warnings


def _mutate_occurrence_status(occurrence_id: int, payload: dict | None, *, action: str) -> dict:
    current = get_occurrence(occurrence_id)
    if not current:
        raise ValueError(f"occurrence {occurrence_id} not found")
    normalized = _normalize_occurrence_payload(payload, action=action)
    warnings = _remove_occurrence_jobs_if_possible(current)
    store = OccurrenceStateStore(DB())
    if action == "complete":
        changed = store.complete(
            occurrence_id,
            completion_mode=str(normalized["completion_mode"]),
            special_handler_result=normalized.get("special_handler_result"),
            completion_source=normalized.get("completion_source"),
            trigger_label=normalized.get("trigger_label"),
            trigger_command=normalized.get("trigger_command"),
        )
    elif action == "skip":
        changed = store.skip(
            occurrence_id,
            completion_mode=str(normalized["completion_mode"]),
            special_handler_result=normalized.get("special_handler_result"),
            completion_source=normalized.get("completion_source"),
            trigger_label=normalized.get("trigger_label"),
            trigger_command=normalized.get("trigger_command"),
        )
    else:
        raise ValueError(f"unsupported occurrence action: {action}")
    if iter_job_refs(current):
        store.clear_jobs(occurrence_id)
    updated = get_occurrence(occurrence_id)
    if not updated:
        raise RuntimeError(f"occurrence {occurrence_id} not found after update")
    updated["changed"] = bool(changed)
    if warnings:
        updated["warnings"] = warnings
    return updated


def complete_occurrence(occurrence_id: int, payload: dict | None = None) -> dict:
    return _mutate_occurrence_status(occurrence_id, payload, action="complete")


def skip_occurrence(occurrence_id: int, payload: dict | None = None) -> dict:
    return _mutate_occurrence_status(occurrence_id, payload, action="skip")


def remove_task(task_id: int, *, hard: bool = False) -> bool:
    current = get_task(task_id)
    if not current:
        return False
    db = DB()
    operation_id = uuid.uuid4().hex

    occurrence_state = OccurrenceStateStore(db)
    row_payloads = occurrence_state.job_payloads_for_task(task_id)
    occurrence_ids = [int(row["occurrence_id"]) for row in row_payloads]
    operation_payload = {
        "hard": bool(hard),
        "job_count": len(row_payloads),
        "jobs": row_payloads,
    }
    _begin_immediate(db)
    try:
        _record_scheduler_operation(
            db,
            operation_id=operation_id,
            task_id=task_id,
            operation="remove_task_hard" if hard else "remove_task_soft",
            payload=operation_payload,
        )
        db_commit()
    except Exception:
        db.execute("ROLLBACK")
        raise

    removed_jobs: list[dict] = []
    removal_warnings: list[str] = []
    try:
        removed_jobs, removal_warnings = _remove_scheduled_jobs(row_payloads)

        _begin_immediate(db)
        try:
            occurrence_state.clear_jobs_for_ids(occurrence_ids, commit=False)
            if not hard:
                db.execute(
                    """
                    UPDATE periodic_occurrences
                    SET status = CASE WHEN status IN ('pending', 'reminded') THEN 'skipped' ELSE status END
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )

            if hard:
                db.execute("DELETE FROM periodic_tasks WHERE id = ?", (task_id,))
            else:
                db.execute("UPDATE periodic_tasks SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (task_id,))
            final_status = "applied_with_warning" if removal_warnings else "applied"
            warning_text = "; ".join(removal_warnings) if removal_warnings else None
            _update_scheduler_operation(db, operation_id=operation_id, status=final_status, error=warning_text)
            db_commit()
        except Exception as exc:
            db.execute("ROLLBACK")
            raise RuntimeError(f"failed to update database state: {exc}") from exc
    except Exception as exc:
        compensation_errors = _recreate_removed_jobs(removed_jobs)
        error_message = str(exc)
        if compensation_errors:
            error_message = f"{error_message}; compensation_failed={'; '.join(compensation_errors)}"
        _begin_immediate(db)
        try:
            _update_scheduler_operation(
                db,
                operation_id=operation_id,
                status="failed",
                error=error_message,
                bump_attempt=True,
            )
            db_commit()
        except Exception:
            db.execute("ROLLBACK")
        raise RuntimeError(error_message) from exc

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
