"""Periodic task domain service for scheduling and execution."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from core.config import get_config
from core.db import DB, clear_task_cache, db_commit, get_periodic_task, get_periodic_tasks
from core.learning import LearningContext
from core.models import PeriodicTask
from core.notifiers import dispatch_message
from core.observability import METRICS, emit_log
from core.paths import PYTHON_BIN, SCRIPTS_DIR
from core.scheduler import TaskScheduler, resolve_monthly_quota_window, to_shanghai_date
from core.system_command_runner import execute_system_handler
from core.system_scheduler import build_job_command, build_job_name, create_once_job, remove_job, supports_system_scheduler
from core.timezones import get_shanghai_tz

SHANGHAI_TZ = get_shanghai_tz()


class PeriodicTaskManager:
    """Manages periodic tasks: scheduling, completion, cleanup."""

    def __init__(self):
        self.db = DB()

    def add_activity(self, **params) -> int:
        """Add a new periodic task."""
        with LearningContext("add_activity", f"Add task: {params.get('name')} ({params.get('cycle_type')})", confidence="H"):
            cur = self.db.execute(
                """
                INSERT INTO periodic_tasks
                (name, category, cycle_type, weekday, day_of_month, range_start, range_end, n_per_month,
                 interval_hours, time_of_day, event_time, timezone, is_active, count_current_month, end_date, reminder_template,
                 dates_list, task_kind, source, legacy_entry_id, special_handler, handler_payload, start_date,
                 delivery_target, delivery_mode, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    params.get('name'),
                    params.get('category', 'Inbox'),
                    params.get('cycle_type', 'once'),
                    params.get('weekday'),
                    params.get('day_of_month'),
                    params.get('range_start'),
                    params.get('range_end'),
                    params.get('n_per_month'),
                    params.get('interval_hours'),
                    params.get('time_of_day', '09:00'),
                    params.get('time_of_day', '09:00'),
                    params.get('end_date'),
                    params.get('reminder_template'),
                    params.get('dates_list'),
                    params.get('task_kind', 'scheduled'),
                    params.get('source', 'chronos'),
                    params.get('legacy_entry_id'),
                    params.get('special_handler'),
                    params.get('handler_payload'),
                    params.get('start_date'),
                    params.get('delivery_target'),
                    params.get('delivery_mode'),
                ),
            )
            db_commit()
            clear_task_cache()
            activity_id = cur.lastrowid
            return activity_id

    def reset_monthly_counters(self, today: date):
        if today.day == 1:
            with LearningContext("reset_monthly_counters", f"Reset monthly counters for {today.strftime('%Y-%m')}", confidence="H"):
                self.db.execute(
                    """
                    UPDATE periodic_tasks
                    SET count_current_month = 0
                    WHERE cycle_type = 'monthly_n_times' AND is_active = 1
                    """
                )
                db_commit()
                clear_task_cache()

    @staticmethod
    def _is_monthly_quota_task(task_row: dict | sqlite3.Row | tuple | None) -> bool:  # type: ignore[name-defined]
        if not task_row:
            return False
        cycle_type = (task_row["cycle_type"] if hasattr(task_row, "keys") else task_row[0]) or ""
        n_per_month = task_row["n_per_month"] if hasattr(task_row, "keys") else task_row[1]
        try:
            quota = int(n_per_month) if n_per_month is not None else 0
        except (TypeError, ValueError):
            quota = 0
        return cycle_type in ("monthly_n_times", "monthly_range") and quota > 0

    @staticmethod
    def _quota_window_for_day(task_row: dict | sqlite3.Row | tuple, target_day: date) -> tuple[date, date] | None:  # type: ignore[name-defined]
        cycle_type = (task_row["cycle_type"] if hasattr(task_row, "keys") else task_row[0]) or ""
        range_start = task_row["range_start"] if hasattr(task_row, "keys") else task_row[3]
        range_end = task_row["range_end"] if hasattr(task_row, "keys") else task_row[4]
        return resolve_monthly_quota_window(
            cycle_type=str(cycle_type),
            target_day=target_day,
            range_start=range_start,
            range_end=range_end,
        )

    def _monthly_quota_reached(self, task: PeriodicTask, target_day: date) -> bool:
        if not task.n_per_month or task.n_per_month <= 0:
            return False
        window = resolve_monthly_quota_window(
            cycle_type=task.cycle_type,
            target_day=target_day,
            range_start=task.range_start,
            range_end=task.range_end,
        )
        if not window:
            return False
        start_day, end_day = window
        row = self.db.execute(
            """
            SELECT COUNT(1)
            FROM periodic_occurrences
            WHERE task_id = ? AND status = 'completed'
              AND date >= ? AND date <= ?
            """,
            (task.id, start_day.isoformat(), end_day.isoformat()),
        ).fetchone()
        completed_count = int(row[0] if row and row[0] is not None else 0)
        return completed_count >= int(task.n_per_month)

    def _apply_monthly_quota_completion(self, *, task_id: int, occurrence_date: date, task_row: sqlite3.Row) -> None:  # type: ignore[name-defined]
        if not self._is_monthly_quota_task(task_row):
            return
        n_per_month = int(task_row["n_per_month"])
        window = self._quota_window_for_day(task_row, occurrence_date)
        if not window:
            return
        start_day, end_day = window
        count_row = self.db.execute(
            """
            SELECT COUNT(1)
            FROM periodic_occurrences
            WHERE task_id = ? AND status = 'completed'
              AND date >= ? AND date <= ?
            """,
            (task_id, start_day.isoformat(), end_day.isoformat()),
        ).fetchone()
        completed_count = int(count_row[0] if count_row and count_row[0] is not None else 0)
        if completed_count < n_per_month:
            return

        self.db.execute(
            """
            UPDATE periodic_occurrences
            SET status = 'completed', is_auto_completed = 1,
                completion_mode = COALESCE(completion_mode, 'auto_quota')
            WHERE task_id = ? AND status IN ('pending', 'reminded')
              AND date >= ? AND date <= ?
            """,
            (task_id, start_day.isoformat(), end_day.isoformat()),
        )
        cleanup_rows = self.db.execute(
            """
            SELECT reminder_job_id, execution_job_id
            FROM periodic_occurrences
            WHERE task_id = ? AND status = 'completed'
              AND date >= ? AND date <= ?
              AND (reminder_job_id IS NOT NULL OR execution_job_id IS NOT NULL)
            """,
            (task_id, start_day.isoformat(), end_day.isoformat()),
        ).fetchall()
        for reminder_job_name, execution_job_name in cleanup_rows:
            if reminder_job_name:
                remove_job(reminder_job_name)
            if execution_job_name:
                remove_job(execution_job_name)
        self.db.execute(
            """
            UPDATE periodic_occurrences
            SET reminder_job_id = NULL, execution_job_id = NULL
            WHERE task_id = ? AND status = 'completed'
              AND date >= ? AND date <= ?
            """,
            (task_id, start_day.isoformat(), end_day.isoformat()),
        )

    def create_occurrence_if_missing(self, task_id: int, occ_date: date, scheduled_time: str | None = None) -> int:
        task = get_periodic_task(task_id)
        scheduled_time = scheduled_time or (task.get('time_of_day') if task else None)
        scheduled_at = None
        if scheduled_time:
            scheduled_at = f"{occ_date.isoformat()}T{scheduled_time}:00"
        self.db.execute(
            """
            INSERT OR IGNORE INTO periodic_occurrences (task_id, date, status, scheduled_time, scheduled_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (task_id, occ_date.isoformat(), scheduled_time, scheduled_at),
        )
        db_commit()
        cur = self.db.execute(
            "SELECT id FROM periodic_occurrences WHERE task_id = ? AND date = ? AND COALESCE(scheduled_time, '') = COALESCE(?, '')",
            (task_id, occ_date.isoformat(), scheduled_time),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur = self.db.execute("SELECT id FROM periodic_occurrences WHERE task_id = ? AND date = ?", (task_id, occ_date.isoformat()))
        row = cur.fetchone()
        return row[0] if row else None

    def _get_occurrence_row(self, occurrence_id: int):
        return self.db.execute(
            """
            SELECT o.id, o.task_id, o.date, o.status, o.reminder_job_id, o.execution_job_id, o.scheduled_time,
                   t.name, t.task_kind, t.time_of_day, t.reminder_template, t.special_handler, t.handler_payload,
                   t.delivery_target
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON t.id = o.task_id
            WHERE o.id = ?
            """,
            (occurrence_id,),
        ).fetchone()

    def _send_message_now(self, message_text: str, *, task: Optional[dict] = None, occurrence_id: Optional[int] = None) -> bool:
        try:
            config = get_config()
        except ValueError as exc:
            METRICS.inc("notify_config_error_total")
            emit_log("notify.config_invalid", level="ERROR", error=str(exc))
            return False

        targets = None
        delivery_target = (task or {}).get("delivery_target")
        if isinstance(delivery_target, str) and delivery_target.strip():
            targets = [chunk.strip() for chunk in delivery_target.split(",") if chunk.strip()]

        meta = {
            "source": "chronos",
            "occurrence_id": occurrence_id,
            "task_id": (task or {}).get("id"),
            "task_name": (task or {}).get("name"),
            "task_kind": (task or {}).get("task_kind"),
        }
        results = dispatch_message(config=config, message=message_text, meta=meta, target_ids=targets)
        if not results:
            METRICS.inc("notify_no_channel_total")
            emit_log("notify.no_channel", level="WARNING")
            return False
        failures = [r for r in results if not r.ok]
        if failures:
            METRICS.inc("notify_partial_failure_total")
            emit_log("notify.partial_failure", level="WARNING", failure_count=len(failures))
            for f in failures:
                emit_log(
                    "notify.channel_failure",
                    level="WARNING",
                    channel_id=f.channel_id,
                    channel_type=f.channel_type,
                    error=f.error,
                )
        success = any(r.ok for r in results)
        METRICS.inc("notify_success_total" if success else "notify_failure_total")
        return success

    def _mark_occurrence_reminded(self, occurrence_id: int, reminder_job_id: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE periodic_occurrences
            SET status = CASE WHEN status = 'pending' THEN 'reminded' ELSE status END,
                reminder_job_id = COALESCE(?, reminder_job_id)
            WHERE id = ?
            """,
            (reminder_job_id, occurrence_id),
        )
        db_commit()

    def _complete_occurrence_internal(
        self,
        occurrence_id: int,
        *,
        completion_mode: str,
        special_handler_result: str | None = None,
    ) -> bool:
        cur = self.db.execute(
            """
            UPDATE periodic_occurrences
            SET status = 'completed',
                completed_at = CURRENT_TIMESTAMP,
                completion_mode = ?,
                special_handler_result = COALESCE(?, special_handler_result)
            WHERE id = ? AND status != 'completed'
            """,
            (completion_mode, special_handler_result, occurrence_id),
        )
        if cur.rowcount <= 0:
            return False
        db_commit()
        cur = self.db.execute("SELECT task_id FROM periodic_occurrences WHERE id = ?", (occurrence_id,))
        row = cur.fetchone()
        if row:
            task_id = row[0]
            cycle_type_row = self.db.execute(
                "SELECT cycle_type, n_per_month, count_current_month, range_start, range_end FROM periodic_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if cycle_type_row and cycle_type_row[0] == 'monthly_n_times':
                self.db.execute("UPDATE periodic_tasks SET count_current_month = count_current_month + 1 WHERE id = ?", (task_id,))
            occ_row = self.db.execute("SELECT date FROM periodic_occurrences WHERE id = ?", (occurrence_id,)).fetchone()
            if occ_row and occ_row[0]:
                try:
                    occ_day = date.fromisoformat(str(occ_row[0]))
                except ValueError:
                    occ_day = to_shanghai_date()
            else:
                occ_day = to_shanghai_date()
            self._apply_monthly_quota_completion(task_id=task_id, occurrence_date=occ_day, task_row=cycle_type_row)
            db_commit()
        return True

    def _skip_occurrence_internal(
        self,
        occurrence_id: int,
        *,
        completion_mode: str,
        special_handler_result: str | None = None,
    ) -> bool:
        cur = self.db.execute(
            """
            UPDATE periodic_occurrences
            SET status = 'skipped',
                completed_at = CURRENT_TIMESTAMP,
                completion_mode = ?,
                special_handler_result = COALESCE(?, special_handler_result),
                reminder_job_id = NULL,
                execution_job_id = NULL
            WHERE id = ? AND status NOT IN ('completed', 'skipped')
            """,
            (completion_mode, special_handler_result, occurrence_id),
        )
        if cur.rowcount <= 0:
            return False
        db_commit()
        return True

    def _schedule_system_occurrence_jobs(self, occurrence_id: int, occ_date: date, time_of_day: str) -> tuple[Optional[str], Optional[str]]:
        if not supports_system_scheduler():
            raise RuntimeError("system scheduler is not supported on this platform")
        hour, minute = map(int, time_of_day.split(':'))
        execute_at = datetime(occ_date.year, occ_date.month, occ_date.day, hour, minute, tzinfo=SHANGHAI_TZ)
        # System tasks should run at due time only; no pre-reminder cron.
        reminder_job_name = None
        execute_job_name = build_job_name("execute", occurrence_id)
        script_path = SCRIPTS_DIR / "periodic_task_manager.py"

        execute_command = build_job_command(PYTHON_BIN, script_path, "--run-system-task", occurrence_id)
        create_once_job(job_name=execute_job_name, command=execute_command, run_at=execute_at)
        return reminder_job_name, execute_job_name

    def fire_reminder_occurrence(self, occurrence_id: int) -> bool:
        row = self._get_occurrence_row(occurrence_id)
        if not row or row["status"] in ("completed", "skipped"):
            return False
        occ_date = date.fromisoformat(row["date"])
        time_of_day = row["scheduled_time"] or row["time_of_day"]
        if not time_of_day:
            return False
        message_text = self._format_reminder_message(row["name"], occ_date, time_of_day, row["reminder_template"], immediate=False)
        task_dict = {"id": row["task_id"], "name": row["name"], "task_kind": row["task_kind"], "delivery_target": row["delivery_target"]}
        if not self._send_message_now(message_text, task=task_dict, occurrence_id=occurrence_id):
            return False
        self._mark_occurrence_reminded(occurrence_id, reminder_job_id=row["reminder_job_id"])
        return True

    def run_system_occurrence(self, occurrence_id: int) -> bool:
        row = self._get_occurrence_row(occurrence_id)
        if not row or row["status"] in ("completed", "skipped"):
            return False
        if row["task_kind"] != "system":
            return False

        result_message = None
        blocked_by_policy = False
        if row["special_handler"] == "run_command":
            METRICS.inc("system_command_attempt_total")
            try:
                execution = execute_system_handler(row["handler_payload"], timeout_seconds=600)
                output_text = str(execution.get("output") or "").strip()
                parsed_output = None
                if output_text:
                    try:
                        parsed_output = json.loads(output_text)
                    except Exception:
                        parsed_output = None

                if execution.get("ok"):
                    METRICS.inc("system_command_success_total")
                    if isinstance(parsed_output, dict):
                        imported_count = parsed_output.get("imported_count")
                        failed_count = parsed_output.get("failed_count")
                        pending = parsed_output.get("pending")
                        scanned = parsed_output.get("scanned")
                        imported_files = parsed_output.get("imported_files") or []
                        failed_files = parsed_output.get("failed") or []
                        lines = [
                            "执行成功",
                            f"- 命令ID：{execution.get('command_id')}",
                            f"- 扫描：{scanned}",
                            f"- 待处理：{pending}",
                            f"- 导入成功：{imported_count}",
                            f"- 导入失败：{failed_count}",
                        ]
                        if imported_files:
                            names = ", ".join(Path(str(f)).stem for f in imported_files[:5])
                            extra = f"（等 {len(imported_files)} 条）" if len(imported_files) > 5 else ""
                            lines.append(f"- 导入文件：{names}{extra}")
                        if failed_files:
                            names = ", ".join(str(f) for f in failed_files[:5])
                            lines.append(f"- 失败文件：{names}")
                        result_message = "\n".join(lines)
                    else:
                        result_message = (
                            f"执行成功\n"
                            f"- 命令ID：{execution.get('command_id')}\n"
                            f"- 退出码：{execution.get('exit_code')}"
                        )
                else:
                    METRICS.inc("system_command_failure_total")
                    result_message = (
                        f"执行失败\n"
                        f"- 命令ID：{execution.get('command_id')}\n"
                        f"- 退出码：{execution.get('exit_code')}\n"
                        f"- 错误输出：{output_text or '无'}"
                    )
                emit_log(
                    "system_command.executed",
                    occurrence_id=occurrence_id,
                    command_id=execution.get("command_id"),
                    exit_code=execution.get("exit_code"),
                    ok=bool(execution.get("ok")),
                )
            except ValueError as exc:
                METRICS.inc("system_command_blocked_total")
                result_message = f"blocked={exc}"
                blocked_by_policy = True
                emit_log("system_command.blocked", level="WARNING", occurrence_id=occurrence_id, error=str(exc))
            except Exception as exc:
                METRICS.inc("system_command_error_total")
                result_message = f"error={exc}"
                emit_log("system_command.error", level="ERROR", occurrence_id=occurrence_id, error=str(exc))
        else:
            result_message = f"system occurrence reached due time for task {row['name']}"

        task_dict = {
            "id": row["task_id"],
            "name": row["name"],
            "task_kind": row["task_kind"],
            "delivery_target": row["delivery_target"],
        }

        if blocked_by_policy:
            self._skip_occurrence_internal(
                occurrence_id,
                completion_mode="blocked_policy",
                special_handler_result=result_message,
            )
            message_text = f"⚠️ 系统任务阻止执行\n任务：{row['name']}\n结果：{result_message or 'blocked'}"
            self._send_message_now(message_text, task=task_dict, occurrence_id=occurrence_id)
            return True

        self._complete_occurrence_internal(
            occurrence_id,
            completion_mode="system_scheduler",
            special_handler_result=result_message,
        )
        self.db.execute(
            "UPDATE periodic_occurrences SET reminder_job_id = NULL, execution_job_id = NULL WHERE id = ?",
            (occurrence_id,),
        )
        db_commit()

        message_text = f"✅ 系统任务已执行\n任务：{row['name']}\n结果：{result_message or 'ok'}"
        self._send_message_now(message_text, task=task_dict, occurrence_id=occurrence_id)
        return True

    def schedule_reminder_job(self, occurrence_id: int, occ_date: date, time_of_day: str) -> Optional[str]:
        if not supports_system_scheduler():
            METRICS.inc("scheduler_unavailable_total")
            emit_log("scheduler.unavailable", level="WARNING", reason="crontab not available")
            return None

        hour, minute = map(int, time_of_day.split(':'))
        run_at = datetime(occ_date.year, occ_date.month, occ_date.day, hour, minute, tzinfo=SHANGHAI_TZ) - timedelta(minutes=5)
        if run_at <= datetime.now(SHANGHAI_TZ):
            # Too late to schedule; rely on complete-overdue or manual firing.
            return None

        job_name = build_job_name("reminder", occurrence_id)
        script_path = SCRIPTS_DIR / "periodic_task_manager.py"
        command = build_job_command(PYTHON_BIN, script_path, "--fire-reminder", occurrence_id)
        create_once_job(job_name=job_name, command=command, run_at=run_at)
        METRICS.inc("scheduler_job_created_total")
        return job_name

    def generate_reminders_for_today(self) -> int:
        today = to_shanghai_date()
        self.reset_monthly_counters(today)

        scheduled = 0
        tasks = get_periodic_tasks(active_only=True)

        for task_dict in tasks:
            task = PeriodicTask(**task_dict)
            scheduler = TaskScheduler(task, today)

            if not scheduler.should_remind_today():
                continue

            schedule_times = scheduler.get_hourly_schedule_for_day(today) if task.cycle_type == 'hourly' else [task.time_of_day]
            for schedule_time in schedule_times:
                if not schedule_time:
                    continue
                occ_id = self.create_occurrence_if_missing(task.id, today, scheduled_time=schedule_time)
                if not occ_id:
                    continue

                cur = self.db.execute("SELECT status, reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
                status, job_name, execution_job_id = cur.fetchone()
                if status not in ('pending', 'reminded'):
                    continue
                if task.cycle_type == 'once' and task.start_date and task.start_date != today.isoformat():
                    continue
                if getattr(task, 'task_kind', 'scheduled') == 'system':
                    if not execution_job_id and schedule_time:
                        try:
                            reminder_job_name, execution_job_name = self._schedule_system_occurrence_jobs(occ_id, today, schedule_time)
                        except Exception as exc:
                            METRICS.inc("scheduler_system_job_error_total")
                            emit_log("scheduler.system_job_schedule_failed", level="ERROR", occurrence_id=occ_id, error=str(exc))
                            continue
                        self.db.execute(
                            "UPDATE periodic_occurrences SET reminder_job_id = COALESCE(?, reminder_job_id), execution_job_id = ? WHERE id = ?",
                            (reminder_job_name, execution_job_name, occ_id),
                        )
                        db_commit()
                        METRICS.inc("scheduler_job_created_total")
                        scheduled += 1
                    continue
                if not job_name:
                    job_name = self.schedule_reminder_job(occ_id, today, schedule_time)
                    if job_name:
                        self.db.execute("UPDATE periodic_occurrences SET reminder_job_id = ? WHERE id = ?", (job_name, occ_id))
                        db_commit()
                        scheduled += 1

        return scheduled

    def cleanup_old_jobs(self, before_date: date) -> int:
        cur = self.db.execute(
            """
            SELECT o.id, o.reminder_job_id, o.execution_job_id, t.task_kind
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON t.id = o.task_id
            WHERE o.date <= ?
              AND (o.reminder_job_id IS NOT NULL OR o.execution_job_id IS NOT NULL)
            """,
            (before_date.isoformat(),),
        )
        jobs = cur.fetchall()

        cleaned = 0
        for occ_id, reminder_job_name, execution_job_name, task_kind in jobs:
            reminder_removed = False
            execution_removed = False
            try:
                reminder_removed = remove_job(reminder_job_name)
                execution_removed = remove_job(execution_job_name)
                if reminder_removed or execution_removed:
                    self.db.execute(
                        "UPDATE periodic_occurrences SET reminder_job_id = NULL, execution_job_id = NULL WHERE id = ?",
                        (occ_id,),
                    )
                    cleaned += 1
            except Exception as e:
                METRICS.inc("scheduler_job_cleanup_error_total")
                emit_log("scheduler.cleanup_failed", level="ERROR", occurrence_id=occ_id, error=str(e))

        db_commit()
        if cleaned:
            METRICS.inc("scheduler_job_removed_total", cleaned)
        return cleaned

    def complete_occurrence(self, occurrence_id: int) -> bool:
        with LearningContext("complete_occurrence", f"Complete occurrence {occurrence_id}", confidence="H"):
            return self._complete_occurrence_internal(occurrence_id, completion_mode='manual')

    def complete_activity_cycle(self, task_id: int, as_of: Optional[date] = None) -> int:
        with LearningContext("complete_activity_cycle", f"Complete all pending for task {task_id} up to today", confidence="H"):
            today = to_shanghai_date(as_of)
            task_dict = get_periodic_task(task_id)
            if not task_dict:
                return 0
            task = PeriodicTask(**task_dict)
            affected = 0

            cur = self.db.execute(
                """
                SELECT id FROM periodic_occurrences
                WHERE task_id = ? AND status = 'pending'
                  AND date <= ?
                  AND strftime('%Y-%m', date) = ?
                """,
                (task_id, today.isoformat(), today.strftime('%Y-%m')),
            )
            pending_ids = [row[0] for row in cur.fetchall()]

            for occ_id in pending_ids:
                self.complete_occurrence(occ_id)
                affected += 1

            updated_task_dict = get_periodic_task(task_id) or {}
            if updated_task_dict:
                updated_task = PeriodicTask(**updated_task_dict)
                if self._monthly_quota_reached(updated_task, today):
                    task_row = self.db.execute(
                        "SELECT cycle_type, n_per_month, count_current_month, range_start, range_end FROM periodic_tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                    if task_row:
                        self._apply_monthly_quota_completion(task_id=task_id, occurrence_date=today, task_row=task_row)
                        db_commit()

            cur = self.db.execute(
                """
                SELECT reminder_job_id, execution_job_id, COALESCE(t.task_kind, 'scheduled')
                FROM periodic_occurrences o
                JOIN periodic_tasks t ON t.id = o.task_id
                WHERE o.task_id = ? AND (o.reminder_job_id IS NOT NULL OR o.execution_job_id IS NOT NULL)
                """,
                (task_id,),
            )
            jobs = cur.fetchall()
            for reminder_job_name, execution_job_name, task_kind in jobs:
                try:
                    remove_job(reminder_job_name)
                    remove_job(execution_job_name)
                except Exception:
                    pass
            self.db.execute(
                "UPDATE periodic_occurrences SET reminder_job_id = NULL, execution_job_id = NULL WHERE task_id = ?",
                (task_id,),
            )
            db_commit()

            return affected

    def _format_reminder_message(self, task_name: str, occ_date: date, time_of_day: str, reminder_template: Optional[str], immediate: bool) -> str:
        if not reminder_template:
            if immediate:
                return f"⏰ 周期任务提醒（补发）：{task_name} 已到时间（{occ_date} {time_of_day}）"
            return f"⏰ 周期任务提醒（提前5分钟）：{task_name} 即将开始"

        template_vars = {
            "task_name": task_name,
            "name": task_name,
            "date": occ_date.isoformat(),
            "time": time_of_day,
            "when": "immediate" if immediate else "scheduled",
        }
        try:
            return reminder_template.format_map(template_vars)
        except KeyError:
            return reminder_template

    def ensure_today_occurrences(self) -> int:
        today = to_shanghai_date()
        self.reset_monthly_counters(today)

        count = 0
        tasks = get_periodic_tasks(active_only=True)

        for task_dict in tasks:
            task = PeriodicTask(**task_dict)
            scheduler = TaskScheduler(task, today)

            if not scheduler.should_remind_today():
                continue
            if self._monthly_quota_reached(task, today):
                continue

            schedule_times = scheduler.get_hourly_schedule_for_day(today) if task.cycle_type == 'hourly' else [task.time_of_day]
            for schedule_time in schedule_times:
                occ_id = self.create_occurrence_if_missing(task.id, today, scheduled_time=schedule_time)
                if not occ_id:
                    continue
                if getattr(task, 'task_kind', 'scheduled') == 'system' and schedule_time:
                    row = self.db.execute(
                        "SELECT execution_job_id FROM periodic_occurrences WHERE id = ?",
                        (occ_id,),
                    ).fetchone()
                    execution_job_id = row[0] if row else None
                    if not execution_job_id:
                        try:
                            reminder_job_name, execution_job_name = self._schedule_system_occurrence_jobs(occ_id, today, schedule_time)
                            self.db.execute(
                                "UPDATE periodic_occurrences SET reminder_job_id = COALESCE(?, reminder_job_id), execution_job_id = ? WHERE id = ?",
                                (reminder_job_name, execution_job_name, occ_id),
                            )
                            db_commit()
                        except Exception as exc:
                            METRICS.inc("scheduler_system_job_error_total")
                            emit_log("scheduler.ensure_system_job_failed", level="ERROR", occurrence_id=occ_id, error=str(exc))
                count += 1

        return count

    def _build_today_todo_snapshot(self, today: date) -> str:
        active_periodic_rows = self.db.execute(
            """
            SELECT o.id, o.date, o.status, t.name, t.cycle_type, o.scheduled_time, COALESCE(t.task_kind, 'scheduled') AS task_kind
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON o.task_id = t.id
            WHERE o.date = ? AND o.status IN ('pending', 'reminded')
            ORDER BY COALESCE(o.scheduled_time, t.time_of_day), t.name, o.id
            """,
            (today.isoformat(),),
        ).fetchall()
        skipped_periodic_rows = self.db.execute(
            """
            SELECT o.id, o.date, o.status, t.name, t.cycle_type, o.scheduled_time
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON o.task_id = t.id
            WHERE o.date = ? AND o.status = 'skipped'
            ORDER BY COALESCE(o.scheduled_time, t.time_of_day), t.name, o.id
            """,
            (today.isoformat(),),
        ).fetchall()

        active_simple_rows = self.db.execute(
            """
            SELECT e.id, e.text, e.status, COALESCE(g.name, 'Inbox') AS group_name
            FROM entries e
            LEFT JOIN groups g ON e.group_id = g.id
            WHERE e.status IN ('pending', 'in_progress')
              AND NOT EXISTS (
                  SELECT 1 FROM periodic_tasks t
                  WHERE t.legacy_entry_id = e.id
              )
            ORDER BY e.id
            """
        ).fetchall()
        skipped_simple_rows = self.db.execute(
            """
            SELECT e.id, e.text, e.status, COALESCE(g.name, 'Inbox') AS group_name
            FROM entries e
            LEFT JOIN groups g ON e.group_id = g.id
            WHERE e.status = 'skipped'
              AND NOT EXISTS (
                  SELECT 1 FROM periodic_tasks t
                  WHERE t.legacy_entry_id = e.id
              )
            ORDER BY e.id
            """
        ).fetchall()

        active_scheduled_periodic_rows = [row for row in active_periodic_rows if row["task_kind"] != "system"]
        active_system_periodic_rows = [row for row in active_periodic_rows if row["task_kind"] == "system"]

        lines = [f"📋 今日待办总览（{today.isoformat()}）"]
        if active_scheduled_periodic_rows:
            lines.append("")
            lines.append("【今日周期任务】")
            for row in active_scheduled_periodic_rows:
                status = row['status']
                if status == 'reminded':
                    status = '已提醒'
                elif status == 'pending':
                    status = '待处理'
                schedule_suffix = f" | 开始时间 {row['scheduled_time']}" if row['scheduled_time'] else ''
                lines.append(f"- FIN-{row['id']} | {row['name']}{schedule_suffix} | {status}")
        else:
            lines.append("")
            lines.append("【今日周期任务】")
            lines.append("- 无")

        if active_simple_rows or active_system_periodic_rows:
            lines.append("")
            lines.append("【其他待办】")
            for row in active_simple_rows:
                status = row['status']
                if status == 'in_progress':
                    status = '进行中'
                elif status == 'pending':
                    status = '待处理'
                lines.append(f"- ID{row['id']} | {row['group_name']} | {row['text']} | {status}")
            for row in active_system_periodic_rows:
                status = row['status']
                if status == 'reminded':
                    status = '已提醒'
                elif status == 'pending':
                    status = '待处理'
                schedule_suffix = f" | 开始时间 {row['scheduled_time']}" if row['scheduled_time'] else ''
                lines.append(f"- FIN-{row['id']} | 系统任务 | {row['name']}{schedule_suffix} | {status}")
        else:
            lines.append("")
            lines.append("【其他待办】")
            lines.append("- 无")

        skipped_total = len(skipped_periodic_rows) + len(skipped_simple_rows)
        if skipped_total:
            lines.append("")
            lines.append(f"【已跳过】共 {skipped_total} 项（默认不混入活跃待办）")
            for row in skipped_periodic_rows:
                schedule_suffix = f" | 开始时间 {row['scheduled_time']}" if row['scheduled_time'] else ''
                lines.append(f"- FIN-{row['id']} | {row['name']}{schedule_suffix} | 已跳过")
            for row in skipped_simple_rows:
                lines.append(f"- ID{row['id']} | {row['group_name']} | {row['text']} | 已跳过")

        return "\n".join(lines)

    def _send_today_todo_snapshot(self, today: date) -> bool:
        message_text = self._build_today_todo_snapshot(today)
        return self._send_message_now(message_text, task={"id": None, "name": "todo_snapshot", "task_kind": "system", "delivery_target": "tg-summary"})

    def run_daily(self) -> int:
        with LearningContext("periodic_manager_daily_run", "Generate today's reminders, clean old cron jobs, and push today's todo snapshot", confidence="H"):
            today = to_shanghai_date()
            scheduled = self.generate_reminders_for_today()
            cleaned = self.cleanup_old_jobs(today - timedelta(days=1))
            snapshot_sent = 1 if self._send_today_todo_snapshot(today) else 0
            METRICS.set_gauge("run_daily.last_scheduled", float(scheduled))
            METRICS.set_gauge("run_daily.last_cleaned", float(cleaned))
            METRICS.set_gauge("run_daily.last_snapshot_sent", float(snapshot_sent))
            emit_log("periodic.run_daily.completed", scheduled=scheduled, cleaned=cleaned, snapshot_sent=snapshot_sent)
            return scheduled + cleaned + snapshot_sent

