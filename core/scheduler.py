"""Scheduling logic: date matching, occurrence generation, quota management."""
import calendar
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo
from functools import lru_cache
import re

from .models import PeriodicTask

SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')

def to_shanghai_date(dt: Optional[datetime] = None) -> date:
    """Convert datetime to Shanghai date (today if None)."""
    if dt is None:
        return datetime.now(SHANGHAI_TZ).date()
    # If it's already a date (not datetime), return as-is
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt
    # Now dt is a datetime
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    else:
        dt = dt.astimezone(SHANGHAI_TZ)
    return dt.date()

def is_same_month(d1: date, d2: date) -> bool:
    return d1.year == d2.year and d1.month == d2.month

@lru_cache(maxsize=128)
def get_weekdays_in_month(year: int, month: int, weekday: int) -> List[date]:
    """Return all dates in month matching weekday (0=Sun)."""
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year+1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month+1, 1) - timedelta(days=1)
    
    dates = []
    current = first_day
    while current <= last_day:
        if current.weekday() == weekday:
            dates.append(current)
        current += timedelta(days=1)
    return dates

class TaskScheduler:
    """Encapsulates all scheduling logic for periodic tasks."""

    def __init__(self, task: PeriodicTask, as_of: Optional[date] = None):
        self.task = task
        self.as_of = to_shanghai_date(as_of)
        # 解析 end_date
        self.end_date = None
        if task.end_date:
            try:
                self.end_date = date.fromisoformat(task.end_date)
            except ValueError:
                self.end_date = None

    def should_remind_today(self) -> bool:
        """Check if today matches the cycle and quota allows."""
        today = self.as_of
        
        # 检查结束日期
        if self.end_date and today > self.end_date:
            return False
        
        if self.task.cycle_type == 'daily':
            return True
        
        if self.task.cycle_type == 'weekly':
            return today.weekday() == self.task.weekday
        
        if self.task.cycle_type == 'monthly_fixed':
            return today.day == self.task.day_of_month
        
        if self.task.cycle_type == 'monthly_range':
            return self._in_monthly_range(today)
        
        if self.task.cycle_type == 'monthly_n_times':
            if today.weekday() != self.task.weekday:
                return False
            # Check quota
            if self.task.count_current_month >= (self.task.n_per_month or 0):
                return False
            return True
        
        return False

    def _in_monthly_range(self, today: date) -> bool:
        """Check if today falls within the configured range (may cross month)."""
        start = self.task.range_start
        end = self.task.range_end
        if start is None or end is None:
            return False
        
        if start <= end:
            return start <= today.day <= end
        
        # Cross-month: two possible intervals
        year = today.year
        month = today.month
        
        # Interval 1: current month start -> next month end
        try:
            interval1_start = date(year, month, start)
            if month == 12:
                interval1_end = date(year+1, 1, end)
            else:
                interval1_end = date(year, month+1, end)
            if interval1_start <= today <= interval1_end:
                return True
        except ValueError:
            pass
        
        # Interval 2: previous month start -> current month end
        try:
            if month == 1:
                interval2_start = date(year-1, 12, start)
            else:
                interval2_start = date(year, month-1, start)
            interval2_end = date(year, month, end)
            if interval2_start <= today <= interval2_end:
                return True
        except ValueError:
            pass
        
        return False

    def get_occurrences_for_month(self, year: int, month: int) -> List[date]:
        """Generate all occurrence dates for the given month, respecting end_date."""
        cycle_type = self.task.cycle_type
        
        if cycle_type == 'daily':
            # Every day of the month
            days_in_month = calendar.monthrange(year, month)[1]
            dates = [date(year, month, d) for d in range(1, days_in_month+1)]
        
        if cycle_type == 'weekly':
            dates = get_weekdays_in_month(year, month, self.task.weekday)
        
        if cycle_type == 'monthly_fixed':
            day = self.task.day_of_month
            try:
                dates = [date(year, month, day)]
            except ValueError:
                dates = []  # Invalid day for this month (e.g., Feb 30)
        
        if cycle_type == 'monthly_range':
            start = self.task.range_start
            end = self.task.range_end
            if start is None or end is None:
                return []
            
            dates = []
            if start <= end:
                for day in range(start, end+1):
                    try:
                        dates.append(date(year, month, day))
                    except ValueError:
                        continue
            else:
                # Cross-month: generate two intervals
                days_in_month = calendar.monthrange(year, month)[1]
                for day in range(start, days_in_month+1):
                    try:
                        dates.append(date(year, month, day))
                    except ValueError:
                        continue
                if month == 12:
                    next_month = 1
                    next_year = year+1
                else:
                    next_month = month+1
                    next_year = year
                for day in range(1, end+1):
                    try:
                        dates.append(date(next_year, next_month, day))
                    except ValueError:
                        continue
        
        if cycle_type == 'monthly_n_times':
            # All matching weekdays in month, limited by quota
            dates = get_weekdays_in_month(year, month, self.task.weekday)
        
        # Filter by end_date if set
        if self.end_date:
            dates = [d for d in dates if d <= self.end_date]
        
        return dates

    def get_pending_dates_in_month(self, year: int, month: int, existing_occurrences: List[Tuple[date, str]]) -> List[date]:
        """Return dates in month that should be pending (not already completed/skipped)."""
        all_dates = self.get_occurrences_for_month(year, month)
        if not all_dates:
            return []
        
        # Filter out dates that are already completed or skipped
        done_dates = {d for d, s in existing_occurrences if s in ('completed', 'skipped')}
        pending_dates = [d for d in all_dates if d not in done_dates]
        
        if self.task.cycle_type == 'monthly_n_times':
            # Apply quota limit
            count_current = self.task.count_current_month
            n = self.task.n_per_month or 0
            # Only keep up to (n - count_current) dates
            allowed = max(0, n - count_current)
            if allowed == 0:
                return []
            # Return earliest 'allowed' dates
            pending_dates.sort()
            return pending_dates[:allowed]
        
        return pending_dates
