#!/usr/bin/env python3
"""
Unified Todo - 统一待办管理入口
支持：list/add/complete/show
自动路由：周期任务 → periodic_task_manager，其他 → 直接操作 entries 表
自然语言解析：支持中文指令
"""
import sqlite3
import subprocess
import re
from pathlib import Path
import sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")
TODO_DB = WORKSPACE / "todo.db"
SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')

def parse_natural_language(text: str) -> dict:
    """解析自然语言指令，返回命令和参数"""
    text = text.strip()
    
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
                    date_str = end_match3.group(1)
                    if len(date_str) == 6:
                        # yyMMdd - 假设2026年
                        year = 2026
                        month = int(date_str[:2])
                        day = int(date_str[2:4])
                    elif len(date_str) == 8:
                        year = int(date_str[:4])
                        month = int(date_str[4:6])
                        day = int(date_str[6:8])
                    else:
                        # 7位，可能是 yyyyMdd 或 yyMMdd 变种，跳过
                        pass
                    if 'year' in locals():
                        end_date = f"{year:04d}-{month:02d}-{day:02d}"
        
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
            weekday_map = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6, '天':6}
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
            weekday_map = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6, '天':6}
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
    conn = sqlite3.connect(TODO_DB)
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
    conn = sqlite3.connect(TODO_DB)
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

