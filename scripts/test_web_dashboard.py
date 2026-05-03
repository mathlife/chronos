#!/usr/bin/env python3
"""Regression checks for web_dashboard."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import db as db_module
from core import paths as paths_module
from scripts.web_dashboard import DashboardHandler, build_snapshot, handle_mutation
from scripts.test_helpers import make_case_dir, reset_db_singleton


SCHEMA_SQL = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    group_id INTEGER
);

CREATE TABLE periodic_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT,
    cycle_type TEXT,
    weekday INTEGER,
    day_of_month INTEGER,
    range_start INTEGER,
    range_end INTEGER,
    n_per_month INTEGER,
    interval_hours INTEGER,
    time_of_day TEXT,
    event_time TEXT,
    timezone TEXT,
    is_active INTEGER,
    count_current_month INTEGER,
    end_date TEXT,
    reminder_template TEXT,
    dates_list TEXT,
    task_kind TEXT,
    source TEXT,
    legacy_entry_id INTEGER,
    special_handler TEXT,
    handler_payload TEXT,
    start_date TEXT,
    delivery_target TEXT,
    delivery_mode TEXT,
    created_at TEXT,
    updated_at TEXT
);

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
);
"""


def prepare_temp_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def test_snapshot_excludes_linked_legacy_entries() -> None:
    case_dir = make_case_dir("web-snapshot")
    db_path = case_dir / "todo.db"
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps({"channels": []}, ensure_ascii=False), encoding="utf-8")
    original_config = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)

    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("INSERT INTO groups (id, name) VALUES (1, 'Inbox')")
    cur.execute("INSERT INTO entries (id, text, status, group_id) VALUES (10, 'legacy linked', 'pending', 1)")
    cur.execute("INSERT INTO entries (id, text, status, group_id) VALUES (11, 'legacy standalone', 'pending', 1)")
    cur.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, legacy_entry_id, created_at, updated_at)
        VALUES (1, 'task-linked', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.commit()
    conn.close()

    snapshot = build_snapshot(db_path, read_only=True)
    identifiers = {item["identifier"] for item in snapshot["today_tasks"]}
    assert "ID10" not in identifiers
    assert "ID11" in identifiers
    assert snapshot["settings"]["read_only"] is True

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton(db_module)


def test_mutation_ops() -> None:
    case_dir = make_case_dir("web-mutation")
    db_path = case_dir / "todo.db"
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps({"channels": []}, ensure_ascii=False), encoding="utf-8")
    original_config = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)

    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    created = handle_mutation(
        "/api/v1/task/create",
        {"payload": {"name": "web-created", "cycle_type": "once", "start_date": "2026-05-03", "time_of_day": "09:00"}},
    )
    task_id = int(created["id"])
    assert created["name"] == "web-created"

    updated = handle_mutation("/api/v1/task/update", {"id": task_id, "patch": {"cycle_type": "daily"}})
    assert updated["cycle_type"] == "daily"

    removed = handle_mutation("/api/v1/task/remove", {"id": task_id, "hard": False})
    assert removed["id"] == task_id

    channel = handle_mutation(
        "/api/v1/channel/put",
        {"channel": {"id": "hook-main", "type": "webhook", "enabled": True, "config": {"url": "https://example.com"}}},
    )
    assert channel["id"] == "hook-main"
    handle_mutation("/api/v1/channel/remove", {"id": "hook-main"})

    settings = handle_mutation("/api/v1/settings/update", {"chat_id": "12345"})
    assert settings["chat_id"] == "12345"

    config_data = json.loads(config_path.read_text(encoding="utf-8"))
    assert config_data.get("chat_id") == "12345"

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton(db_module)


def test_read_only_server_rejects_mutation() -> None:
    case_dir = make_case_dir("web-read-only")
    db_path = case_dir / "todo.db"
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps({"channels": []}, ensure_ascii=False), encoding="utf-8")
    original_config = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)

    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    DashboardHandler.db_path = db_path
    DashboardHandler.basic_auth_token = None
    DashboardHandler.debug_errors = False
    DashboardHandler.read_only_mode = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    host, port = server.server_address

    def serve_one() -> None:
        server.handle_request()

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()

    payload = json.dumps({"chat_id": "1"}).encode("utf-8")
    request = Request(
        f"http://{host}:{port}/api/v1/settings/update",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        try:
            urlopen(request, timeout=5)
            raise AssertionError("expected HTTPError for read-only mutation")
        except HTTPError as exc:
            assert exc.code == 400
            body = exc.read().decode("utf-8")
            assert "read-only mode" in body
    finally:
        server.server_close()

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton(db_module)


def test_health_endpoint() -> None:
    case_dir = make_case_dir("web-health")
    db_path = case_dir / "todo.db"
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps({"channels": []}, ensure_ascii=False), encoding="utf-8")
    original_config = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_path)

    prepare_temp_db(db_path)
    paths_module.TODO_DB = db_path
    db_module.TODO_DB = db_path
    reset_db_singleton(db_module)

    DashboardHandler.db_path = db_path
    DashboardHandler.basic_auth_token = None
    DashboardHandler.debug_errors = False
    DashboardHandler.read_only_mode = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    host, port = server.server_address

    def serve_one() -> None:
        server.handle_request()

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()

    try:
        response = urlopen(f"http://{host}:{port}/api/v1/health", timeout=5)
        body = json.loads(response.read().decode("utf-8"))
        assert body.get("status") in {"ok", "degraded"}
        assert body.get("db_ok") is True
        assert "metrics" in body
    finally:
        server.server_close()

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton(db_module)


if __name__ == "__main__":
    test_snapshot_excludes_linked_legacy_entries()
    print("[ok] snapshot excludes linked legacy entries")
    test_mutation_ops()
    print("[ok] mutation ops for task/channel/settings")
    test_read_only_server_rejects_mutation()
    print("[ok] read-only server rejects mutation")
    test_health_endpoint()
    print("[ok] health endpoint returns db and metrics status")
