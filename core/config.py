"""Configuration module for Chronos skill."""
import os
import json
from pathlib import Path
from typing import Optional


def get_chat_id() -> str:
    """Get the chat ID for reminders.
    
    Priority:
    1. Environment variable: CHRONOS_CHAT_ID
    2. Config file: ~/.config/chronos/config.json (field: chat_id)
    3. Raises error if not configured
    
    Returns:
        Chat ID as string
        
    Raises:
        ValueError: If chat_id is not configured
    """
    # 1. Check environment variable
    chat_id = os.getenv("CHRONOS_CHAT_ID")
    if chat_id:
        return chat_id.strip()
    
    # 2. Check config file
    config_path = Path.home() / ".config" / "chronos" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                if 'chat_id' in config and config['chat_id']:
                    return str(config['chat_id']).strip()
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Failed to read chronos config: {e}")
    
    # 3. No default - require configuration
    raise ValueError(
        "CHRONOS_CHAT_ID not configured. Please set:\n"
        "  1. Environment variable: export CHRONOS_CHAT_ID='your_chat_id'\n"
        "  2. Or config file: echo '{\"chat_id\": \"your_chat_id\"}' > ~/.config/chronos/config.json"
    )


def get_config() -> dict:
    """Get full configuration dictionary."""
    config = {
        'chat_id': get_chat_id(),
    }
    
    # Load additional config file if exists
    config_path = Path.home() / ".config" / "chronos" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                file_config = json.load(f)
                config.update(file_config)
        except (json.JSONDecodeError, IOError):
            pass
    
    return config
