"""Database layer with connection pooling and caching."""
import sqlite3
from pathlib import Path
from functools import lru_cache
from typing import Optional

WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")
TODO_DB = WORKSPACE / "todo.db"

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
            self._conn = sqlite3.connect(TODO_DB)
            self._conn.row_factory = sqlite3.Row

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
def get_financial_activities(active_only: bool = True):
    """Fetch all financial activities (cached)."""
    query = "SELECT * FROM financial_activities"
    if active_only:
        query += " WHERE is_active = 1"
    cur = DB().execute(query)
    rows = cur.fetchall()
    return [dict(row) for row in rows]

@lru_cache(maxsize=128)
def get_financial_activity(activity_id: int):
    """Fetch single activity by ID (cached)."""
    cur = DB().execute("SELECT * FROM financial_activities WHERE id = ?", (activity_id,))
    row = cur.fetchone()
    return dict(row) if row else None

def clear_activity_cache():
    """Clear activity cache (called after updates)."""
    get_financial_activities.cache_clear()
    get_financial_activity.cache_clear()
