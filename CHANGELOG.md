# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- End-to-end reminder chain coverage for occurrence creation, reminder persistence, immediate reminder fallback, missing-config behavior, and cleanup.
- `scripts/check_git_hygiene.sh` to fail fast when tracked `__pycache__` or `*.pyc` artifacts leak into the repo.
- `core/openclaw_cron.py` helper to centralize OpenClaw cron add/remove command construction.
- `scripts/schema_preflight.py` to verify the actual runtime DB, required tables, key constraints, duplicate occurrence groups, and invalid statuses before any schema migration.

### Changed
- Reminder cron creation/removal now goes through a shared helper instead of duplicating argv construction.
- README verification steps now include config diagnostics, schema preflight, full test discovery, legacy cron dry-run cleanup, and git hygiene checks.

### Fixed
- Removed previously tracked Python cache artifacts from git history going forward.
- Eliminated changelog duplication and aligned the current hardening notes with real behavior.

## [1.0.0] - 2026-03-16

### Added
- Initial stable release.
