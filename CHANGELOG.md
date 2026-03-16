# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Major Refactor**: Renamed to **Chronos** - Universal Periodic Task Manager
- Generic naming: `financial_*` → `periodic_*` (tables, variables, scripts)
- Standalone unified entry: `todo.py` (replaces `unified_todo.py`)
- New manager: `periodic_task_manager.py` (replaces `financial_activity_manager.py`)
- Database schema: `periodic_tasks` + `periodic_occurrences`
- Support for all periodic use cases (not just financial)
- Robust cron scheduling with past-time protection
- Quota auto-complete fix (only completes up to today)
- **End date support**: Tasks can have `end_date`; scheduling respects it (no occurrences after end)
- **Natural language parsing**: Extract end date from phrases like "到2025年3月31日结束"
- **Double-loop learning integration**: Added LearningContext to all critical operations

### Changed
- Unified todo no longer calls `todo.sh` - direct database access
- Improved `complete_activity_cycle` logic to avoid auto-completing future dates
- TaskScheduler now considers `end_date` in `should_remind_today()` and `get_occurrences_for_month()`

### Fixed
- Cron scheduling failures for past times (now skipped)
- monthly_n_times auto-complete bug (limited to current month up to today)

## [1.0.0] - 2026-03-16

Initial stable release.

## [1.0.0] - 2026-03-16

Initial stable release.
