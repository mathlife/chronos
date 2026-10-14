[English](README.md)

# Chronos

⏰ 开源的周期性任务调度器：像 cron 一样定义"每隔一段时间做一次"的事，自动通过 Telegram / Webhook 发送提醒，必要时把任务落成系统级定时作业。不用搭服务器，一个 CLI 全部搞定。

[![CI](https://github.com/mathlife/chronos/actions/workflows/ci.yml/badge.svg)](https://github.com/mathlife/chronos/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 这是什么

Chronos 帮你管理所有周期性的事——每天、每周、每月、每 N 小时、某天一次：

- **到点提醒**：通过 Telegram 或 Webhook 把提醒推送到你手机上，而不是只写进某台服务器的日志里
- **自动执行**：系统类任务可以直接落成 Linux crontab 作业，到点自动跑脚本
- **好管**：自带 Web 管理界面，任务的增删改查、通知渠道配置都在浏览器里点几下完成
- **好接**：提供 JSON 命令行 API，脚本、其他程序、AI Agent 都能调用

## 谁适合用

- **个人用户**：还信用卡提醒、吃药提醒、定期备份、每月抢券（比如农行每月秒杀）、域名续费提醒……
- **开发者 / 运维**：定时跑脚本、服务健康检查、日志清理、数据同步，不用再为每个小任务写 systemd unit
- **AI Agent 工作流**：给 OpenClaw 这类 Agent 提供"到点做事"的能力，通过 JSON API 集成

## 快速开始（5 分钟）

```bash
# 1. 交互式配置（选择通知渠道）
python3 skills/chronos/scripts/setup_config.py --interactive

# 非交互式示例：Telegram 渠道
python3 skills/chronos/scripts/setup_config.py \
  --channel telegram \
  --channel-id tg-main \
  --bot-token "<bot_token>" \
  --chat-id "<chat_id>"

# Webhook 渠道
python3 skills/chronos/scripts/setup_config.py \
  --channel webhook \
  --channel-id hook-main \
  --webhook-url "https://example.com/chronos-hook"

# 2. 添加任务
# 每天 09:30 提醒
python3 skills/chronos/scripts/todo.py add "还信用卡" \
  --cycle-type daily \
  --time 09:30

# 每 4 小时一次
python3 skills/chronos/scripts/todo.py add "喝水休息" \
  --cycle-type hourly \
  --interval-hours 4 \
  --time 08:00

# 每月 1、10、25 号
python3 skills/chronos/scripts/todo.py add "检查账单" \
  --cycle-type monthly_dates \
  --monthly-dates 1,10,25 \
  --time 09:00

# 每周一 10:00
python3 skills/chronos/scripts/todo.py add "每周例会" \
  --cycle-type weekly \
  --weekday 0 \
  --time 10:00

# 3. 查看今天的任务
python3 skills/chronos/scripts/todo.py today
```

## 和其他方案比

| | cron | systemd timers | APScheduler | **Chronos** |
|---|---|---|---|---|
| 通知提醒 | ❌（需自己接邮件） | ❌ | ❌（需自己写） | ✅ Telegram / Webhook 开箱即用 |
| 管理界面 | ❌ | ❌ | ❌ | ✅ Web Dashboard |
| 跨平台 | 仅 Unix | 仅 Linux | ✅（嵌入代码） | ✅（独立工具，Linux/macOS） |
| 使用门槛 | 写 crontab 表达式 | 写 unit 文件 | 写 Python 代码 | ✅ 一条命令添加任务 |
| 程序化调用 | ❌ | ❌ | ✅ | ✅ JSON CLI API |
| 定时执行脚本 | ✅ | ✅ | 需自己实现 | ✅ 可落成 crontab 作业 |

一句话：cron 只管"到点执行"，Chronos 还管"到点提醒你"和"让你管得明白"。

## Web 管理界面

```bash
# 本机访问（默认，无需鉴权）
python3 skills/chronos/scripts/web_dashboard.py --host 127.0.0.1 --port 8765

# 远程访问（建议开启基础鉴权）
python3 skills/chronos/scripts/web_dashboard.py \
  --host 0.0.0.0 \
  --port 8765 \
  --basic-auth "admin:change-me-now"

# 只读模式（仅查询，禁用修改接口）
python3 skills/chronos/scripts/web_dashboard.py --host 127.0.0.1 --port 8765 --read-only
```

浏览器打开 `http://127.0.0.1:8765/`：查看配置、通知渠道、全部周期任务、今日任务；增删改任务、管理渠道、健康检查都在界面里完成。

远程部署建议：保持 `--host 127.0.0.1` 并通过可信反向代理暴露；绑定非本地地址时务必设置 `--basic-auth`。

## JSON API（供程序/脚本调用）

`scripts/chronos_api.py` 提供机器友好的 JSON 输入输出：

```bash
# 列出任务
python3 skills/chronos/scripts/chronos_api.py task list --active-only all

# 创建任务
python3 skills/chronos/scripts/chronos_api.py task create \
  --payload '{"name":"每周例会","cycle_type":"weekly","weekday":0,"time_of_day":"10:00","task_kind":"scheduled"}'

# 更新任务
python3 skills/chronos/scripts/chronos_api.py task update --id 12 \
  --payload '{"delivery_target":"tg-main,hook-main"}'

# 配置通知渠道
python3 skills/chronos/scripts/chronos_api.py channel put \
  --payload '{"id":"tg-main","type":"telegram","enabled":true,"config":{"bot_token":"<token>","chat_id":"<chat_id>"}}'
```

所有命令返回 JSON：成功 `{"ok": true, "data": ...}`，失败 `{"ok": false, "error": "..."}`。

## 任务类型速查

| cycle_type | 说明 | 示例 |
|---|---|---|
| `once` | 单次，指定日期 | `--cycle-type once --start-date 2026-05-01 --time 10:00` |
| `daily` | 每天 | `--cycle-type daily --time 09:30` |
| `hourly` | 每 N 小时（按天展开） | `--cycle-type hourly --interval-hours 4 --time 08:00` |
| `weekly` | 每周（0=周一） | `--cycle-type weekly --weekday 0 --time 10:00` |
| `monthly_dates` | 每月固定几号 | `--cycle-type monthly_dates --monthly-dates 1,10,25` |
| `monthly_range` | 每月日期窗口内，可配每月限额 | `--cycle-type monthly_range --range-start 1 --range-end 31 --n-per-month 1` |

系统类任务（自动执行脚本而非仅提醒）：加 `--task-kind system --system-command "python3 /path/to/script.py"`，Chronos 会在 Linux 上为其创建 crontab 作业。详见下方"深入"部分的说明。

## 深入

- [架构与数据模型](docs/architecture.md) —— 表结构、各类调度的语义、系统任务的执行机制
- [历史数据迁移](docs/architecture.md#历史数据迁移) —— 从旧版 `entries` 表迁移到标准任务的操作步骤
- [更新日志](CHANGELOG.md)

## License

MIT
