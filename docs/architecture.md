# Chronos 架构与数据模型

> 面向开发者/贡献者的内部文档。用户文档见 [README](../README.md)。

## 数据模型

- `periodic_tasks`：调度任务的定义表（canonical）。
- `periodic_occurrences`：执行/提醒记录表（canonical）。
- `entries`：仅保留用于收件箱式一次性备忘和历史兼容，不再作为调度状态。

支持的周期类型：`once`（指定 `start_date` 的单次任务）、`daily`、`hourly`（`interval_hours` 1–24 + `time_of_day` 锚点）、`weekly`、`monthly_dates`（固定日期列表）、`monthly_range`（日期窗口，可选 `n_per_month` 月限额，支持跨月窗口如 `10 -> 次月 03`）。

兼容性：旧 `monthly_fixed` 归一化为 `monthly_dates`；旧 `monthly_n_times` 归一化为 `monthly_range` + `n_per_month`。

## Hourly 展开语义

`cycle_type=hourly` 按天展开：从 `time_of_day` 锚点出发，每 `interval_hours` 生成当天所有同分钟时刻（含锚点之前）。例如 `--interval-hours 4 --time 08:00` 展开为 `00:00, 04:00, 08:00, 12:00, 16:00, 20:00`。不引入跨天偏移状态。

## Monthly quota 语义

`monthly_range` + `n_per_month` 的典型用法（例：每月秒杀每月限 1 次）：在月限额未用完时按天提醒；一旦某次 occurrence 被完成，消耗月限额，当月剩余提醒自动完成不再触发；下个月界重置后自然恢复。实现上 `weekday=NULL` 表示按天节奏而非按周节奏。

## 系统任务执行机制

`task_kind=system` + `--system-command` 的任务会在 Linux 上被物化为 crontab 作业：

- 只接受已存在的 `.py` / `.sh` 文件；禁止内联（`-c`）、模块（`-m`）和 stdin 执行
- 脚本必须位于 Chronos 项目根目录或 `system_command_allowed_roots` 配置的目录下
- 执行状态机：原子认领为 `running` → 成功 `completed` / 策略拒绝 `skipped` / 命令失败 `failed`
- 系统任务不发送 5 分钟提前提醒
- 各通知渠道独立重试，失败渠道不影响已成功的渠道

特殊行为通过任务元数据 `special_handler` 显式声明，而非自由文本正则匹配。

## 历史数据迁移

### 迁移策略

`scripts/migrate_legacy_entries.py` 保守地把旧 `entries` 表迁移到标准任务：

自动处理：
- 可确定性关联到已存在标准任务的旧行（按归一化名称链接）
- 旧 Meta-Review 行 → `task_kind=system` + `special_handler=meta_review_fallback` 任务
- 旧 `每 N 小时 … memory_manager.py sync` 行 → `task_kind=system` + `cycle_type=hourly` + `special_handler=sync_subagent_memory` 任务
- 调度语义确定的简单括号式循环行 → 新建标准任务

不自动处理：语义模糊的自由文本行、无已知 handler 语义的 every-N-hours 行。

通过 `periodic_tasks.legacy_entry_id` 和 `source`（`legacy_entries_linked` / `legacy_entries_migrated`）保留可追溯性。

### 归档步骤

迁移完成后运行 `scripts/archive_legacy_entries.py`：

1. 找到已被 `periodic_tasks.legacy_entry_id` 链接的 `entries` 行
2. 补归档元数据列（`chronos_readonly`、`chronos_archived_at`、`chronos_archive_reason`、`chronos_archived_from_status`、`chronos_linked_task_id`）
3. 优先使用 `status='archived'` 作为一等归档标记；旧的受限枚举不支持时回退到元数据
4. 原行保留不动以备审计；`todo.py complete/skip` 对只读归档行会直接拒绝并指回关联的标准任务

推荐顺序：

```bash
python3 skills/chronos/scripts/migrate_legacy_entries.py --db /path/to/.Chonos/config/todo.db
python3 skills/chronos/scripts/migrate_legacy_entries.py --db /path/to/.Chonos/config/todo.db --apply
python3 skills/chronos/scripts/archive_legacy_entries.py --db /path/to/.Chonos/config/todo.db
python3 skills/chronos/scripts/archive_legacy_entries.py --db /path/to/.Chonos/config/todo.db --apply
python3 skills/chronos/scripts/normalize_historical_residues.py --db /path/to/.Chonos/config/todo.db
python3 skills/chronos/scripts/normalize_historical_residues.py --db /path/to/.Chonos/config/todo.db --apply
```

### 历史残留清理

`normalize_historical_residues.py` 只处理已脱离正常语义的残留：

- `task_id` 已不存在的孤儿 `periodic_occurrences`（删除）
- `cycle_type='once'` 但 `start_date IS NULL` 且能安全推断出唯一日期的任务（回填 `start_date`）

其余一律不动，留待人工处理。
