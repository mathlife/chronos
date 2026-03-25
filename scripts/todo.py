#!/usr/bin/env python3
"""
Unified Todo - 统一待办管理入口
支持：list/add/complete/show
自动路由：周期任务 → periodic_task_manager，其他 → 直接操作 entries 表
自然语言解析：支持中文指令
"""
import argparse
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from core.paths import OPENCLAW_BIN, PYTHON_BIN, SCRIPTS_DIR, TODO_DB, WORKSPACE

MANAGER_SCRIPT = SCRIPTS_DIR / 'periodic_task_manager.py'
CYCLE_TYPES = ['once', 'daily', 'weekly', 'monthly_fixed', 'monthly_range', 'monthly_n_times']
TIME_PATTERN = re.compile(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)')
META_REVIEW_PATTERN = re.compile(r'meta[- ]?review|meta_auditor\.py', re.IGNORECASE)
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
    parser = argparse.ArgumentParser(
        description="Chronos unified todo",
        add_help=True,
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all pending tasks")

    add_parser = subparsers.add_parser("add", help="Add a task")
    add_parser.add_argument("name", help="Task name")
    add_parser.add_argument("--category", default="Inbox")
    add_parser.add_argument("--cycle-type", default="once", choices=CYCLE_TYPES)
    add_parser.add_argument("--time", dest="time_of_day", type=parse_time_of_day, default="09:00")
    add_parser.add_argument("--weekday", type=int)
    add_parser.add_argument("--day", dest="day_of_month", type=int)
    add_parser.add_argument("--range-start", type=int)
    add_parser.add_argument("--range-end", type=int)
    add_parser.add_argument("--n-per-month", type=int)
    add_parser.add_argument("--end-date")
    add_parser.add_argument("--reminder-template")

    complete_parser = subparsers.add_parser("complete", help="Complete a task")
    complete_parser.add_argument("identifier")

    complete_overdue_parser = subparsers.add_parser("complete-overdue", help="Complete today's overdue scheduled tasks")
    complete_overdue_parser.add_argument("--dry-run", action="store_true", help="Show what would be completed without changing state")
    complete_overdue_parser.add_argument("--now", dest="now_override", help="Testing override for current timestamp (YYYY-MM-DDTHH:MM)")

    skip_parser = subparsers.add_parser("skip", help="Skip a task")
    skip_parser.add_argument("identifier")

    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("identifier")

    return parser


def parse_entry_identifier(identifier: str) -> int:
    """Accept plain numeric IDs and legacy ID-prefixed forms."""
    normalized = identifier.strip()
    if normalized.upper().startswith('ID'):
        normalized = normalized[2:]
    return int(normalized)


def parse_compact_end_date(date_str: str) -> str | None:
    """Parse YYYYMMDD or YYMMDD compact end-date formats."""
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


def parse_natural_language(text: str) -> dict:
    """解析自然语言指令，返回命令和参数"""
    text = text.strip()

    if re.search(r'逾期|过时|已过时间', text) and re.search(r'完成|补完成|自动完成', text):
        return {'cmd': 'complete-overdue'}

    # 查询命令
    if re.search(r'查询|查看|今日|待办|任务', text) and not re.search(r'添加|新增|创建', text):
        if '详情' in text or re.search(r'FIN-\d+|ID\d+', text):
            match = re.search(r'(FIN-\d+|ID\d+)', text)
            if match:
                return {'cmd': 'show', 'identifier': match.group(1)}
        else:
            return {'cmd': 'list'}

    # 跳过命令
    if re.search(r'跳过|跳過|skipping?', text):
        match = re.search(r'(FIN-\d+|ID\d+)', text)
        if match:
            return {'cmd': 'skip', 'identifier': match.group(1)}
        return {'cmd': 'skip', 'identifier': None}

    # 完成命令
    if re.search(r'完成|标记完成', text):
        match = re.search(r'(FIN-\d+|ID\d+)', text)
        if match:
            return {'cmd': 'complete', 'identifier': match.group(1)}
        return {'cmd': 'complete', 'identifier': None}

    # 添加命令
    if re.search(r'添加|新增|创建', text):
        # 提取结束日期（支持多种格式）
        end_date = None
        # 格式1: 到2025年3月31日结束
        end_match = re.search(r'到(\d{4})年(\d{1,2})月(\d{1,2})日结束', text)
        if end_match:
            year = int(end_match.group(1))
            month = int(end_match.group(2))
            day = int(end_match.group(3))
            end_date = f"{year:04d}-{month:02d}-{day:02d}"
        else:
            # 格式2: 到3月31日结束
            end_match2 = re.search(r'到(\d{1,2})月(\d{1,2})日结束', text)
            if end_match2:
                month = int(end_match2.group(1))
                day = int(end_match2.group(2))
                year = datetime.now().year
                end_date = f"{year:04d}-{month:02d}-{day:02d}"
            else:
                # 格式3: 结束日期20260630 (8位) 或 2026063 (7位，少见)
                end_match3 = re.search(r'结束日期(\d{6,8})', text)
                if end_match3:
                    end_date = parse_compact_end_date(end_match3.group(1))

        # 移除结束日期标记（不影响原始文本用于解析其他字段）
        text_clean = re.sub(r'到\d{4}年\d{1,2}月\d{1,2}日结束', '', text)
        text_clean = re.sub(r'到\d{1,2}月\d{1,2}日结束', '', text_clean)
        text_clean = re.sub(r'结束日期\d{6,8}', '', text_clean)

        # 提取任务名
        name = '新任务'

        # 1. 优先"叫"后面
        call_match = re.search(r'叫\s*(.+?)(?:，|,|$)', text_clean)
        if call_match:
            name = call_match.group(1).strip()
        else:
            # 2. 针对每周类型：提取"周X HH:MM"后剩余部分
            after_add = re.sub(r'^添加\s*(?:待办|任务)?\s*[，,]\s*', '', text_clean)

            # 匹配"周X 时间"模式
            weekday_pattern = r'(周[一二三四五六日天]|星期[一二三四五六日天])\s*(\d{1,2})(?:[:：]\s*(\d{2}))?点?'
            m = re.search(weekday_pattern, after_add)
            if m:
                # 周期描述结束位置
                end_pos = m.end()
                remaining = after_add[end_pos:].strip('，, ')
                if remaining:
                    name = remaining
                else:
                    # 没有剩余，用周期描述前的部分
                    before_part = after_add[:m.start()].strip('，, ')
                    if before_part:
                        name = before_part
            else:
                # 其他类型：取第一个周期关键词之前
                keywords = ['每周', '每天', '每日', '每月']
                first_kw_pos = len(after_add)
                for kw in keywords:
                    pos = after_add.find(kw)
                    if pos != -1 and pos < first_kw_pos:
                        first_kw_pos = pos

                if first_kw_pos > 0:
                    name = after_add[:first_kw_pos].strip('，, ')
                else:
                    name = after_add.strip('，, ')

        # 清理
        name = re.sub(r'，|,|到\d+年.*$|到.*结束$', '', name).strip()
        if not name:
            name = '新任务'

        params = {'name': name}

        # 周期类型
        if '每月' in text and ('次' in text or '最多' in text):
            params['cycle_type'] = 'monthly_n_times'
            n_match = re.search(r'每月最多?(\d+)次', text)
            if n_match:
                params['n_per_month'] = int(n_match.group(1))
            weekday_map = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
            for char, num in weekday_map.items():
                if f'周{char}' in text or f'星期{char}' in text:
                    params['weekday'] = num
                    break
        elif '每月' in text and ('号' in text or '日' in text):
            if '到' in text or '至' in text:
                params['cycle_type'] = 'monthly_range'
                range_match = re.search(r'每月(\d+)号到(\d+)号', text)
                if range_match:
                    params['range_start'] = int(range_match.group(1))
                    params['range_end'] = int(range_match.group(2))
            else:
                params['cycle_type'] = 'monthly_fixed'
                day_match = re.search(r'每月(\d+)号', text)
                if day_match:
                    params['day_of_month'] = int(day_match.group(1))
        elif '每周' in text:
            params['cycle_type'] = 'weekly'
            weekday_map = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
            for char, num in weekday_map.items():
                if f'周{char}' in text or f'星期{char}' in text:
                    params['weekday'] = num
                    break
        elif '每天' in text or '每日' in text:
            params['cycle_type'] = 'daily'

        # 提取时间
        time_match = re.search(r'(\d{1,2})[:：]\s*(\d{2})', text)
        if not time_match:
            time_match = re.search(r'(\d{1,2})点', text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2)) if time_match.lastindex >= 2 else 0
            params['time_of_day'] = f"{hour:02d}:{minute:02d}"
        else:
            params['time_of_day'] = '09:00'

        if end_date:
            params['end_date'] = end_date

        return {'cmd': 'add', **params}

    return {'cmd': 'unknown', 'text': text}


