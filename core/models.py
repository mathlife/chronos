"""Data models for periodic tasks."""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List

@dataclass
class PeriodicTask:
    id: int
    name: str
    category: str = 'Inbox'
    cycle_type: str = 'once'  # once|daily|weekly|monthly_fixed|monthly_range|monthly_n_times
    weekday: Optional[int] = None  # 0-6 (0=Monday, Python weekday)
    day_of_month: Optional[int] = None  # 1-31
    range_start: Optional[int] = None
    range_end: Optional[int] = None
    n_per_month: Optional[int] = None
    time_of_day: str = '09:00'  # HH:MM
    event_time: Optional[str] = None
    timezone: str = 'Asia/Shanghai'
    is_active: bool = True
    count_current_month: int = 0
    end_date: Optional[str] = None  # YYYY-MM-DD, NULL means no end
    reminder_template: Optional[str] = None  # Custom reminder message template
    # Monitoring fields (optional, for backward compatibility)
    last_reminder_error: Optional[str] = None
    reminder_error_count: int = 0
    last_reminder_error_at: Optional[str] = None
    # Additional fields that may exist in DB
    dates_list: Optional[str] = None
    created_at: str = field(default_factory=lambda: date.today().isoformat())
    updated_at: str = field(default_factory=lambda: date.today().isoformat())

    @property
    def is_monthly_n_times(self) -> bool:
        return self.cycle_type == 'monthly_n_times'

@dataclass
class PeriodicOccurrence:
    id: int
    task_id: int
    date: date
    status: str = 'pending'  # pending|reminded|completed|skipped
    reminder_job_id: Optional[str] = None
    is_auto_completed: bool = False
    completed_at: Optional[str] = None
