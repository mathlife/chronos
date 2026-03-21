"""Main periodic task manager using the new core modules."""
import sys
import argparse
from pathlib import Path

# Add core module to path
SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR))

import sqlite3
import subprocess
import json
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

from core.db import DB, db_commit, clear_task_cache, get_periodic_tasks, get_periodic_task
from core.scheduler import TaskScheduler, to_shanghai_date
from core.learning import LearningContext
from core.models import PeriodicTask
from core.config import get_chat_id
from core.paths import OPENCLAW_BIN

CYCLE_TYPES = ['once', 'daily', 'weekly', 'monthly_fixed', 'monthly_range', 'monthly_n_times']

SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')


def parse_time_of_day(value: str) -> str:
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', value.strip())
    if not match:
        raise argparse.ArgumentTypeError("time must be HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise argparse.ArgumentTypeError("time must be HH:MM (00:00-23:59)")
    return f"{hour:02d}:{minute:02d}"


def validate_add_params(args: argparse.Namespace) -> None:
    if args.weekday is not None and (args.weekday < 0 or args.weekday > 6):
        raise ValueError("weekday must be 0-6 (Mon=0)")
    if args.day_of_month is not None and (args.day_of_month < 1 or args.day_of_month > 31):
        raise ValueError("day must be 1-31")
    if args.range_start is not None and (args.range_start < 1 or args.range_start > 31):
        raise ValueError("range-start must be 1-31")
    if args.range_end is not None and (args.range_end < 1 or args.range_end > 31):
        raise ValueError("range-end must be 1-31")
    if args.n_per_month is not None and args.n_per_month <= 0:
        raise ValueError("n-per-month must be > 0")
    if args.end_date:
        try:
            date.fromisoformat(args.end_date)
        except ValueError as exc:
            raise ValueError("end-date must be YYYY-MM-DD") from exc

    if args.cycle_type == 'weekly' and args.weekday is None:
        raise ValueError("weekly tasks require --weekday")
    if args.cycle_type == 'monthly_fixed' and args.day_of_month is None:
        raise ValueError("monthly_fixed tasks require --day")
    if args.cycle_type == 'monthly_range' and (args.range_start is None or args.range_end is None):
        raise ValueError("monthly_range tasks require --range-start and --range-end")
    if args.cycle_type == 'monthly_n_times' and (args.weekday is None or args.n_per_month is None):
        raise ValueError("monthly_n_times tasks require --weekday and --n-per-month")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronos periodic task manager")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add", action="store_true", help="Add a periodic task")
    group.add_argument("--complete-activity", type=int, help="Complete activity by task id")
    group.add_argument("--ensure-today", action="store_true", help="Ensure today's occurrences")

    parser.add_argument("--name")
    parser.add_argument("--category", default="Inbox")
    parser.add_argument("--cycle-type", default="once", choices=CYCLE_TYPES)
    parser.add_argument("--time", dest="time_of_day", type=parse_time_of_day, default="09:00")
    parser.add_argument("--weekday", type=int)
    parser.add_argument("--day", dest="day_of_month", type=int)
    parser.add_argument("--range-start", type=int)
    parser.add_argument("--range-end", type=int)
    parser.add_argument("--n-per-month", type=int)
    parser.add_argument("--end-date")
    parser.add_argument("--reminder-template")

    return parser

