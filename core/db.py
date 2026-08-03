"""Database layer with connection pooling and caching."""
import sqlite3
import threading
from functools import lru_cache
from typing import Optional

from .paths import TODO_DB


TASK_SCHEMA_COLUMNS = {
    'reminder_template': "ALTER TABLE periodic_tasks ADD COLUMN reminder_template TEXT",
    'last_reminder_error': "ALTER TABLE periodic_tasks ADD COLUMN last_reminder_error TEXT",
    'reminder_error_count': "ALTER TABLE periodic_tasks ADD COLUMN reminder_error_count INTEGER DEFAULT 0",
    'last_reminder_error_at': "ALTER TABLE periodic_tasks ADD COLUMN last_reminder_error_at TIMESTAMP",
    'task_kind': "ALTER TABLE periodic_tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'scheduled'",
    'source': "ALTER TABLE periodic_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'chronos'",
    'legacy_entry_id': "ALTER TABLE periodic_tasks ADD COLUMN legacy_entry_id INTEGER",
    'special_handler': "ALTER TABLE periodic_tasks ADD COLUMN special_handler TEXT",
    'handler_payload': "ALTER TABLE periodic_tasks ADD COLUMN handler_payload TEXT",
    'start_date': "ALTER TABLE periodic_tasks ADD COLUMN start_date TEXT",
    'delivery_target': "ALTER TABLE periodic_tasks ADD COLUMN delivery_target TEXT",
    'delivery_mode': "ALTER TABLE periodic_tasks ADD COLUMN delivery_mode TEXT",
    'dates_list': "ALTER TABLE periodic_tasks ADD COLUMN dates_list TEXT",
    'interval_hours': "ALTER TABLE periodic_tasks ADD COLUMN interval_hours INTEGER",
}

OCCURRENCE_SCHEMA_COLUMNS = {
    'completion_mode': "ALTER TABLE periodic_occurrences ADD COLUMN completion_mode TEXT",
    'special_handler_result': "ALTER TABLE periodic_occurrences ADD COLUMN special_handler_result TEXT",
    'scheduled_time': "ALTER TABLE periodic_occurrences ADD COLUMN scheduled_time TEXT",
    'scheduled_at': "ALTER TABLE periodic_occurrences ADD COLUMN scheduled_at TEXT",
    'legacy_entry_id': "ALTER TABLE periodic_occurrences ADD COLUMN legacy_entry_id INTEGER",
    'execution_job_id': "ALTER TABLE periodic_occurrences ADD COLUMN execution_job_id TEXT",
    'completion_source': "ALTER TABLE periodic_occurrences ADD COLUMN completion_source TEXT",
    'trigger_label': "ALTER TABLE periodic_occurrences ADD COLUMN trigger_label TEXT",
    'trigger_command': "ALTER TABLE periodic_occurrences ADD COLUMN trigger_command TEXT",
}

