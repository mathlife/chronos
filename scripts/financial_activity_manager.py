"""Main financial activity manager using the new core modules."""
import sys
from pathlib import Path

# Add core module to path (skills/custom-todo-manager/core)
SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR))

import sqlite3
import subprocess
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

from core.db import DB, db_commit, clear_activity_cache, get_financial_activities, get_financial_activity
from core.scheduler import ActivityScheduler, to_shanghai_date
from core.learning import LearningContext
from core.models import FinancialActivity

SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')

class FinancialActivityManager:
    """Manages financial activities: scheduling, completion, cleanup."""

    def __init__(self):
        self.db = DB()

    def reset_monthly_counters(self, today: date):
        """Reset monthly_n_times counters on the 1st."""
        if today.day == 1:
            self.db.execute("""
                UPDATE financial_activities 
                SET count_current_month = 0 
                WHERE cycle_type = 'monthly_n_times' AND is_active = 1
            """)
            db_commit()
            clear_activity_cache()

    def create_occurrence_if_missing(self, activity_id: int, occ_date: date):
        """Create occurrence row if not exists."""
        self.db.execute("""
            INSERT OR IGNORE INTO financial_occurrences (activity_id, date, status)
            VALUES (?, ?, 'pending')
        """, (activity_id, occ_date.isoformat()))
        db_commit()

    def schedule_reminder_cron(self, activity_id: int, occ_date: date, time_of_day: str) -> Optional[str]:
        """Create a one-shot cron job for this occurrence."""
        cur = self.db.execute("SELECT name FROM financial_activities WHERE id = ?", (activity_id,))
        row = cur.fetchone()
        if not row:
            return None
        activity_name = row[0]
        
        # Combine date + time in Shanghai timezone, convert to UTC ISO
        dt_shanghai = datetime(occ_date.year, occ_date.month, occ_date.day, 
                               *map(int, time_of_day.split(':')), tzinfo=SHANGHAI_TZ)
        utc_dt = dt_shanghai.astimezone(ZoneInfo('UTC'))
        iso_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        
        job_name = f"fin_reminder_{activity_id}_{occ_date.strftime('%Y%m%d')}"
        message_text = f"⏰ 金融活动提醒：{activity_name} 将于 14:00 开始"
        
        cmd = [
            "openclaw", "cron", "add",
            "--name", job_name,
            "--at", iso_time,
            "--system-event", message_text,
            "--session", "main"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
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
        activities = get_financial_activities(active_only=True)
        
        for activity_dict in activities:
            activity = FinancialActivity(**activity_dict)
            scheduler = ActivityScheduler(activity, today)
            
            if not scheduler.should_remind_today():
                continue
            
            # Ensure occurrence exists
            self.create_occurrence_if_missing(activity.id, today)
            
            # Check if reminder already scheduled
            cur = self.db.execute("""
                SELECT id, reminder_job_id FROM financial_occurrences 
                WHERE activity_id = ? AND date = ? AND status IN ('pending', 'reminded')
            """, (activity.id, today.isoformat()))
            row = cur.fetchone()
            if row:
                occ_id, job_name = row
                if not job_name:
                    job_name = self.schedule_reminder_cron(activity.id, today, activity.time_of_day)
                    if job_name:
                        self.db.execute("UPDATE financial_occurrences SET reminder_job_id = ? WHERE id = ?", (job_name, occ_id))
                        db_commit()
                        scheduled += 1
        
        return scheduled

    def cleanup_old_jobs(self, before_date: date) -> int:
        """Remove cron jobs for occurrences on or before given date."""
        cur = self.db.execute("""
            SELECT o.id, o.reminder_job_id 
            FROM financial_occurrences o
            WHERE o.date <= ? AND o.reminder_job_id IS NOT NULL
        """, (before_date.isoformat(),))
        jobs = cur.fetchall()
        
        cleaned = 0
        for occ_id, job_name in jobs:
            list_result = subprocess.run(["openclaw", "cron", "list", "--json"], capture_output=True, text=True)
            if list_result.returncode == 0:
                try:
                    jobs_list = json.loads(list_result.stdout)
                    for job in jobs_list.get("jobs", []):
                        if job.get("name") == job_name:
                            subprocess.run(["openclaw", "cron", "remove", job["id"]], capture_output=True)
                            cleaned += 1
                except json.JSONDecodeError:
                    pass
            self.db.execute("UPDATE financial_occurrences SET reminder_job_id = NULL WHERE id = ?", (occ_id,))
        db_commit()
        return cleaned

    def complete_occurrence(self, occurrence_id: int) -> int:
        """Mark a single occurrence as completed. Returns rows affected."""
        cur = self.db.execute("UPDATE financial_occurrences SET status = 'completed' WHERE id = ?", (occurrence_id,))
        affected = cur.rowcount
        
        # If monthly_n_times, increment counter
        cur.execute("SELECT activity_id FROM financial_occurrences WHERE id = ?", (occurrence_id,))
        row = cur.fetchone()
        if row:
            activity_id = row[0]
            cur.execute("SELECT cycle_type FROM financial_activities WHERE id = ?", (activity_id,))
            cycle_type_row = cur.fetchone()
            if cycle_type_row and cycle_type_row[0] == 'monthly_n_times':
                self.db.execute("UPDATE financial_activities SET count_current_month = count_current_month + 1 WHERE id = ?", (activity_id,))
        db_commit()
        return affected

    def complete_activity_cycle(self, activity_id: int, as_of: Optional[date] = None) -> int:
        """Complete all pending occurrences for an activity up to today."""
        today = to_shanghai_date(as_of)
        activity = FinancialActivity(**get_financial_activity(activity_id) or {})
        affected = 0
        
        # 1. Complete all pending in current month (including today and future dates)
        cur = self.db.execute("""
            SELECT id FROM financial_occurrences 
            WHERE activity_id = ? AND status = 'pending' 
              AND strftime('%Y-%m', date) = ?
        """, (activity_id, today.strftime('%Y-%m')))
        pending_ids = [row[0] for row in cur.fetchall()]
        
        for occ_id in pending_ids:
            self.complete_occurrence(occ_id)
            affected += 1
        
        # 2. For monthly_n_times, check quota and auto-complete remaining in current month if quota full
        if activity.cycle_type == 'monthly_n_times':
            updated_activity = FinancialActivity(**get_financial_activity(activity_id))
            if updated_activity.count_current_month >= (updated_activity.n_per_month or 0):
                # Auto-complete any remaining pending in current month (future dates)
                cur = self.db.execute("""
                    UPDATE financial_occurrences 
                    SET status = 'completed', is_auto_completed = 1
                    WHERE activity_id = ? 
                      AND status = 'pending'
                      AND strftime('%Y-%m', date) = ?
                      AND date > ?
                """, (activity_id, today.strftime('%Y-%m'), today.isoformat()))
                affected += cur.rowcount
                db_commit()
        
        # 3. Cleanup cron jobs for this activity
        self.cleanup_occurrence_jobs(activity_id)
        
        # 4. Also mark original entries as done
        self.db.execute("""
            UPDATE entries 
            SET status = 'done' 
            WHERE text LIKE ? AND status = 'pending'
        """, (f"%{activity.name}%",))
        db_commit()
        
        return affected

    def cleanup_occurrence_jobs(self, activity_id: int):
        """Remove cron jobs for all occurrences of this activity."""
        cur = self.db.execute("""
            SELECT reminder_job_id FROM financial_occurrences 
            WHERE activity_id = ? AND reminder_job_id IS NOT NULL
        """, (activity_id,))
        job_names = [row[0] for row in cur.fetchall()]
        
        for job_name in job_names:
            list_result = subprocess.run(["openclaw", "cron", "list", "--json"], capture_output=True, text=True)
            if list_result.returncode == 0:
                try:
                    jobs = json.loads(list_result.stdout)
                    for job in jobs.get("jobs", []):
                        if job.get("name") == job_name:
                            subprocess.run(["openclaw", "cron", "remove", job["id"]], capture_output=True)
                except json.JSONDecodeError:
                    pass
        
        self.db.execute("UPDATE financial_occurrences SET reminder_job_id = NULL WHERE activity_id = ?", (activity_id,))
        db_commit()

    def add_activity(self, name: str, cycle_type: str, time_of_day: str, **kwargs) -> int:
        """Add a new financial activity. Returns activity ID."""
        category = kwargs.get('category', 'Inbox')
        params = {
            'name': name,
            'category': category,
            'cycle_type': cycle_type,
            'time_of_day': time_of_day,
            'event_time': time_of_day,
            'weekday': kwargs.get('weekday'),
            'day_of_month': kwargs.get('day_of_month'),
            'range_start': kwargs.get('range_start'),
            'range_end': kwargs.get('range_end'),
            'n_per_month': kwargs.get('n_per_month'),
        }
        
        cur = self.db.execute("""
            INSERT INTO financial_activities 
            (name, category, cycle_type, weekday, day_of_month, range_start, range_end, n_per_month, 
             time_of_day, event_time, timezone, is_active, count_current_month, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            params['name'], params['category'], params['cycle_type'],
            params['weekday'], params['day_of_month'], params['range_start'], params['range_end'],
            params['n_per_month'], params['time_of_day'], params['event_time']
        ))
        db_commit()
        clear_activity_cache()
        return cur.lastrowid

    def run_daily(self) -> int:
        """Daily main entry: generate reminders and clean old jobs."""
        with LearningContext("financial_manager_daily_run", 
                             "Generate today's reminders and clean old cron jobs",
                             confidence="H"):
            today = to_shanghai_date()
            scheduled = self.generate_reminders_for_today()
            cleaned = self.cleanup_old_jobs(today - timedelta(days=1))
            
            outcome = f"Scheduled {scheduled}, cleaned {cleaned}"
            # We don't have direct access to LearningContext here in this simple wrapper,
            # but prediction/outcome will be logged by the script's main().
            return scheduled + cleaned

def main():
    import sys
    manager = FinancialActivityManager()
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
            print(f"✅ Added activity {activity_id}: {params.get('name')}")
        
        elif len(sys.argv) > 1 and sys.argv[1] == '--complete-activity':
            if len(sys.argv) > 2:
                activity_id = int(sys.argv[2])
                affected = manager.complete_activity_cycle(activity_id)
                print(f"Completed {affected} occurrences for activity {activity_id}")
            else:
                print("Usage: --complete-activity <activity_id>")
        else:
            result = manager.run_daily()
            print(f"Financial activity manager: processed {result} items")
    finally:
        manager.db.close()

if __name__ == "__main__":
    main()
