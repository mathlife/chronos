"""Notification backends for Chronos (pluggable, config-driven)."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import urllib.request
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .db import DB
from .paths import PYTHON_BIN, SCRIPTS_DIR
from .system_scheduler import build_action_command, create_once_job, supports_system_scheduler
from .timezones import get_shanghai_tz

SHANGHAI_TZ = get_shanghai_tz()


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    channel_id: str
    channel_type: str
    error: str | None = None


class Notifier:
    channel_type: str

    def __init__(self, *, channel_id: str, config: dict):
        self.channel_id = channel_id
        self.config = dict(config or {})

    def send(self, message: str, *, meta: Optional[dict] = None) -> NotifyResult:
        raise NotImplementedError


class WebhookNotifier(Notifier):
    channel_type = "webhook"

    def send(self, message: str, *, meta: Optional[dict] = None) -> NotifyResult:
        url = (self.config.get("url") or "").strip()
        if not url:
            return NotifyResult(False, self.channel_id, self.channel_type, "missing webhook url")

        payload = {
            "message": message,
            "meta": meta or {},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        extra_headers = self.config.get("headers") or {}
        if isinstance(extra_headers, dict):
            for k, v in extra_headers.items():
                if k and v is not None:
                    headers[str(k)] = str(v)

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= int(getattr(resp, "status", 200)) < 300:
                    return NotifyResult(True, self.channel_id, self.channel_type)
                return NotifyResult(False, self.channel_id, self.channel_type, f"http_status={getattr(resp, 'status', None)}")
        except Exception as exc:
            return NotifyResult(False, self.channel_id, self.channel_type, str(exc))


class TelegramNotifier(Notifier):
    channel_type = "telegram"

    def send(self, message: str, *, meta: Optional[dict] = None) -> NotifyResult:
        token = (self.config.get("bot_token") or "").strip()
        chat_id = self.config.get("chat_id")
        if not token:
            return NotifyResult(False, self.channel_id, self.channel_type, "missing bot_token")
        if chat_id is None or str(chat_id).strip() == "":
            return NotifyResult(False, self.channel_id, self.channel_type, "missing chat_id")

        api = f"https://api.telegram.org/bot{token}/sendMessage"
        body = {
            "chat_id": str(chat_id).strip(),
            "text": message,
        }
        if self.config.get("parse_mode"):
            body["parse_mode"] = str(self.config.get("parse_mode"))
        data = urllib.parse.urlencode(body).encode("utf-8")
        req = urllib.request.Request(api, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= int(getattr(resp, "status", 200)) < 300:
                    return NotifyResult(True, self.channel_id, self.channel_type)
                return NotifyResult(False, self.channel_id, self.channel_type, f"http_status={getattr(resp, 'status', None)}")
        except Exception as exc:
            return NotifyResult(False, self.channel_id, self.channel_type, str(exc))


def _build_notifier(channel: dict) -> Notifier | None:
    channel_id = str(channel.get("id") or "").strip()
    channel_type = str(channel.get("type") or "").strip().lower()
    if not channel_id or not channel_type:
        return None
    if channel.get("enabled", True) is False:
        return None
    config = channel.get("config") if isinstance(channel.get("config"), dict) else channel

    if channel_type == WebhookNotifier.channel_type:
        return WebhookNotifier(channel_id=channel_id, config=config)
    if channel_type == TelegramNotifier.channel_type:
        return TelegramNotifier(channel_id=channel_id, config=config)
    return None


def load_notifiers(config: dict) -> list[Notifier]:
    channels = config.get("channels") if isinstance(config, dict) else None
    if not isinstance(channels, list):
        return []
    result: list[Notifier] = []
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        notifier = _build_notifier(channel)
        if notifier:
            result.append(notifier)
    return result


def dispatch_message(
    *,
    config: dict,
    message: str,
    meta: Optional[dict] = None,
    target_ids: Optional[Iterable[str]] = None,
) -> list[NotifyResult]:
    notifiers = load_notifiers(config)
    allowed = {str(x).strip() for x in target_ids} if target_ids is not None else None
    results: list[NotifyResult] = []
    for notifier in notifiers:
        if allowed is not None and notifier.channel_id not in allowed:
            continue
        results.append(notifier.send(message, meta=meta))
    return results


def dispatch_and_record(
    *,
    config: dict,
    message: str,
    meta: Optional[dict] = None,
    target_ids: Optional[Iterable[str]] = None,
) -> list[NotifyResult]:
    """Dispatch once and persist channel-level outcomes for reusable retry handling."""
    results = dispatch_message(config=config, message=message, meta=meta, target_ids=target_ids)
    occurrence_id = (meta or {}).get("occurrence_id")
    task_id = (meta or {}).get("task_id")
    db = DB()
    for result in results:
        key_payload = json.dumps(
            {
                "occurrence_id": occurrence_id,
                "task_id": task_id,
                "channel_id": result.channel_id,
                "message": message,
                "meta": meta or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        delivery_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        db.execute(
            """
            INSERT INTO notification_delivery
            (delivery_key, occurrence_id, task_id, channel_id, channel_type, message, meta, status,
             attempt_count, next_retry_at, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1,
                    CASE WHEN ? THEN NULL ELSE datetime('now', '+5 minutes') END,
                    ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(delivery_key) DO UPDATE SET
                status = excluded.status,
                attempt_count = notification_delivery.attempt_count + 1,
                next_retry_at = excluded.next_retry_at,
                last_error = excluded.last_error,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                delivery_key,
                occurrence_id,
                task_id,
                result.channel_id,
                result.channel_type,
                message,
                json.dumps(meta or {}, ensure_ascii=False),
                "sent" if result.ok else "retry",
                1 if result.ok else 0,
                result.error,
            ),
        )
    db.commit()
    if any(not result.ok for result in results):
        schedule_delivery_retry()
    return results


