import unittest
from datetime import date

from core.models import PeriodicTask
from core.scheduler import TaskScheduler


def make_task(**overrides) -> PeriodicTask:
    params = {
        "id": 1,
        "name": "test-task",
        "cycle_type": "daily",
        "category": "Inbox",
        "time_of_day": "09:00",
    }
    params.update(overrides)
    return PeriodicTask(**params)


class TaskSchedulerTests(unittest.TestCase):
    def test_cross_month_range_only_returns_dates_in_requested_month(self):
        task = make_task(cycle_type="monthly_range", range_start=11, range_end=5)
        scheduler = TaskScheduler(task, date(2026, 3, 1))

        occurrences = scheduler.get_occurrences_for_month(2026, 3)

        self.assertIn(date(2026, 3, 1), occurrences)
        self.assertIn(date(2026, 3, 5), occurrences)
        self.assertIn(date(2026, 3, 11), occurrences)
        self.assertIn(date(2026, 3, 31), occurrences)
        self.assertNotIn(date(2026, 4, 1), occurrences)
        self.assertEqual(len(occurrences), 26)

    def test_weekly_task_without_weekday_never_matches(self):
        task = make_task(cycle_type="weekly", weekday=None)
        scheduler = TaskScheduler(task, date(2026, 3, 18))

        self.assertFalse(scheduler.should_remind_today())
        self.assertEqual(scheduler.get_occurrences_for_month(2026, 3), [])

    def test_unknown_cycle_type_returns_empty_occurrences(self):
        task = make_task(cycle_type="once")
        scheduler = TaskScheduler(task, date(2026, 3, 18))

        self.assertFalse(scheduler.should_remind_today())
        self.assertEqual(scheduler.get_occurrences_for_month(2026, 3), [])


if __name__ == "__main__":
    unittest.main()
