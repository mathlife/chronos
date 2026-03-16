#!/usr/bin/env python3
"""
Unified Todo - 统一待办管理入口
支持：list/add/complete/show
自动路由：金融活动 → financial_activity_manager，其他 → 直接操作 entries 表
"""
import sqlite3
import subprocess
import json
from pathlib import Path
import sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")
TODO_DB = WORKSPACE / "todo.db"
SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')

def get_financial_pending():
    """获取金融活动待办"""
    conn = sqlite3.connect(TODO_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT a.id as activity_id, a.name, a.category, a.cycle_type, 
               o.id as occ_id, o.date, o.status
        FROM financial_occurrences o
        JOIN financial_activities a ON o.activity_id = a.id
        WHERE o.status IN ('pending', 'reminded')
        ORDER BY o.date, a.name
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def get_simple_pending():
    """获取原 todo 系统中的待办（直接查询 entries 表）"""
    conn = sqlite3.connect(TODO_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, e.text, e.status, g.name as group_name
        FROM entries e
        LEFT JOIN groups g ON e.group_id = g.id
        WHERE e.status IN ('pending', 'in_progress')
        ORDER BY e.id
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def cmd_list():
    """列出所有待办（合并视图）"""
    financial = get_financial_pending()
    simple = get_simple_pending()
    
    print("=== Chronos Todo List ===\n")
    
    if financial:
        print("【金融活动】")
        for activity_id, name, category, cycle_type, occ_id, date_str, status in financial:
            print(f"  [FIN-{occ_id}] {date_str} | {name} ({cycle_type}) | {status}")
        print()
    
    if simple:
        print("【其他任务】")
        for entry_id, text, status, group_name in simple:
            group = group_name or 'Inbox'
            print(f"  [ID{entry_id}] {group} | {text} | {status}")
        print()
    
    if not financial and not simple:
        print("✅ 没有待办任务。")

def cmd_add(text, category='Inbox', cycle_type='once', **kwargs):
    """添加任务（自动路由：非 once 周期任务使用 manager，once 或简单任务直接插入）"""
    # 只要不是 once 类型，都走 manager（支持所有复杂周期）
    if cycle_type != 'once':
        # 使用 financial_activity_manager.py 添加（更名为 manager）
        manager_script = WORKSPACE / 'skills' / 'chronos' / 'scripts' / 'financial_activity_manager.py'
        args = [
            'python3', str(manager_script),
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
        
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ 已添加金融活动：{text}")
        else:
            print(f"❌ 添加失败：{result.stderr}")
    else:
        # 直接插入 entries 表（简单任务）
        try:
            conn = sqlite3.connect(TODO_DB)
            cur = conn.cursor()
            # 获取或创建分组
            cur.execute("SELECT id FROM groups WHERE name = ?", (category,))
            row = cur.fetchone()
            if row:
                group_id = row[0]
            else:
                cur.execute("INSERT INTO groups (name) VALUES (?)", (category,))
                group_id = cur.lastrowid
                conn.commit()
            
            cur.execute("""
                INSERT INTO entries (text, status, group_id, created_at, updated_at)
                VALUES (?, 'pending', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (text, group_id))
            conn.commit()
            entry_id = cur.lastrowid
            conn.close()
            print(f"✅ 已添加任务 ID {entry_id}: {text}")
        except Exception as e:
            print(f"❌ 添加失败：{e}")

def cmd_complete(identifier):
    """完成待办"""
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        manager_script = WORKSPACE / 'skills' / 'chronos' / 'scripts' / 'financial_activity_manager.py'
        result = subprocess.run(
            ['python3', str(manager_script), '--complete-activity', str(occ_id)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"✅ 已完成金融活动 ID {identifier}")
        else:
            print(f"❌ 完成失败：{result.stderr}")
    else:
        entry_id = int(identifier)
        try:
            conn = sqlite3.connect(TODO_DB)
            cur = conn.cursor()
            cur.execute("UPDATE entries SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (entry_id,))
            if cur.rowcount > 0:
                conn.commit()
                print(f"✅ 已完成任务 ID {entry_id}")
            else:
                print(f"❌ 未找到 ID {entry_id}")
            conn.close()
        except Exception as e:
            print(f"❌ 完成失败：{e}")

def cmd_show(identifier):
    """显示任务详情"""
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        conn = sqlite3.connect(TODO_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT a.name, a.cycle_type, o.date, o.status, o.reminder_job_id
            FROM financial_occurrences o
            JOIN financial_activities a ON o.activity_id = a.id
            WHERE o.id = ?
        """, (occ_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            name, cycle_type, date_str, status, job_id = row
            print(f"【金融活动】{name}")
            print(f"周期类型：{cycle_type}")
            print(f"日期：{date_str}")
            print(f"状态：{status}")
            print(f"提醒任务：{job_id or '无'}")
        else:
            print(f"❌ 未找到 FIN-{occ_id}")
    else:
        entry_id = int(identifier)
        conn = sqlite3.connect(TODO_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT e.text, e.status, g.name as group_name
            FROM entries e
            LEFT JOIN groups g ON e.group_id = g.id
            WHERE e.id = ?
        """, (entry_id,))
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
        print("用法：unified_todo.py [list|add|complete|show] [参数]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'list':
        cmd_list()
    elif cmd == 'add':
        # 解析参数
        text = sys.argv[-1]
        kwargs = {}
        i = 2
        while i < len(sys.argv) - 1:
            arg = sys.argv[i]
            if arg == '--category' and i + 1 < len(sys.argv):
                kwargs['category'] = sys.argv[i+1]; i += 2
            elif arg == '--time' and i + 1 < len(sys.argv):
                kwargs['time'] = sys.argv[i+1]; i += 2
            elif arg == '--weekday' and i + 1 < len(sys.argv):
                kwargs['weekday'] = int(sys.argv[i+1]); i += 2
            elif arg == '--day' and i + 1 < len(sys.argv):
                kwargs['day_of_month'] = int(sys.argv[i+1]); i += 2
            elif arg == '--range-start' and i + 1 < len(sys.argv):
                kwargs['range_start'] = int(sys.argv[i+1]); i += 2
            elif arg == '--range-end' and i + 1 < len(sys.argv):
                kwargs['range_end'] = int(sys.argv[i+1]); i += 2
            elif arg == '--n-per-month' and i + 1 < len(sys.argv):
                kwargs['n_per_month'] = int(sys.argv[i+1]); i += 2
            elif arg == '--financial':
                kwargs['financial'] = True; i += 1
            elif arg == '--cycle-type' and i + 1 < len(sys.argv):
                kwargs['cycle_type'] = sys.argv[i+1]; i += 2
            else:
                i += 1
        cmd_add(text, **kwargs)
    elif cmd == 'complete':
        if len(sys.argv) < 3:
            print("用法：unified_todo.py complete <ID|FIN-occ_id>")
            sys.exit(1)
        cmd_complete(sys.argv[2])
    elif cmd == 'show':
        if len(sys.argv) < 3:
            print("用法：unified_todo.py show <ID|FIN-occ_id>")
            sys.exit(1)
        cmd_show(sys.argv[2])
    else:
        print(f"未知命令：{cmd}")

if __name__ == "__main__":
    main()
