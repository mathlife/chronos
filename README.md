# Chronos

Chronos is a lightweight recurring-task engine backed by `periodic_tasks` + `periodic_occurrences`, with temporary compatibility for legacy `entries` rows.

## Phase-1 data model direction

- `periodic_tasks` is the canonical definition table for scheduled work.
- `periodic_occurrences` is the canonical execution/reminder table.
- `entries` remains only for inbox-style one-shot notes and legacy compatibility.
- Scheduled `once` tasks with an explicit `start_date` now use canonical task storage.
- `monthly_dates` is a supported cycle type.
- Special system behaviors should live in explicit task metadata (`special_handler`) instead of free-text regex whenever possible.

## Quick examples

```bash
python3 skills/chronos/scripts/todo.py add "一次性计划任务" \
  --cycle-type once \
  --start-date 2026-03-27 \
  --time 10:00

python3 skills/chronos/scripts/todo.py add "Meta-Review fallback" \
  --cycle-type daily \
  --time 02:00 \
  --task-kind system \
  --special-handler meta_review_fallback

python3 skills/chronos/scripts/todo.py complete-overdue --dry-run
python3 skills/chronos/scripts/schema_preflight.py
```
