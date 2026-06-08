#!/usr/bin/env python3
"""
Unified Todo - 统一待办管理入口
支持：list/add/complete/show
自动路由：周期任务 → periodic_task_manager，其他 → 直接操作 entries 表
自然语言解析：支持中文指令
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SKILL_DIR.parent.parent
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(WORKSPACE_ROOT))

from core.legacy_archive import (
    archive_block_message,
    archive_display_label,
    build_entry_archive_state,
    get_entry_columns,
    legacy_archive_select_expressions,
)
from core.config import get_config_path
from core.notifiers import dispatch_message
from core.paths import PYTHON_BIN, SCRIPTS_DIR, TODO_DB, WORKSPACE
from core.models import ALLOWED_CYCLE_TYPES
from core.scheduler import resolve_monthly_quota_window
from core.system_command_runner import execute_system_handler
from core.system_scheduler import remove_job
from scripts.todo_nl import parse_natural_language
try:
    from scripts.subagent_sync_ledger import looks_like_subagent_session
except ModuleNotFoundError:
    def looks_like_subagent_session(session_id: str | None) -> bool:
        return bool(session_id)

MANAGER_SCRIPT = SCRIPTS_DIR / 'periodic_task_manager.py'
CYCLE_TYPES = list(ALLOWED_CYCLE_TYPES)
TIME_PATTERN = re.compile(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)')
META_REVIEW_PATTERN = re.compile(r'meta[- ]?review|meta_auditor\.py', re.IGNORECASE)
EVERY_N_HOURS_PATTERN = re.compile(r'每\s*(\d+)\s*小时')
RECURRING_ENTRY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r'\bdaily\b',
        r'每\s*日',
        r'每天',
        r'每\s*周',
        r'每周',
        r'每\s*月',
        r'每月',
        r'每\s*\d+\s*小时',
        r'\[每[周月日天].*?重复\]',
        r'meta[- ]?review',
    )
]


def parse_time_of_day(value: str) -> str:
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', value.strip())
    if not match:
        raise argparse.ArgumentTypeError("time must be HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise argparse.ArgumentTypeError("time must be HH:MM (00:00-23:59)")
    return f"{hour:02d}:{minute:02d}"


def validate_add_args(args: argparse.Namespace) -> None:
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

    if args.cycle_type == 'once' and args.start_date is None:
        return
    if args.cycle_type == 'once' and not args.start_date:
        raise ValueError("scheduled once tasks require --start-date YYYY-MM-DD")
    if args.cycle_type == 'hourly' and args.interval_hours is None:
        args.interval_hours = 1
    if args.cycle_type == 'weekly' and args.weekday is None:
        raise ValueError("weekly tasks require --weekday")
    if args.cycle_type == 'monthly_fixed' and args.day_of_month is None:
        raise ValueError("monthly_fixed tasks require --day")
    if args.cycle_type == 'monthly_range' and (args.range_start is None or args.range_end is None):
        raise ValueError("monthly_range tasks require --range-start and --range-end")
    if args.cycle_type == 'monthly_n_times' and args.n_per_month is None:
        raise ValueError("monthly_n_times tasks require --n-per-month")
    if args.cycle_type == 'monthly_dates' and not args.dates_list:
        raise ValueError("monthly_dates tasks require --dates-list")
    if args.system_command and args.task_kind != 'system':
        raise ValueError("--system-command requires --task-kind system")
    if args.system_command and args.special_handler and args.special_handler != 'run_command':
        raise ValueError("--system-command cannot be combined with another special_handler")

    # Canonicalize monthly aliases for unified behavior:
    # monthly_fixed -> monthly_dates(single day)
    # monthly_n_times -> monthly_range(1-31) + n_per_month
    if args.cycle_type == 'monthly_fixed':
        args.cycle_type = 'monthly_dates'
        if not args.dates_list and args.day_of_month is not None:
            args.dates_list = str(int(args.day_of_month))
    elif args.cycle_type == 'monthly_n_times':
        args.cycle_type = 'monthly_range'
        if args.range_start is None:
            args.range_start = 1
        if args.range_end is None:
            args.range_end = 31


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Chronos unified todo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  todo.py list --include-skipped\n"
            "  todo.py add \"写日报\" --cycle-type daily --time 09:00\n"
            "  todo.py add \"每周例会\" --cycle-type weekly --weekday 0 --time 10:00\n"
            "  todo.py complete FIN-123\n"
            "  todo.py complete-overdue --dry-run\n\n"
            "Compatibility notes:\n"
            "  - monthly_fixed remains supported; it is normalized to monthly_dates.\n"
            "  - monthly_n_times remains supported; it is normalized to monthly_range.\n"
            "  - You can still pass natural-language instructions without a subcommand."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List all pending tasks")
    list_parser.add_argument("--include-skipped", action="store_true", help="Include skipped tasks in the list view")

    add_parser = subparsers.add_parser(
        "add",
        help="Add a task",
        description=(
            "Add a one-off or recurring task. Legacy monthly aliases are still accepted:\n"
            "- monthly_fixed -> monthly_dates\n"
            "- monthly_n_times -> monthly_range"
        ),
    )
    add_parser.add_argument("name", help="Task name")
    add_parser.add_argument("--category", default="Inbox", help="Task category/group name")
    add_parser.add_argument(
        "--cycle-type",
        default="once",
        choices=CYCLE_TYPES,
        help="Schedule type. monthly_fixed/monthly_n_times are accepted for compatibility.",
    )
    add_parser.add_argument("--time", dest="time_of_day", type=parse_time_of_day, default="09:00", help="Scheduled time in HH:MM")
    add_parser.add_argument("--weekday", type=int, help="Weekday for weekly/monthly quota tasks (Mon=0 .. Sun=6)")
    add_parser.add_argument("--day", dest="day_of_month", type=int, help="Day of month for monthly_fixed alias")
    add_parser.add_argument("--range-start", type=int, help="Start day for monthly_range window")
    add_parser.add_argument("--range-end", type=int, help="End day for monthly_range window")
    add_parser.add_argument("--n-per-month", type=int, help="Monthly quota count for monthly_n_times/monthly_range")
    add_parser.add_argument("--interval-hours", type=int, help="Hour interval for hourly tasks")
    add_parser.add_argument("--dates-list", help="Comma-separated days for monthly_dates, e.g. 1,15,28")
    add_parser.add_argument("--start-date", help="Start date for once tasks or delayed activation (YYYY-MM-DD)")
    add_parser.add_argument("--end-date", help="Optional end date (YYYY-MM-DD)")
    add_parser.add_argument("--reminder-template", help="Custom reminder message template")
    add_parser.add_argument("--task-kind", default="scheduled", help="Task kind, e.g. scheduled or system")
    add_parser.add_argument("--special-handler", help="Special handler name for system/special tasks")
    add_parser.add_argument("--handler-payload", help="JSON/text payload passed to special handler")
    add_parser.add_argument("--system-command", help="System command ID for --task-kind system tasks")

    complete_parser = subparsers.add_parser("complete", help="Complete a task")
    complete_parser.add_argument("identifier")

    complete_overdue_parser = subparsers.add_parser(
        "complete-overdue",
        help="Complete today's overdue scheduled tasks",
        description="Complete overdue tasks from today. Use --system-only to limit processing to system tasks.",
    )
    complete_overdue_parser.add_argument("--dry-run", action="store_true", help="Show what would be completed without changing state")
    complete_overdue_parser.add_argument("--system-only", action="store_true", help="Only process overdue system tasks and skip regular scheduled/legacy tasks")
    complete_overdue_parser.add_argument("--now", dest="now_override", help="Testing override for current timestamp (YYYY-MM-DDTHH:MM)")

    skip_parser = subparsers.add_parser("skip", help="Skip a task")
    skip_parser.add_argument("identifier")

    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("identifier")

    return parser


def parse_entry_identifier(identifier: str) -> int:
    normalized = identifier.strip()
    if normalized.upper().startswith('ID'):
        normalized = normalized[2:]
    return int(normalized)


def parse_compact_end_date(date_str: str) -> str | None:
    if len(date_str) == 8:
        year = int(date_str[:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
    elif len(date_str) == 6:
        year = 2000 + int(date_str[:2])
        month = int(date_str[2:4])
        day = int(date_str[4:6])
    else:
        return None

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def get_periodic_pending(include_skipped: bool = False):
    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    allowed_statuses = ['pending', 'reminded']
    if include_skipped:
        allowed_statuses.append('skipped')
    placeholders = ', '.join('?' for _ in allowed_statuses)
    cur.execute(
        f"""
        SELECT t.id as task_id, t.name, t.category, t.cycle_type,
               o.id as occ_id, o.date, o.status, o.scheduled_time
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON o.task_id = t.id
        WHERE o.status IN ({placeholders})
        ORDER BY o.date, COALESCE(o.scheduled_time, t.time_of_day), t.name
        """,
        allowed_statuses,
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_simple_pending(include_skipped: bool = False):
    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    allowed_statuses = ['pending', 'in_progress']
    if include_skipped:
        allowed_statuses.append('skipped')
    placeholders = ', '.join('?' for _ in allowed_statuses)
    cur.execute(
        f"""
        SELECT e.id, e.text, e.status, g.name as group_name
        FROM entries e
        LEFT JOIN groups g ON e.group_id = g.id
        WHERE e.status IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM periodic_tasks t
              WHERE t.legacy_entry_id = e.id
          )
        ORDER BY e.id
        """,
        allowed_statuses,
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def ensure_today_occurrences() -> None:
    try:
        subprocess.run([PYTHON_BIN, str(MANAGER_SCRIPT), '--ensure-today'], capture_output=True, text=True, timeout=10)
    except Exception as e:
        print(f"⚠️  生成今日任务失败: {e}")


def extract_scheduled_time(text: str) -> str | None:
    match = TIME_PATTERN.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def is_recurring_legacy_entry(text: str) -> bool:
    return any(pattern.search(text) for pattern in RECURRING_ENTRY_PATTERNS)


def is_meta_review_entry(text: str) -> bool:
    return bool(META_REVIEW_PATTERN.search(text))


def get_overdue_periodic_tasks(now: datetime | None = None, *, system_only: bool = False) -> list[dict]:
    now = now or datetime.now()
    today = now.date().isoformat()
    current_time = now.strftime('%H:%M')

    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    task_kind_filter = "AND COALESCE(t.task_kind, 'scheduled') = 'system'" if system_only else ""
    cur.execute(
        f"""
        SELECT o.id, t.id, t.name, o.date, t.cycle_type,
               COALESCE(o.scheduled_time, t.time_of_day), o.status, t.special_handler, t.handler_payload
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON o.task_id = t.id
        WHERE o.date = ?
          AND o.status IN ('pending', 'reminded')
          AND COALESCE(o.scheduled_time, t.time_of_day) IS NOT NULL
          AND COALESCE(o.scheduled_time, t.time_of_day) != ''
          AND COALESCE(o.scheduled_time, t.time_of_day) <= ?
          {task_kind_filter}
        ORDER BY COALESCE(o.scheduled_time, t.time_of_day), t.name, o.id
        """,
        (today, current_time),
    )
    rows = [
        {
            'identifier': f'FIN-{occ_id}',
            'occurrence_id': occ_id,
            'task_id': task_id,
            'name': name,
            'date': occ_date,
            'cycle_type': cycle_type,
            'time_of_day': scheduled_time,
            'status': status,
            'special_handler': special_handler,
            'handler_payload': handler_payload,
        }
        for occ_id, task_id, name, occ_date, cycle_type, scheduled_time, status, special_handler, handler_payload in cur.fetchall()
    ]
    conn.close()
    return rows


def get_overdue_legacy_entries(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    current_time = now.strftime('%H:%M')

    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.id, e.text, e.status, g.name as group_name
        FROM entries e
        LEFT JOIN groups g ON e.group_id = g.id
        WHERE e.status IN ('pending', 'in_progress')
          AND NOT EXISTS (
              SELECT 1 FROM periodic_tasks t
              WHERE t.legacy_entry_id = e.id
          )
        ORDER BY e.id
        """
    )

    results = []
    for entry_id, text, status, group_name in cur.fetchall():
        scheduled_time = extract_scheduled_time(text)
        if not scheduled_time:
            continue
        if scheduled_time > current_time:
            continue
        if not (is_recurring_legacy_entry(text) or is_meta_review_entry(text)):
            continue

        is_system_legacy = (group_name or '').strip().lower() == 'system'
        task_kind = 'system' if is_system_legacy else 'scheduled'
        special_handler = 'meta_review_fallback' if (is_system_legacy and is_meta_review_entry(text)) else None

        results.append(
            {
                'identifier': f'ID{entry_id}',
                'entry_id': entry_id,
                'text': text,
                'group_name': group_name or 'Inbox',
                'scheduled_time': scheduled_time,
                'status': status,
                'special_handler': special_handler,
                'task_kind': task_kind,
            }
        )
    conn.close()
    return results


