#!/usr/bin/env python3
"""Regression tests for NotebookLM memory sync export sizing."""
from __future__ import annotations

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = PROJECT_ROOT / "scripts" / "sync_notebooklm_memories.py"
spec = importlib.util.spec_from_file_location("sync_notebooklm_memories", MODULE_PATH)
assert spec is not None and spec.loader is not None
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def test_export_to_md_caps_file_below_notebooklm_single_source_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.jsonl"
    # >20 MiB input; current exporter used to write the raw prefix without a byte-level cap.
    source.write_text("A" * (22 * 1024 * 1024), encoding="utf-8")

    exported = sync.export_to_md(source, tmp_path / "exports")

    assert exported.stat().st_size <= sync.NOTEBOOKLM_SOURCE_SAFE_BYTES
    text = exported.read_text(encoding="utf-8")
    assert "truncated by sync script" in text


if __name__ == "__main__":
    test_export_to_md_caps_file_below_notebooklm_single_source_limit(Path("/tmp/chronos-sync-test"))
    print("[ok] notebooklm sync size regression checks passed")