def schedule_delivery_retry(*, delay_minutes: int = 5) -> bool:
    if not supports_system_scheduler():
        return False
    from datetime import datetime, timedelta

    run_at = datetime.now(SHANGHAI_TZ) + timedelta(minutes=max(1, int(delay_minutes)))
    command = build_action_command(PYTHON_BIN, SCRIPTS_DIR / "periodic_task_manager.py", "--retry-deliveries")
    create_once_job(job_name="chronos_delivery_retry", command=command, run_at=run_at)
    return True


def retry_due_deliveries(*, config: dict, limit: int = 50) -> int:
    """Retry failed channels only; successful sibling channels are never re-sent."""
    db = DB()
    rows = db.execute(
        """
        SELECT id, channel_id, message, meta, attempt_count
        FROM notification_delivery
        WHERE status = 'retry' AND next_retry_at <= CURRENT_TIMESTAMP AND attempt_count < 5
        ORDER BY next_retry_at, id
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    processed = 0
    for row in rows:
        meta_raw = row["meta"] if isinstance(row, sqlite3.Row) else row[3]
        try:
            meta = json.loads(meta_raw or "{}")
        except json.JSONDecodeError:
            meta = {}
        results = dispatch_message(
            config=config,
            message=row["message"],
            meta=meta,
            target_ids=[row["channel_id"]],
        )
        result = results[0] if results else NotifyResult(False, row["channel_id"], "unknown", "channel unavailable")
        attempt_count = int(row["attempt_count"]) + 1
        exhausted = attempt_count >= 5
        delay_minutes = min(60, 5 * (2 ** max(0, attempt_count - 1)))
        db.execute(
            """
            UPDATE notification_delivery
            SET status = ?, attempt_count = ?,
                next_retry_at = CASE WHEN ? THEN NULL ELSE datetime('now', ?) END,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                "sent" if result.ok else ("failed" if exhausted else "retry"),
                attempt_count,
                1 if result.ok or exhausted else 0,
                f"+{delay_minutes} minutes",
                result.error,
                row["id"],
            ),
        )
        if result.ok and meta.get("occurrence_id") and meta.get("task_kind") != "system":
            db.execute(
                "UPDATE periodic_occurrences SET status = 'reminded' WHERE id = ? AND status = 'pending'",
                (int(meta["occurrence_id"]),),
            )
        processed += 1
    db.commit()
    next_due = db.execute(
        "SELECT MIN(next_retry_at) FROM notification_delivery WHERE status = 'retry'"
    ).fetchone()
    if next_due and next_due[0]:
        from datetime import datetime

        due_at = datetime.fromisoformat(str(next_due[0]) + "+00:00")
        now_utc = datetime.now(due_at.tzinfo)
        delay_seconds = max(60, int((due_at - now_utc).total_seconds()))
        schedule_delivery_retry(delay_minutes=(delay_seconds + 59) // 60)
    return processed
