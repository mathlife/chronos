"""Main periodic task manager using the new core modules."""
import sys
from pathlib import Path

# Add core module to path
SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR))

import sqlite3
import subprocess
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

from core.db import DB, db_commit, clear_task_cache, get_periodic_tasks, get_periodic_task
from core.scheduler import TaskScheduler, to_shanghai_date
from core.learning import LearningContext
from core.models import PeriodicTask

SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')

class PeriodicTaskManager:
    """Manages periodic tasks: scheduling, completion, cleanup."""

    def __init__(self):
        self.db = DB()

    def add_activity(self, **params) -> int:
        """Add a new periodic task."""
        cur = self.db.execute("""
            INSERT INTO periodic_tasks 
            (name, category, cycle_type, weekday, day_of_month, range_start, range_end, n_per_month, 
             time_of_day, event_time, timezone, is_active, count_current_month, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
            params.get('time_of_day', '09:00')
        ))
        db_commit()
        clear_task_cache()
        return cur.lastrowid

    def reset_monthly_counters(self, today: date):
        """Reset monthly_n_times counters on the 1st."""
        if today.day == 1:
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
        """Create a one-shot cron job for this occurrence. Returns job_name or None if in past."""
        cur = self.db.execute("SELECT name FROM periodic_tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        task_name = row[0]
        
        # Combine date + time in Shanghai timezone, convert to UTC ISO
        dt_shanghai = datetime(occ_date.year, occ_date.month, occ_date.day, 
                               *map(int, time_of_day.split(':')), tzinfo=SHANGHAI_TZ)
        utc_dt = dt_shanghai.astimezone(ZoneInfo('UTC'))
        
        # Check if the time is in the past
        now_utc = datetime.now(ZoneInfo('UTC'))
        if utc_dt <= now_utc:
            # Time already passed, skip cron scheduling (occurrence remains pending for manual completion)
            return None
        
        iso_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        
        job_name = f"task_reminder_{task_id}_{occ_date.strftime('%Y%m%d')}"
        message_text = f"⏰ 周期任务提醒：{task_name} 即将开始"
        
        cmd = [
            "openclaw", "cron", "add",
            "--name", job_name,
            "--at", iso_time,
            "--system-event", message_text,
            "--session", "main"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
            cur = self.db.execute("SELECT reminder_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
            job_name = cur.fetchone()[0]
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
                    ["openclaw", "cron", "remove", job_name],
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
        today = to_shanghai_date(as_of)
        task = PeriodicTask(**(get_periodic_task(task_id) or {}))
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
                    ["openclaw", "cron", "remove", job_name],
                    capture_output=True, text=True, timeout=10
                )
            except:
                pass
        
        return affected

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
    import sys
    manager = PeriodicTaskManager()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == '--add':
            # Parse args
            args = sys.argv[2:]
            params = {}
            i = 0
            while i < len(args):
                if args[i] == '--name' and i + 1 < len(args):
                    params['name'] = args[i+1]; i += 2
                elif args[i] == '--category' and i + 1 < len(args):
                    params['category'] = args[i+1]; i += 2
                elif args[i] == '--cycle-type' and i + 1 < len(args):
                    params['cycle_type'] = args[i+1]; i += 2
                elif args[i] == '--time' and i + 1 < len(args):
                    params['time_of_day'] = args[i+1]; i += 2
                elif args[i] == '--weekday' and i + 1 < len(args):
                    params['weekday'] = int(args[i+1]); i += 2
                elif args[i] == '--day' and i + 1 < len(args):
                    params['day_of_month'] = int(args[i+1]); i += 2
                elif args[i] == '--range-start' and i + 1 < len(args):
                    params['range_start'] = int(args[i+1]); i += 2
                elif args[i] == '--range-end' and i + 1 < len(args):
                    params['range_end'] = int(args[i+1]); i += 2
                elif args[i] == '--n-per-month' and i + 1 < len(args):
                    params['n_per_month'] = int(args[i+1]); i += 2
                else:
                    i += 1
            
            activity_id = manager.add_activity(**params)
            print(f"✅ Added task {activity_id}: {params.get('name')}")
        
        elif len(sys.argv) > 1 and sys.argv[1] == '--complete-activity':
            if len(sys.argv) > 2:
                task_id = int(sys.argv[2])
                affected = manager.complete_activity_cycle(task_id)
                print(f"Completed {affected} occurrences for task {task_id}")
            else:
                print("Usage: --complete-activity <task_id>")
        
        elif len(sys.argv) > 1 and sys.argv[1] == '--ensure-today':
            count = manager.ensure_today_occurrences()
            print(f"Ensured {count} occurrences for today")
        
        else:
            result = manager.run_daily()
            print(f"Periodic task manager: processed {result} items")
    finally:
        manager.db.close()

if __name__ == "__main__":
    main()