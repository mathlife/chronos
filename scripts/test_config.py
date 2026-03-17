#!/usr/bin/env python3
"""Test suite for Chronos configuration module."""
import os
import sys
import json
import tempfile
from pathlib import Path

# Add skill to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_chat_id, get_config


def test_default():
    """Test default fallback when no env or config."""
    # Clear environment
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    # Ensure no config file exists
    result = get_chat_id()
    assert result == "YOUR_CHAT_ID", f"Expected YOUR_CHAT_ID, got {result}"
    print("✓ Default chat_id is YOUR_CHAT_ID")


def test_env_override():
    """Test environment variable takes precedence."""
    os.environ['CHRONOS_CHAT_ID'] = "999888777"
    result = get_chat_id()
    assert result == "999888777", f"Expected 999888777, got {result}"
    del os.environ['CHRONOS_CHAT_ID']
    print("✓ Environment variable overrides config file")


def test_config_file():
    """Test reading from config file."""
    # Clear env
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    # Create temp config
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / ".config" / "chronos"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        
        test_chat_id = "777666555"
        with open(config_file, 'w') as f:
            json.dump({"chat_id": test_chat_id}, f)
        
        # Temporarily override home to use temp dir
        original_home = os.environ.get('HOME')
        os.environ['HOME'] = tmpdir
        
        try:
            result = get_chat_id()
            assert result == test_chat_id, f"Expected {test_chat_id}, got {result}"
            print("✓ Config file is read correctly")
        finally:
            if original_home:
                os.environ['HOME'] = original_home
            else:
                del os.environ['HOME']


def test_partial_config():
    """Test that other config keys don't break chat_id lookup."""
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / ".config" / "chronos"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        
        with open(config_file, 'w') as f:
            json.dump({"other_key": "value"}, f)
        
        original_home = os.environ.get('HOME')
        os.environ['HOME'] = tmpdir
        
        try:
            result = get_chat_id()
            assert result == "YOUR_CHAT_ID", f"Expected default, got {result}"
            print("✓ Falls back to default when chat_id missing in config")
        finally:
            if original_home:
                os.environ['HOME'] = original_home
            else:
                del os.environ['HOME']


def test_get_config():
    """Test get_config returns full config dict."""
    if 'CHRONOS_CHAT_ID' in os.environ:
        del os.environ['CHRONOS_CHAT_ID']
    
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / ".config" / "chronos"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        
        test_chat_id = "111222333"
        with open(config_file, 'w') as f:
            json.dump({"chat_id": test_chat_id, "custom_key": "custom_value"}, f)
        
        original_home = os.environ.get('HOME')
        os.environ['HOME'] = tmpdir
        
        try:
            config = get_config()
            assert config['chat_id'] == test_chat_id, f"Chat ID mismatch"
            assert config['custom_key'] == "custom_value", f"Custom key missing"
            print("✓ get_config returns merged configuration")
        finally:
            if original_home:
                os.environ['HOME'] = original_home
            else:
                del os.environ['HOME']


if __name__ == "__main__":
    print("Running Chronos config tests...\n")
    test_default()
    test_env_override()
    test_config_file()
    test_partial_config()
    test_get_config()
    print("\n✅ All tests passed!")
