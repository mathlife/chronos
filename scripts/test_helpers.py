#!/usr/bin/env python3
"""Shared helpers for local regression scripts."""
from __future__ import annotations

import uuid
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = PROJECT_ROOT / ".tmp_tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def make_case_dir(case_name: str) -> Path:
    case_dir = TMP_ROOT / f"{case_name}-{uuid.uuid4().hex}"
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


def reset_db_singleton(db_module: ModuleType) -> None:
    if hasattr(db_module.DB, "reset_for_tests"):
        db_module.DB.reset_for_tests()
    else:
        if db_module.DB._conn is not None:
            db_module.DB._conn.close()
        db_module.DB._conn = None
        db_module.DB._instance = None
    db_module.clear_task_cache()