def count_pending_predictions() -> int:
    predictions_file = WORKSPACE / 'PREDICTIONS.md'
    if not predictions_file.exists():
        return 0
    content = predictions_file.read_text(encoding='utf-8')
    active_section = re.search(r'## Active Predictions\n(.*?)(?:\n## |\Z)', content, re.DOTALL)
    if not active_section:
        return 0
    return active_section.group(1).count('### ')


def count_active_friction_conflicts() -> int:
    friction_file = WORKSPACE / 'FRICTION.md'
    if not friction_file.exists():
        return 0
    content = friction_file.read_text(encoding='utf-8')
    active_section = re.search(r'## Active Conflicts\n(.*?)(?:\n## |\Z)', content, re.DOTALL)
    if not active_section:
        return 0
    return len(re.findall(r'^### ', active_section.group(1), re.MULTILINE))


def append_memory_log(line: str, log_date: date | None = None) -> None:
    log_date = log_date or datetime.now().date()
    memory_dir = WORKSPACE / 'memory'
    memory_dir.mkdir(parents=True, exist_ok=True)
    log_path = memory_dir / f'{log_date.isoformat()}.md'
    existing = log_path.read_text(encoding='utf-8') if log_path.exists() else ''
    if line in existing:
        return
    prefix = '' if existing.endswith('\n') or not existing else '\n'
    log_path.write_text(existing + prefix + line + '\n', encoding='utf-8')


