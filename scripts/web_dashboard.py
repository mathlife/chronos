#!/usr/bin/env python3
"""Web dashboard for Chronos settings and tasks."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import sqlite3
import sys
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_raw_config, inspect_config, save_raw_config
from core.integration_api import create_task, delete_channel, put_channel, remove_task, update_task
from core.paths import TODO_DB
from core.system_scheduler import supports_system_scheduler
from core.timezones import get_shanghai_tz

SHANGHAI_TZ = get_shanghai_tz()

HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Chronos Dashboard</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --card:#ffffff; --line:#d7deeb; --text:#1e2838; --muted:#64748b; --accent:#0f766e; --warn:#b45309; --ok:#166534; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: linear-gradient(160deg,#f7fafc,#eef3ff); color:var(--text); }
    .wrap { max-width:1200px; margin:0 auto; padding:20px; }
    h1 { margin:0 0 8px; font-size:28px; }
    .meta { color:var(--muted); margin-bottom:14px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(320px,1fr)); gap:14px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow:0 6px 14px rgba(12,20,40,0.04); }
    h2 { margin:0 0 10px; font-size:18px; }
    table { width:100%; border-collapse: collapse; font-size:13px; }
    th, td { text-align:left; border-bottom:1px solid #eef2f8; padding:6px 4px; vertical-align: top; }
    th { color:#334155; font-weight:600; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; background:#ecfeff; color:var(--accent); font-size:12px; }
    .warn { color:var(--warn); }
    .ok { color:var(--ok); }
    .muted { color:var(--muted); }
    pre { margin:0; white-space:pre-wrap; word-break: break-word; font-size:12px; background:#f8fafc; border:1px solid #e8edf5; border-radius:10px; padding:8px; }
    textarea,input { width:100%; border:1px solid #ced7e8; border-radius:8px; padding:8px; font-size:13px; }
    textarea { min-height:90px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .row { display:grid; grid-template-columns: 1fr 1fr; gap:8px; }
    .btns { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    button { border:0; border-radius:8px; padding:7px 12px; background:#0f766e; color:#fff; cursor:pointer; }
    button.alt { background:#334155; }
    button.warn { background:#b45309; color:#fff; }
    #opResult { margin-top:8px; font-size:13px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Chronos Dashboard</h1>
    <div class="meta" id="meta">loading...</div>
    <div class="grid">
      <section class="card">
        <h2>Runtime Settings</h2>
        <div id="settings"></div>
      </section>
      <section class="card">
        <h2>Channels</h2>
        <div id="channels"></div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Edit Guard</h2>
        <div class="row">
          <div>
            <label><input type="checkbox" id="editMode" /> Enable edit mode in browser</label>
            <div class="muted">Server read-only mode still has priority.</div>
          </div>
          <div class="muted" id="editState"></div>
        </div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Config & Channel Ops</h2>
        <div class="row">
          <div>
            <label>Legacy chat_id (optional)</label>
            <input id="legacyChatId" placeholder="e.g. 123456" />
            <div class="btns">
              <button onclick="updateSettings()">Update chat_id</button>
            </div>
          </div>
          <div>
            <label>Remove channel by id</label>
            <input id="removeChannelId" placeholder="e.g. tg-main" />
            <div class="btns">
              <button class="warn" onclick="removeChannel()">Remove channel</button>
            </div>
          </div>
        </div>
        <label style="margin-top:8px; display:block;">Upsert channel JSON</label>
        <textarea id="channelJson">{"id":"tg-main","type":"telegram","enabled":true,"config":{"bot_token":"<token>","chat_id":"<chat_id>"}}</textarea>
        <div class="btns">
          <button class="alt" onclick="putChannel()">Upsert channel</button>
        </div>
        <div id="opResult"></div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Task Ops</h2>
        <label>Create task JSON</label>
        <textarea id="createTaskJson">{"name":"每周例会","cycle_type":"weekly","weekday":0,"time_of_day":"10:00","task_kind":"scheduled"}</textarea>
        <div class="btns">
          <button onclick="createTask()">Create task</button>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Update task id</label>
            <input id="updateTaskId" placeholder="e.g. 12" />
          </div>
          <div>
            <label>Remove task id</label>
            <input id="removeTaskId" placeholder="e.g. 12" />
          </div>
        </div>
        <label style="margin-top:8px; display:block;">Update patch JSON</label>
        <textarea id="updateTaskJson">{"delivery_target":"tg-main,hook-main"}</textarea>
        <div class="btns">
          <button class="alt" onclick="updateTask()">Update task</button>
          <button class="warn" onclick="removeTask(false)">Deactivate task</button>
          <button class="warn" onclick="removeTask(true)">Hard delete task</button>
        </div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Today Tasks</h2>
        <div id="today"></div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>All Periodic Tasks</h2>
        <div id="tasks"></div>
      </section>
    </div>
  </div>
  <script>
    const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    function table(headers, rows) {
      if (!rows.length) return '<div class="muted">empty</div>';
      const th = '<tr>' + headers.map(h => `<th>${esc(h)}</th>`).join('') + '</tr>';
      const tr = rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('');
      return `<table>${th}${tr}</table>`;
    }
    function setResult(ok, msg) {
      const box = document.getElementById('opResult');
      box.className = ok ? 'ok' : 'warn';
      box.textContent = msg;
      setTimeout(() => { box.textContent = ''; }, 6000);
    }
    let serverReadOnly = false;
    function editingEnabled() {
      return document.getElementById('editMode').checked;
    }
    function ensureWritableAction() {
      if (serverReadOnly) throw new Error('server is running in read-only mode');
      if (!editingEnabled()) throw new Error('enable edit mode first');
    }
    function applyEditLock() {
      const enabled = editingEnabled() && !serverReadOnly;
      const targets = [
        'legacyChatId','removeChannelId','channelJson','createTaskJson',
        'updateTaskId','removeTaskId','updateTaskJson'
      ];
      for (const id of targets) {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
      }
      document.querySelectorAll('button').forEach((btn) => {
        const text = (btn.textContent || '').toLowerCase();
        if (text.includes('create') || text.includes('update') || text.includes('remove') || text.includes('delete') || text.includes('deactivate') || text.includes('upsert')) {
          btn.disabled = !enabled;
        }
      });
      const state = document.getElementById('editState');
      if (serverReadOnly) {
        state.textContent = 'Server mode: read-only';
      } else if (enabled) {
        state.textContent = 'Server mode: writable; browser edit mode enabled';
      } else {
        state.textContent = 'Server mode: writable; browser edit mode disabled';
      }
    }
    document.addEventListener('DOMContentLoaded', () => {
      const em = document.getElementById('editMode');
      em.addEventListener('change', applyEditLock);
      applyEditLock();
    });
    async function callApi(path, payload) {
      const res = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload || {})
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'request failed');
      return data;
    }
    async function createTask() {
      try {
        ensureWritableAction();
        const payload = JSON.parse(document.getElementById('createTaskJson').value);
        const r = await callApi('/api/task/create', { payload });
        setResult(true, `created task ${r.data.id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function updateTask() {
      try {
        ensureWritableAction();
        const id = Number(document.getElementById('updateTaskId').value.trim());
        const patch = JSON.parse(document.getElementById('updateTaskJson').value);
        const r = await callApi('/api/task/update', { id, patch });
        setResult(true, `updated task ${r.data.id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function removeTask(hard) {
      try {
        ensureWritableAction();
        const id = Number(document.getElementById('removeTaskId').value.trim());
        if (hard) {
          const sure = window.prompt(`Type DELETE-${id} to confirm hard deletion`);
          if (sure !== `DELETE-${id}`) throw new Error('hard delete cancelled');
        }
        await callApi('/api/task/remove', { id, hard });
        setResult(true, hard ? `hard-deleted task ${id}` : `deactivated task ${id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function putChannel() {
      try {
        ensureWritableAction();
        const channel = JSON.parse(document.getElementById('channelJson').value);
        await callApi('/api/channel/put', { channel });
        setResult(true, `upserted channel ${channel.id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function removeChannel() {
      try {
        ensureWritableAction();
        const id = document.getElementById('removeChannelId').value.trim();
        await callApi('/api/channel/remove', { id });
        setResult(true, `removed channel ${id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function updateSettings() {
      try {
        ensureWritableAction();
        const chat_id = document.getElementById('legacyChatId').value.trim();
        await callApi('/api/settings/update', { chat_id });
        setResult(true, 'updated settings');
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function load() {
      const res = await fetch('/api/snapshot');
      const data = await res.json();
      document.getElementById('meta').textContent = `updated: ${data.generated_at} | today: ${data.today}`;
      const s = data.settings || {};
      serverReadOnly = Boolean(s.read_only);
      document.getElementById('settings').innerHTML = `
        <div><span class="pill">${esc(s.config_status || 'unknown')}</span></div>
        <pre>config_path: ${esc(s.config_path)}
config_exists: ${esc(s.config_exists)}
channels_present: ${esc(s.channels_present)}
system_scheduler: ${esc(s.system_scheduler)}
read_only: ${esc(s.read_only)}
db_path: ${esc(s.db_path)}</pre>
        ${s.error ? `<div class="warn">error: ${esc(s.error)}</div>` : ''}
      `;
      applyEditLock();
      if (data.legacy_chat_id) {
        document.getElementById('legacyChatId').value = data.legacy_chat_id;
      }
      const channels = data.channels || [];
      document.getElementById('channels').innerHTML = table(
        ['id','type','enabled','config'],
        channels.map(c => [
          esc(c.id),
          esc(c.type),
          esc(c.enabled),
          `<pre>${esc(JSON.stringify(c.config || {}, null, 2))}</pre>`
        ])
      );
      const today = data.today_tasks || [];
      document.getElementById('today').innerHTML = table(
        ['id','name','kind','cycle','time','status','source'],
        today.map(t => [
          esc(t.identifier),
          esc(t.name),
          esc(t.task_kind),
          esc(t.cycle_type),
          esc(t.scheduled_time || ''),
          esc(t.status),
          esc(t.source || '')
        ])
      );
      const tasks = data.tasks || [];
      document.getElementById('tasks').innerHTML = table(
        ['id','name','active','kind','cycle','time','delivery_target','source'],
        tasks.map(t => [
          esc(t.id),
          esc(t.name),
          esc(t.is_active),
          esc(t.task_kind || 'scheduled'),
          esc(t.cycle_type),
          esc(t.time_of_day || ''),
          esc(t.delivery_target || ''),
          esc(t.source || '')
        ])
      );
    }
    load();
    setInterval(load, 30000);
  </script>
</body>
</html>
"""


