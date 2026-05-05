"""Whitelist-based system command execution helpers."""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from .config import get_raw_config
from .paths import PYTHON_BIN, SCRIPTS_DIR


DEFAULT_SYSTEM_COMMAND_TEMPLATES: dict[str, list[str]] = {
    "echo": ["echo", "{arg0}"],
    "python3": ["python3", "{*args}"],
    "bash": ["bash", "{*args}"],
    "todo_complete_overdue": [PYTHON_BIN, str(SCRIPTS_DIR / "todo.py"), "complete-overdue"],
    "periodic_ensure_today": [PYTHON_BIN, str(SCRIPTS_DIR / "periodic_task_manager.py"), "--ensure-today"],
}


def _get_templates() -> dict[str, list[str]]:
    raw = get_raw_config().get("system_command_templates")
    templates: dict[str, list[str]] = dict(DEFAULT_SYSTEM_COMMAND_TEMPLATES)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
                templates[key.strip()] = [item.strip() for item in value]
    return templates


def build_handler_payload_from_legacy_command(raw_command: str) -> str:
    """Convert CLI input to the new payload shape."""
    parts = [chunk.strip() for chunk in shlex.split(str(raw_command or "").strip()) if chunk.strip()]
    if not parts:
        raise ValueError("system command cannot be empty")
    command_id = parts[0]
    args = parts[1:]
    return json.dumps({"command_id": command_id, "args": args}, ensure_ascii=False)


def _parse_payload(handler_payload: str | None) -> tuple[str, list[str]]:
    if not handler_payload or not str(handler_payload).strip():
        raise ValueError("missing handler_payload for system command")
    raw = str(handler_payload).strip()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("legacy raw shell command is blocked; use command_id + args payload") from exc

    if not isinstance(decoded, dict):
        raise ValueError("handler_payload must be a JSON object")
    if "command" in decoded or "system_command" in decoded:
        raise ValueError("legacy shell command fields are blocked; use command_id + args")

    command_id = str(decoded.get("command_id") or "").strip()
    if not command_id:
        raise ValueError("handler_payload.command_id is required")
    args = decoded.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("handler_payload.args must be a string array")
    if len(args) > 16:
        raise ValueError("handler_payload.args length exceeds limit")
    return command_id, args


def _render_argv(command_id: str, args: list[str]) -> list[str]:
    templates = _get_templates()
    template = templates.get(command_id)
    if not template:
        raise ValueError(f"command_id '{command_id}' is not in whitelist")

    values = {f"arg{idx}": arg for idx, arg in enumerate(args)}
    values["args"] = " ".join(args)
    rendered: list[str] = []
    for token in template:
        if token == "{*args}":
            rendered.extend(args)
            continue
        try:
            rendered.append(token.format_map(values))
        except KeyError as exc:
            raise ValueError(f"missing placeholder value for template token: {token}") from exc
    return rendered


def execute_system_handler(handler_payload: str | None, *, timeout_seconds: int = 600) -> dict[str, Any]:
    """Execute one whitelisted command payload."""
    command_id, args = _parse_payload(handler_payload)
    argv = _render_argv(command_id, args)
    completed = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout_seconds)
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    output = stdout or stderr
    if len(output) > 500:
        output = output[:500]
    return {
        "command_id": command_id,
        "argv": argv,
        "exit_code": int(completed.returncode),
        "output": output,
        "ok": completed.returncode == 0,
    }
