#!/usr/bin/env python3
"""
Unified Todo - 统一待办管理入口
支持：list/add/complete/show
自动路由：金融活动 → financial_activity_manager，其他 → todo.sh
"""
import sqlite3
import subprocess
import json
from pathlib import Path
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")
TODO_DB = WORKSPACE / "todo.db"

def get_financial_pending():
    """获取金融活动待办（合并 financial_occurrences 和 financial_activities）"""
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
    """获取原 todo 系统中的待办（非金融活动，或未迁移的任务）"""
    # 调用 todo.sh list，解析输出
    result = subprocess.run(
        ["bash", str(WORKSPACE / "skills/todo-management/scripts/todo.sh"), "entry", "list"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    lines = result.stdout.strip().split('\n')
    entries = []
    for line in lines:
        parts = line.split(' | ')
        if len(parts) >= 4:
            entry_id, status, group, text = parts[:4]
            if status in ('pending', 'in_progress'):
                entries.append({
                    'id': entry_id,
                    'status': status,
                    'group': group,
                    'text': text,
                    'source': 'simple'
                })
    return entries

def cmd_list():
    """列出所有待办（合并视图）"""
    financial = get_financial_pending()
    simple = get_simple_pending()
    
    print("=== Unified Todo List ===\n")
    
    if financial:
        print("【金融活动】")
        for activity_id, name, category, cycle_type, occ_id, date, status in financial:
            print(f"  [FIN-{occ_id}] {date} | {name} ({cycle_type}) | {status}")
        print()
    
    if simple:
        print("【其他任务】")
        for e in simple:
            print(f"  [ID{e['id']}] {e['group']} | {e['text']} | {e['status']}")
        print()
    
    if not financial and not simple:
        print("✅ 没有待办任务。")

def cmd_add(text, category='Inbox', cycle_type='once', **kwargs):
    """添加任务（自动路由：金融活动周期走 manager，其他走 todo.sh）"""
    # 简单判断是否为金融活动：包含特定关键词或显式指定 --financial
    is_financial = kwargs.get('financial', False) or any(kw in text for kw in ['银行', '华夏', '浦大', '携程', '京东'])
    
    if is_financial and cycle_type != 'once':
        # 使用 financial_activity_manager.py 添加
        manager_script = WORKSPACE / 'skills' / 'custom-todo-manager' / 'scripts' / 'financial_activity_manager.py'
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
        # 使用 todo.sh 添加
        cmd = ["bash", str(WORKSPACE / "skills/todo-management/scripts/todo.sh"), "entry", "create", text]
        if category != 'Inbox':
            cmd.append(f"--group={category}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ 已添加：{text}")
        else:
            print(f"❌ 添加失败：{result.stderr}")

def cmd_complete(identifier):
    """完成待办（支持金融活动的 ID 如 FIN-123 或普通 ID）"""
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        # 查找 occurrence 的 activity_id
        conn = sqlite3.connect(TODO_DB)
        cur = conn.cursor()
        cur.execute("SELECT activity_id FROM financial_occurrences WHERE id = ?", (occ_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            activity_id = row[0]
            manager_script = WORKSPACE / 'skills' / 'custom-todo-manager' / 'scripts' / 'financial_activity_manager.py'
            result = subprocess.run(
                ['python3', str(manager_script), '--complete-activity', str(activity_id)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"✅ 已完成金融活动 ID {identifier}")
            else:
                print(f"❌ 完成失败：{result.stderr}")
        else:
            print(f"❌ 未找到 FIN-{occ_id}")
    else:
        # 普通 todo ID
        entry_id = int(identifier)
        result = subprocess.run(
            ["bash", str(WORKSPACE / "skills/todo-management/scripts/todo.sh"), "entry", "status", str(entry_id), "--status=done"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"✅ 已完成任务 ID {entry_id}")
        else:
            print(f"❌ 完成失败：{result.stderr}")

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
            name, cycle_type, date, status, job_id = row
            print(f"【金融活动】{name}")
            print(f"周期类型：{cycle_type}")
            print(f"日期：{date}")
            print(f"状态：{status}")
            print(f"提醒任务：{job_id or '无'}")
        else:
            print(f"❌ 未找到 FIN-{occ_id}")
    else:
        entry_id = int(identifier)
        result = subprocess.run(
            ["bash", str(WORKSPACE / "skills/todo-management/scripts/todo.sh"), "entry", "show", str(entry_id)],
            capture_output=True, text=True
        )
        print(result.stdout if result.returncode == 0 else f"❌ 未找到 ID {entry_id}")

def main():
    if len(sys.argv) < 2:
        print("用法：unified_todo.py [list|add|complete|show] [参数]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'list':
        cmd_list()
    elif cmd == 'add':
        # 解析参数（简单实现：text 作为最后一个参数，其他通过 --flag 传递）
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
