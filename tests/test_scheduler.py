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
        task = make_task(cycle_type="bogus")
        scheduler = TaskScheduler(task, date(2026, 3, 18))

        self.assertFalse(scheduler.should_remind_today())
        self.assertEqual(scheduler.get_occurrences_for_month(2026, 3), [])

    def test_once_task_uses_start_date_as_single_occurrence(self):
        task = make_task(cycle_type="once", start_date="2026-03-18")
        scheduler = TaskScheduler(task, date(2026, 3, 18))

        self.assertTrue(scheduler.should_remind_today())
        self.assertEqual(scheduler.get_occurrences_for_month(2026, 3), [date(2026, 3, 18)])

    def test_monthly_dates_generates_requested_days(self):
        task = make_task(cycle_type="monthly_dates", dates_list="1, 15, 31")
        scheduler = TaskScheduler(task, date(2026, 3, 15))

        self.assertTrue(scheduler.should_remind_today())
        self.assertEqual(
            scheduler.get_occurrences_for_month(2026, 3),
            [date(2026, 3, 1), date(2026, 3, 15), date(2026, 3, 31)],
        )


if __name__ == "__main__":
    unittest.main()
