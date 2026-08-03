"""CLI entry for periodic task manager."""
from __future__ import annotations

import argparse
import sys
from datetime import date

from core.models import ALLOWED_CYCLE_TYPES
from core.observability import emit_log
from core.system_command_runner import build_handler_payload_from_legacy_command
from service.periodic_service import PeriodicTaskManager

CYCLE_TYPES = list(ALLOWED_CYCLE_TYPES)


def parse_time_of_day(value: str) -> str:
    import re
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
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
    if args.quota is not None and args.quota <= 0:
        raise ValueError("quota must be > 0")
    if args.interval_hours is not None and (args.interval_hours <= 0 or args.interval_hours > 24):
        raise ValueError("interval-hours must be 1-24")
    if args.end_date:
        try:
            date.fromisoformat(args.end_date)
        except ValueError as exc:
            raise ValueError("end-date must be YYYY-MM-DD") from exc
    if args.start_date:
        try:
            date.fromisoformat(args.start_date)
        except ValueError as exc:
            raise ValueError("start-date must be YYYY-MM-DD") from exc
    if args.dates_list:
        cleaned = [chunk.strip() for chunk in args.dates_list.split(",") if chunk.strip()]
        if not cleaned:
            raise ValueError("monthly_dates tasks require --dates-list")
        parsed_days = []
        for chunk in cleaned:
            try:
                day = int(chunk)
            except ValueError as exc:
                raise ValueError("dates-list must contain comma-separated day numbers") from exc
            if day < 1 or day > 31:
                raise ValueError("dates-list day must be 1-31")
            parsed_days.append(day)
        args.dates_list = ",".join(str(day) for day in sorted(set(parsed_days)))

    if args.cycle_type == "once" and not args.start_date:
        raise ValueError("scheduled once tasks require --start-date YYYY-MM-DD")
    if args.cycle_type == "hourly" and args.interval_hours is None:
        args.interval_hours = 1
    if args.cycle_type == "weekly" and args.weekday is None:
        raise ValueError("weekly tasks require --weekday")
    if args.cycle_type == "monthly_fixed" and args.day_of_month is None:
        raise ValueError("monthly_fixed tasks require --day")
    if args.cycle_type == "monthly_range" and (args.range_start is None or args.range_end is None):
        raise ValueError("monthly_range tasks require --range-start and --range-end")
    if args.cycle_type == "monthly_n_times" and args.n_per_month is None:
        raise ValueError("monthly_n_times tasks require --n-per-month")
    if args.cycle_type == "monthly_dates" and not args.dates_list:
        raise ValueError("monthly_dates tasks require --dates-list")
    if args.system_command and args.task_kind != "system":
        raise ValueError("--system-command requires --task-kind system")
    if args.system_command and args.special_handler and args.special_handler != "run_command":
        raise ValueError("--system-command cannot be combined with another special_handler")

    # Canonicalize monthly aliases for unified downstream handling.
    if args.cycle_type == "monthly_fixed":
        args.cycle_type = "monthly_dates"
        if not args.dates_list and args.day_of_month is not None:
            args.dates_list = str(int(args.day_of_month))
    elif args.cycle_type == "monthly_n_times":
        args.cycle_type = "monthly_range"
        if args.range_start is None:
            args.range_start = 1
        if args.range_end is None:
            args.range_end = 31


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronos periodic task manager")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add", action="store_true", help="Add a periodic task")
    group.add_argument("--update", action="store_true", help="Update an existing task")
    group.add_argument("--complete-activity", type=int, help="Complete activity by task id")
    group.add_argument("--ensure-today", action="store_true", help="Ensure today's occurrences")
    group.add_argument("--fire-reminder", action="store_true", help="Fire a reminder for a scheduled occurrence")
    group.add_argument("--run-system-task", action="store_true", help="Execute a due system occurrence and mark it completed")

    parser.add_argument("--name")
    parser.add_argument("--category", default="Inbox")
    parser.add_argument("--cycle-type", default="once", choices=CYCLE_TYPES)
    parser.add_argument("--time", dest="time_of_day", type=parse_time_of_day, default="09:00")
    parser.add_argument("--weekday", type=int)
    parser.add_argument("--day", dest="day_of_month", type=int)
    parser.add_argument("--range-start", type=int)
    parser.add_argument("--range-end", type=int)
    parser.add_argument("--n-per-month", type=int)
    parser.add_argument("--interval-hours", type=int)
    parser.add_argument("--quota", type=int, help="Maximum completions per month")
    parser.add_argument("--dates-list")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--reminder-template")
    parser.add_argument("--task-kind", default="scheduled")
    parser.add_argument("--source", default="chronos")
    parser.add_argument("--legacy-entry-id", type=int)
    parser.add_argument("--special-handler")
    parser.add_argument("--handler-payload")
    parser.add_argument("--system-command", help="whitelisted command syntax: <command_id> [arg1 ...]")
    parser.add_argument("--delivery-target")
    parser.add_argument("--delivery-mode")
    parser.add_argument("--occurrence-id", type=int)

    return parser