def _safe_query(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict]:
    try:
        cur = conn.execute(query, params)
        columns = [item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []


def build_snapshot(db_path: Path, *, read_only: bool = False) -> dict:
    now = datetime.now(SHANGHAI_TZ)
    today = now.date().isoformat()
    config_info = inspect_config()

    snapshot = {
        "generated_at": now.isoformat(timespec="seconds"),
        "today": today,
        "legacy_chat_id": None,
        "settings": {
            "config_status": config_info.get("status"),
            "config_path": config_info.get("config_path"),
            "config_exists": config_info.get("config_exists"),
            "channels_present": config_info.get("channels_present"),
            "system_scheduler": supports_system_scheduler(),
            "read_only": bool(read_only),
            "db_path": str(db_path),
            "error": config_info.get("error"),
        },
        "channels": [],
        "tasks": [],
        "today_tasks": [],
    }

    raw_config = config_info.get("config") if isinstance(config_info.get("config"), dict) else {}
    legacy_chat_id = raw_config.get("chat_id")
    if legacy_chat_id is not None:
        snapshot["legacy_chat_id"] = str(legacy_chat_id)
    channels = raw_config.get("channels")
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            snapshot["channels"].append(
                {
                    "id": channel.get("id"),
                    "type": channel.get("type"),
                    "enabled": False if channel.get("enabled", True) is False else True,
                    "config": channel.get("config") if isinstance(channel.get("config"), dict) else channel,
                }
            )

    if not db_path.exists():
        return snapshot

    conn = sqlite3.connect(str(db_path))
    try:
        tasks = _safe_query(
            conn,
            """
            SELECT id, name, cycle_type, time_of_day, is_active, task_kind, source, delivery_target
            FROM periodic_tasks
            ORDER BY is_active DESC, id DESC
            """,
        )
        snapshot["tasks"] = tasks

        today_rows = _safe_query(
            conn,
            """
            SELECT
              o.id AS occ_id,
              t.id AS task_id,
              t.name,
              t.cycle_type,
              COALESCE(t.task_kind, 'scheduled') AS task_kind,
              COALESCE(o.scheduled_time, t.time_of_day) AS scheduled_time,
              o.status,
              t.source
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON t.id = o.task_id
            WHERE o.date = ?
            ORDER BY COALESCE(o.scheduled_time, t.time_of_day), t.name, o.id
            """,
            (today,),
        )
        today_tasks = []
        for row in today_rows:
            row["identifier"] = f"FIN-{row.get('occ_id')}"
            today_tasks.append(row)

        simple_rows = _safe_query(
            conn,
            """
            SELECT e.id, e.text, e.status, COALESCE(g.name, 'Inbox') AS group_name
            FROM entries e
            LEFT JOIN groups g ON g.id = e.group_id
            WHERE e.status IN ('pending', 'in_progress')
              AND NOT EXISTS (
                  SELECT 1 FROM periodic_tasks t
                  WHERE t.legacy_entry_id = e.id
              )
            ORDER BY e.id
            """,
        )
        for row in simple_rows:
            today_tasks.append(
                {
                    "identifier": f"ID{row.get('id')}",
                    "name": row.get("text"),
                    "cycle_type": "legacy",
                    "task_kind": "entry",
                    "scheduled_time": "",
                    "status": row.get("status"),
                    "source": row.get("group_name"),
                }
            )
        snapshot["today_tasks"] = today_tasks
    finally:
        conn.close()

    return snapshot


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = TODO_DB
    basic_auth_token: str | None = None
    debug_errors: bool = False
    read_only_mode: bool = False

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_auth():
            self._write_auth_required()
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._write_html(HTML_PAGE)
            return
        if parsed.path == "/api/snapshot":
            payload = build_snapshot(self.db_path, read_only=self.read_only_mode)
            self._write_json(payload)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            self._write_auth_required()
            return
        parsed = urlparse(self.path)
        try:
            if self.read_only_mode:
                raise ValueError("server is running in read-only mode")
            payload = self._read_json_body()
            result = handle_mutation(parsed.path, payload)
            self._write_json({"ok": True, "data": result})
        except ValueError as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            if self.debug_errors:
                self._write_json({"ok": False, "error": str(exc)}, status=500)
            else:
                print(f"[web_dashboard] internal error on {parsed.path}: {exc}", file=sys.stderr)
                traceback.print_exc()
                self._write_json({"ok": False, "error": "operation failed"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _write_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json(self, payload: dict, *, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_auth_required(self) -> None:
        body = b"Authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Chronos Dashboard"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        expected = self.basic_auth_token
        if not expected:
            return True
        actual = self.headers.get("Authorization") or ""
        return hmac.compare_digest(actual, expected)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data


def handle_mutation(path: str, payload: dict) -> dict:
    if path == "/api/task/create":
        task_payload = payload.get("payload")
        if not isinstance(task_payload, dict):
            raise ValueError("payload must be an object")
        return create_task(task_payload)
    if path == "/api/task/update":
        task_id = int(payload.get("id"))
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        return update_task(task_id, patch)
    if path == "/api/task/remove":
        task_id = int(payload.get("id"))
        hard = bool(payload.get("hard", False))
        removed = remove_task(task_id, hard=hard)
        if not removed:
            raise ValueError(f"task {task_id} not found")
        return {"id": task_id, "hard": hard}
    if path == "/api/channel/put":
        channel = payload.get("channel")
        if not isinstance(channel, dict):
            raise ValueError("channel must be an object")
        return put_channel(channel)
    if path == "/api/channel/remove":
        channel_id = str(payload.get("id") or "").strip()
        if not channel_id:
            raise ValueError("id is required")
        removed = delete_channel(channel_id)
        if not removed:
            raise ValueError(f"channel {channel_id} not found")
        return {"id": channel_id}
    if path == "/api/settings/update":
        chat_id = str(payload.get("chat_id") or "").strip()
        raw = get_raw_config()
        if chat_id:
            raw["chat_id"] = chat_id
        else:
            raw.pop("chat_id", None)
        save_raw_config(raw)
        return {"chat_id": chat_id or None}
    raise ValueError("unsupported endpoint")


def _is_local_bind(host: str) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _encode_basic_auth_token(credential: str) -> str:
    raw = credential.encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronos web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", default=str(TODO_DB))
    parser.add_argument("--basic-auth", help="HTTP Basic auth credential in user:password format")
    parser.add_argument("--allow-unauthenticated-remote", action="store_true", help="Allow non-local bind without auth (unsafe)")
    parser.add_argument("--read-only", action="store_true", help="Disable all mutation APIs")
    parser.add_argument("--debug-errors", action="store_true", help="Return internal error details in HTTP responses")
    parser.add_argument("--dump-json", action="store_true", help="Print one snapshot JSON and exit")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db_path).expanduser()
    if args.dump_json:
        print(json.dumps(build_snapshot(db_path, read_only=bool(args.read_only)), ensure_ascii=False, indent=2))
        return 0

    if not _is_local_bind(args.host) and not args.basic_auth and not args.allow_unauthenticated_remote:
        print(
            "Refusing remote bind without authentication. "
            "Set --basic-auth user:password (recommended) "
            "or use --allow-unauthenticated-remote (unsafe).",
            file=sys.stderr,
        )
        return 2

    DashboardHandler.db_path = db_path
    DashboardHandler.debug_errors = bool(args.debug_errors)
    DashboardHandler.basic_auth_token = _encode_basic_auth_token(args.basic_auth) if args.basic_auth else None
    DashboardHandler.read_only_mode = bool(args.read_only)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Chronos dashboard running at http://{args.host}:{args.port}")
    print(f"Using DB: {db_path}")
    if args.basic_auth:
        print("Basic auth: enabled")
    elif not _is_local_bind(args.host):
        print("Warning: running without auth on remote bind (unsafe)")
    if args.read_only:
        print("Mutation APIs: disabled (read-only mode)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
