# Chronos

通用周期任务管理器 - 适用于所有定时任务场景。支持6种周期类型、每月N次配额、自动cron、统一视图。

## Features

- **6种周期类型**: `once`, `daily`, `weekly`, `monthly_fixed`, `monthly_range`, `monthly_n_times`
- **智能配额**: 每月N次基于活动日计数，用完后自动完成剩余日期（适用于抢券、签到等限次活动）
- **自动提醒**: Cron 任务自动生成/清理，无需手动管理
- **统一视图**: `todo.py` 合并显示周期任务和其他任务
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
python3 skills/chronos/scripts/todo.py list

# 添加任务（自动路由：复杂周期 → manager，简单任务 → entries）
python3 skills/chronos/scripts/todo.py add "任务名" \
  --category "分组" \
  [--cycle-type once|daily|weekly|monthly_fixed|monthly_range|monthly_n_times] \
  [--time "HH:MM"] \
  [--weekday 0-6] \
  [--day 1-31] \
  [--range-start 1-31 --range-end 1-31] \
  [--n-per-month N]

# 完成任务
python3 skills/chronos/scripts/todo.py complete <ID|FIN-occ_id>

# 查看详情
python3 skills/chronos/scripts/todo.py show <ID|FIN-occ_id>
```

### Direct Manager

```bash
# 每日自动运行（cron 03:30）
python3 skills/chronos/scripts/periodic_task_manager.py

# 手动添加周期任务
python3 skills/chronos/scripts/periodic_task_manager.py --add \
  --name "任务名" \
  --category "日常" \
  --cycle-type monthly_n_times \
  --weekday 2 \
  --n-per-month 2 \
  --time "10:00"

# 批量完成活动（完成本月所有未完成任务）
python3 skills/chronos/scripts/periodic_task_manager.py --complete-activity <task_id>
```

### Migration

旧 `financial_*` 表数据已迁移到 `periodic_*` 新表，迁移脚本已删除。系统已自动完成迁移。

## Examples

```bash
# 区间任务：每月11号到次月5号，每天13:55提醒
python3 skills/chronos/scripts/todo.py add "每日活动" \
  --cycle-type monthly_range \
  --range-start 11 --range-end 5 \
  --time "13:55" \
  --category "日常"

# 每月N次：每周三10:00，每月最多2次
python3 skills/chronos/scripts/todo.py add "周三抢券" \
  --cycle-type monthly_n_times \
  --weekday 2 \
  --n-per-month 2 \
  --time "10:00" \
  --category "活动"

# 每日签到
python3 skills/chronos/scripts/todo.py add "每日签到" \
  --cycle-type daily \
  --time "09:00" \
  --category "日常"

# 每周提醒：每周四10:00
python3 skills/chronos/scripts/todo.py add "周报提交" \
  --cycle-type weekly \
  --weekday 3 \
  --time "10:00" \
  --category "工作"

# 每月固定日期：每月15号09:00
python3 skills/chronos/scripts/todo.py add "月度总结" \
  --cycle-type monthly_fixed \
  --day 15 \
  --time "09:00" \
  --category "工作"
```

## Configuration

Chronos supports configurable reminder destinations via chat ID.

### Chat ID Configuration

Reminder notifications are sent to a specific chat. Configure using:

**1. Environment variable (highest priority):**
```bash
export CHRONOS_CHAT_ID="your_chat_id_here"
```

**2. Config file (fallback):**
Create `~/.config/chronos/config.json`:
```json
{
  "chat_id": "your_chat_id_here"
}
```

**3. Default:** If neither is set, uses `YOUR_CHAT_ID` (original hardcoded value).

### Priority Order

1. `CHRONOS_CHAT_ID` environment variable
2. `~/.config/chronos/config.json` → `chat_id` field
3. Default: `YOUR_CHAT_ID`

### Examples

```bash
# Method 1: Environment variable (temporary, session-only)
CHRONOS_CHAT_ID="12345678" python3 skills/chronos/scripts/todo.py add "每日签到" --cycle-type daily --time "09:00"

# Method 2: Persistent environment variable (add to ~/.bashrc or ~/.profile)
export CHRONOS_CHAT_ID="12345678"

# Method 3: Config file (persistent, no shell config needed)
mkdir -p ~/.config/chronos
cat > ~/.config/chronos/config.json <<EOF
{
  "chat_id": "12345678"
}
EOF
```

## Testing Configuration

Run the included test suite to verify configuration works:

```bash
python3 skills/chronos/scripts/test_config.py
```

## Architecture

- `core/`: 核心模块（数据库、调度、模型、双环学习）
- `scripts/`: 入口脚本
- `todo.db`: SQLite 数据库（共享）

## Migration from todo-management

数据已自动迁移完成。现在使用 `todo.py` 作为唯一入口，`periodic_task_manager.py` 作为周期任务管理器。Cron 任务每天 03:30 自动运行。

## License

MIT

## Author

Created by Mirror (AI companion) for Kong.
