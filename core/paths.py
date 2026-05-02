"""Runtime path and command helpers for Chronos."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

_DEFAULT_WORKSPACE = Path.home() / ".Chonos" / "workspace"
_PROJECT_LOCAL_WORKSPACE = PROJECT_ROOT / ".Chonos"
_CONFIG_DIRNAME = "config"


def _explicit_workspace_candidates() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("CHRONOS_WORKSPACE",):
        raw_value = os.getenv(env_name)
        if raw_value:
            candidates.append(Path(raw_value).expanduser())
    return candidates


def _workspace_candidates() -> list[Path]:
    candidates = _explicit_workspace_candidates()
    candidates.extend([_PROJECT_LOCAL_WORKSPACE, _DEFAULT_WORKSPACE])
    return candidates


def _candidate_db_paths(workspace: Path) -> list[Path]:
    return [
        workspace / _CONFIG_DIRNAME / "todo.db",
        workspace / "todo.db",  # legacy fallback
    ]


def resolve_workspace() -> Path:
    """Return the best workspace root for the current runtime."""
    explicit_candidates = _explicit_workspace_candidates()
    if explicit_candidates:
        for candidate in explicit_candidates:
            if candidate.exists():
                return candidate
        return explicit_candidates[0]

    for candidate in _workspace_candidates():
        if any(db_path.exists() for db_path in _candidate_db_paths(candidate)):
            return candidate

    for candidate in _workspace_candidates():
        if candidate.exists():
            return candidate

    return _PROJECT_LOCAL_WORKSPACE


WORKSPACE = resolve_workspace()
CONFIG_DIR = WORKSPACE / _CONFIG_DIRNAME
TODO_DB = Path(os.getenv("CHRONOS_DB_PATH", str(CONFIG_DIR / "todo.db"))).expanduser()
PYTHON_BIN = os.getenv("CHRONOS_PYTHON_BIN") or sys.executable or "python"
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")


def get_prediction_logger_path() -> Path | None:
    """Return the prediction logger script path when available."""
    configured = os.getenv("CHRONOS_PREDICTION_LOGGER")
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None

    default_path = WORKSPACE / "scripts" / "prediction_logger.py"
    return default_path if default_path.exists() else None
