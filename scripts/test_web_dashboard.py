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
from scripts.web_dashboard import DashboardHandler, HTML_PAGE, build_snapshot, handle_mutation
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


def test_today_mutation_ops() -> None:
    case_dir = make_case_dir("web-today-mutation")
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
    conn.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'today-occ', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences (id, task_id, date, status, scheduled_time, scheduled_at)
        VALUES (21, 1, '2026-05-03', 'pending', '09:00', '2026-05-03T09:00:00+08:00')
        """
    )
    conn.execute("INSERT INTO entries (id, text, status, group_id) VALUES (31, 'legacy-old', 'pending', NULL)")
    conn.commit()
    conn.close()

    occ_updated = handle_mutation(
        "/api/v1/today/update",
        {"identifier": "FIN-21", "patch": {"name": "today-occ-new", "status": "in_progress", "scheduled_time": "10:15"}},
        db_path=db_path,
    )
    assert occ_updated["identifier"] == "FIN-21"

    ent_updated = handle_mutation(
        "/api/v1/today/update",
        {"identifier": "ID31", "patch": {"name": "legacy-new", "status": "completed"}},
        db_path=db_path,
    )
    assert ent_updated["identifier"] == "ID31"

    conn = sqlite3.connect(str(db_path))
    occ_row = conn.execute(
        """
        SELECT t.name, o.status, o.scheduled_time
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON t.id = o.task_id
        WHERE o.id = 21
        """
    ).fetchone()
    assert occ_row is not None
    assert occ_row[0] == "today-occ-new"
    assert occ_row[1] == "in_progress"
    assert occ_row[2] == "10:15"
    entry_row = conn.execute("SELECT text, status FROM entries WHERE id = 31").fetchone()
    assert entry_row is not None
    assert entry_row[0] == "legacy-new"
    assert entry_row[1] == "completed"
    conn.close()

    removed_occ = handle_mutation("/api/v1/today/remove", {"identifier": "FIN-21"}, db_path=db_path)
    assert removed_occ["identifier"] == "FIN-21"
    removed_entry = handle_mutation("/api/v1/today/remove", {"identifier": "ID31"}, db_path=db_path)
    assert removed_entry["identifier"] == "ID31"

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT 1 FROM periodic_occurrences WHERE id = 21").fetchone() is None
    assert conn.execute("SELECT 1 FROM entries WHERE id = 31").fetchone() is None
    conn.close()

    if original_config is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original_config
    reset_db_singleton(db_module)


def test_today_occurrence_update_completed_uses_state_transition_on_old_schema() -> None:
    case_dir = make_case_dir("web-today-complete-state")
    db_path = case_dir / "todo.db"
    prepare_temp_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'web-complete', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (41, 1, '2026-05-03', 'pending', 'reminder-41', 'execute-41', '09:00', '2026-05-03T09:00:00+08:00')
        """
    )
    conn.commit()
    conn.close()

    updated = handle_mutation(
        "/api/v1/today/update",
        {"identifier": "FIN-41", "patch": {"name": "web-complete-new", "status": "completed", "scheduled_time": "10:30"}},
        db_path=db_path,
    )
    assert updated["identifier"] == "FIN-41"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """
        SELECT t.name, o.status, o.scheduled_time, o.completed_at, o.completion_mode,
               o.reminder_job_id, o.execution_job_id
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON t.id = o.task_id
        WHERE o.id = 41
        """
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "web-complete-new"
    assert row[1] == "completed"
    assert row[2] == "10:30"
    assert row[3] is not None
    assert row[4] == "manual"
    assert row[5] is None
    assert row[6] is None


def test_today_occurrence_update_skipped_records_web_metadata_when_columns_exist() -> None:
    case_dir = make_case_dir("web-today-skip-state")
    db_path = case_dir / "todo.db"
    prepare_temp_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        ALTER TABLE periodic_occurrences ADD COLUMN completion_source TEXT;
        ALTER TABLE periodic_occurrences ADD COLUMN trigger_label TEXT;
        ALTER TABLE periodic_occurrences ADD COLUMN trigger_command TEXT;
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'web-skip', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (42, 1, '2026-05-03', 'reminded', 'reminder-42', 'execute-42', '09:00', '2026-05-03T09:00:00+08:00')
        """
    )
    conn.commit()
    conn.close()

    updated = handle_mutation(
        "/api/v1/today/update",
        {"identifier": "FIN-42", "patch": {"name": "web-skip-new", "status": "skipped", "scheduled_time": "10:45"}},
        db_path=db_path,
    )
    assert updated["identifier"] == "FIN-42"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """
        SELECT t.name, o.status, o.scheduled_time, o.completed_at, o.completion_mode,
               o.completion_source, o.trigger_label, o.trigger_command,
               o.reminder_job_id, o.execution_job_id
        FROM periodic_occurrences o
        JOIN periodic_tasks t ON t.id = o.task_id
        WHERE o.id = 42
        """
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "web-skip-new"
    assert row[1] == "skipped"
    assert row[2] == "10:45"
    assert row[3] is not None
    assert row[4] == "manual"
    assert row[5] == "web_dashboard"
    assert row[6] == "web_today_update"
    assert row[7] == "web_dashboard today update"
    assert row[8] is None
    assert row[9] is None


def test_today_occurrence_remove_supports_old_schema_without_execution_job_id() -> None:
    case_dir = make_case_dir("web-today-remove-old-schema")
    db_path = case_dir / "todo.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE periodic_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            cycle_type TEXT,
            time_of_day TEXT,
            timezone TEXT,
            is_active INTEGER,
            count_current_month INTEGER,
            task_kind TEXT,
            source TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reminder_job_id TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'old-remove', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (51, 1, '2026-05-03', 'pending', 'reminder-51', '09:00', '2026-05-03T09:00:00+08:00')
        """
    )
    conn.commit()
    conn.close()

    removed = handle_mutation("/api/v1/today/remove", {"identifier": "FIN-51"}, db_path=db_path)
    assert removed["identifier"] == "FIN-51"

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT 1 FROM periodic_occurrences WHERE id = 51").fetchone() is None
    conn.close()


