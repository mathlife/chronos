[中文](README.zh.md)

# Chronos

⏰ An open-source recurring-task scheduler: define "do this every so often" like cron, get reminded via Telegram / Webhooks, and optionally materialize tasks as OS-level scheduled jobs. No server to run — one CLI does it all.

[![CI](https://github.com/mathlife/chronos/actions/workflows/ci.yml/badge.svg)](https://github.com/mathlife/chronos/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is it

Chronos manages everything recurring — daily, weekly, monthly, every N hours, one-shot on a date:

- **Reminders that reach you**: pushes via Telegram or Webhooks to your phone, not just lines in some server's log
- **Automatic execution**: system tasks can be materialized as Linux crontab jobs that run scripts on schedule
- **Easy to manage**: built-in Web dashboard for creating, editing, and organizing tasks and notification channels
- **Easy to integrate**: JSON CLI API so scripts, other programs, and AI agents can drive it

## Who is it for

- **Individuals**: credit-card repayment reminders, medication reminders, regular backups, monthly flash-sale alerts, domain renewal reminders…
- **Developers / ops**: scheduled scripts, health checks, log rotation, data syncs — without writing a systemd unit for every little job
- **AI agent workflows**: gives agents like OpenClaw "do things on schedule" capability via the JSON API

## Quickstart (5 minutes)

```bash
# 1. Interactive setup (choose notification channels)
python3 skills/chronos/scripts/setup_config.py --interactive

# Non-interactive example: Telegram channel
python3 skills/chronos/scripts/setup_config.py \
  --channel telegram \
  --channel-id tg-main \
  --bot-token "<bot_token>" \
  --chat-id "<chat_id>"

# Webhook channel
python3 skills/chronos/scripts/setup_config.py \
  --channel webhook \
  --channel-id hook-main \
  --webhook-url "https://example.com/chronos-hook"

# 2. Add tasks
# Daily at 09:30
python3 skills/chronos/scripts/todo.py add "Pay credit card" \
  --cycle-type daily \
  --time 09:30

# Every 4 hours
python3 skills/chronos/scripts/todo.py add "Stretch break" \
  --cycle-type hourly \
  --interval-hours 4 \
  --time 08:00

# On the 1st, 10th, 25th of each month
python3 skills/chronos/scripts/todo.py add "Review bills" \
  --cycle-type monthly_dates \
  --monthly-dates 1,10,25 \
  --time 09:00

# Every Monday at 10:00
python3 skills/chronos/scripts/todo.py add "Weekly standup" \
  --cycle-type weekly \
  --weekday 0 \
  --time 10:00

# 3. See today's tasks
python3 skills/chronos/scripts/todo.py today
```

## How it compares

| | cron | systemd timers | APScheduler | **Chronos** |
|---|---|---|---|---|
| Notifications | ❌ (wire up email yourself) | ❌ | ❌ (write it yourself) | ✅ Telegram / Webhooks out of the box |
| Management UI | ❌ | ❌ | ❌ | ✅ Web dashboard |
| Cross-platform | Unix only | Linux only | ✅ (embedded in code) | ✅ (standalone tool, Linux/macOS) |
| Learning curve | crontab expressions | unit files | Python code | ✅ one command per task |
| Programmatic API | ❌ | ❌ | ✅ | ✅ JSON CLI API |
| Run scripts on schedule | ✅ | ✅ | DIY | ✅ materializes as crontab jobs |

In one line: cron only does "run on time" — Chronos also does "remind me on time" and "let me manage it all clearly".

## Web dashboard

```bash
# Local access (default, no auth required)
python3 skills/chronos/scripts/web_dashboard.py --host 127.0.0.1 --port 8765

# Remote access (recommended: enable basic auth)
python3 skills/chronos/scripts/web_dashboard.py \
  --host 0.0.0.0 \
  --port 8765 \
  --basic-auth "admin:change-me-now"

# Read-only mode (queries only, mutations disabled)
python3 skills/chronos/scripts/web_dashboard.py --host 127.0.0.1 --port 8765 --read-only
```

Open `http://127.0.0.1:8765/` in your browser: inspect config, channels, all periodic tasks, today's tasks; create/update/remove tasks, manage channels, and check health — all in the UI.

For remote deployment, keep `--host 127.0.0.1` and expose it through a trusted reverse proxy; always set `--basic-auth` when binding a non-local address.

## JSON API (for programs and scripts)

`scripts/chronos_api.py` provides machine-friendly JSON input/output:

```bash
# List tasks
python3 skills/chronos/scripts/chronos_api.py task list --active-only all

# Create a task
python3 skills/chronos/scripts/chronos_api.py task create \
  --payload '{"name":"Weekly standup","cycle_type":"weekly","weekday":0,"time_of_day":"10:00","task_kind":"scheduled"}'

# Update a task
python3 skills/chronos/scripts/chronos_api.py task update --id 12 \
  --payload '{"delivery_target":"tg-main,hook-main"}'

# Configure a notification channel
python3 skills/chronos/scripts/chronos_api.py channel put \
  --payload '{"id":"tg-main","type":"telegram","enabled":true,"config":{"bot_token":"<token>","chat_id":"<chat_id>"}}'
```

Every command returns JSON: success `{"ok": true, "data": ...}`, error `{"ok": false, "error": "..."}`.

## Task types cheat sheet

| cycle_type | Meaning | Example |
|---|---|---|
| `once` | One-shot on a date | `--cycle-type once --start-date 2026-05-01 --time 10:00` |
| `daily` | Every day | `--cycle-type daily --time 09:30` |
| `hourly` | Every N hours (expanded per day) | `--cycle-type hourly --interval-hours 4 --time 08:00` |
| `weekly` | Weekly (0 = Monday) | `--cycle-type weekly --weekday 0 --time 10:00` |
| `monthly_dates` | Fixed days of month | `--cycle-type monthly_dates --monthly-dates 1,10,25` |
| `monthly_range` | Day window each month, optional monthly quota | `--cycle-type monthly_range --range-start 1 --range-end 31 --n-per-month 1` |

System tasks (run a script instead of just reminding): add `--task-kind system --system-command "python3 /path/to/script.py"` and Chronos creates a crontab job for it on Linux. See the internals doc below for details.

## Go deeper

- [Architecture & data model](docs/architecture.md) — table layout, scheduling semantics, how system tasks execute
- [Historical data migration](docs/architecture.md#historical-data-migration) — migrating from the legacy `entries` table
- [Changelog](CHANGELOG.md)

## License

MIT
