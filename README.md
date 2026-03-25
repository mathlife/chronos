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

python3 skills/chronos/scripts/migrate_legacy_entries.py --db /home/ubuntu/.openclaw/workspace/todo.db
python3 skills/chronos/scripts/migrate_legacy_entries.py --db /home/ubuntu/.openclaw/workspace/todo.db --apply
python3 skills/chronos/scripts/todo.py complete-overdue --dry-run
python3 skills/chronos/scripts/schema_preflight.py
```

## Legacy migration policy

Phase 2 adds `scripts/migrate_legacy_entries.py` for conservative migration out of `entries`.

What it will do automatically:
- link obvious legacy rows to an already-existing canonical task by deterministic normalized name
- create an explicit `task_kind=system` + `special_handler=meta_review_fallback` task for legacy Meta-Review rows
- create canonical tasks for simple bracketed recurring rows only when the schedule is deterministic

What it will not do automatically:
- ambiguous free-text rows
- unsupported cadences like `每 4 小时 ...` (these stay in manual review until a richer cadence model or another owner is chosen)

Traceability is preserved through `periodic_tasks.legacy_entry_id` and `source` (`legacy_entries_linked` / `legacy_entries_migrated`).
