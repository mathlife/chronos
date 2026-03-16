---
name: chronos
description: 通用周期任务管理器 - 支持6种周期类型、每月N次配额、自动cron、统一视图，适用于所有定时任务场景
version: 1.0.0
metadata: {"openclaw":{"emoji":"⏰","requires":{"bins":["sqlite3","openclaw"]}}}
user-invocable: true
---

# Custom Todo Manager

## What this skill controls
- **金融活动表**：`financial_activities` + `financial_occurrences`（在 `./todo.db`）
- **原 todo 表**：`entries`（兼容旧任务）
- **统一入口**：`unified_todo.py`

## Capabilities

### 周期类型
- `once`：一次性
- `daily`：每天
- `weekly`：每周指定星期
- `monthly_fixed`：每月固定日期
- `monthly_range`：每月区间（如11号→次月5号）
- `monthly_n_times`：每月N次（基于活动日计数，配额用完后自动完成剩余日期）

### 自动功能
- 每天 03:30 自动运行管理器，生成今日提醒
- 配额在每月1号自动重置
- 完成任务时自动清理原 entries（标记 done）
- Cron 任务自动创建/清理（单次触发后自动删除）

## Commands

### unified_todo.py（主入口）

```bash
# 列出所有待办（合并金融活动 + 其他任务）
python3 skills/custom-todo-manager/scripts/unified_todo.py list

# 添加任务
python3 skills/custom-todo-manager/scripts/unified_todo.py add "任务名" \
  [--category "分组"] \
  [--financial] \
  [--cycle-type once|daily|weekly|monthly_fixed|monthly_range|monthly_n_times] \
  [--time "HH:MM"] \
  [--weekday 0-6] \
  [--day 1-31] \
  [--range-start 1-31 --range-end 1-31] \
  [--n-per-month N]

# 完成任务
python3 skills/custom-todo-manager/scripts/unified_todo.py complete <ID|FIN-occ_id>

# 查看详情
python3 skills/custom-todo-manager/scripts/unified_todo.py show <ID|FIN-occ_id>
```

### financial_activity_manager.py（直接调用）

```bash
# 每日自动运行（由 cron 03:30 触发）
python3 skills/custom-todo-manager/scripts/financial_activity_manager.py

# 手动添加活动
python3 skills/custom-todo-manager/scripts/financial_activity_manager.py --add \
  --name "活动名" \
  --category "金融/活动" \
  --cycle-type monthly_n_times \
  --weekday 2 \
  --n-per-month 1 \
  --time "10:00"

# 批量完成活动（完成当期剩余 + 清理 cron + 清理原 entries）
python3 skills/custom-todo-manager/scripts/financial_activity_manager.py --complete-activity <activity_id>
```

### migrate_entries_to_financial.py（数据迁移）

```bash
# 预览迁移计划
python3 skills/custom-todo-manager/scripts/migrate_entries_to_financial.py

# 执行迁移（将 entries 中的金融活动迁移到新表，原条目标记 done）
python3 skills/custom-todo-manager/scripts/migrate_entries_to_financial.py --execute
```

## Notes

- 金融活动的 `occurrences` 表按天生成，状态：`pending`/`reminded`/`completed`/`skipped`
- `monthly_n_times` 类型：配额用完前，符合条件的活动日生成 `pending`；配额用完后，剩余活动日自动 `completed`
- 原 `todo.sh` 仍然可用（非金融任务），但建议统一使用 `unified_todo.py`

## Migration

1. 确认数据无误后，运行迁移脚本
2. 将日常使用的命令切换为 `unified_todo.py`
3. 可保留 `todo-management` skill 以备兼容，但建议逐步淘汰