def cmd_skip(identifier):
    """跳过待办（不扣减配额）"""
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        try:
            conn = sqlite3.connect(TODO_DB)
            cur = conn.cursor()
            
            # 获取 occurrence 信息
            cur.execute("SELECT task_id, date FROM periodic_occurrences WHERE id = ?", (occ_id,))
            row = cur.fetchone()
            if not row:
                print(f"❌ 未找到 FIN-{occ_id}")
                conn.close()
                return
            task_id, date_str = row
            
            # 检查当前状态
            cur.execute("SELECT status FROM periodic_occurrences WHERE id = ?", (occ_id,))
            current_status = cur.fetchone()[0]
            if current_status == 'skipped':
                print(f"⚠️  FIN-{occ_id} 已经是跳过状态")
                conn.close()
                return
            
            # 标记为 skipped（不删除，不扣配额）
            cur.execute("UPDATE periodic_occurrences SET status = 'skipped' WHERE id = ?", (occ_id,))
            
            # 清理该 task 的 cron 任务（如果已安排）
            cur.execute("SELECT reminder_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
            job_name = cur.fetchone()[0]
            if job_name:
                try:
                    subprocess.run(
                        ["openclaw", "cron", "remove", job_name],
                        capture_output=True, text=True, timeout=10
                    )
                except:
                    pass
            
            conn.commit()
            conn.close()
            
            print(f"✅ 已跳过 FIN-{occ_id}（配额不受影响）")
        except Exception as e:
            print(f"❌ 跳过失败：{e}")
    else:
        entry_id = int(identifier)
        try:
            conn = sqlite3.connect(TODO_DB)
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
            
            # entries 表新增 skipped 状态
            cur.execute("UPDATE entries SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (entry_id,))
            conn.commit()
            conn.close()
            print(f"✅ 已跳过任务 ID {entry_id}")
        except Exception as e:
            print(f"❌ 跳过失败：{e}")

def cmd_list():
    """列出所有待办（合并视图）"""
    # 确保今天的 occurrence 已生成（不包含清理逻辑）
    manager_script = WORKSPACE / 'skills' / 'chronos' / 'scripts' / 'periodic_task_manager.py'
    try:
        subprocess.run(
            ['python3', str(manager_script), '--ensure-today'],
            capture_output=True,
            timeout=10  # 防止卡死
        )
    except Exception as e:
        print(f"⚠️  生成今日任务失败: {e}")
    
    periodic = get_periodic_pending()
    simple = get_simple_pending()
    
    print("=== Chronos Todo List ===\n")
    
    if periodic:
        print("【周期任务】")
        for task_id, name, category, cycle_type, occ_id, date_str, status in periodic:
            # 显示跳过状态
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
    # 只要不是 once 类型，都走 manager（支持所有复杂周期）
    if cycle_type != 'once':
        # 使用 periodic_task_manager.py 添加
        manager_script = WORKSPACE / 'skills' / 'chronos' / 'scripts' / 'periodic_task_manager.py'
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
        if 'end_date' in kwargs:
            args.extend(['--end-date', kwargs['end_date']])
        
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ 已添加周期任务：{text}")
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
        try:
            conn = sqlite3.connect(TODO_DB)
            cur = conn.cursor()
            # 获取 occurrence 信息
            cur.execute("SELECT task_id, date FROM periodic_occurrences WHERE id = ?", (occ_id,))
            row = cur.fetchone()
            if not row:
                print(f"❌ 未找到 FIN-{occ_id}")
                conn.close()
                return
            task_id, date_str = row
            
            # 检查当前状态，如果已经是 skipped 则不能完成
            cur.execute("SELECT status FROM periodic_occurrences WHERE id = ?", (occ_id,))
            current_status = cur.fetchone()[0]
            if current_status == 'skipped':
                print(f"❌ 无法完成已跳过的任务 FIN-{occ_id}")
                conn.close()
                return
            
            # 标记 occurrence 为 completed
            cur.execute("UPDATE periodic_occurrences SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?", (occ_id,))
            affected = cur.rowcount
            
            # 如果是 monthly_n_times，增加计数
            cur.execute("SELECT cycle_type FROM periodic_tasks WHERE id = ?", (task_id,))
            cycle_type = cur.fetchone()[0]
            if cycle_type == 'monthly_n_times':
                cur.execute("UPDATE periodic_tasks SET count_current_month = count_current_month + 1 WHERE id = ?", (task_id,))
            
            conn.commit()
            conn.close()
            
            # 清理该 task 的 cron 任务（并检查配额）
            manager_script = WORKSPACE / 'skills' / 'chronos' / 'scripts' / 'periodic_task_manager.py'
            result = subprocess.run(
                ['python3', str(manager_script), '--complete-activity', str(task_id)],
                capture_output=True, text=True
            )
            print(f"✅ 已完成 FIN-{occ_id}（任务ID {task_id}）")
        except Exception as e:
            print(f"❌ 完成失败：{e}")
    else:
        entry_id = int(identifier)
        try:
            conn = sqlite3.connect(TODO_DB)
            cur = conn.cursor()
            cur.execute("SELECT status FROM entries WHERE id = ?", (entry_id,))
            row = cur.fetchone()
            if not row:
                print(f"❌ 未找到 ID {entry_id}")
                conn.close()
                return
            
            current_status = row[0]
            if current_status == 'skipped':
                print(f"❌ 无法完成已跳过的任务 ID {entry_id}")
                conn.close()
                return
            
            cur.execute("UPDATE entries SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (entry_id,))
            if cur.rowcount > 0:
                conn.commit()
                print(f"✅ 已完成任务 ID {entry_id}")
            else:
                print(f"❌ 未找到 ID {entry_id}")
            conn.close()
        except Exception as e:
            print(f"❌ 完成失败：{e}")

def cmd_skip(identifier):
    """跳过待办（不扣减配额）"""
    if identifier.startswith('FIN-'):
        occ_id = int(identifier[4:])
        try:
            conn = sqlite3.connect(TODO_DB)
            cur = conn.cursor()
            
            # 获取 occurrence 信息
            cur.execute("SELECT task_id, date FROM periodic_occurrences WHERE id = ?", (occ_id,))
            row = cur.fetchone()
            if not row:
                print(f"❌ 未找到 FIN-{occ_id}")
                conn.close()
                return
            task_id, date_str = row
            
            # 检查当前状态
            cur.execute("SELECT status FROM periodic_occurrences WHERE id = ?", (occ_id,))
            current_status = cur.fetchone()[0]
            if current_status == 'skipped':
                print(f"⚠️  FIN-{occ_id} 已经是跳过状态")
                conn.close()
                return
            
            # 标记为 skipped（不删除，不扣配额）
            cur.execute("UPDATE periodic_occurrences SET status = 'skipped' WHERE id = ?", (occ_id,))
            
            # 清理该 task 的 cron 任务（如果已安排）
            cur.execute("SELECT reminder_job_id FROM periodic_occurrences WHERE id = ?", (occ_id,))
            job_name = cur.fetchone()[0]
            if job_name:
                try:
                    subprocess.run(
                        ["openclaw", "cron", "remove", job_name],
                        capture_output=True, text=True, timeout=10
                    )
                except:
                    pass
            
            conn.commit()
            conn.close()
            
            print(f"✅ 已跳过 FIN-{occ_id}（配额不受影响）")
        except Exception as e:
            print(f"❌ 跳过失败：{e}")
    else:
        entry_id = int(identifier)
        try:
            conn = sqlite3.connect(TODO_DB)
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
            
            # entries 表新增 skipped 状态
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
        conn = sqlite3.connect(TODO_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT t.name, t.cycle_type, o.date, o.status, o.reminder_job_id
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON o.task_id = t.id
            WHERE o.id = ?
        """, (occ_id,))
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
        print("用法：todo.py [list|add|complete|skip|show] [参数] 或直接说自然语言")
        print("  list          - 列出所有待办")
        print("  add <任务名>  - 添加任务（需额外参数指定周期）")
        print("  complete <ID> - 完成任务")
        print("  skip <ID>     - 跳过任务（不影响配额）")
        print("  show <ID>     - 查看详情")
        print("自然语言示例：")
        print("  \"跳过 FIN-123\" - 跳过周期任务")
        print("  \"跳过 45\"     - 跳过普通任务")
        print("  \"查询待办\"     - 列出所有待办")
        sys.exit(1)
    
    # 检查是否为显式命令
    explicit_cmd = sys.argv[1]
    if explicit_cmd in ['list', 'add', 'complete', 'show', 'skip']:
        cmd = explicit_cmd
        if cmd == 'list':
            cmd_list()
        elif cmd == 'add':
            if len(sys.argv) < 3:
                print("用法：todo.py add <任务名> [参数]")
                sys.exit(1)
            text = sys.argv[2]
            kwargs = {}
            i = 3
            while i < len(sys.argv):
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
                elif arg == '--cycle-type' and i + 1 < len(sys.argv):
                    kwargs['cycle_type'] = sys.argv[i+1]; i += 2
                else:
                    i += 1
            cmd_add(text, **kwargs)
        elif cmd == 'skip':
            if len(sys.argv) < 3:
                print("用法：todo.py skip <ID|FIN-occ_id>")
                sys.exit(1)
            cmd_skip(sys.argv[2])
        elif cmd == 'complete':
            if len(sys.argv) < 3:
                print("用法：todo.py complete <ID|FIN-occ_id>")
                sys.exit(1)
            cmd_complete(sys.argv[2])
        elif cmd == 'show':
            if len(sys.argv) < 3:
                print("用法：todo.py show <ID|FIN-occ_id>")
                sys.exit(1)
            cmd_show(sys.argv[2])
    else:
        nl_text = ' '.join(sys.argv[1:])
        parsed = parse_natural_language(nl_text)
        if parsed['cmd'] == 'unknown':
            print(f"无法识别的指令：{nl_text}")
            print("支持的指令：添加待办、查询待办、完成任务、跳过任务、查看详情")
            sys.exit(1)
        elif parsed['cmd'] == 'list':
            cmd_list()
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
            
            # 打印解析结果（调试用）
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