SCHEDULER_OPERATION_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_operation_log (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    task_id INTEGER,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

INDEX_CANDIDATES = (
    ("periodic_occurrences", "CREATE INDEX IF NOT EXISTS idx_occurrences_date_status_task ON periodic_occurrences(date, status, task_id)"),
    ("periodic_tasks", "CREATE INDEX IF NOT EXISTS idx_tasks_active_cycle ON periodic_tasks(is_active, cycle_type)"),
    ("entries", "CREATE INDEX IF NOT EXISTS idx_entries_status_group ON entries(status, group_id)"),
    ("periodic_tasks", "CREATE INDEX IF NOT EXISTS idx_tasks_legacy_entry ON periodic_tasks(legacy_entry_id)"),
    ("scheduler_operation_log", "CREATE INDEX IF NOT EXISTS idx_scheduler_op_task_status ON scheduler_operation_log(task_id, status, created_at)"),
)


class DB:
    """Singleton database connection with query caching."""
    _instance: Optional['DB'] = None
    _local = threading.local()
    _schema_lock = threading.Lock()
    _schema_ready = False
    # Backward-compat alias used by local regression scripts.
    _conn: Optional[sqlite3.Connection] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Connection is lazily created per thread in _get_conn.
        return

    def _create_connection(self) -> sqlite3.Connection:
        TODO_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(TODO_DB), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            # Improves writer/reader concurrency on Linux sqlite.
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._create_connection()
            self._local.conn = conn
            DB._conn = conn
            self._ensure_schema_once()
        return conn

    def _ensure_schema_once(self) -> None:
        with DB._schema_lock:
            if DB._schema_ready:
                return
            try:
                ensure_schema(self)
            except sqlite3.Error as exc:
                print(f"Warning: failed to ensure schema: {exc}")
            else:
                DB._schema_ready = True

    def execute(self, query: str, params: tuple = ()):
        cur = self._get_conn().cursor()
        cur.execute(query, params)
        return cur

    def executemany(self, query: str, params_list: list):
        cur = self._get_conn().cursor()
        cur.executemany(query, params_list)
        return cur

    def commit(self):
        self._get_conn().commit()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
            DB._conn = None

    @classmethod
    def reset_for_tests(cls) -> None:
        conn = getattr(cls._local, "conn", None)
        if conn:
            conn.close()
        cls._local.conn = None
        cls._conn = None
        cls._instance = None
        cls._schema_ready = False


# Convenience functions
def db_execute(query: str, params: tuple = ()):
    return DB().execute(query, params)


def db_commit():
    DB().commit()


@lru_cache(maxsize=128)
def get_periodic_tasks(active_only: bool = True):
    """Fetch all periodic tasks (cached)."""
    query = "SELECT * FROM periodic_tasks"
    if active_only:
        query += " WHERE is_active = 1"
    cur = DB().execute(query)
    rows = cur.fetchall()
    return [dict(row) for row in rows]


@lru_cache(maxsize=128)
def get_periodic_task(task_id: int):
    """Fetch single task by ID (cached)."""
    cur = DB().execute("SELECT * FROM periodic_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def clear_task_cache():
    """Clear task cache (called after updates)."""
    get_periodic_tasks.cache_clear()
    get_periodic_task.cache_clear()


def _ensure_table_columns(db: DB, table_name: str, statements: dict[str, str]) -> None:
    cur = db.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in cur.fetchall()}
    changed = False
    for column_name, statement in statements.items():
        if column_name not in columns:
            db.execute(statement)
            changed = True
    if changed:
        db.commit()


def _get_table_sql(db: DB, table_name: str) -> str:
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (table_name,)).fetchone()
    return row[0] if row and row[0] else ""


def _table_exists(db: DB, table_name: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _ensure_index_candidates(db: DB) -> None:
    for table_name, statement in INDEX_CANDIDATES:
        if _table_exists(db, table_name):
            db.execute(statement)
    db.commit()


def _rebuild_occurrences_for_hourly(db: DB) -> None:
    sql = _get_table_sql(db, 'periodic_occurrences')
    if 'UNIQUE(task_id, date, scheduled_time)' in sql:
        return

    db.execute("ALTER TABLE periodic_occurrences RENAME TO periodic_occurrences_old")
    old_columns = {row[1] for row in db.execute("PRAGMA table_info(periodic_occurrences_old)").fetchall()}
    db.execute(
        """
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reminder_job_id TEXT,
            execution_job_id TEXT,
            is_auto_completed BOOLEAN DEFAULT 0,
            completed_at TEXT,
            completion_mode TEXT,
            special_handler_result TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT,
            legacy_entry_id INTEGER,
            FOREIGN KEY (task_id) REFERENCES periodic_tasks(id) ON DELETE CASCADE,
            UNIQUE(task_id, date, scheduled_time)
        )
        """
    )
    execution_job_id_expr = "execution_job_id" if "execution_job_id" in old_columns else "NULL AS execution_job_id"
    db.execute(
        f"""
        INSERT INTO periodic_occurrences (
            id, task_id, date, status, reminder_job_id, execution_job_id, is_auto_completed, completed_at,
            completion_mode, special_handler_result, scheduled_time, scheduled_at, legacy_entry_id
        )
        SELECT
            id, task_id, date, status, reminder_job_id, {execution_job_id_expr}, COALESCE(is_auto_completed, 0), completed_at,
            completion_mode, special_handler_result, scheduled_time, scheduled_at, legacy_entry_id
        FROM periodic_occurrences_old
        """
    )
    db.execute("DROP TABLE periodic_occurrences_old")
    db.commit()


def ensure_schema(db: Optional[DB] = None):
    """Ensure database schema has all phase-1 Chronos columns."""
    db = db or DB()
    db.execute(SCHEDULER_OPERATION_LOG_TABLE_SQL)

    tasks_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='periodic_tasks'"
    ).fetchone()
    if tasks_table:
        _ensure_table_columns(db, 'periodic_tasks', TASK_SCHEMA_COLUMNS)

    occurrences_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='periodic_occurrences'"
    ).fetchone()
    if occurrences_table:
        _ensure_table_columns(db, 'periodic_occurrences', OCCURRENCE_SCHEMA_COLUMNS)
        _rebuild_occurrences_for_hourly(db)
    _ensure_index_candidates(db)
