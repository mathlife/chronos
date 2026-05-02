"""Configuration module for Chronos skill."""
import json
import os
from pathlib import Path


def get_config_path() -> Path:
    """Return the configuration file path."""
    configured = os.getenv("CHRONOS_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "chronos" / "config.json"


def _load_config_file(config_path: Path) -> tuple[dict, str | None]:
    if not config_path.exists():
        return {}, None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, IOError) as exc:
        return {}, f"Failed to read chronos config: {exc}"


def _write_config_file(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def inspect_config() -> dict:
    """Return structured diagnostics for configuration resolution."""
    config_path = get_config_path()
    env_chat_id = os.getenv("CHRONOS_CHAT_ID")
    file_config, file_error = _load_config_file(config_path)
    file_chat_id = file_config.get("chat_id") if isinstance(file_config, dict) else None
    channels = file_config.get("channels") if isinstance(file_config, dict) else None
    channels_present = isinstance(channels, list) and any(isinstance(c, dict) for c in channels)

    env_chat_id = env_chat_id.strip() if isinstance(env_chat_id, str) else env_chat_id
    file_chat_id = str(file_chat_id).strip() if file_chat_id is not None else None

    resolved_chat_id = None
    source = None
    status = "ok"
    error = file_error

    if env_chat_id:
        resolved_chat_id = env_chat_id
        source = "env"
    elif file_chat_id:
        resolved_chat_id = file_chat_id
        source = "config"
    else:
        if not channels_present:
            status = "error"
            if not error:
                error = (
                    "No notification channels configured. Add `channels` to "
                    f"{config_path} (recommended) or set CHRONOS_CHAT_ID for legacy OpenClaw delivery."
                )
        else:
            status = "ok"

    return {
        "status": status,
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "env_chat_id_present": bool(env_chat_id),
        "file_chat_id_present": bool(file_chat_id),
        "channels_present": channels_present,
        "chat_id": resolved_chat_id,
        "source": source,
        "error": error,
        "config": file_config if isinstance(file_config, dict) else {},
    }


def get_raw_config() -> dict:
    """Read config file without validation fallback logic."""
    config_path = get_config_path()
    config, _error = _load_config_file(config_path)
    return dict(config) if isinstance(config, dict) else {}


def save_raw_config(config: dict) -> dict:
    """Persist full config dictionary and return the saved value."""
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    config_path = get_config_path()
    _write_config_file(config_path, config)
    return config


def validate_config() -> dict:
    """Return config diagnostics and raise when config is invalid."""
    info = inspect_config()
    if info["status"] != "ok":
        raise ValueError(info["error"])
    return info


def get_chat_id() -> str:
    """Get the chat ID for reminders.

    Priority:
    1. Environment variable: CHRONOS_CHAT_ID
    2. Config file: ~/.config/chronos/config.json or CHRONOS_CONFIG_PATH (field: chat_id)
    3. Raises error if not configured
    """
    return validate_config()["chat_id"]


def get_config() -> dict:
    """Get full configuration dictionary."""
    info = validate_config()
    config = dict(info["config"])
    config["chat_id"] = info["chat_id"]
    config["chat_id_source"] = info["source"]
    config["config_path"] = info["config_path"]
    return config


def get_channels() -> list[dict]:
    """Return configured notification channels (may be empty)."""
    config = get_config()
    channels = config.get("channels")
    if isinstance(channels, list):
        return [c for c in channels if isinstance(c, dict)]
    return []


def set_channels(channels: list[dict]) -> list[dict]:
    """Replace notification channels in config and persist."""
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")
    normalized = [dict(c) for c in channels if isinstance(c, dict)]
    raw = get_raw_config()
    raw["channels"] = normalized
    save_raw_config(raw)
    return normalized


def upsert_channel(channel: dict) -> dict:
    """Insert or update one channel by id."""
    if not isinstance(channel, dict):
        raise ValueError("channel must be an object")
    channel_id = str(channel.get("id") or "").strip()
    channel_type = str(channel.get("type") or "").strip()
    if not channel_id:
        raise ValueError("channel.id is required")
    if not channel_type:
        raise ValueError("channel.type is required")

    channels = get_raw_config().get("channels")
    existing = [dict(c) for c in channels if isinstance(c, dict)] if isinstance(channels, list) else []
    replaced = False
    for index, item in enumerate(existing):
        if str(item.get("id") or "").strip() == channel_id:
            existing[index] = dict(channel)
            replaced = True
            break
    if not replaced:
        existing.append(dict(channel))
    set_channels(existing)
    return dict(channel)


def remove_channel(channel_id: str) -> bool:
    """Remove one channel by id; return True when removed."""
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        raise ValueError("channel_id is required")
    channels = get_raw_config().get("channels")
    existing = [dict(c) for c in channels if isinstance(c, dict)] if isinstance(channels, list) else []
    filtered = [c for c in existing if str(c.get("id") or "").strip() != channel_id]
    removed = len(filtered) != len(existing)
    if removed:
        set_channels(filtered)
    return removed
