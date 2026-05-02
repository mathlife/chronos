# Chronos

Chronos 是一个轻量级周期任务引擎，核心基于 `periodic_tasks` + `periodic_occurrences`，并对历史 `entries` 数据保留兼容能力。

当前 Chronos 运行时已不再依赖 `openclaw` 完成常规调度与通知。`openclaw`（或其他外部调用方）可以通过 `scripts/chronos_api.py` 提供的 JSON CLI 接口进行集成。

## Phase-1 数据模型方向

- `periodic_tasks`：任务定义主表（canonical definition）。
- `periodic_occurrences`：执行/提醒实例主表（canonical execution/reminder）。
- `entries`：仅保留 inbox 风格单次任务和 legacy 兼容用途。
- 带 `start_date` 的 `once` 任务已迁移到 canonical 存储。
- `system` 任务可在 Linux 上通过 `crontab` 落地为系统级调度任务。
- 支持 `monthly_dates` 周期类型。
- 支持 `hourly` 周期类型（`interval_hours` + `time_of_day` 锚点语义）。
- `monthly_n_times` 支持 `weekday=0..6`（按周）和 `weekday=NULL`（按天），并继续执行月度配额。
- 特殊系统行为建议通过显式 `special_handler` 元数据表达，而不是依赖自由文本正则。

## 快速示例

```bash
python3 skills/chronos/scripts/setup_config.py --interactive

# Linux 非交互初始化示例
# Telegram
python3 skills/chronos/scripts/setup_config.py \
  --channel telegram \
  --channel-id tg-main \
  --bot-token "<bot_token>" \
  --chat-id "<chat_id>"

# Webhook
python3 skills/chronos/scripts/setup_config.py \
  --channel webhook \
  --channel-id hook-main \
  --webhook-url "https://example.com/chronos-hook"

# 可选：自定义配置路径
export CHRONOS_CONFIG_PATH="$(pwd)/.Chonos/config/config.json"

python3 skills/chronos/scripts/todo.py add "一次性计划任务" \
  --cycle-type once \
  --start-date 2026-03-27 \
  --time 10:00

python3 skills/chronos/scripts/todo.py add "同步 subagent 记忆" \
  --cycle-type hourly \
  --interval-hours 4 \
  --time 08:00 \
  --task-kind system \
  --special-handler sync_subagent_memory

python3 skills/chronos/scripts/todo.py add "Meta-Review fallback" \
  --cycle-type daily \
  --time 02:00 \
  --task-kind system \
  --special-handler meta_review_fallback

python3 skills/chronos/scripts/todo.py add "刷新本地缓存" \
  --cycle-type daily \
  --time 09:30 \
  --task-kind system \
  --system-command "powershell -NoProfile -File D:\\ops\\refresh-cache.ps1"

python3 skills/chronos/scripts/migrate_legacy_entries.py --db "$(pwd)/.Chonos/config/todo.db"
python3 skills/chronos/scripts/migrate_legacy_entries.py --db "$(pwd)/.Chonos/config/todo.db" --apply
python3 skills/chronos/scripts/archive_legacy_entries.py --db "$(pwd)/.Chonos/config/todo.db"
python3 skills/chronos/scripts/archive_legacy_entries.py --db "$(pwd)/.Chonos/config/todo.db" --apply
python3 skills/chronos/scripts/normalize_historical_residues.py --db "$(pwd)/.Chonos/config/todo.db"
python3 skills/chronos/scripts/normalize_historical_residues.py --db "$(pwd)/.Chonos/config/todo.db" --apply
python3 skills/chronos/scripts/todo.py complete-overdue --dry-run
python3 skills/chronos/scripts/schema_preflight.py
```

## 集成 API（给 OpenClaw 或其他调用方）

`scripts/chronos_api.py` 提供机器友好的 JSON 输入输出，覆盖任务与通知渠道管理。

