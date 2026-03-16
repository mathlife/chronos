#!/usr/bin/env python3
"""
迁移 entries 到 financial_activities
dry-run 模式：打印映射，不执行修改
--execute：实际执行迁移
"""
import sqlite3
import re
from datetime import datetime
from pathlib import Path
import sys

WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")
TODO_DB = WORKSPACE / "todo.db"

WEEKDAY_MAP = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '日': 0}

def parse_entry_text(text):
    text = text.strip()
    if '华夏10分精彩' in text:
        return None
    
    name = text
    name = re.sub(r'^\[每周重复\]\s*', '', name)
    name = re.sub(r'^\[每月重复\]\s*', '', name)
    name = re.sub(r'^\[每月两次\]\s*', '', name)
    name = re.sub(r'^\[每天\]\s*', '', name)
    name = re.sub(r'\s*\(.*', '', name).strip()
    
    cycle_type = None
    params = {}
    
    if text.startswith('[每月两次]'):
        cycle_type = 'monthly_n_times'
        m = re.search(r'每周([一二三四五六日])\s+(\d{1,2}):(\d{2})', text)
        if m:
            params['weekday'] = WEEKDAY_MAP[m.group(1)]
            params['time_of_day'] = f"{m.group(2)}:{m.group(3)}"
            params['n_per_month'] = 2
    
    elif text.startswith('[每月重复]'):
        if '每天' in text:
            cycle_type = 'daily'
            m = re.search(r'每天\s+(\d{1,2}):(\d{2})', text)
            if m:
                params['time_of_day'] = f"{m.group(1)}:{m.group(2)}"
            else:
                params['time_of_day'] = '09:00'
        else:
            cycle_type = 'monthly_n_times'
            m = re.search(r'每周([一二三四五六日])\s+(\d{1,2}):(\d{2})', text)
            if m:
                params['weekday'] = WEEKDAY_MAP[m.group(1)]
                params['time_of_day'] = f"{m.group(2)}:{m.group(3)}"
                params['n_per_month'] = 1
    
    elif text.startswith('[每周重复]'):
        cycle_type = 'weekly'
        m = re.search(r'每周([一二三四五六日])\s+(\d{1,2}):(\d{2})', text)
        if m:
            params['weekday'] = WEEKDAY_MAP[m.group(1)]
            params['time_of_day'] = f"{m.group(2)}:{m.group(3)}"
    
    elif text.startswith('[每天]'):
        cycle_type = 'daily'
        m = re.search(r'每天\s+(\d{1,2}):(\d{2})', text)
        if m:
            params['time_of_day'] = f"{m.group(1)}:{m.group(2)}"
        else:
            params['time_of_day'] = '09:00'
    
    elif '华夏10分精彩' in text:
        return None
    
    else:
        return None
    
    return name, cycle_type, params

def get_group_name(cur, group_id):
    cur.execute("SELECT name FROM groups WHERE id = ?", (group_id,))
    row = cur.fetchone()
    return row[0] if row else 'Inbox'

def main():
    dry_run = '--execute' not in sys.argv
    
    conn = sqlite3.connect(TODO_DB)
    cur = conn.cursor()
    
    cur.execute("""
        SELECT e.id, e.text, e.group_id, e.status
        FROM entries e
        WHERE e.status IN ('pending', 'in_progress')
          AND e.text NOT LIKE '%华夏10分精彩%'
        ORDER BY e.id
    """)
    entries = cur.fetchall()
    
    print(f"Found {len(entries)} entries to consider migrating.\n")
    
    mappings = []
    for entry_id, text, group_id, status in entries:
        parsed = parse_entry_text(text)
        if not parsed:
            print(f"⚠️  Skipping ID {entry_id}: cannot parse '{text}'")
            continue
        
        name, cycle_type, params = parsed
        group_name = get_group_name(cur, group_id)
        
        mappings.append({
            'id': entry_id,
            'name': name,
            'cycle_type': cycle_type,
            'params': params,
            'group': group_name,
            'status': status
        })
    
    print("=== Migration Plan (dry-run) ===\n")
    for m in mappings:
        print(f"Entry ID {m['id']}: {m['name']}")
        print(f"  Group: {m['group']}")
        print(f"  Cycle Type: {m['cycle_type']}")
        print(f"  Params: {m['params']}")
        print(f"  Current Status: {m['status']}")
        print()
    
    if dry_run:
        print("⚠️  Dry-run only. Use --execute to apply migration.")
        conn.close()
        return
    
    print("🔧 Executing migration...\n")
    
    for m in mappings:
        cur.execute("""
            INSERT OR REPLACE INTO financial_activities 
            (name, category, cycle_type, weekday, day_of_month, range_start, range_end, n_per_month, time_of_day, event_time, timezone, is_active, count_current_month, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Asia/Shanghai', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            m['name'],
            m['group'],
            m['cycle_type'],
            m['params'].get('weekday'),
            m['params'].get('day_of_month'),
            m['params'].get('range_start'),
            m['params'].get('range_end'),
            m['params'].get('n_per_month'),
            m['params'].get('time_of_day'),
            m['params'].get('time_of_day')
        ))
        activity_id = cur.lastrowid
        
        cur.execute("UPDATE entries SET status = 'done' WHERE id = ?", (m['id'],))
        
        print(f"✅ Migrated entry {m['id']} -> activity {activity_id}")
    
    conn.commit()
    conn.close()
    print(f"\n✅ Migration complete. {len(mappings)} entries migrated.")

if __name__ == "__main__":
    main()
