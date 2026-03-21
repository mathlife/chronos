#!/usr/bin/env python3
"""Test suite for Chronos configuration module."""
import os
import sys
import json
import shutil
import uuid
from pathlib import Path

# Add skill to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_chat_id, get_config

TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp_tests"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_config_dir() -> Path:
    path = TMP_ROOT / f"tmp_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def set_config_path(config_file: Path) -> str | None:
    original = os.environ.get("CHRONOS_CONFIG_PATH")
    os.environ["CHRONOS_CONFIG_PATH"] = str(config_file)
    return original


def restore_config_path(original: str | None) -> None:
    if original is None:
        os.environ.pop("CHRONOS_CONFIG_PATH", None)
    else:
        os.environ["CHRONOS_CONFIG_PATH"] = original


def test_default():
    """Test explicit failure when no env or config is provided."""
    # Clear environment
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    tmpdir = make_temp_config_dir()
    original_config_path = set_config_path(tmpdir / "config.json")
    try:
        try:
            get_chat_id()
            raise AssertionError("Expected get_chat_id() to raise ValueError")
        except ValueError:
            print("[ok] Missing chat_id now raises a clear error")
    finally:
        restore_config_path(original_config_path)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_env_override():
    """Test environment variable takes precedence."""
    os.environ['CHRONOS_CHAT_ID'] = "999888777"
    result = get_chat_id()
    assert result == "999888777", f"Expected 999888777, got {result}"
    del os.environ['CHRONOS_CHAT_ID']
    print("[ok] Environment variable overrides config file")


def test_config_file():
    """Test reading from config file."""
    # Clear env
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    # Create temp config
    tmpdir = make_temp_config_dir()
    config_file = tmpdir / "config.json"
        
    test_chat_id = "777666555"
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump({"chat_id": test_chat_id}, f)
        
    original_config_path = set_config_path(config_file)
    
    try:
        result = get_chat_id()
        assert result == test_chat_id, f"Expected {test_chat_id}, got {result}"
        print("[ok] Config file is read correctly")
    finally:
        restore_config_path(original_config_path)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_partial_config():
    """Test that missing chat_id in config still raises a clear error."""
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    tmpdir = make_temp_config_dir()
    config_file = tmpdir / "config.json"
        
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump({"other_key": "value"}, f)
        
    original_config_path = set_config_path(config_file)
    
    try:
        try:
            get_chat_id()
            raise AssertionError("Expected missing chat_id to raise ValueError")
        except ValueError:
            print("[ok] Missing chat_id in config raises ValueError")
    finally:
        restore_config_path(original_config_path)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_get_config():
    """Test get_config returns full config dict."""
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    tmpdir = make_temp_config_dir()
    config_file = tmpdir / "config.json"
        
    test_chat_id = "111222333"
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump({"chat_id": test_chat_id, "custom_key": "custom_value"}, f)
        
    original_config_path = set_config_path(config_file)
    
    try:
        config = get_config()
        assert config['chat_id'] == test_chat_id, f"Chat ID mismatch"
        assert config['custom_key'] == "custom_value", f"Custom key missing"
        print("[ok] get_config returns merged configuration")
    finally:
        restore_config_path(original_config_path)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    print("Running Chronos config tests...\n")
    test_default()
    test_env_override()
    test_config_file()
    test_partial_config()
    test_get_config()
    print("\nAll tests passed.")
