"""Configuration module for Chronos skill."""
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .paths import CONFIG_DIR

_CONFIG_WRITE_LOCK = threading.RLock()
try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - non-posix
    fcntl = None


def get_config_path() -> Path:
    """Return the configuration file path."""
    configured = os.getenv("CHRONOS_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    return CONFIG_DIR / "config.json"


def _load_config_file(config_path: Path) -> tuple[dict, str | None]:
    if not config_path.exists():
        return {}, None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, IOError) as exc:
        return {}, f"Failed to read chronos config: {exc}"


@contextmanager
def _config_write_lock(config_path: Path):
    with _CONFIG_WRITE_LOCK:
        if fcntl is None:
            yield
            return
        lock_path = config_path.with_name(config_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_config_file_unlocked(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{config_path.name}.", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_config_file(config_path: Path, data: dict) -> None:
    with _config_write_lock(config_path):
        _write_config_file_unlocked(config_path, data)


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


def update_raw_config(*, updates: dict | None = None, remove_keys: tuple[str, ...] = ()) -> dict:
    """Atomically patch top-level config keys without losing concurrent updates."""
    updates = dict(updates or {})
    config_path = get_config_path()
    with _config_write_lock(config_path):
        raw, _error = _load_config_file(config_path)
        raw = dict(raw) if isinstance(raw, dict) else {}
        raw.update(updates)
        for key in remove_keys:
            raw.pop(key, None)
        _write_config_file_unlocked(config_path, raw)
        return raw


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
    2. Config file: <workspace>/config/config.json or CHRONOS_CONFIG_PATH (field: chat_id)
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
    config_path = get_config_path()
    with _config_write_lock(config_path):
        raw, _error = _load_config_file(config_path)
        raw = dict(raw) if isinstance(raw, dict) else {}
        raw["channels"] = normalized
        _write_config_file_unlocked(config_path, raw)
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

    config_path = get_config_path()
    with _config_write_lock(config_path):
        raw, _error = _load_config_file(config_path)
        raw = dict(raw) if isinstance(raw, dict) else {}
        channels = raw.get("channels")
        existing = [dict(c) for c in channels if isinstance(c, dict)] if isinstance(channels, list) else []
        replaced = False
        for index, item in enumerate(existing):
            if str(item.get("id") or "").strip() == channel_id:
                existing[index] = dict(channel)
                replaced = True
                break
        if not replaced:
            existing.append(dict(channel))
        raw["channels"] = existing
        _write_config_file_unlocked(config_path, raw)
    return dict(channel)


def remove_channel(channel_id: str) -> bool:
    """Remove one channel by id; return True when removed."""
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        raise ValueError("channel_id is required")
    config_path = get_config_path()
    with _config_write_lock(config_path):
        raw, _error = _load_config_file(config_path)
        raw = dict(raw) if isinstance(raw, dict) else {}
        channels = raw.get("channels")
        existing = [dict(c) for c in channels if isinstance(c, dict)] if isinstance(channels, list) else []
        filtered = [c for c in existing if str(c.get("id") or "").strip() != channel_id]
        removed = len(filtered) != len(existing)
        if removed:
            raw["channels"] = filtered
            _write_config_file_unlocked(config_path, raw)
    return removed
