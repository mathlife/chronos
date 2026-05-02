#!/usr/bin/env python3
"""First-run config bootstrap for Chronos."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config_path, get_raw_config, save_raw_config


def _prompt(text: str, *, required: bool = False, default: str | None = None) -> str | None:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{text}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if value:
            return value
        if not required:
            return None
        print("This field is required.")


def _prompt_yes_no(text: str, *, default_no: bool = True) -> bool:
    hint = "y/N" if default_no else "Y/n"
    value = input(f"{text} ({hint}): ").strip().lower()
    if not value:
        return not default_no
    return value in {"y", "yes"}


def _upsert_channel(channels: list[dict], channel: dict) -> list[dict]:
    channel_id = str(channel.get("id") or "").strip()
    replaced = False
    result: list[dict] = []
    for item in channels:
        if str(item.get("id") or "").strip() == channel_id:
            result.append(channel)
            replaced = True
        else:
            result.append(item)
    if not replaced:
        result.append(channel)
    return result


def _build_channel_from_args(args: argparse.Namespace) -> dict | None:
    if args.channel == "none":
        return None
    if not args.channel:
        return None

    channel_id = args.channel_id or ("tg-main" if args.channel == "telegram" else "hook-main")
    if args.channel == "telegram":
        if not args.bot_token or not args.chat_id:
            raise ValueError("telegram channel requires --bot-token and --chat-id")
        return {
            "id": channel_id,
            "type": "telegram",
            "enabled": True,
            "config": {
                "bot_token": args.bot_token,
                "chat_id": args.chat_id,
            },
        }
    if args.channel == "webhook":
        if not args.webhook_url:
            raise ValueError("webhook channel requires --webhook-url")
        return {
            "id": channel_id,
            "type": "webhook",
            "enabled": True,
            "config": {
                "url": args.webhook_url,
            },
        }
    raise ValueError(f"unsupported channel type: {args.channel}")


def _build_channel_interactive() -> dict | None:
    print("Choose notification channel type:")
    print("  1) telegram")
    print("  2) webhook")
    print("  3) skip for now")
    choice = _prompt("Select", required=True, default="1")
    if choice == "1":
        channel_id = _prompt("Channel id", default="tg-main") or "tg-main"
        bot_token = _prompt("Telegram bot token", required=True)
        chat_id = _prompt("Telegram chat id", required=True)
        return {
            "id": channel_id,
            "type": "telegram",
            "enabled": True,
            "config": {"bot_token": bot_token, "chat_id": chat_id},
        }
    if choice == "2":
        channel_id = _prompt("Channel id", default="hook-main") or "hook-main"
        url = _prompt("Webhook URL", required=True)
        return {
            "id": channel_id,
            "type": "webhook",
            "enabled": True,
            "config": {"url": url},
        }
    if choice == "3":
        return None
    raise ValueError("invalid selection")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronos first-run config setup")
    parser.add_argument("--interactive", action="store_true", help="Run interactive wizard")
    parser.add_argument("--force", action="store_true", help="Overwrite existing channels without confirmation")
    parser.add_argument("--replace-channels", action="store_true", help="Replace full channels list instead of upsert")
    parser.add_argument("--channel", choices=["telegram", "webhook", "none"], help="Channel type for non-interactive mode")
    parser.add_argument("--channel-id", help="Channel id (default: tg-main or hook-main)")
    parser.add_argument("--bot-token", help="Telegram bot token")
    parser.add_argument("--chat-id", help="Telegram chat id (also writes legacy top-level chat_id)")
    parser.add_argument("--webhook-url", help="Webhook URL")
    parser.add_argument("--print-json", action="store_true", help="Print final config as JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = get_config_path()
    existing = get_raw_config()
    existing_channels = existing.get("channels")
    existing_channels = [dict(c) for c in existing_channels if isinstance(c, dict)] if isinstance(existing_channels, list) else []

    interactive_mode = args.interactive or (args.channel is None and not any([args.bot_token, args.chat_id, args.webhook_url]))

    channel: dict | None
    if interactive_mode:
        print("Chronos Setup Wizard")
        print(f"Config path: {config_path}")
        if existing_channels and not args.force:
            if not _prompt_yes_no("Existing channels found. Continue and modify config?", default_no=True):
                print("Cancelled.")
                return 2
        channel = _build_channel_interactive()
        if channel is not None and not args.replace_channels:
            replace = _prompt_yes_no("Replace existing channels list?", default_no=True)
            args.replace_channels = replace
        if channel and channel["type"] == "telegram":
            if not _prompt_yes_no("Also set top-level legacy chat_id field?", default_no=False):
                pass
            else:
                args.chat_id = str(channel.get("config", {}).get("chat_id", "")).strip() or args.chat_id
    else:
        channel = _build_channel_from_args(args)

    updated = dict(existing)
    if channel is not None:
        if args.replace_channels:
            updated["channels"] = [channel]
        else:
            updated["channels"] = _upsert_channel(existing_channels, channel)

    if args.chat_id:
        updated["chat_id"] = str(args.chat_id).strip()

    if updated == existing:
        print("No changes applied.")
        return 0

    if (
        config_path.exists()
        and args.replace_channels
        and existing_channels
        and not args.force
        and not interactive_mode
    ):
        print(
            f"Config exists at {config_path} with existing channels. "
            "Use --force with --replace-channels to overwrite channel list."
        )
        return 2

    save_raw_config(updated)
    print(f"Config written: {config_path}")
    channels = updated.get("channels")
    count = len(channels) if isinstance(channels, list) else 0
    print(f"channels: {count}")
    if args.print_json:
        print(json.dumps(updated, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
