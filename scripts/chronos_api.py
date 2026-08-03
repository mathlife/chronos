#!/usr/bin/env python3
"""Machine-friendly JSON API for external callers (e.g. OpenClaw)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.integration_api import (
    complete_occurrence,
    create_task,
    delete_channel,
    get_occurrence,
    get_task,
    list_channels,
    list_tasks,
    put_channel,
    remove_task,
    replace_channels,
    skip_occurrence,
    update_task,
)


def _load_json_arg(raw: str) -> Any:
    text = str(raw or "").strip()
    if text.startswith("@"):
        file_path = Path(text[1:]).expanduser()
        text = file_path.read_text(encoding="utf-8")
    return json.loads(text)


def _emit(payload: dict, *, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Chronos integration JSON API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  chronos_api.py task list --active-only true\n"
            "  chronos_api.py task get --id 42\n"
            "  chronos_api.py task create --payload '{\"name\":\"Daily review\",\"cycle_type\":\"daily\",\"time_of_day\":\"09:00\"}'\n"
            "  chronos_api.py occurrence complete --id 4725 --payload '{\"completion_mode\":\"manual\"}'\n"
            "  chronos_api.py channel replace --payload @channels.json\n\n"
            "Output is always JSON with shape: {\"ok\": bool, \"data\": ..., \"error\": ...}."
        ),
    )
    subparsers = parser.add_subparsers(dest="resource", required=True)

    task_parser = subparsers.add_parser("task", help="Task operations")
    task_subparsers = task_parser.add_subparsers(dest="task_cmd", required=True)
    task_subparsers.add_parser("list", help="List periodic tasks").add_argument(
        "--active-only", choices=["true", "false", "all"], default="all"
    )
    task_subparsers.add_parser("get", help="Get one task").add_argument("--id", type=int, required=True)
    task_subparsers.add_parser("create", help="Create a task").add_argument("--payload", required=True)
    update_parser = task_subparsers.add_parser("update", help="Patch a task")
    update_parser.add_argument("--id", type=int, required=True)
    update_parser.add_argument("--payload", required=True)
    remove_parser = task_subparsers.add_parser("remove", help="Deactivate or hard-delete task")
    remove_parser.add_argument("--id", type=int, required=True)
    remove_parser.add_argument("--hard", action="store_true")

    occurrence_parser = subparsers.add_parser("occurrence", help="Single occurrence operations")
    occurrence_subparsers = occurrence_parser.add_subparsers(dest="occurrence_cmd", required=True)
    occurrence_subparsers.add_parser("get", help="Get one occurrence").add_argument("--id", type=int, required=True)
    complete_parser = occurrence_subparsers.add_parser("complete", help="Mark one occurrence completed")
    complete_parser.add_argument("--id", type=int, required=True)
    complete_parser.add_argument("--payload", default="{}")
    skip_parser = occurrence_subparsers.add_parser("skip", help="Mark one occurrence skipped")
    skip_parser.add_argument("--id", type=int, required=True)
    skip_parser.add_argument("--payload", default="{}")

    channel_parser = subparsers.add_parser("channel", help="Channel operations")
    channel_subparsers = channel_parser.add_subparsers(dest="channel_cmd", required=True)
    channel_subparsers.add_parser("list", help="List channels")
    channel_subparsers.add_parser("replace", help="Replace channels").add_argument("--payload", required=True)
    channel_subparsers.add_parser("put", help="Upsert one channel").add_argument("--payload", required=True)
    channel_subparsers.add_parser("remove", help="Remove one channel").add_argument("--id", required=True)

    return parser


def _parse_active_only(raw: str) -> bool | None:
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.resource == "task":
            if args.task_cmd == "list":
                data = list_tasks(active_only=_parse_active_only(args.active_only))
                return _emit({"ok": True, "data": data}, code=0)
            if args.task_cmd == "get":
                data = get_task(args.id)
                if data is None:
                    return _emit({"ok": False, "error": f"task {args.id} not found"}, code=2)
                return _emit({"ok": True, "data": data}, code=0)
            if args.task_cmd == "create":
                payload = _load_json_arg(args.payload)
                if not isinstance(payload, dict):
                    raise ValueError("task create payload must be an object")
                data = create_task(payload)
                return _emit({"ok": True, "data": data}, code=0)
            if args.task_cmd == "update":
                payload = _load_json_arg(args.payload)
                if not isinstance(payload, dict):
                    raise ValueError("task update payload must be an object")
                data = update_task(args.id, payload)
                return _emit({"ok": True, "data": data}, code=0)
            if args.task_cmd == "remove":
                removed = remove_task(args.id, hard=args.hard)
                if not removed:
                    return _emit({"ok": False, "error": f"task {args.id} not found"}, code=2)
                return _emit({"ok": True, "data": {"id": args.id, "hard": args.hard}}, code=0)

        if args.resource == "occurrence":
            if args.occurrence_cmd == "get":
                data = get_occurrence(args.id)
                if data is None:
                    return _emit({"ok": False, "error": f"occurrence {args.id} not found"}, code=2)
                return _emit({"ok": True, "data": data}, code=0)
            if args.occurrence_cmd == "complete":
                payload = _load_json_arg(args.payload)
                if not isinstance(payload, dict):
                    raise ValueError("occurrence complete payload must be an object")
                data = complete_occurrence(args.id, payload)
                return _emit({"ok": True, "data": data}, code=0)
            if args.occurrence_cmd == "skip":
                payload = _load_json_arg(args.payload)
                if not isinstance(payload, dict):
                    raise ValueError("occurrence skip payload must be an object")
                data = skip_occurrence(args.id, payload)
                return _emit({"ok": True, "data": data}, code=0)

        if args.resource == "channel":
            if args.channel_cmd == "list":
                return _emit({"ok": True, "data": list_channels()}, code=0)
            if args.channel_cmd == "replace":
                payload = _load_json_arg(args.payload)
                if not isinstance(payload, list):
                    raise ValueError("channel replace payload must be a list")
                data = replace_channels(payload)
                return _emit({"ok": True, "data": data}, code=0)
            if args.channel_cmd == "put":
                payload = _load_json_arg(args.payload)
                if not isinstance(payload, dict):
                    raise ValueError("channel put payload must be an object")
                data = put_channel(payload)
                return _emit({"ok": True, "data": data}, code=0)
            if args.channel_cmd == "remove":
                removed = delete_channel(args.id)
                if not removed:
                    return _emit({"ok": False, "error": f"channel {args.id} not found"}, code=2)
                return _emit({"ok": True, "data": {"id": args.id}}, code=0)

        return _emit({"ok": False, "error": "unsupported command"}, code=2)
    except Exception as exc:
        return _emit({"ok": False, "error": str(exc)}, code=1)


if __name__ == "__main__":
    raise SystemExit(main())
