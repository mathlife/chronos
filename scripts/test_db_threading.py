#!/usr/bin/env python3
"""Regression check for thread-safe DB usage."""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import db as db_module
from core import paths as paths_module
from scripts.test_helpers import make_case_dir, reset_db_singleton


def prepare_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE thread_probe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker INTEGER NOT NULL,
            seq INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def test_multithread_insert() -> None:
    db_path = make_case_dir("db-threading") / "todo.db"
    prepare_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    errors: list[Exception] = []
    error_lock = threading.Lock()

    def worker(worker_id: int) -> None:
        try:
            for seq in range(15):
                db_module.DB().execute(
                    "INSERT INTO thread_probe (worker, seq) VALUES (?, ?)",
                    (worker_id, seq),
                )
                db_module.db_commit()
        except Exception as exc:
            with error_lock:
                errors.append(exc)
        finally:
            db_module.DB().close()

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if errors:
        raise AssertionError(f"thread workers failed: {errors}")

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM thread_probe").fetchone()[0]
    conn.close()
    assert count == 60

    reset_db_singleton(db_module)


if __name__ == "__main__":
    test_multithread_insert()
    print("[ok] multithread sqlite writes pass with thread-local DB connections")
