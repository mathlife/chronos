"""Centralized state transitions for periodic occurrences.

This module intentionally focuses on DB state transitions only. External scheduler
side effects (creating/removing cron jobs) stay in service layers for now, but all
callers should converge here for occurrence status updates so terminal-state rules
and metadata handling remain consistent.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Protocol


TERMINAL_STATUSES = {"completed", "skipped"}
JOB_REF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("reminder", "reminder_job_id"),
    ("execution", "execution_job_id"),
)


def iter_job_refs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return available scheduler job refs from an occurrence payload.

    This centralizes the mapping from occurrence DB pointer columns to scheduler
    job kinds so service/API layers don't duplicate reminder/execution branching.
    """
    refs: list[tuple[str, str]] = []
    for kind, column in JOB_REF_COLUMNS:
        job_name = payload.get(column)
        if job_name:
            refs.append((kind, str(job_name)))
    return refs


def iter_job_refs_from_pair(reminder_job_id: Any, execution_job_id: Any) -> list[tuple[str, str]]:
    """Return available scheduler job refs from a raw (reminder, execution) pair."""
    return iter_job_refs({"reminder_job_id": reminder_job_id, "execution_job_id": execution_job_id})


class SqlExecutor(Protocol):
    def execute(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def commit(self) -> None:
        ...


class OccurrenceStateStore:
    """Small repository-like wrapper for periodic_occurrences state changes."""

    def __init__(self, db: SqlExecutor):
        self.db = db
        self._columns: set[str] | None = None

    def _occurrence_columns(self) -> set[str]:
        if self._columns is None:
            rows = self.db.execute("PRAGMA table_info(periodic_occurrences)").fetchall()
            self._columns = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}
        return self._columns

    def _has_column(self, column: str) -> bool:
        return column in self._occurrence_columns()

    def mark_reminded(self, occurrence_id: int, *, reminder_job_id: str | None = None, commit: bool = True) -> bool:
        """Move pending -> reminded; never alter terminal/non-pending status.

        Returns True only when the occurrence was actually moved to reminded.
        """
        cur = self.db.execute(
            """
            UPDATE periodic_occurrences
            SET status = 'reminded',
                reminder_job_id = COALESCE(?, reminder_job_id)
            WHERE id = ? AND status = 'pending'
            """,
            (reminder_job_id, occurrence_id),
        )
        changed = getattr(cur, "rowcount", 0) > 0
        if changed and commit:
            self.db.commit()
        return changed

    def complete(
        self,
        occurrence_id: int,
        *,
        completion_mode: str,
        special_handler_result: str | None = None,
        completion_source: str | None = None,
        trigger_label: str | None = None,
        trigger_command: str | None = None,
        clear_jobs: bool = True,
        commit: bool = True,
    ) -> bool:
        """Mark a non-terminal occurrence completed and attach completion metadata."""
        assignments = [
            "status = 'completed'",
            "completed_at = CURRENT_TIMESTAMP",
            "completion_mode = ?",
            "special_handler_result = COALESCE(?, special_handler_result)",
        ]
        params: list[object] = [completion_mode, special_handler_result]
        optional_values = {
            "completion_source": completion_source,
            "trigger_label": trigger_label,
            "trigger_command": trigger_command,
        }
        for column, value in optional_values.items():
            if self._has_column(column):
                assignments.append(f"{column} = COALESCE(?, {column})")
                params.append(value)
        if clear_jobs:
            if self._has_column("reminder_job_id"):
                assignments.append("reminder_job_id = NULL")
            if self._has_column("execution_job_id"):
                assignments.append("execution_job_id = NULL")
        params.append(occurrence_id)

        cur = self.db.execute(
            f"""
            UPDATE periodic_occurrences
            SET {', '.join(assignments)}
            WHERE id = ? AND status NOT IN ('completed', 'skipped')
            """,
            tuple(params),
        )
        changed = getattr(cur, "rowcount", 0) > 0
        if changed and commit:
            self.db.commit()
        return changed

    def skip(
        self,
        occurrence_id: int,
        *,
        completion_mode: str,
        special_handler_result: str | None = None,
        completion_source: str | None = None,
        trigger_label: str | None = None,
        trigger_command: str | None = None,
        commit: bool = True,
    ) -> bool:
        """Mark a non-terminal occurrence skipped and clear pending jobs."""
        assignments = [
            "status = 'skipped'",
            "completed_at = CURRENT_TIMESTAMP",
            "completion_mode = ?",
            "special_handler_result = COALESCE(?, special_handler_result)",
        ]
        params: list[object] = [completion_mode, special_handler_result]
        optional_values = {
            "completion_source": completion_source,
            "trigger_label": trigger_label,
            "trigger_command": trigger_command,
        }
        for column, value in optional_values.items():
            if self._has_column(column):
                assignments.append(f"{column} = COALESCE(?, {column})")
                params.append(value)
        if self._has_column("reminder_job_id"):
            assignments.append("reminder_job_id = NULL")
        if self._has_column("execution_job_id"):
            assignments.append("execution_job_id = NULL")
        params.append(occurrence_id)

        cur = self.db.execute(
            f"""
            UPDATE periodic_occurrences
            SET {', '.join(assignments)}
            WHERE id = ? AND status NOT IN ('completed', 'skipped')
            """,
            tuple(params),
        )
        changed = getattr(cur, "rowcount", 0) > 0
        if changed and commit:
            self.db.commit()
        return changed

    def job_pointer_columns(self) -> list[str]:
        """Return available occurrence job pointer columns in stable order."""
        columns = self._occurrence_columns()
        return [column for column in ("reminder_job_id", "execution_job_id") if column in columns]

    def find_ids_with_jobs(self, where_sql: str, params: tuple[object, ...] = ()) -> list[int]:
        """Return occurrence ids matching caller criteria and having any job pointer set.

        ``where_sql`` is an internal SQL fragment controlled by service code, not user input.
        The job-pointer predicate stays centralized here so callers do not need to know
        whether the current schema has reminder_job_id, execution_job_id, or both.
        """
        job_columns = self.job_pointer_columns()
        if not job_columns:
            return []
        not_null_predicate = " OR ".join(f"{column} IS NOT NULL" for column in job_columns)
        rows = self.db.execute(
            f"""
            SELECT id
            FROM periodic_occurrences
            WHERE ({where_sql}) AND ({not_null_predicate})
            """,
            params,
        ).fetchall()
        return [int(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]

    def find_ids_with_jobs_for_task(self, task_id: int) -> list[int]:
        return self.find_ids_with_jobs("task_id = ?", (task_id,))

    def find_ids_with_jobs_for_task_on_date(self, task_id: int, occ_date: str) -> list[int]:
        return self.find_ids_with_jobs("task_id = ? AND date = ?", (task_id, occ_date))

    def find_completed_ids_with_jobs_in_date_window(self, task_id: int, start_date: str, end_date: str) -> list[int]:
        return self.find_ids_with_jobs(
            "task_id = ? AND status = 'completed' AND date >= ? AND date <= ?",
            (task_id, start_date, end_date),
        )

    def find_ids_with_jobs_before_or_on(self, occ_date: str) -> list[int]:
        return self.find_ids_with_jobs("date <= ?", (occ_date,))

    def job_payloads_for_task(self, task_id: int) -> list[dict[str, Any]]:
        """Return scheduler job payload rows for a task's occurrences with job pointers."""
        occurrence_ids = self.find_ids_with_jobs_for_task(task_id)
        payloads: list[dict[str, Any]] = []
        for occurrence_id in occurrence_ids:
            select_expressions = [
                "o.id AS occurrence_id",
                "o.date",
                "o.scheduled_time",
                "t.time_of_day",
            ]
            for column in ("reminder_job_id", "execution_job_id"):
                if self._has_column(column):
                    select_expressions.append(f"o.{column} AS {column}")
                else:
                    select_expressions.append(f"NULL AS {column}")
            row = self.db.execute(
                f"""
                SELECT {', '.join(select_expressions)}
                FROM periodic_occurrences o
                JOIN periodic_tasks t ON t.id = o.task_id
                WHERE o.id = ?
                """,
                (occurrence_id,),
            ).fetchone()
            if row is None:
                continue
            if isinstance(row, sqlite3.Row):
                payloads.append({key: row[key] for key in row.keys()})
            else:
                keys = ["occurrence_id", "date", "scheduled_time", "time_of_day", "reminder_job_id", "execution_job_id"]
                payloads.append(dict(zip(keys, row)))
        return payloads

    def clear_jobs_for_ids(self, occurrence_ids: list[int], *, commit: bool = False) -> list[tuple[str | None, str | None]]:
        """Clear job pointers for multiple occurrences and return their previous job refs."""
        job_refs = [self.clear_jobs(occurrence_id, commit=False) for occurrence_id in occurrence_ids]
        if commit:
            self.db.commit()
        return job_refs

    def set_jobs(
        self,
        occurrence_id: int,
        *,
        reminder_job_id: str | None = None,
        execution_job_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        """Set available scheduler job pointer columns for one occurrence.

        Passing ``None`` preserves the existing value. Optional columns are skipped
        when running against older schemas.
        """
        assignments: list[str] = []
        params: list[object] = []
        if self._has_column("reminder_job_id"):
            assignments.append("reminder_job_id = COALESCE(?, reminder_job_id)")
            params.append(reminder_job_id)
        if self._has_column("execution_job_id"):
            assignments.append("execution_job_id = COALESCE(?, execution_job_id)")
            params.append(execution_job_id)
        if not assignments:
            return False
        params.append(occurrence_id)
        cur = self.db.execute(
            f"UPDATE periodic_occurrences SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
        changed = getattr(cur, "rowcount", 0) > 0
        if changed and commit:
            self.db.commit()
        return changed

    def update_non_terminal(
        self,
        occurrence_id: int,
        *,
        status: str,
        scheduled_time: str | None = None,
        commit: bool = True,
    ) -> tuple[str | None, str | None]:
        """Update an active occurrence's status/time, clear jobs, and return old job refs.

        Terminal rows (completed/skipped) are not changed.
        """
        job_columns = self.job_pointer_columns()
        select_columns = job_columns or ["id"]
        row = self.db.execute(
            f"SELECT {', '.join(select_columns)} FROM periodic_occurrences WHERE id = ? AND status NOT IN ('completed', 'skipped')",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            return (None, None)

        def value_for(column: str) -> str | None:
            if column not in job_columns:
                return None
            if isinstance(row, sqlite3.Row):
                return row[column]
            return row[select_columns.index(column)]

        reminder_job_id = value_for("reminder_job_id")
        execution_job_id = value_for("execution_job_id")
        assignments = ["status = ?"]
        params: list[object] = [status]
        if self._has_column("scheduled_time"):
            assignments.append("scheduled_time = ?")
            params.append(scheduled_time)
        assignments.extend(f"{column} = NULL" for column in job_columns)
        params.append(occurrence_id)
        self.db.execute(
            f"UPDATE periodic_occurrences SET {', '.join(assignments)} WHERE id = ? AND status NOT IN ('completed', 'skipped')",
            tuple(params),
        )
        if commit:
            self.db.commit()
        return (reminder_job_id, execution_job_id)

    def get_job_refs(self, occurrence_id: int) -> tuple[str | None, str | None]:
        """Return current job IDs without clearing DB pointers."""
        select_columns = self.job_pointer_columns()
        if not select_columns:
            return (None, None)
        row = self.db.execute(
            f"SELECT {', '.join(select_columns)} FROM periodic_occurrences WHERE id = ?",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            return (None, None)

        def value_for(column: str) -> str | None:
            if column not in select_columns:
                return None
            if isinstance(row, sqlite3.Row):
                return row[column]
            return row[select_columns.index(column)]

        return (value_for("reminder_job_id"), value_for("execution_job_id"))

    def clear_jobs(self, occurrence_id: int, *, commit: bool = True) -> tuple[str | None, str | None]:
        """Return current job IDs, then clear available DB job pointer columns."""
        select_columns = self.job_pointer_columns()
        if not select_columns:
            return (None, None)
        row = self.db.execute(
            f"SELECT {', '.join(select_columns)} FROM periodic_occurrences WHERE id = ?",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            return (None, None)

        def value_for(column: str) -> str | None:
            if column not in select_columns:
                return None
            if isinstance(row, sqlite3.Row):
                return row[column]
            return row[select_columns.index(column)]

        reminder_job_id = value_for("reminder_job_id")
        execution_job_id = value_for("execution_job_id")
        clear_columns = [f"{column} = NULL" for column in select_columns]
        self.db.execute(
            f"UPDATE periodic_occurrences SET {', '.join(clear_columns)} WHERE id = ?",
            (occurrence_id,),
        )
        if commit:
            self.db.commit()
        return (reminder_job_id, execution_job_id)