def run_meta_review_fallback(task_text: str, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    pending_predictions = count_pending_predictions()
    active_conflicts = count_active_friction_conflicts()
    note = (
        f"- {now.strftime('%H:%M')} Meta-Review fallback completed via direct PREDICTIONS.md/FRICTION.md inspection "
        f"for overdue task: {task_text}. Pending predictions: {pending_predictions}; active conflicts: {active_conflicts}."
    )
    append_memory_log(note, log_date=now.date())
    return True, note


def run_sync_subagent_memory(handler_payload: str | None, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    memory_manager = WORKSPACE / 'scripts' / 'memory_manager.py'
    if not memory_manager.exists():
        return False, f"❌ memory_manager.py 不存在: {memory_manager}"

    filter_keyword = ':subagent:'
    sync_targets: list[str] = []
    synced_counts: dict[str, int] = {}
    payload = {}
    if handler_payload:
        try:
            payload = json.loads(handler_payload)
        except json.JSONDecodeError:
            payload = {}
        filter_keyword = payload.get('session_filter', filter_keyword)

    try:
        result = subprocess.run([PYTHON_BIN, str(memory_manager), 'pending-subagents'], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return False, f"❌ 读取 subagent ledger 失败: {result.stderr or result.stdout}"
        pending_entries = json.loads(result.stdout or '[]')
    except Exception as exc:
        return False, f"❌ 读取 subagent ledger 失败: {exc}"

    for entry in pending_entries:
        if not isinstance(entry, dict):
            continue
        session_id = entry.get('session_id')
        if not looks_like_subagent_session(session_id):
            continue
        if filter_keyword and filter_keyword not in session_id:
            continue
        sync_targets.append(session_id)
        sync_result = subprocess.run([PYTHON_BIN, str(memory_manager), 'sync', session_id], capture_output=True, text=True, timeout=20)
        if sync_result.returncode != 0:
            subprocess.run(
                [PYTHON_BIN, str(memory_manager), 'mark-subagent-failed', session_id, (sync_result.stderr or sync_result.stdout or '').strip()[:500]],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return False, f"❌ 同步 {session_id} 失败: {sync_result.stderr or sync_result.stdout}"
        match = re.search(r'Synced\s+(\d+)\s+memories', sync_result.stdout)
        synced_count = int(match.group(1)) if match else 0
        synced_counts[session_id] = synced_count
        mark_result = subprocess.run(
            [PYTHON_BIN, str(memory_manager), 'mark-subagent-synced', session_id, str(synced_count)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if mark_result.returncode != 0:
            return False, f"❌ 标记 {session_id} 已同步失败: {mark_result.stderr or mark_result.stdout}"

    total_synced = sum(synced_counts.values())
    note = f"- {now.strftime('%H:%M')} Subagent memory sync completed. sessions={len(sync_targets)} total_memories={total_synced} details={json.dumps(synced_counts, ensure_ascii=False)}"
    append_memory_log(note, log_date=now.date())
    return True, note


def run_system_command(handler_payload: str | None) -> tuple[bool, str]:
    if not handler_payload:
        return True, "system command task had no command payload"
    try:
        execution = execute_system_handler(handler_payload, timeout_seconds=600)
    except ValueError as exc:
        return False, f"blocked={exc}"
    except Exception as exc:  # pragma: no cover - external command failure path
        return False, f"error={exc}"
    message = f"command_id={execution['command_id']}; exit_code={execution['exit_code']}"
    if execution.get("output"):
        message = f"{message}; output={execution['output']}"
    return bool(execution.get("ok")), message


def run_special_handler(handler_name: str | None, handler_payload: str | None, task_text: str, now: datetime | None = None) -> tuple[bool, str]:
    if not handler_name:
        return True, ''
    if handler_name == 'meta_review_fallback':
        return run_meta_review_fallback(task_text, now=now)
    if handler_name == 'sync_subagent_memory':
        return run_sync_subagent_memory(handler_payload, now=now)
    if handler_name == 'run_command':
        return run_system_command(handler_payload)
    return False, f"❌ 不支持的 special_handler: {handler_name}"


def build_merged_special_handler_result(
    base_result: str,
    *,
    identifier: str,
    name: str,
    scheduled_time: str | None,
    occurrence_date: str | None,
    merge_key: str,
    merged_count: int,
    merged_index: int,
) -> str:
    schedule_label = scheduled_time or '无时间'
    date_label = occurrence_date or '未知日期'
    merge_suffix = (
        f" [merged occurrence {merged_index}/{merged_count}; merge_key={merge_key}; "
        f"source={identifier} {name} @ {date_label} {schedule_label}]"
    )
    if merge_suffix in base_result:
        return base_result
    return f"{base_result}{merge_suffix}"


def complete_periodic_occurrence(
    occ_id: int,
    *,
    completion_mode: str = 'manual',
    special_handler_result: str | None = None,
    completion_source: str = 'manual_cli',
    trigger_label: str | None = None,
    trigger_command: str | None = None,
    allow_auto_for_scheduled: bool = False,
) -> tuple[bool, str]:
    conn = sqlite3.connect(str(TODO_DB))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT o.task_id, o.status, COALESCE(t.task_kind, 'scheduled'), o.date, o.reminder_job_id, o.execution_job_id
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON o.task_id = t.id
            WHERE o.id = ?
            """,
            (occ_id,),
        )
        row = cur.fetchone()
        if not row:
            return False, f"❌ 未找到 FIN-{occ_id}"

        task_id, current_status, task_kind, occurrence_date, reminder_job_id, execution_job_id = row
        if current_status == 'skipped':
            return False, f"❌ 无法完成已跳过的任务 FIN-{occ_id}"
        if current_status == 'completed':
            return True, f"⚠️  FIN-{occ_id} 已完成"

        if completion_source != 'manual_cli' and task_kind != 'system' and not allow_auto_for_scheduled:
            return False, f"❌ 自动路径拒绝处理非 system 任务 FIN-{occ_id} (task_kind={task_kind})"

        occurrence_columns = {info[1] for info in cur.execute("PRAGMA table_info(periodic_occurrences)").fetchall()}
        extra_assignments = []
        extra_params: list[str | None] = []
        if 'completion_source' in occurrence_columns:
            extra_assignments.append("completion_source = ?")
            extra_params.append(completion_source)
        if 'trigger_label' in occurrence_columns:
            extra_assignments.append("trigger_label = COALESCE(?, trigger_label)")
            extra_params.append(trigger_label)
        if 'trigger_command' in occurrence_columns:
            extra_assignments.append("trigger_command = COALESCE(?, trigger_command)")
            extra_params.append(trigger_command)

        update_sql = """
            UPDATE periodic_occurrences
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                completion_mode = ?, special_handler_result = COALESCE(?, special_handler_result)
        """
        if extra_assignments:
            update_sql += ",\n                " + ",\n                ".join(extra_assignments)
        update_sql += "\n            WHERE id = ?\n            "
        cur.execute(update_sql, (completion_mode, special_handler_result, *extra_params, occ_id))
        remove_job(reminder_job_id)
        remove_job(execution_job_id)
        if reminder_job_id or execution_job_id:
            cur.execute("UPDATE periodic_occurrences SET reminder_job_id = NULL, execution_job_id = NULL WHERE id = ?", (occ_id,))
        task_columns = {row[1] for row in cur.execute("PRAGMA table_info(periodic_tasks)").fetchall()}
        select_columns = ['cycle_type']
        if 'n_per_month' in task_columns:
            select_columns.append('n_per_month')
        if 'count_current_month' in task_columns:
            select_columns.append('count_current_month')
        if 'range_start' in task_columns:
            select_columns.append('range_start')
        if 'range_end' in task_columns:
            select_columns.append('range_end')
        column_index = {name: idx for idx, name in enumerate(select_columns)}
        cur.execute(f"SELECT {', '.join(select_columns)} FROM periodic_tasks WHERE id = ?", (task_id,))
        cycle_type_row = cur.fetchone()
        cycle_type = cycle_type_row[0] if cycle_type_row else None
        n_per_month = cycle_type_row[column_index['n_per_month']] if cycle_type_row and 'n_per_month' in column_index else None
        if cycle_type in ('monthly_n_times', 'monthly_range') and n_per_month is not None:
            if 'count_current_month' in task_columns:
                cur.execute("UPDATE periodic_tasks SET count_current_month = count_current_month + 1 WHERE id = ?", (task_id,))
            try:
                quota_limit = int(n_per_month)
            except (TypeError, ValueError):
                quota_limit = 0
            range_start = cycle_type_row[column_index['range_start']] if cycle_type_row and 'range_start' in column_index else None
            range_end = cycle_type_row[column_index['range_end']] if cycle_type_row and 'range_end' in column_index else None
            occ_day = date.fromisoformat(occurrence_date) if occurrence_date else datetime.now().date()
            window = resolve_monthly_quota_window(
                cycle_type=cycle_type or '',
                target_day=occ_day,
                range_start=range_start,
                range_end=range_end,
            )
            if quota_limit > 0 and window:
                win_start, win_end = window
                cur.execute(
                    """
                    SELECT COUNT(1)
                    FROM periodic_occurrences
                    WHERE task_id = ? AND status = 'completed'
                      AND date >= ? AND date <= ?
                    """,
                    (task_id, win_start.isoformat(), win_end.isoformat()),
                )
                completed_count = int((cur.fetchone() or [0])[0] or 0)
                if completed_count >= quota_limit:
                    quota_sql = """
                        UPDATE periodic_occurrences
                        SET status = 'completed', is_auto_completed = 1,
                            completion_mode = COALESCE(completion_mode, 'auto_quota')
                    """
                    if 'completion_source' in occurrence_columns:
                        quota_sql += ",\n                            completion_source = COALESCE(completion_source, 'quota')"
                    if 'trigger_label' in occurrence_columns:
                        quota_sql += ",\n                            trigger_label = COALESCE(trigger_label, 'monthly_quota')"
                    if 'trigger_command' in occurrence_columns:
                        quota_sql += ",\n                            trigger_command = COALESCE(trigger_command, 'complete_periodic_occurrence')"
                    quota_sql += """
                        WHERE task_id = ? AND status IN ('pending', 'reminded')
                          AND date >= ? AND date <= ?
                    """
                    cur.execute(quota_sql, (task_id, win_start.isoformat(), win_end.isoformat()))
                    cur.execute(
                        """
                        SELECT reminder_job_id, execution_job_id, COALESCE(t.task_kind, 'scheduled')
                        FROM periodic_occurrences o
                        JOIN periodic_tasks t ON t.id = o.task_id
                        WHERE o.task_id = ? AND o.status = 'completed'
                          AND o.date >= ? AND o.date <= ?
                          AND (o.reminder_job_id IS NOT NULL OR o.execution_job_id IS NOT NULL)
                        """,
                        (task_id, win_start.isoformat(), win_end.isoformat()),
                    )
                    for quota_reminder_job_id, quota_execution_job_id, quota_task_kind in cur.fetchall():
                        remove_job(quota_reminder_job_id)
                        remove_job(quota_execution_job_id)
                    cur.execute(
                        """
                        UPDATE periodic_occurrences
                        SET reminder_job_id = NULL, execution_job_id = NULL
                        WHERE task_id = ? AND status = 'completed'
                          AND date >= ? AND date <= ?
                        """,
                        (task_id, win_start.isoformat(), win_end.isoformat()),
                    )

        conn.commit()
    finally:
        conn.close()

    return True, f"✅ 已完成 FIN-{occ_id}（任务ID {task_id}）"


def get_entry_archive_state(cur: sqlite3.Cursor, entry_id: int) -> dict | None:
    columns = get_entry_columns(cur.connection)
    expressions = legacy_archive_select_expressions(columns)
    cur.execute(
        f"SELECT status, {expressions['chronos_readonly']} AS chronos_readonly, {expressions['chronos_archived_at']} AS chronos_archived_at, {expressions['chronos_archived_from_status']} AS chronos_archived_from_status, {expressions['chronos_linked_task_id']} AS chronos_linked_task_id, {expressions['chronos_archive_reason']} AS chronos_archive_reason FROM entries WHERE id = ?",
        (entry_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = {description[0]: row[index] for index, description in enumerate(cur.description)}
    return build_entry_archive_state(data)


def complete_legacy_entry(entry_id: int) -> tuple[bool, str]:
    conn = sqlite3.connect(str(TODO_DB))
    try:
        cur = conn.cursor()
        state = get_entry_archive_state(cur, entry_id)
        if not state:
            return False, f"❌ 未找到 ID {entry_id}"

        current_status = state['status']
        if state['is_archived']:
            return False, archive_block_message(entry_id, state)
        if current_status == 'skipped':
            return False, f"❌ 无法完成已跳过的任务 ID {entry_id}"
        if current_status == 'done':
            return True, f"⚠️  ID {entry_id} 已完成"

        cur.execute("UPDATE entries SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (entry_id,))
        conn.commit()
        return True, f"✅ 已完成任务 ID {entry_id}"
    finally:
        conn.close()


def complete_identifier(identifier: str) -> tuple[bool, str]:
    if identifier.startswith('FIN-'):
        return complete_periodic_occurrence(
            int(identifier[4:]),
            completion_source='manual_cli',
            trigger_label='manual_complete',
            trigger_command='todo.py complete',
            allow_auto_for_scheduled=True,
        )
    return complete_legacy_entry(parse_entry_identifier(identifier))


def complete_overdue_tasks(now: datetime | None = None, dry_run: bool = False, system_only: bool = False) -> dict:
    now = now or datetime.now()
    ensure_today_occurrences()

    periodic = get_overdue_periodic_tasks(now, system_only=system_only)
    legacy = [] if system_only else get_overdue_legacy_entries(now)

    completed: list[str] = []
    simulated: list[str] = []
    errors: list[str] = []
    handled = []
    special_handler_cache: dict[tuple[str, int, str], tuple[bool, str]] = {}

    for task in periodic:
        handled.append(task['identifier'])
        merge_key = None
        if task.get('special_handler') and task.get('cycle_type') == 'hourly' and task.get('date'):
            merge_key = (task['special_handler'], task['task_id'], task['date'])
        if dry_run:
            label = f"{task['identifier']} {task['name']} @ {task['time_of_day']}"
            if task.get('special_handler'):
                label += f" [{task['special_handler']}]"
                if merge_key:
                    merged_total = sum(
                        1
                        for candidate in periodic
                        if candidate.get('special_handler') == task.get('special_handler')
                        and candidate.get('cycle_type') == 'hourly'
                        and candidate.get('task_id') == task.get('task_id')
                        and candidate.get('date') == task.get('date')
                    )
                    if merged_total > 1:
                        label += f" [merge-once day-batch x{merged_total}]"
            simulated.append(label)
            continue

        completion_mode = 'auto_overdue'
        special_result = None
        if task.get('special_handler'):
            if merge_key:
                cached = special_handler_cache.get(merge_key)
                if cached is None:
                    cached = run_special_handler(task.get('special_handler'), task.get('handler_payload'), task['name'], now=now)
                    special_handler_cache[merge_key] = cached
                    if cached[0]:
                        merged_total = sum(
                            1
                            for candidate in periodic
                            if candidate.get('special_handler') == task.get('special_handler')
                            and candidate.get('cycle_type') == 'hourly'
                            and candidate.get('task_id') == task.get('task_id')
                            and candidate.get('date') == task.get('date')
                        )
                        if merged_total > 1:
                            completed.append(
                                f"📝 {cached[1]} [hourly merge-once applied for task_id={task['task_id']} date={task['date']} occurrences={merged_total}]"
                            )
                        else:
                            completed.append(f"📝 {cached[1]}")
                ok, message = cached
                if not ok:
                    errors.append(message)
                    continue
                merged_tasks = [
                    candidate for candidate in periodic
                    if candidate.get('special_handler') == task.get('special_handler')
                    and candidate.get('cycle_type') == 'hourly'
                    and candidate.get('task_id') == task.get('task_id')
                    and candidate.get('date') == task.get('date')
                ]
                merged_total = len(merged_tasks)
                merged_index = next(
                    (index for index, candidate in enumerate(merged_tasks, start=1) if candidate['occurrence_id'] == task['occurrence_id']),
                    1,
                )
                merge_key_label = f"{task['special_handler']}:{task['task_id']}:{task['date']}"
                special_result = build_merged_special_handler_result(
                    message,
                    identifier=task['identifier'],
                    name=task['name'],
                    scheduled_time=task.get('time_of_day'),
                    occurrence_date=task.get('date'),
                    merge_key=merge_key_label,
                    merged_count=merged_total,
                    merged_index=merged_index,
                )
                completion_mode = 'fallback_handler_merged'
            else:
                ok, message = run_special_handler(task.get('special_handler'), task.get('handler_payload'), task['name'], now=now)
                if not ok:
                    errors.append(message)
                    continue
                special_result = message
                completion_mode = 'fallback_handler'
                completed.append(f"📝 {message}")

        ok, message = complete_periodic_occurrence(
            task['occurrence_id'],
            completion_mode=completion_mode,
            special_handler_result=special_result,
            completion_source='handler' if task.get('special_handler') else 'cron_full',
            trigger_label=task.get('special_handler') or ('complete_overdue_system' if system_only else 'complete_overdue'),
            trigger_command='todo.py complete-overdue --system-only' if system_only else 'todo.py complete-overdue',
            allow_auto_for_scheduled=False,
        )
        if ok:
            completed.append(message)
        else:
            errors.append(message)

    for entry in legacy:
        handled.append(entry['identifier'])
        if entry.get('task_kind') != 'system':
            if dry_run:
                simulated.append(f"{entry['identifier']} {entry['text']} @ {entry['scheduled_time']} [blocked non-system legacy]")
            else:
                errors.append(f"❌ 自动路径拒绝处理非 system 任务 {entry['identifier']} (legacy recurring entry)")
            continue
        if dry_run:
            label = entry['text']
            if entry.get('special_handler') == 'meta_review_fallback':
                label += ' [meta-review-fallback]'
            simulated.append(f"{entry['identifier']} {label} @ {entry['scheduled_time']}")
            continue

        if entry.get('special_handler') == 'meta_review_fallback':
            ok, message = run_meta_review_fallback(entry['text'], now=now)
            if ok:
                completed.append(f"📝 {message}")
            else:
                errors.append(message)
                continue

        ok, message = complete_legacy_entry(entry['entry_id'])
        if ok:
            completed.append(message)
        else:
            errors.append(message)

    return {
        'now': now,
        'periodic': periodic,
        'legacy': legacy,
        'handled': handled,
        'completed': completed,
        'simulated': simulated,
        'errors': errors,
    }


def cmd_list(include_skipped: bool = False):
    ensure_today_occurrences()

    periodic = get_periodic_pending(include_skipped=include_skipped)
    simple = get_simple_pending(include_skipped=include_skipped)

    print("=== Chronos Todo List ===\n")

    if periodic:
        print("【周期任务】")
        for task_id, name, category, cycle_type, occ_id, date_str, status, scheduled_time in periodic:
            display_status = "已跳过" if status == 'skipped' else status
            time_suffix = f" @ {scheduled_time}" if scheduled_time else ''
            print(f"  [FIN-{occ_id}] {date_str}{time_suffix} | {name} ({cycle_type}) | {display_status}")
        print()

    if simple:
        print("【其他任务】")
        for entry_id, text, status, group_name in simple:
            display_status = "已跳过" if status == 'skipped' else status
            group = group_name or 'Inbox'
            print(f"  [ID{entry_id}] {group} | {text} | {display_status}")
        print()

    if include_skipped:
        print("（已包含 skipped 项）")

    if not periodic and not simple:
        if include_skipped:
            print("✅ 没有待办或已跳过任务。")
        else:
            print("✅ 没有待办任务。")


def build_periodic_manager_args(text: str, *, category: str, cycle_type: str, kwargs: dict[str, object]) -> list[str]:
    args = [
        PYTHON_BIN,
        str(MANAGER_SCRIPT),
        '--add',
        '--name', text,
        '--category', category,
        '--cycle-type', cycle_type,
        '--time', str(kwargs.get('time', '09:00')),
    ]
    option_specs: list[tuple[str, str]] = [
        ('weekday', '--weekday'),
        ('day_of_month', '--day'),
        ('n_per_month', '--n-per-month'),
        ('interval_hours', '--interval-hours'),
        ('dates_list', '--dates-list'),
        ('start_date', '--start-date'),
        ('end_date', '--end-date'),
        ('reminder_template', '--reminder-template'),
        ('task_kind', '--task-kind'),
        ('special_handler', '--special-handler'),
        ('handler_payload', '--handler-payload'),
        ('system_command', '--system-command'),
    ]
    for key, flag in option_specs:
        value = kwargs.get(key)
        if value not in (None, ''):
            args.extend([flag, str(value)])
    range_start = kwargs.get('range_start')
    range_end = kwargs.get('range_end')
    if range_start is not None and range_end is not None:
        args.extend(['--range-start', str(range_start), '--range-end', str(range_end)])
    return args


def cmd_add(text, category='Inbox', cycle_type='once', **kwargs):
    is_scheduled_once = cycle_type == 'once' and kwargs.get('start_date')
    is_scheduled_recurring = cycle_type != 'once'
    if is_scheduled_once or is_scheduled_recurring:
        args = build_periodic_manager_args(text, category=category, cycle_type=cycle_type, kwargs=kwargs)

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ 已添加周期任务：{text}")
        else:
            print(f"❌ 添加失败：{result.stderr or result.stdout}")
    else:
        try:
            conn = sqlite3.connect(str(TODO_DB))
            cur = conn.cursor()
            cur.execute("SELECT id FROM groups WHERE name = ?", (category,))
            row = cur.fetchone()
            if row:
                group_id = row[0]
            else:
                cur.execute("INSERT INTO groups (name) VALUES (?)", (category,))
                group_id = cur.lastrowid
                conn.commit()

            cur.execute(
                """
                INSERT INTO entries (text, status, group_id, created_at, updated_at)
                VALUES (?, 'pending', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (text, group_id),
            )
            conn.commit()
            entry_id = cur.lastrowid
            conn.close()
            print(f"✅ 已添加任务 ID {entry_id}: {text}")
        except Exception as e:
            print(f"❌ 添加失败：{e}")


def cmd_complete(identifier):
    ok, message = complete_identifier(identifier)
    print(message)
    if not ok:
        return


def cmd_complete_overdue(now_override: str | None = None, dry_run: bool = False, system_only: bool = False):
    now = datetime.strptime(now_override, '%Y-%m-%dT%H:%M') if now_override else datetime.now()
    result = complete_overdue_tasks(now=now, dry_run=dry_run, system_only=system_only)

    print(f"=== Overdue Completion @ {result['now'].strftime('%Y-%m-%d %H:%M')} ===")
    if dry_run:
        if result['simulated']:
            for item in result['simulated']:
                print(f"DRY-RUN {item}")
        else:
            print("✅ 没有需要补完成的今日逾期任务。")
        return

    for message in result['completed']:
        print(message)
    for message in result['errors']:
        print(message)
    if not result['completed'] and not result['errors']:
        print("✅ 没有需要补完成的今日逾期任务。")


def cmd_skip(identifier):
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        try:
            conn = sqlite3.connect(str(TODO_DB))
            cur = conn.cursor()

            cur.execute("SELECT task_id, date FROM periodic_occurrences WHERE id = ?", (occ_id,))
            row = cur.fetchone()
            if not row:
                print(f"❌ 未找到 FIN-{occ_id}")
                conn.close()
                return

            cur.execute("SELECT status, reminder_job_id, execution_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
            current_status, job_name, execution_job_id = cur.fetchone()
            if current_status == 'skipped':
                print(f"⚠️  FIN-{occ_id} 已经是跳过状态")
                conn.close()
                return

            cur.execute(
                "UPDATE periodic_occurrences SET status = 'skipped', completion_source = COALESCE(completion_source, 'manual_cli'), trigger_label = COALESCE(trigger_label, 'manual_skip'), trigger_command = COALESCE(trigger_command, 'todo.py skip') WHERE id = ?",
                (occ_id,),
            )

            if job_name:
                try:
                    remove_job(job_name)
                except Exception:
                    pass
            if execution_job_id:
                try:
                    remove_job(execution_job_id)
                except Exception:
                    pass
            if job_name or execution_job_id:
                cur.execute("UPDATE periodic_occurrences SET reminder_job_id = NULL, execution_job_id = NULL WHERE id = ?", (occ_id,))

            conn.commit()
            conn.close()

            print(f"✅ 已跳过 FIN-{occ_id}（配额不受影响）")
        except Exception as e:
            print(f"❌ 跳过失败：{e}")
    else:
        entry_id = parse_entry_identifier(identifier)
        try:
            conn = sqlite3.connect(str(TODO_DB))
            cur = conn.cursor()
            state = get_entry_archive_state(cur, entry_id)
            if not state:
                print(f"❌ 未找到 ID {entry_id}")
                conn.close()
                return

            current_status = state['status']
            if state['is_archived']:
                print(archive_block_message(entry_id, state))
                conn.close()
                return
            if current_status == 'skipped':
                print(f"⚠️  ID {entry_id} 已经是跳过状态")
                conn.close()
                return

            cur.execute("UPDATE entries SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (entry_id,))
            conn.commit()
            conn.close()
            print(f"✅ 已跳过任务 ID {entry_id}")
        except Exception as e:
            print(f"❌ 跳过失败：{e}")


def cmd_show(identifier):
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        conn = sqlite3.connect(str(TODO_DB))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.name, t.cycle_type, t.special_handler, o.date, o.status,
                   o.reminder_job_id, o.execution_job_id, o.completion_mode, o.special_handler_result, o.scheduled_time,
                   o.completion_source, o.trigger_label, o.trigger_command
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON o.task_id = t.id
            WHERE o.id = ?
            """,
            (occ_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            name, cycle_type, special_handler, date_str, status, job_id, execution_job_id, completion_mode, special_handler_result, scheduled_time, completion_source, trigger_label, trigger_command = row
            print(f"【周期任务】{name}")
            print(f"周期类型：{cycle_type}")
            print(f"日期：{date_str}")
            print(f"时间：{scheduled_time or '无'}")
            print(f"状态：{status}")
            print(f"special_handler：{special_handler or '无'}")
            print(f"completion_mode：{completion_mode or '无'}")
            print(f"completion_source：{completion_source or '无'}")
            print(f"trigger_label：{trigger_label or '无'}")
            print(f"trigger_command：{trigger_command or '无'}")
            print(f"handler_result：{special_handler_result or '无'}")
            print(f"提醒任务：{job_id or '无'}")
            print(f"执行任务：{execution_job_id or '无'}")
        else:
            print(f"❌ 未找到 FIN-{occ_id}")
    else:
        entry_id = parse_entry_identifier(identifier)
        conn = sqlite3.connect(str(TODO_DB))
        cur = conn.cursor()
        columns = get_entry_columns(conn)
        expressions = legacy_archive_select_expressions(columns, table_alias='e')
        cur.execute(
            f"""
            SELECT e.text, e.status, g.name as group_name,
                   {expressions['chronos_readonly']} AS chronos_readonly,
                   {expressions['chronos_archived_at']} AS chronos_archived_at,
                   {expressions['chronos_archived_from_status']} AS chronos_archived_from_status,
                   {expressions['chronos_linked_task_id']} AS chronos_linked_task_id,
                   {expressions['chronos_archive_reason']} AS chronos_archive_reason
            FROM entries e
            LEFT JOIN groups g ON e.group_id = g.id
            WHERE e.id = ?
            """,
            (entry_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            text, status, group_name, chronos_readonly, chronos_archived_at, chronos_archived_from_status, chronos_linked_task_id, chronos_archive_reason = row
            group = group_name or 'Inbox'
            state = build_entry_archive_state({
                'status': status,
                'chronos_readonly': chronos_readonly,
                'chronos_archived_at': chronos_archived_at,
                'chronos_archived_from_status': chronos_archived_from_status,
                'chronos_linked_task_id': chronos_linked_task_id,
                'chronos_archive_reason': chronos_archive_reason,
            })
            print(f"【任务】{text}")
            print(f"分组：{group}")
            print(f"状态：{status}")
            if state['is_archived']:
                print(archive_display_label(state))
                print(f"关联周期任务：{state['chronos_linked_task_id'] or '无'}")
                print(f"归档时间：{state['chronos_archived_at'] or '无'}")
                print(f"归档原因：{state['chronos_archive_reason'] or '无'}")
        else:
            print(f"❌ 未找到 ID {entry_id}")



def build_add_kwargs(
    *,
    category: str,
    cycle_type: str,
    time_of_day: str,
    task_kind: str | None = None,
    weekday: int | None = None,
    day_of_month: int | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
    n_per_month: int | None = None,
    interval_hours: int | None = None,
    dates_list: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    reminder_template: str | None = None,
    special_handler: str | None = None,
    handler_payload: str | None = None,
    system_command: str | None = None,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        'category': category,
        'cycle_type': cycle_type,
        'time': time_of_day,
    }
    if task_kind is not None:
        kwargs['task_kind'] = task_kind
    if weekday is not None:
        kwargs['weekday'] = weekday
    if day_of_month is not None:
        kwargs['day_of_month'] = day_of_month
    if range_start is not None:
        kwargs['range_start'] = range_start
    if range_end is not None:
        kwargs['range_end'] = range_end
    if n_per_month is not None:
        kwargs['n_per_month'] = n_per_month
    if interval_hours is not None:
        kwargs['interval_hours'] = interval_hours
    if dates_list is not None:
        kwargs['dates_list'] = dates_list
    if start_date is not None:
        kwargs['start_date'] = start_date
    if end_date is not None:
        kwargs['end_date'] = end_date
    if reminder_template is not None:
        kwargs['reminder_template'] = reminder_template
    if special_handler is not None:
        kwargs['special_handler'] = special_handler
    if handler_payload is not None:
        kwargs['handler_payload'] = handler_payload
    if system_command is not None:
        kwargs['system_command'] = system_command
    return kwargs

def main():
    if len(sys.argv) < 2:
        print("用法：todo.py [list|add|complete|complete-overdue|skip|show] [参数] 或直接说自然语言")
        print("  list                 - 列出所有待办")
        print("  add <任务名>         - 添加任务（需额外参数指定周期）")
        print("  complete <ID>        - 完成任务")
        print("  complete-overdue     - 补完成今天已过时的计划任务")
        print("  skip <ID>            - 跳过任务（不影响配额）")
        print("  show <ID>            - 查看详情")
        print("自然语言示例：")
        print('  "跳过 FIN-123"          - 跳过周期任务')
        print('  "查询待办"              - 列出所有待办')
        print('  "自动完成逾期待办"      - 补完成今日已过时间的任务')
        sys.exit(1)

    if sys.argv[1] in ('-h', '--help'):
        build_parser().print_help()
        return

    explicit_cmd = sys.argv[1]
    if explicit_cmd in ['list', 'add', 'complete', 'complete-overdue', 'show', 'skip']:
        parser = build_parser()
        args = parser.parse_args()

        if args.command == 'list':
            cmd_list(include_skipped=args.include_skipped)
        elif args.command == 'add':
            try:
                validate_add_args(args)
            except ValueError as exc:
                print(f"参数错误：{exc}")
                sys.exit(2)

            kwargs = build_add_kwargs(
                category=args.category,
                cycle_type=args.cycle_type,
                time_of_day=args.time_of_day,
                task_kind=args.task_kind,
                weekday=args.weekday,
                day_of_month=args.day_of_month,
                range_start=args.range_start,
                range_end=args.range_end,
                n_per_month=args.n_per_month,
                interval_hours=args.interval_hours,
                dates_list=args.dates_list,
                start_date=args.start_date,
                end_date=args.end_date,
                reminder_template=args.reminder_template,
                special_handler=args.special_handler,
                handler_payload=args.handler_payload,
                system_command=args.system_command,
            )

            cmd_add(args.name, **kwargs)
        elif args.command == 'skip':
            cmd_skip(args.identifier)
        elif args.command == 'complete':
            cmd_complete(args.identifier)
        elif args.command == 'complete-overdue':
            cmd_complete_overdue(now_override=args.now_override, dry_run=args.dry_run, system_only=args.system_only)
        elif args.command == 'show':
            cmd_show(args.identifier)
    else:
        nl_text = ' '.join(sys.argv[1:])
        parsed = parse_natural_language(nl_text, parse_compact_end_date=parse_compact_end_date)
        if parsed['cmd'] == 'unknown':
            print(f"无法识别的指令：{nl_text}")
            print("支持的指令：添加待办、查询待办、完成任务、跳过任务、查看详情、自动完成逾期任务")
            sys.exit(1)
        elif parsed['cmd'] == 'list':
            cmd_list()
        elif parsed['cmd'] == 'complete-overdue':
            cmd_complete_overdue()
        elif parsed['cmd'] == 'skip':
            if parsed.get('identifier'):
                cmd_skip(parsed['identifier'])
            else:
                print("请指定要跳过的任务 ID（如 FIN-123 或 45）")
                sys.exit(1)
        elif parsed['cmd'] == 'complete':
            if parsed.get('identifier'):
                cmd_complete(parsed['identifier'])
            else:
                print("请指定要完成的任务 ID（如 FIN-123 或 45）")
                sys.exit(1)
        elif parsed['cmd'] == 'show':
            if parsed.get('identifier'):
                cmd_show(parsed['identifier'])
            else:
                print("请指定要查看的任务 ID")
                sys.exit(1)
        elif parsed['cmd'] == 'add':
            name = parsed.get('name', '新任务')
            category = parsed.get('category', 'Inbox')
            cycle_type = parsed.get('cycle_type', 'once')
            time_of_day = parsed.get('time_of_day', '09:00')
            weekday = parsed.get('weekday')
            day_of_month = parsed.get('day_of_month')
            range_start = parsed.get('range_start')
            range_end = parsed.get('range_end')
            n_per_month = parsed.get('n_per_month')
            interval_hours = parsed.get('interval_hours')
            end_date = parsed.get('end_date')

            print(f"🔍 解析结果：名称={name}, 周期={cycle_type}, 时间={time_of_day}, 星期={weekday}, 日期={day_of_month}, 区间={range_start}-{range_end}, 次数={n_per_month}, 间隔小时={interval_hours}, 结束={end_date}")

            kwargs = build_add_kwargs(
                category=category,
                cycle_type=cycle_type,
                time_of_day=time_of_day,
                weekday=weekday,
                day_of_month=day_of_month,
                range_start=range_start,
                range_end=range_end,
                n_per_month=n_per_month,
                interval_hours=interval_hours,
                end_date=end_date,
            )

            cmd_add(name, **kwargs)


if __name__ == "__main__":
    main()