def run_cli(argv: list[str] | None = None) -> int:
    manager = PeriodicTaskManager()
    try:
        parser = build_parser()
        args = parser.parse_args(argv)

        if args.add:
            if not args.name:
                print("Missing required --name for --add")
                return 2
            try:
                validate_add_params(args)
            except ValueError as exc:
                print(f"参数错误：{exc}")
                return 2

            params = {
                "name": args.name,
                "category": args.category,
                "cycle_type": args.cycle_type,
                "time_of_day": args.time_of_day,
                "task_kind": args.task_kind,
                "source": args.source,
            }
            if args.weekday is not None:
                params["weekday"] = args.weekday
            if args.day_of_month is not None:
                params["day_of_month"] = args.day_of_month
            if args.range_start is not None:
                params["range_start"] = args.range_start
            if args.range_end is not None:
                params["range_end"] = args.range_end
            if args.n_per_month is not None:
                params["n_per_month"] = args.n_per_month
            if args.quota is not None:
                params["quota"] = args.quota
            if args.interval_hours is not None:
                params["interval_hours"] = args.interval_hours
            if args.dates_list is not None:
                params["dates_list"] = args.dates_list
            if args.start_date is not None:
                params["start_date"] = args.start_date
            if args.end_date is not None:
                params["end_date"] = args.end_date
            if args.reminder_template is not None:
                params["reminder_template"] = args.reminder_template
            if args.legacy_entry_id is not None:
                params["legacy_entry_id"] = args.legacy_entry_id
            if args.special_handler is not None:
                params["special_handler"] = args.special_handler
            if args.handler_payload is not None:
                params["handler_payload"] = args.handler_payload
            if args.system_command is not None:
                params["special_handler"] = "run_command"
                params["handler_payload"] = build_handler_payload_from_legacy_command(args.system_command)
            if args.delivery_target is not None:
                params["delivery_target"] = args.delivery_target
            if args.delivery_mode is not None:
                params["delivery_mode"] = args.delivery_mode

            activity_id = manager.add_activity(**params)
            manager.ensure_today_occurrences()
            print(f"✅ Added task {activity_id}: {params.get('name')}")
            return 0

        if args.update:
            # Update an existing task
            if args.task_id is None:
                print("Missing required --task-id for --update")
                return 2
            # Reuse same validation logic as add (but optional fields)
            try:
                # Validate provided fields (similar to add)
                validate_add_params(args)
            except ValueError as exc:
                print(f"参数错误：{exc}")
                return 2
            # Build payload with only provided args
            payload = {}
            if args.name is not None:
                payload["name"] = args.name
            if args.category is not None:
                payload["category"] = args.category
            if args.cycle_type is not None:
                payload["cycle_type"] = args.cycle_type
            if args.time_of_day is not None:
                payload["time_of_day"] = args.time_of_day
            if args.weekday is not None:
                payload["weekday"] = args.weekday
            if args.day_of_month is not None:
                payload["day_of_month"] = args.day_of_month
            if args.range_start is not None:
                payload["range_start"] = args.range_start
            if args.range_end is not None:
                payload["range_end"] = args.range_end
            if args.n_per_month is not None:
                payload["n_per_month"] = args.n_per_month
            if args.quota is not None:
                payload["quota"] = args.quota
            if args.interval_hours is not None:
                payload["interval_hours"] = args.interval_hours
            if args.dates_list is not None:
                payload["dates_list"] = args.dates_list
            if args.start_date is not None:
                payload["start_date"] = args.start_date
            if args.end_date is not None:
                payload["end_date"] = args.end_date
            if args.reminder_template is not None:
                payload["reminder_template"] = args.reminder_template
            if args.legacy_entry_id is not None:
                payload["legacy_entry_id"] = args.legacy_entry_id
            if args.special_handler is not None:
                payload["special_handler"] = args.special_handler
            if args.handler_payload is not None:
                payload["handler_payload"] = args.handler_payload
            if args.system_command is not None:
                payload["special_handler"] = "run_command"
                payload["handler_payload"] = build_handler_payload_from_legacy_command(args.system_command)
            if args.delivery_target is not None:
                payload["delivery_target"] = args.delivery_target
            if args.delivery_mode is not None:
                payload["delivery_mode"] = args.delivery_mode

            import json, subprocess, shlex
            json_payload = json.dumps(payload, ensure_ascii=False)
            cmd = ["python3", "/home/ubuntu/chronos/scripts/chronos_api.py", "task", "update", "--id", str(args.task_id), "--payload", json_payload]
            result = subprocess.run(cmd, capture_output=True, text=True)
            print(result.stdout.strip())
            if result.returncode != 0:
                print("更新任务失败")
                return 1
            return 0

        if args.fire_reminder:
            if args.occurrence_id is None:
                print("Missing required --occurrence-id for --fire-reminder")
                return 2
            ok = manager.fire_reminder_occurrence(args.occurrence_id)
            print(f"Reminder fired: {ok}")
            return 0

        if args.run_system_task:
            if args.occurrence_id is None:
                print("Missing required --occurrence-id for --run-system-task")
                return 2
            ok = manager.run_system_occurrence(args.occurrence_id)
            print(f"System occurrence executed: {ok}")
            return 0

        if args.complete_activity is not None:
            affected = manager.complete_activity_cycle(args.complete_activity)
            print(f"Completed {affected} occurrences for task {args.complete_activity}")
            return 0

        if args.ensure_today:
            count = manager.ensure_today_occurrences()
            print(f"Ensured {count} occurrences for today")
            return 0

        result = manager.run_daily()
        print(f"Periodic task manager: processed {result} items")
        return 0
    except Exception as exc:
        emit_log("periodic.cli.error", level="ERROR", error=str(exc))
        raise
    finally:
        manager.db.close()


def main() -> None:
    code = run_cli()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
