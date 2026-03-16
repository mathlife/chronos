---
name: chronos
description: 通用周期任务管理器 - 支持6种周期类型、每月N次配额、自动cron、统一视图，适用于所有定时任务场景
version: 1.1.0
metadata: {"openclaw":{"emoji":"⏰","requires":{"bins":["sqlite3","openclaw"]}}}
user-invocable: true
---

# Chronos - Universal Periodic Task Manager

## What this skill controls
- **周期任务表**：`periodic_tasks` + `periodic_occurrences`（在 `./todo.db`）
- **原 todo 表**：`entries`（兼容旧一次性任务）
- **统一入口**：`todo.py`

## Capabilities

### 周期类型
- `once`：一次性
- `daily`：每天
- `weekly`：每周（指定星期几）
- `monthly_fixed`：每月固定日期（如15号）
- `monthly_range`：每月区间（如11号→5号，跨月）
- `monthly_n_times`：每月限次（如每周三，每月最多2次）

### 智能配额
- 仅 `completed` 计数，`skip` 不计
- 配额用满后自动完成当月剩余活动日
- 每月1号自动重置计数器

### 自动提醒
- Cron 任务自动生成（未来事件）
- 每日自动清理过期 cron
- 时间已过则跳过（避免错误）

### 统一视图
- `todo.py list`：合并显示周期任务和普通任务
- `todo.py add`：智能路由（周期任务 → manager，一次性 → entries）
- `todo.py complete`：完成单个 occurrence 或普通任务
- `todo.py show`：查看详情

## Usage

```bash
# 列出所有待办（自动确保今天 occurrence 已生成）
python3 skills/chronos/scripts/todo.py list

# 添加周期任务（例如：每月2次，每周三10:00）
python3 skills/chronos/scripts/todo.py add "周三抢券" \
  --cycle-type monthly_n_times \
  --weekday 2 \
  --n-per-month 2 \
  --time 10:00

# 添加一次性任务
python3 skills/chronos/scripts/todo.py add "买牛奶" --category 生活

# 完成任务
python3 skills/chronos/scripts/todo.py complete FIN-123  # 周期任务 occurrence
python3 skills/chronos/scripts/todo.py complete 45      # 普通任务 ID

# 查看详情
python3 skills/chronos/scripts/todo.py show FIN-123
```

## Database Schema

### periodic_tasks
- `id`, `name`, `category`, `cycle_type`, `weekday`, `day_of_month`, `range_start`, `range_end`, `n_per_month`
- `time_of_day`, `event_time`, `timezone`, `is_active`, `count_current_month`, `created_at`, `updated_at`

### periodic_occurrences
- `id`, `task_id`, `date`, `status` (pending/reminded/completed/skipped)
- `reminder_job_id`, `is_auto_completed`, `completed_at`

### entries + groups
- 旧表，用于一次性任务，保留兼容

## Migration

旧 `financial_activities` 和 `financial_occurrences` 数据已自动迁移到新表。旧表已删除。

## Notes

- 所有时间基于 `Asia/Shanghai` 时区
- Cron 提醒转换为 UTC 后提交给 Gateway
- `todo.py list` 调用 `--ensure-today` 轻量生成（不安排 cron），完整 cron 由每日 manager 负责
- 双环学习集成在 `periodic_task_manager.py` 的关键操作中
