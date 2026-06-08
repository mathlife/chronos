#!/usr/bin/env python3
"""Natural-language parsing helpers for Chronos todo CLI."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable


def parse_natural_language(text: str, *, parse_compact_end_date: Callable[[str], str | None]) -> dict[str, object]:
    text = text.strip()

    if re.search(r'逾期|过时|已过时间', text) and re.search(r'完成|补完成|自动完成', text):
        return {'cmd': 'complete-overdue'}

    if re.search(r'查询|查看|今日|待办|任务', text) and not re.search(r'添加|新增|创建', text):
        if '详情' in text or re.search(r'FIN-\d+|ID\d+', text):
            match = re.search(r'(FIN-\d+|ID\d+)', text)
            if match:
                return {'cmd': 'show', 'identifier': match.group(1)}
        else:
            return {'cmd': 'list'}

    if re.search(r'跳过|跳過|skipping?', text):
        match = re.search(r'(FIN-\d+|ID\d+)', text)
        if match:
            return {'cmd': 'skip', 'identifier': match.group(1)}
        return {'cmd': 'skip', 'identifier': None}

    if re.search(r'完成|标记完成', text):
        match = re.search(r'(FIN-\d+|ID\d+)', text)
        if match:
            return {'cmd': 'complete', 'identifier': match.group(1)}
        return {'cmd': 'complete', 'identifier': None}

    if re.search(r'添加|新增|创建', text):
        end_date = None
        end_match = re.search(r'到(\d{4})年(\d{1,2})月(\d{1,2})日结束', text)
        if end_match:
            year = int(end_match.group(1))
            month = int(end_match.group(2))
            day = int(end_match.group(3))
            end_date = f"{year:04d}-{month:02d}-{day:02d}"
        else:
            end_match2 = re.search(r'到(\d{1,2})月(\d{1,2})日结束', text)
            if end_match2:
                month = int(end_match2.group(1))
                day = int(end_match2.group(2))
                year = datetime.now().year
                end_date = f"{year:04d}-{month:02d}-{day:02d}"
            else:
                end_match3 = re.search(r'结束日期(\d{6,8})', text)
                if end_match3:
                    end_date = parse_compact_end_date(end_match3.group(1))

        text_clean = re.sub(r'到\d{4}年\d{1,2}月\d{1,2}日结束', '', text)
        text_clean = re.sub(r'到\d{1,2}月\d{1,2}日结束', '', text_clean)
        text_clean = re.sub(r'结束日期\d{6,8}', '', text_clean)

        name = '新任务'
        call_match = re.search(r'叫\s*(.+?)(?:，|,|$)', text_clean)
        if call_match:
            name = call_match.group(1).strip()
        else:
            after_add = re.sub(r'^添加\s*(?:待办|任务)?\s*[，,]\s*', '', text_clean)
            weekday_pattern = r'(周[一二三四五六日天]|星期[一二三四五六日天])\s*(\d{1,2})(?:[:：]\s*(\d{2}))?点?'
            m = re.search(weekday_pattern, after_add)
            if m:
                end_pos = m.end()
                remaining = after_add[end_pos:].strip('，, ')
                if remaining:
                    name = remaining
                else:
                    before_part = after_add[:m.start()].strip('，, ')
                    if before_part:
                        name = before_part
            else:
                keywords = ['每周', '每天', '每日', '每月', '每小时']
                first_kw_pos = len(after_add)
                for kw in keywords:
                    pos = after_add.find(kw)
                    if pos != -1 and pos < first_kw_pos:
                        first_kw_pos = pos
                if first_kw_pos > 0:
                    name = after_add[:first_kw_pos].strip('，, ')
                else:
                    name = after_add.strip('，, ')

        schedule_markers = ['每周', '每天', '每日', '每月', '每小时']
        marker_pos = len(name)
        for marker in schedule_markers:
            pos = name.find(marker)
            if pos != -1 and pos < marker_pos:
                marker_pos = pos
        if marker_pos < len(name):
            name = name[:marker_pos].strip('，, ')

        name = re.sub(r'，|,|到\d+年.*$|到.*结束$', '', name).strip()
        if not name:
            name = '新任务'

        params = {'name': name}

        every_hours = re.search(r'每\s*(\d+)\s*小时', text)
        if every_hours:
            params['cycle_type'] = 'hourly'
            params['interval_hours'] = int(every_hours.group(1))
        elif '每月' in text and ('次' in text or '最多' in text):
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

        time_match = re.search(r'(\d{1,2})[:：]\s*(\d{2})', text)
        if not time_match:
            time_match = re.search(r'(\d{1,2})点', text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2)) if time_match.lastindex and time_match.lastindex >= 2 else 0
            params['time_of_day'] = f"{hour:02d}:{minute:02d}"
        else:
            params['time_of_day'] = '09:00'

        if end_date:
            params['end_date'] = end_date

        return {'cmd': 'add', **params}

    return {'cmd': 'unknown', 'text': text}