```bash
# 列出任务
python3 skills/chronos/scripts/chronos_api.py task list --active-only all

# 创建任务
python3 skills/chronos/scripts/chronos_api.py task create \
  --payload '{"name":"每周例会","cycle_type":"weekly","weekday":0,"time_of_day":"10:00","task_kind":"scheduled"}'

# 更新任务
python3 skills/chronos/scripts/chronos_api.py task update --id 12 \
  --payload '{"delivery_target":"tg-main,hook-main"}'

# 新增或更新通知渠道
python3 skills/chronos/scripts/chronos_api.py channel put \
  --payload '{"id":"tg-main","type":"telegram","enabled":true,"config":{"bot_token":"<token>","chat_id":"<chat_id>"}}'

# 删除通知渠道
python3 skills/chronos/scripts/chronos_api.py channel remove --id tg-main
```

返回格式统一为 JSON：

- 成功：`{"ok": true, "data": ...}`
- 失败：`{"ok": false, "error": "..."}`

## Web Dashboard（查询 + 管理）

Chronos 内置 Web UI，可查看与维护配置和任务。

```bash
# Linux 本地访问（默认，无需鉴权）
python3 skills/chronos/scripts/web_dashboard.py --host 127.0.0.1 --port 8765

# Linux 远程访问（建议开启 Basic Auth）
python3 skills/chronos/scripts/web_dashboard.py \
  --host 0.0.0.0 \
  --port 8765 \
  --basic-auth "admin:change-me-now"

# 只读模式（只查询，不允许写接口）
python3 skills/chronos/scripts/web_dashboard.py --host 127.0.0.1 --port 8765 --read-only
```

浏览器打开：

- `http://127.0.0.1:8765/`

功能：

- 查询：运行时配置、通知渠道、全部周期任务、当日任务。
- 管理：任务新增/修改/删除（停用或硬删除）、渠道 upsert/remove、legacy `chat_id` 更新。

部署建议：

- 推荐保持 `--host 127.0.0.1`，需要远程访问时通过可信反向代理暴露。
- 若绑定非本地地址，请设置 `--basic-auth user:password`。否则服务会默认拒绝启动（除非显式传 `--allow-unauthenticated-remote`）。

## hourly 语义说明

`cycle_type=hourly` 按“天内时隙”扩展。

必填字段：

- `interval_hours`：1-24 的整数。
- `time_of_day`：`HH:MM` 锚点时间。

每天扩展规则：

- 从锚点分钟开始。
- 每隔 `interval_hours` 生成同分钟时隙。
- 同时包含当天更早时隙。

例如：`--interval-hours 4 --time 08:00` 会扩展为：

- `00:00, 04:00, 08:00, 12:00, 16:00, 20:00`

该规则与历史“每 4 小时同步 subagent 记忆”的期望一致，且不引入跨天偏移状态。

## 任务分类

Chronos 面向用户的调度分类：

- `once`：一次性任务（必须给 `start_date`）。
- `monthly_n_times`：每月最多完成 `n_per_month` 次，可选 `weekday` 约束。
- 其他周期任务：`daily`、`hourly`、`weekly`、`monthly_fixed`、`monthly_range`、`monthly_dates`。

对于确定性命令型系统任务：

- 设定 `task_kind=system`。
- 通过 `--system-command` 隐式设置 `special_handler=run_command`。
- Chronos 在 Linux 上通过 `crontab` 为提醒与执行分别建系统任务。
- 提醒在 `scheduled_time` 前 5 分钟触发。
- 到达执行时间后，occurrence 直接标记为 `completed`（不继续停留在 `pending`）。

`sync_subagent_memory` 现在通过 `memory_manager.py pending-subagents` 读取 `memory/subagent_sync_ledger.json` 账本，不再从全部内存中猜测 session id。账本语义统一在 `scripts/subagent_sync_ledger.py`；OpenClaw 的 subagent 完成路径会自动写账本。`memory_manager.py record-subagent <session_id>` 仅用于人工补录。成功同步后会 `mark-subagent-synced`，失败则保持 pending 并追加错误轨迹。

当 `complete-overdue` 处理同一天同一任务下多条逾期 hourly occurrence，且该任务配置了 `special_handler` 时，Chronos 会按“任务+日期”批次仅执行一次 handler，再将每条 occurrence 标记为 `completion_mode=fallback_handler_merged`，并写入对应的 `special_handler_result` 追踪（`merge_key`、批次索引/总数、来源 occurrence）。

## 月配额 + 日频语义

例如 `福建农行秒杀京东卡`，目标语义不是纯 `daily`，而是：

