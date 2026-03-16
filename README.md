# Chronos

通用周期任务管理器 - 适用于所有定时任务场景。支持6种周期类型、每月N次配额、自动cron、统一视图。

## Features

- **6种周期类型**: `once`, `daily`, `weekly`, `monthly_fixed`, `monthly_range`, `monthly_n_times`
- **智能配额**: 每月N次基于活动日计数，用完后自动完成剩余日期（适用于抢券、签到等限次活动）
- **自动提醒**: Cron 任务自动生成/清理，无需手动管理
- **统一视图**: `unified_todo.py` 合并显示金融活动和其他任务
- **数据迁移**: 自动迁移旧 todo-management 数据
- **双环学习**: 内置预测- outcome 追踪
- **通用场景**: 适用于所有定时任务（日常签到、活动提醒、周期性维护等）

## Installation

```bash
# Clone into your OpenClaw skills directory
cd ~/.openclaw/workspace/skills
git clone https://github.com/mathlife/chronos.git
```

## Usage

### Unified Todo List

```bash
# 查看所有待办（合并周期任务 + 其他任务）
python3 skills/chronos/scripts/unified_todo.py list

# 添加任务（自动路由：复杂周期 → manager，简单任务 → entries）
python3 skills/chronos/scripts/unified_todo.py add "任务名" \
  --category "分组" \
  [--cycle-type once|daily|weekly|monthly_fixed|monthly_range|monthly_n_times] \
  [--time "HH:MM"] \
  [--weekday 0-6] \
  [--day 1-31] \
  [--range-start 1-31 --range-end 1-31] \
  [--n-per-month N]

# 完成任务
python3 skills/chronos/scripts/unified_todo.py complete <ID|FIN-occ_id>

# 查看详情
python3 skills/chronos/scripts/unified_todo.py show <ID|FIN-occ_id>
```

### Direct Manager

```bash
# 每日自动运行（cron 03:30）
python3 skills/chronos/scripts/financial_activity_manager.py

# 手动添加周期任务
python3 skills/chronos/scripts/financial_activity_manager.py --add \
  --name "活动名" \
  --category "日常" \
  --cycle-type monthly_n_times \
  --weekday 2 \
  --n-per-month 2 \
  --time "10:00"

# 批量完成活动（完成本月所有未完成任务）
python3 skills/chronos/scripts/financial_activity_manager.py --complete-activity <activity_id>
```

### Migration

```bash
# 预览迁移计划（将旧 entries 迁移到新表）
python3 skills/chronos/scripts/migrate_entries_to_financial.py

# 执行迁移
python3 skills/chronos/scripts/migrate_entries_to_financial.py --execute
```

## Examples

```bash
# 区间任务：每月11号到次月5号，每天13:55提醒
python3 skills/chronos/scripts/unified_todo.py add "每日活动" \
  --cycle-type monthly_range \
  --range-start 11 --range-end 5 \
  --time "13:55" \
  --category "日常"

# 每月N次：每周三10:00，每月最多2次
python3 skills/chronos/scripts/unified_todo.py add "周三抢券" \
  --cycle-type monthly_n_times \
  --weekday 2 \
  --n-per-month 2 \
  --time "10:00" \
  --category "活动"

# 每日签到
python3 skills/chronos/scripts/unified_todo.py add "每日签到" \
  --cycle-type daily \
  --time "09:00" \
  --category "日常"

# 每周提醒：每周四10:00
python3 skills/chronos/scripts/unified_todo.py add "周报提交" \
  --cycle-type weekly \
  --weekday 3 \
  --time "10:00" \
  --category "工作"

# 每月固定日期：每月15号09:00
python3 skills/chronos/scripts/unified_todo.py add "月度总结" \
  --cycle-type monthly_fixed \
  --day 15 \
  --time "09:00" \
  --category "工作"
```

## Architecture

- `core/`: 核心模块（数据库、调度、模型、双环学习）
- `scripts/`: 入口脚本
- `todo.db`: SQLite 数据库（共享）

## Migration from todo-management

1. 运行迁移脚本（自动将旧条目迁移到新表）
2. 切换使用 `unified_todo.py` 作为唯一入口
3. 可删除旧 `todo-management` skill（已备份）
4. Cron 任务 `financial_activity_manager` 每天 03:30 自动运行，接管提醒

## License

MIT

## Author

Created by Mirror (AI companion) for Kong.