class PeriodicTaskManager:
    """Manages periodic tasks: scheduling, completion, cleanup."""

    def __init__(self):
        self.db = DB()

    def add_activity(self, **params) -> int:
        """Add a new periodic task."""
        with LearningContext("add_activity", 
                             f"Add task: {params.get('name')} ({params.get('cycle_type')})",
                             confidence="H"):
            cur = self.db.execute("""
                INSERT INTO periodic_tasks 
                (name, category, cycle_type, weekday, day_of_month, range_start, range_end, n_per_month, 
                 time_of_day, event_time, timezone, is_active, count_current_month, end_date, reminder_template,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', 1, 0, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (
                params.get('name'),
                params.get('category', 'Inbox'),
                params.get('cycle_type', 'once'),
                params.get('weekday'),
                params.get('day_of_month'),
                params.get('range_start'),
                params.get('range_end'),
                params.get('n_per_month'),
                params.get('time_of_day', '09:00'),
                params.get('time_of_day', '09:00'),
                params.get('end_date'),
                params.get('reminder_template')
            ))
            db_commit()
            clear_task_cache()
            activity_id = cur.lastrowid
            return activity_id

    def reset_monthly_counters(self, today: date):
        """Reset monthly_n_times counters on the 1st."""
        if today.day == 1:
            with LearningContext("reset_monthly_counters", 
                                 f"Reset monthly counters for {today.strftime('%Y-%m')}",
                                 confidence="H"):
                self.db.execute("""
                    UPDATE periodic_tasks 
                    SET count_current_month = 0 
                    WHERE cycle_type = 'monthly_n_times' AND is_active = 1
                """)
                db_commit()
                clear_task_cache()

    def create_occurrence_if_missing(self, task_id: int, occ_date: date) -> int:
        """Create occurrence row if not exists. Returns occurrence ID or None."""
        self.db.execute("""
            INSERT OR IGNORE INTO periodic_occurrences (task_id, date, status)
            VALUES (?, ?, 'pending')
        """, (task_id, occ_date.isoformat()))
        db_commit()
        # Return the ID
        cur = self.db.execute("SELECT id FROM periodic_occurrences WHERE task_id = ? AND date = ?", (task_id, occ_date.isoformat()))
        row = cur.fetchone()
        return row[0] if row else None

    def schedule_reminder_cron(self, task_id: int, occ_date: date, time_of_day: str) -> Optional[str]:
        """Create a one-shot cron job for this occurrence. Returns job_name or None if in past.
        Reminder is scheduled 5 minutes before the actual event time."""
        cur = self.db.execute(
            "SELECT name, reminder_template FROM periodic_tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        task_name, reminder_template = row[0], row[1]

        try:
            chat_id = get_chat_id()
        except ValueError as exc:
            print(f"Chronos chat_id not configured: {exc}")
            return None
        
        # Parse time_of_day and subtract 5 minutes for reminder
        hour, minute = map(int, time_of_day.split(':'))
        reminder_minute = minute - 5
        reminder_hour = hour
        reminder_date = occ_date
        
        # Handle underflow (e.g., 00:05 -> 23:55 previous day)
        if reminder_minute < 0:
            reminder_minute += 60
            reminder_hour -= 1
            if reminder_hour < 0:
                reminder_hour += 24
                reminder_date = occ_date - timedelta(days=1)
        
        # Combine date + time in Shanghai timezone, convert to UTC ISO
        dt_shanghai = datetime(reminder_date.year, reminder_date.month, reminder_date.day,
                               reminder_hour, reminder_minute, tzinfo=SHANGHAI_TZ)
        utc_dt = dt_shanghai.astimezone(ZoneInfo('UTC'))
        
        # Check if the time is in the past
        now_utc = datetime.now(ZoneInfo('UTC'))
        if utc_dt <= now_utc:
            # Time already passed: send immediate reminder as system event
            message_text = self._format_reminder_message(
                task_name, occ_date, time_of_day, reminder_template, immediate=True
            )
            try:
                # Send immediate system event
                subprocess.run([
                    OPENCLAW_BIN, "cron", "add",
                    "--name", f"reminder_immediate_{task_id}_{occ_date.strftime('%Y%m%d%H%M')}",
                    "--at", now_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "--message", message_text,
                    "--session", "isolated",
                    "--announce",
                    "--to", chat_id
                ], capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"Failed to send immediate reminder: {e}")
            return None  # No persistent cron job
        
        iso_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        
        job_name = f"task_reminder_{task_id}_{occ_date.strftime('%Y%m%d')}"
        message_text = self._format_reminder_message(
            task_name, occ_date, time_of_day, reminder_template, immediate=False
        )
        
        cmd = [
            OPENCLAW_BIN, "cron", "add",
            "--name", job_name,
            "--at", iso_time,
            "--message", message_text,
            "--session", "isolated",
            "--announce",
            "--to", chat_id
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"Failed to schedule cron: {e}")
            return None
        if result.returncode == 0:
            return job_name
        else:
            print(f"Failed to schedule cron: {result.stderr}")
            return None

    def generate_reminders_for_today(self) -> int:
        """Generate today's reminder jobs. Returns count scheduled."""
        today = to_shanghai_date()
        self.reset_monthly_counters(today)
        
        scheduled = 0
        tasks = get_periodic_tasks(active_only=True)
        
        for task_dict in tasks:
            task = PeriodicTask(**task_dict)
            scheduler = TaskScheduler(task, today)
            
            if not scheduler.should_remind_today():
                continue
            
            # Ensure occurrence exists (create if not exists)
            occ_id = self.create_occurrence_if_missing(task.id, today)
            if not occ_id:
                # Already exists, get its id
                cur = self.db.execute("SELECT id FROM periodic_occurrences WHERE task_id = ? AND date = ?", (task.id, today.isoformat()))
                row = cur.fetchone()
                if row:
                    occ_id = row[0]
                else:
                    continue
            
            # Check if reminder already scheduled
            cur = self.db.execute("SELECT status, reminder_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
            status, job_name = cur.fetchone()
            if status not in ('pending', 'reminded'):
                continue
            if not job_name:
                job_name = self.schedule_reminder_cron(task.id, today, task.time_of_day)
                if job_name:
                    self.db.execute("UPDATE periodic_occurrences SET reminder_job_id = ? WHERE id = ?", (job_name, occ_id))
                    db_commit()
                    scheduled += 1
        
        return scheduled

    def cleanup_old_jobs(self, before_date: date) -> int:
        """Remove cron jobs for occurrences on or before given date."""
        cur = self.db.execute("""
            SELECT o.id, o.reminder_job_id 
            FROM periodic_occurrences o
            WHERE o.date <= ? AND o.reminder_job_id IS NOT NULL
        """, (before_date.isoformat(),))
        jobs = cur.fetchall()
        
        cleaned = 0
        for occ_id, job_name in jobs:
            try:
                result = subprocess.run(
                    [OPENCLAW_BIN, "cron", "remove", job_name],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    self.db.execute("UPDATE periodic_occurrences SET reminder_job_id = NULL WHERE id = ?", (occ_id,))
                    cleaned += 1
            except subprocess.TimeoutExpired:
                print(f"Timeout removing cron job {job_name}")
            except Exception as e:
                print(f"Error removing cron job {job_name}: {e}")
        
        db_commit()
        return cleaned

    def complete_occurrence(self, occurrence_id: int) -> bool:
        """Mark an occurrence as completed."""
        with LearningContext("complete_occurrence", 
                             f"Complete occurrence {occurrence_id}",
                             confidence="H"):
            cur = self.db.execute("""
                UPDATE periodic_occurrences 
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
                WHERE id = ? AND status != 'completed'
            """, (occurrence_id,))
            affected = cur.rowcount
            if affected > 0:
                db_commit()
                # If monthly_n_times, increment counter
                cur = self.db.execute("SELECT task_id FROM periodic_occurrences WHERE id = ?", (occurrence_id,))
                row = cur.fetchone()
                if row:
                    task_id = row[0]
                    cur = self.db.execute("SELECT cycle_type FROM periodic_tasks WHERE id = ?", (task_id,))
                    cycle_type_row = cur.fetchone()
                    if cycle_type_row and cycle_type_row[0] == 'monthly_n_times':
                        self.db.execute("UPDATE periodic_tasks SET count_current_month = count_current_month + 1 WHERE id = ?", (task_id,))
                        db_commit()
            return affected > 0

    def complete_activity_cycle(self, task_id: int, as_of: Optional[date] = None) -> int:
        """Complete all pending occurrences for a task up to today."""
        with LearningContext("complete_activity_cycle", 
                             f"Complete all pending for task {task_id} up to today",
                             confidence="H"):
            today = to_shanghai_date(as_of)
            task_dict = get_periodic_task(task_id)
            if not task_dict:
                return 0
            task = PeriodicTask(**task_dict)
            affected = 0
            
            # 1. Complete all pending up to today (including today)
            cur = self.db.execute("""
                SELECT id FROM periodic_occurrences 
                WHERE task_id = ? AND status = 'pending' 
                  AND date <= ?
                  AND strftime('%Y-%m', date) = ?
            """, (task_id, today.isoformat(), today.strftime('%Y-%m')))
            pending_ids = [row[0] for row in cur.fetchall()]
            
            for occ_id in pending_ids:
                self.complete_occurrence(occ_id)
                affected += 1
            
            # 2. For monthly_n_times, check quota and auto-complete remaining in current month if quota full
            if task.cycle_type == 'monthly_n_times':
                updated_task = PeriodicTask(**(get_periodic_task(task_id) or {}))
                if updated_task.count_current_month >= (updated_task.n_per_month or 0):
                    # Auto-complete any remaining pending in current month (future dates)
                    cur = self.db.execute("""
                        UPDATE periodic_occurrences 
                        SET status = 'completed', is_auto_completed = 1
                        WHERE task_id = ? AND status = 'pending' 
                          AND strftime('%Y-%m', date) = ?
                    """, (task_id, today.strftime('%Y-%m')))
                    affected += cur.rowcount
                    db_commit()
            
            # 3. Clear any pending reminder cron jobs for this task (no longer needed)
            cur = self.db.execute("""
                SELECT reminder_job_id FROM periodic_occurrences 
                WHERE task_id = ? AND reminder_job_id IS NOT NULL
            """, (task_id,))
            job_names = [row[0] for row in cur.fetchall()]
            for job_name in job_names:
                try:
                    subprocess.run(
                        [OPENCLAW_BIN, "cron", "remove", job_name],
                        capture_output=True, text=True, timeout=10
                    )
                except:
                    pass
            
            return affected

    def _format_reminder_message(
        self,
        task_name: str,
        occ_date: date,
        time_of_day: str,
        reminder_template: Optional[str],
        immediate: bool,
    ) -> str:
        if not reminder_template:
            if immediate:
                return f"⏰ 周期任务提醒（补发）：{task_name} 已到时间（{occ_date} {time_of_day}）"
            return f"⏰ 周期任务提醒（提前5分钟）：{task_name} 即将开始"

        template_vars = {
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
        """Lightweight: only ensure today's occurrences exist (no cleanup, no cron scheduling)."""
        today = to_shanghai_date()
        self.reset_monthly_counters(today)
        
        count = 0
        tasks = get_periodic_tasks(active_only=True)
        
        for task_dict in tasks:
            task = PeriodicTask(**task_dict)
            scheduler = TaskScheduler(task, today)
            
            if not scheduler.should_remind_today():
                continue
            
            occ_id = self.create_occurrence_if_missing(task.id, today)
            if occ_id:
                count += 1
        
        return count

    def run_daily(self) -> int:
        """Daily main entry: generate reminders and clean old cron jobs."""
        with LearningContext("periodic_manager_daily_run", 
                             "Generate today's reminders and clean old cron jobs",
                             confidence="H"):
            today = to_shanghai_date()
            scheduled = self.generate_reminders_for_today()
            cleaned = self.cleanup_old_jobs(today - timedelta(days=1))
            
            # Prediction outcome logged by LearningContext
            return scheduled + cleaned

def main():
    manager = PeriodicTaskManager()
    try:
        parser = build_parser()
        args = parser.parse_args()

        if args.add:
            if not args.name:
                print("Missing required --name for --add")
                sys.exit(2)
            try:
                validate_add_params(args)
            except ValueError as exc:
                print(f"参数错误：{exc}")
                sys.exit(2)

            params = {
                'name': args.name,
                'category': args.category,
                'cycle_type': args.cycle_type,
                'time_of_day': args.time_of_day,
            }
            if args.weekday is not None:
                params['weekday'] = args.weekday
            if args.day_of_month is not None:
                params['day_of_month'] = args.day_of_month
            if args.range_start is not None:
                params['range_start'] = args.range_start
            if args.range_end is not None:
                params['range_end'] = args.range_end
            if args.n_per_month is not None:
                params['n_per_month'] = args.n_per_month
            if args.end_date is not None:
                params['end_date'] = args.end_date
            if args.reminder_template is not None:
                params['reminder_template'] = args.reminder_template

            activity_id = manager.add_activity(**params)
            print(f"✅ Added task {activity_id}: {params.get('name')}")

        elif args.complete_activity is not None:
            affected = manager.complete_activity_cycle(args.complete_activity)
            print(f"Completed {affected} occurrences for task {args.complete_activity}")

        elif args.ensure_today:
            count = manager.ensure_today_occurrences()
            print(f"Ensured {count} occurrences for today")

        else:
            result = manager.run_daily()
            print(f"Periodic task manager: processed {result} items")
    finally:
        manager.db.close()

if __name__ == "__main__":
    main()