- 当月配额未用尽时按天提醒。
- 任一 occurrence 完成后，消耗当月配额。
- 自动完成当月剩余 pending/reminded occurrence，停止后续提醒。
- 月初计数器重置后自然恢复。

建模方式：

- `cycle_type=monthly_n_times`
- `n_per_month=1`
- `weekday=NULL`（表示日频而非周频）

## Legacy 迁移策略

Phase 2+ 引入 `scripts/migrate_legacy_entries.py`，以保守策略将任务从 `entries` 迁移到 canonical 模型。

自动处理：

- 按规范化名称将明显 legacy 行关联到已存在 canonical 任务。
- 为 legacy Meta-Review 行创建显式 `task_kind=system` + `special_handler=meta_review_fallback` 任务。
- 为 legacy `每 N 小时 ... memory_manager.py sync` 行创建显式 `task_kind=system` + `cycle_type=hourly` + `special_handler=sync_subagent_memory` 任务。
- 对规则确定的 bracket 周期文本行创建 canonical 任务。

不会自动处理：

- 语义模糊的自由文本。
- 无明确 handler 语义的 every-N-hours 行。

可追溯性通过 `periodic_tasks.legacy_entry_id` 与 `source`（`legacy_entries_linked` / `legacy_entries_migrated`）保留。

## Legacy 最终归档步骤

完成迁移并建立关联后，执行 `scripts/archive_legacy_entries.py`。

作用：

- 找出已被 `periodic_tasks.legacy_entry_id` 关联的 `entries` 行。
- 若缺失则补齐归档元数据列：
  - `chronos_readonly`
  - `chronos_archived_at`
  - `chronos_archive_reason`
  - `chronos_archived_from_status`
  - `chronos_linked_task_id`
- 若运行中的 `entries` 模式允许，优先使用 `status='archived'` 作为一等归档标记。
- 对旧版受限状态枚举，继续以 `chronos_archived_*` 元数据作为兼容回退。
- 修复部分归档状态不完整的行。
- 原始行保留不删，便于审计与追溯。
- 因 linked 行已被 live 列表/快照/overdue 查询排除，活跃视图保持干净。

操作边界：

- live 调度状态仍以 `periodic_tasks` + `periodic_occurrences` 为准。
- legacy `entries` 的归档判定采用共享语义：优先 `status='archived'`，其次 `chronos_archived_at` / `chronos_archive_reason` / `chronos_archived_from_status`。
- Chronos 不做大范围 `entries.status` 枚举迁移；若部署仍不支持 `archived`，则保留原状态并依赖 `chronos_archived_*` 元数据。

操作效果：

- 已迁移 legacy 行不再被识别为 live legacy 任务。
- `todo.py show ID<n>` 仍可查看归档轨迹。
- `todo.py complete ID<n>` / `skip ID<n>` 对只读归档行将 fail-closed，并引导到关联的 canonical 周期任务。

推荐执行顺序：

1. `python3 skills/chronos/scripts/migrate_legacy_entries.py --db /path/to/.Chonos/config/todo.db`
2. `python3 skills/chronos/scripts/migrate_legacy_entries.py --db /path/to/.Chonos/config/todo.db --apply`
3. `python3 skills/chronos/scripts/archive_legacy_entries.py --db /path/to/.Chonos/config/todo.db`
4. `python3 skills/chronos/scripts/archive_legacy_entries.py --db /path/to/.Chonos/config/todo.db --apply`
5. `python3 skills/chronos/scripts/normalize_historical_residues.py --db /path/to/.Chonos/config/todo.db`
6. `python3 skills/chronos/scripts/normalize_historical_residues.py --db /path/to/.Chonos/config/todo.db --apply`

## 历史残留清理

`normalize_historical_residues.py` 的设计是“窄口径、可预测”，仅处理已经脱离 live 语义的残留：

- `task_id` 已失效的孤儿 `periodic_occurrences`。
- `cycle_type='once'` 且 `start_date IS NULL`，但可安全推导唯一 canonical 日期的任务。

规范化规则：

- 删除孤儿 occurrence。
- 若 once 任务只有一个 occurrence 日期，则 `start_date = 该日期`。
- 若 once 任务无 occurrence 但有 `end_date`，则 `start_date = end_date`。
- 其他情况保持不变，待人工复核。

这样可确保清理行为可解释、可审计，不改变活跃周期行为。