def test_today_occurrence_update_to_reminded_clears_stale_jobs() -> None:
    case_dir = make_case_dir("web-today-reminded-clears-jobs")
    db_path = case_dir / "todo.db"
    prepare_temp_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'web-reminded', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, execution_job_id, scheduled_time, scheduled_at)
        VALUES (52, 1, '2026-05-03', 'in_progress', 'reminder-52', 'execute-52', '09:00', '2026-05-03T09:00:00+08:00')
        """
    )
    conn.commit()
    conn.close()

    updated = handle_mutation(
        "/api/v1/today/update",
        {"identifier": "FIN-52", "patch": {"name": "web-reminded-new", "status": "reminded", "scheduled_time": "10:20"}},
        db_path=db_path,
    )
    assert updated["identifier"] == "FIN-52"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT t.name, o.status, o.scheduled_time, o.reminder_job_id, o.execution_job_id "
        "FROM periodic_occurrences o JOIN periodic_tasks t ON t.id = o.task_id WHERE o.id = 52"
    ).fetchone()
    conn.close()

    assert row == ("web-reminded-new", "reminded", "10:20", None, None)


def test_today_occurrence_update_supports_old_schema_without_execution_job_id() -> None:
    case_dir = make_case_dir("web-today-update-old-schema")
    db_path = case_dir / "todo.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE periodic_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            cycle_type TEXT,
            time_of_day TEXT,
            timezone TEXT,
            is_active INTEGER,
            count_current_month INTEGER,
            task_kind TEXT,
            source TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE periodic_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reminder_job_id TEXT,
            scheduled_time TEXT,
            scheduled_at TEXT
        );
        INSERT INTO periodic_tasks
        (id, name, category, cycle_type, time_of_day, timezone, is_active, count_current_month, task_kind, source, created_at, updated_at)
        VALUES (1, 'old-update', 'Inbox', 'daily', '09:00', 'Asia/Shanghai', 1, 0, 'scheduled', 'chronos', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        INSERT INTO periodic_occurrences
        (id, task_id, date, status, reminder_job_id, scheduled_time, scheduled_at)
        VALUES (53, 1, '2026-05-03', 'pending', 'reminder-53', '09:00', '2026-05-03T09:00:00+08:00');
        """
    )
    conn.commit()
    conn.close()

    updated = handle_mutation(
        "/api/v1/today/update",
        {"identifier": "FIN-53", "patch": {"name": "old-update-new", "status": "reminded", "scheduled_time": "10:30"}},
        db_path=db_path,
    )
    assert updated["identifier"] == "FIN-53"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT t.name, o.status, o.scheduled_time, o.reminder_job_id "
        "FROM periodic_occurrences o JOIN periodic_tasks t ON t.id = o.task_id WHERE o.id = 53"
    ).fetchone()
    conn.close()
    assert row == ("old-update-new", "reminded", "10:30", None)


def test_web_today_job_removal_uses_shared_job_ref_iterator() -> None:
    source = (PROJECT_ROOT / "scripts" / "web_dashboard.py").read_text()
    helper_body = source.split("def _remove_scheduler_jobs_for_payload", 1)[1].split("def update_today_task", 1)[0]
    update_body = source.split("def update_today_task", 1)[1].split("def remove_today_task", 1)[0]
    remove_body = source.split("def remove_today_task", 1)[1].split("def handle_mutation", 1)[0]
    combined = update_body + remove_body

    assert "iter_job_refs(" in helper_body
    assert "_remove_scheduler_jobs_for_pair(" in combined
    assert "update_non_terminal(" in update_body
    assert "reminder_job_id = row" not in combined
    assert "execution_job_id = row" not in combined
    assert "reminder_job_id, execution_job_id =" not in combined
    assert "if reminder_job_id" not in combined
    assert "if execution_job_id" not in combined


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


def test_all_tasks_table_has_edit_actions() -> None:
    assert "selectTaskById" in HTML_PAGE
    assert "removeSelectedTask(false" in HTML_PAGE
    assert "removeSelectedTask(true" in HTML_PAGE
    assert "All Periodic Tasks" in HTML_PAGE


if __name__ == "__main__":
    test_snapshot_excludes_linked_legacy_entries()
    print("[ok] snapshot excludes linked legacy entries")
    test_mutation_ops()
    print("[ok] mutation ops for task/channel/settings")
    test_today_mutation_ops()
    print("[ok] today mutation ops for occurrence/entry")
    test_read_only_server_rejects_mutation()
    print("[ok] read-only server rejects mutation")
    test_health_endpoint()
    print("[ok] health endpoint returns db and metrics status")
    test_all_tasks_table_has_edit_actions()
    print("[ok] all periodic tasks table has edit/deactivate/delete actions")
