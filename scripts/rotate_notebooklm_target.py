#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
from pathlib import Path

DB_DEFAULT = Path('/home/ubuntu/chronos/.Chonos/config/todo.db')


def sh(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def create_notebook(title: str) -> str:
    rc, out, err = sh(['notebooklm', 'create', title, '--json'])
    if rc != 0:
        raise RuntimeError((err or out).strip())
    obj = json.loads(out)
    nb = (obj.get('notebook') or {})
    nbid = str(nb.get('id') or '').strip()
    if not nbid:
        raise RuntimeError(f'notebook id missing in response: {out[:300]}')
    return nbid


def load_sync_task_payload(db_path: Path, sync_task_id: int) -> tuple[dict, int, str]:
    if not db_path.exists():
        raise RuntimeError(f'database not found: {db_path}')
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute('SELECT handler_payload FROM periodic_tasks WHERE id=?', (sync_task_id,)).fetchone()
    if not row:
        raise RuntimeError(f'sync task id={sync_task_id} not found')
    payload = json.loads(row[0])
    if not isinstance(payload, dict):
        raise RuntimeError('handler_payload must be a JSON object')
    args = payload.get('args')
    if not isinstance(args, list):
        raise RuntimeError('handler_payload.args must be a list')
    if '--notebook-id' not in args:
        raise RuntimeError('handler_payload.args missing --notebook-id')
    index = args.index('--notebook-id')
    if index + 1 >= len(args) or not str(args[index + 1] or '').strip():
        raise RuntimeError('handler_payload.args invalid --notebook-id value')
    return payload, index, str(args[index + 1])


def update_sync_task(db_path: Path, sync_task_id: int, payload: dict, notebook_arg_index: int, notebook_id: str) -> dict:
    args = list(payload['args'])
    old = str(args[notebook_arg_index + 1])
    args[notebook_arg_index + 1] = notebook_id
    payload = dict(payload)
    payload['args'] = args
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            'UPDATE periodic_tasks SET handler_payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), sync_task_id),
        )
    return {'old_notebook_id': old, 'new_notebook_id': notebook_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--sync-task-id', type=int, default=18)
    ap.add_argument('--db', default=str(DB_DEFAULT))
    ap.add_argument('--title-prefix', default='OpenClaw Daily Logs')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    month = dt.datetime.now().strftime('%Y-%m')
    title = f"{args.title_prefix} {month}"

    db_path = Path(args.db)
    payload, notebook_arg_index, old_notebook_id = load_sync_task_payload(db_path, args.sync_task_id)

    if args.dry_run:
        print(json.dumps({'ok': True, 'dry_run': True, 'would_create_title': title, 'sync_task_id': args.sync_task_id, 'old_notebook_id': old_notebook_id}, ensure_ascii=False))
        return 0

    nbid = create_notebook(title)
    changed = update_sync_task(db_path, args.sync_task_id, payload, notebook_arg_index, nbid)
    print(json.dumps({'ok': True, 'created_notebook_title': title, 'created_notebook_id': nbid, 'sync_task_id': args.sync_task_id, **changed}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
