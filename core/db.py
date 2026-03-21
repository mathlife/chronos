"""Database layer with connection pooling and caching."""
import sqlite3
from functools import lru_cache
from typing import Optional

from .paths import TODO_DB

class DB:
    """Singleton database connection with query caching."""
    _instance: Optional['DB'] = None
    _conn: Optional[sqlite3.Connection] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._conn is None:
            TODO_DB.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(TODO_DB))
            self._conn.row_factory = sqlite3.Row
            try:
                ensure_schema(self)
            except sqlite3.Error as exc:
                print(f"Warning: failed to ensure schema: {exc}")

    def execute(self, query: str, params: tuple = ()):
        cur = self._conn.cursor()
        cur.execute(query, params)
        return cur

    def executemany(self, query: str, params_list: list):
        cur = self._conn.cursor()
        cur.executemany(query, params_list)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

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

def ensure_schema(db: Optional[DB] = None):
    """Ensure database schema has all required columns."""
    db = db or DB()
    table_row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='periodic_tasks'"
    ).fetchone()
    if not table_row:
        return

    cur = db.execute("PRAGMA table_info(periodic_tasks)")
    columns = {row[1] for row in cur.fetchall()}  # column name at index 1

    # Add reminder_template column if missing
    if 'reminder_template' not in columns:
        db.execute("ALTER TABLE periodic_tasks ADD COLUMN reminder_template TEXT")
        db.commit()
        print("Added reminder_template column to periodic_tasks")

    # Add error tracking columns for monitoring
    if 'last_reminder_error' not in columns:
        db.execute("ALTER TABLE periodic_tasks ADD COLUMN last_reminder_error TEXT")
        db.execute("ALTER TABLE periodic_tasks ADD COLUMN reminder_error_count INTEGER DEFAULT 0")
        db.execute("ALTER TABLE periodic_tasks ADD COLUMN last_reminder_error_at TIMESTAMP")
        db.commit()
        print("Added error tracking columns to periodic_tasks")
