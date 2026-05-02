"""Notification backends for Chronos (pluggable, config-driven)."""
from __future__ import annotations

import json
import urllib.request
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


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