def get_periodic_pending():
    """获取周期任务待办（包含 skipped 状态以便显示）"""
    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id as task_id, t.name, t.category, t.cycle_type,
               o.id as occ_id, o.date, o.status
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON o.task_id = t.id
        WHERE o.status IN ('pending', 'reminded', 'skipped')
        ORDER BY o.date, t.name
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_simple_pending():
    """获取原 todo 系统中的待办（直接查询 entries 表，包含 skipped）"""
    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, e.text, e.status, g.name as group_name
        FROM entries e
        LEFT JOIN groups g ON e.group_id = g.id
        WHERE e.status IN ('pending', 'in_progress', 'skipped')
        ORDER BY e.id
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def ensure_today_occurrences() -> None:
    try:
        subprocess.run(
            [PYTHON_BIN, str(MANAGER_SCRIPT), '--ensure-today'],
            capture_output=True,
            text=True,
            timeout=10,
        )
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


def get_overdue_periodic_tasks(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    today = now.date().isoformat()
    current_time = now.strftime('%H:%M')

    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.id, t.id, t.name, t.time_of_day, o.status
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON o.task_id = t.id
        WHERE o.date = ?
          AND o.status IN ('pending', 'reminded')
          AND t.time_of_day IS NOT NULL
          AND t.time_of_day != ''
          AND t.time_of_day <= ?
        ORDER BY t.time_of_day, t.name
        """,
        (today, current_time),
    )
    rows = [
        {
            'identifier': f'FIN-{occ_id}',
            'occurrence_id': occ_id,
            'task_id': task_id,
            'name': name,
            'time_of_day': time_of_day,
            'status': status,
        }
        for occ_id, task_id, name, time_of_day, status in cur.fetchall()
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
        results.append(
            {
                'identifier': f'ID{entry_id}',
                'entry_id': entry_id,
                'text': text,
                'group_name': group_name or 'Inbox',
                'scheduled_time': scheduled_time,
                'status': status,
                'special_handler': 'meta_review_fallback' if is_meta_review_entry(text) else None,
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


def run_meta_review_fallback(entry_text: str, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    pending_predictions = count_pending_predictions()
    active_conflicts = count_active_friction_conflicts()
    note = (
        f"- {now.strftime('%H:%M')} Meta-Review fallback completed via direct PREDICTIONS.md/FRICTION.md inspection "
        f"for overdue task: {entry_text}. Pending predictions: {pending_predictions}; active conflicts: {active_conflicts}."
    )
    append_memory_log(note, log_date=now.date())
    return True, note


def complete_periodic_occurrence(occ_id: int) -> tuple[bool, str]:
    conn = sqlite3.connect(str(TODO_DB))
    try:
        cur = conn.cursor()
        cur.execute("SELECT task_id, status FROM periodic_occurrences WHERE id = ?", (occ_id,))
        row = cur.fetchone()
        if not row:
            return False, f"❌ 未找到 FIN-{occ_id}"

        task_id, current_status = row
        if current_status == 'skipped':
            return False, f"❌ 无法完成已跳过的任务 FIN-{occ_id}"
        if current_status == 'completed':
            return True, f"⚠️  FIN-{occ_id} 已完成"

        cur.execute(
            "UPDATE periodic_occurrences SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (occ_id,),
        )
        cur.execute("SELECT cycle_type FROM periodic_tasks WHERE id = ?", (task_id,))
        cycle_type_row = cur.fetchone()
        cycle_type = cycle_type_row[0] if cycle_type_row else None
        if cycle_type == 'monthly_n_times':
            cur.execute("UPDATE periodic_tasks SET count_current_month = count_current_month + 1 WHERE id = ?", (task_id,))

        conn.commit()
    finally:
        conn.close()

    subprocess.run(
        [PYTHON_BIN, str(MANAGER_SCRIPT), '--complete-activity', str(task_id)],
        capture_output=True,
        text=True,
    )
    return True, f"✅ 已完成 FIN-{occ_id}（任务ID {task_id}）"


def complete_legacy_entry(entry_id: int) -> tuple[bool, str]:
    conn = sqlite3.connect(str(TODO_DB))
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM entries WHERE id = ?", (entry_id,))
        row = cur.fetchone()
        if not row:
            return False, f"❌ 未找到 ID {entry_id}"

        current_status = row[0]
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
        return complete_periodic_occurrence(int(identifier[4:]))
    return complete_legacy_entry(parse_entry_identifier(identifier))


def complete_overdue_tasks(now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or datetime.now()
    ensure_today_occurrences()

    periodic = get_overdue_periodic_tasks(now)
    legacy = get_overdue_legacy_entries(now)

    completed: list[str] = []
    simulated: list[str] = []
    errors: list[str] = []
    handled = []

    for task in periodic:
        handled.append(task['identifier'])
        if dry_run:
            simulated.append(f"{task['identifier']} {task['name']} @ {task['time_of_day']}")
            continue
        ok, message = complete_periodic_occurrence(task['occurrence_id'])
        if ok:
            completed.append(message)
        else:
            errors.append(message)

    for entry in legacy:
        handled.append(entry['identifier'])
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


def cmd_list():
    """列出所有待办（合并视图）"""
    ensure_today_occurrences()

    periodic = get_periodic_pending()
    simple = get_simple_pending()

    print("=== Chronos Todo List ===\n")

    if periodic:
        print("【周期任务】")
        for task_id, name, category, cycle_type, occ_id, date_str, status in periodic:
            display_status = "已跳过" if status == 'skipped' else status
            print(f"  [FIN-{occ_id}] {date_str} | {name} ({cycle_type}) | {display_status}")
        print()

    if simple:
        print("【其他任务】")
        for entry_id, text, status, group_name in simple:
            display_status = "已跳过" if status == 'skipped' else status
            group = group_name or 'Inbox'
            print(f"  [ID{entry_id}] {group} | {text} | {display_status}")
        print()

    if not periodic and not simple:
        print("✅ 没有待办任务。")


def cmd_add(text, category='Inbox', cycle_type='once', **kwargs):
    """添加任务（自动路由：非 once 周期任务使用 manager，once 或简单任务直接插入）"""
    if cycle_type != 'once':
        args = [
            PYTHON_BIN, str(MANAGER_SCRIPT),
            '--add',
            '--name', text,
            '--category', category,
            '--cycle-type', cycle_type,
            '--time', kwargs.get('time', '09:00')
        ]
        if 'weekday' in kwargs:
            args.extend(['--weekday', str(kwargs['weekday'])])
        if 'day_of_month' in kwargs:
            args.extend(['--day', str(kwargs['day_of_month'])])
        if 'range_start' in kwargs and 'range_end' in kwargs:
            args.extend(['--range-start', str(kwargs['range_start']), '--range-end', str(kwargs['range_end'])])
        if 'n_per_month' in kwargs:
            args.extend(['--n-per-month', str(kwargs['n_per_month'])])
        if 'end_date' in kwargs:
            args.extend(['--end-date', kwargs['end_date']])
        if 'reminder_template' in kwargs:
            args.extend(['--reminder-template', kwargs['reminder_template']])

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ 已添加周期任务：{text}")
        else:
            print(f"❌ 添加失败：{result.stderr}")
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
    """完成待办"""
    ok, message = complete_identifier(identifier)
    print(message)
    if not ok:
        return


def cmd_complete_overdue(now_override: str | None = None, dry_run: bool = False):
    now = datetime.strptime(now_override, '%Y-%m-%dT%H:%M') if now_override else datetime.now()
    result = complete_overdue_tasks(now=now, dry_run=dry_run)

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
    """跳过待办（不扣减配额）"""
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
            task_id, date_str = row

            cur.execute("SELECT status FROM periodic_occurrences WHERE id = ?", (occ_id,))
            current_status = cur.fetchone()[0]
            if current_status == 'skipped':
                print(f"⚠️  FIN-{occ_id} 已经是跳过状态")
                conn.close()
                return

            cur.execute("UPDATE periodic_occurrences SET status = 'skipped' WHERE id = ?", (occ_id,))

            cur.execute("SELECT reminder_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
            job_name = cur.fetchone()[0]
            if job_name:
                try:
                    subprocess.run(
                        [OPENCLAW_BIN, "cron", "remove", job_name],
                        capture_output=True, text=True, timeout=10
                    )
                except Exception:
                    pass

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
            cur.execute("SELECT status FROM entries WHERE id = ?", (entry_id,))
            row = cur.fetchone()
            if not row:
                print(f"❌ 未找到 ID {entry_id}")
                conn.close()
                return

            current_status = row[0]
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
    """显示任务详情"""
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        conn = sqlite3.connect(str(TODO_DB))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.name, t.cycle_type, o.date, o.status, o.reminder_job_id
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON o.task_id = t.id
            WHERE o.id = ?
            """,
            (occ_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            name, cycle_type, date_str, status, job_id = row
            print(f"【周期任务】{name}")
            print(f"周期类型：{cycle_type}")
            print(f"日期：{date_str}")
            print(f"状态：{status}")
            print(f"提醒任务：{job_id or '无'}")
        else:
            print(f"❌ 未找到 FIN-{occ_id}")
    else:
        entry_id = parse_entry_identifier(identifier)
        conn = sqlite3.connect(str(TODO_DB))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.text, e.status, g.name as group_name
            FROM entries e
            LEFT JOIN groups g ON e.group_id = g.id
            WHERE e.id = ?
            """,
            (entry_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            text, status, group_name = row
            group = group_name or 'Inbox'
            print(f"【任务】{text}")
            print(f"分组：{group}")
            print(f"状态：{status}")
        else:
            print(f"❌ 未找到 ID {entry_id}")


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

    explicit_cmd = sys.argv[1]
    if explicit_cmd in ['list', 'add', 'complete', 'complete-overdue', 'show', 'skip']:
        parser = build_parser()
        args = parser.parse_args()

        if args.command == 'list':
            cmd_list()
        elif args.command == 'add':
            try:
                validate_add_args(args)
            except ValueError as exc:
                print(f"参数错误：{exc}")
                sys.exit(2)

            kwargs = {
                'category': args.category,
                'cycle_type': args.cycle_type,
                'time': args.time_of_day,
            }
            if args.weekday is not None:
                kwargs['weekday'] = args.weekday
            if args.day_of_month is not None:
                kwargs['day_of_month'] = args.day_of_month
            if args.range_start is not None:
                kwargs['range_start'] = args.range_start
            if args.range_end is not None:
                kwargs['range_end'] = args.range_end
            if args.n_per_month is not None:
                kwargs['n_per_month'] = args.n_per_month
            if args.end_date is not None:
                kwargs['end_date'] = args.end_date
            if args.reminder_template is not None:
                kwargs['reminder_template'] = args.reminder_template

            cmd_add(args.name, **kwargs)
        elif args.command == 'skip':
            cmd_skip(args.identifier)
        elif args.command == 'complete':
            cmd_complete(args.identifier)
        elif args.command == 'complete-overdue':
            cmd_complete_overdue(now_override=args.now_override, dry_run=args.dry_run)
        elif args.command == 'show':
            cmd_show(args.identifier)
    else:
        nl_text = ' '.join(sys.argv[1:])
        parsed = parse_natural_language(nl_text)
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
            end_date = parsed.get('end_date')

            print(f"🔍 解析结果：名称={name}, 周期={cycle_type}, 时间={time_of_day}, 星期={weekday}, 日期={day_of_month}, 区间={range_start}-{range_end}, 次数={n_per_month}, 结束={end_date}")

            kwargs = {
                'category': category,
                'cycle_type': cycle_type,
                'time': time_of_day,
            }
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
            if end_date is not None:
                kwargs['end_date'] = end_date

            cmd_add(name, **kwargs)


if __name__ == "__main__":
    main()
