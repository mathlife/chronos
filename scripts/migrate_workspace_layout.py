#!/usr/bin/env python3
"""Migrate legacy workspace files into .Chonos/config layout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_config_path
from core.paths import TODO_DB, WORKSPACE


@dataclass
class Plan:
    kind: str  # db|config
    source: str
    dest: str
    action: str  # copy|skip|conflict|missing
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy Chronos workspace files to .Chonos/config")
    parser.add_argument("--apply", action="store_true", help="Apply migration (default: dry-run)")
    parser.add_argument("--remove-source", action="store_true", help="Remove source file after successful copy")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _db_candidates() -> list[Path]:
    candidates = [
        PROJECT_ROOT / "todo.db",
        WORKSPACE / "todo.db",
        Path.home() / ".Chonos" / "workspace" / "todo.db",
    ]
    return _unique_paths(candidates)


def _config_candidates() -> list[Path]:
    candidates = [
        PROJECT_ROOT / ".Chonos" / "config.json",
        Path.home() / ".config" / "chronos" / "config.json",
        Path.home() / ".Chonos" / "workspace" / "config.json",
    ]
    return _unique_paths(candidates)


def build_plans(dest: Path, candidates: list[Path], *, kind: str) -> list[Plan]:
    plans: list[Plan] = []
    existing_sources = [p for p in candidates if p.exists() and p != dest]
    if not existing_sources:
        plans.append(
            Plan(
                kind=kind,
                source="",
                dest=str(dest),
                action="missing",
                reason="no legacy source file found",
            )
        )
        return plans

    dest_exists = dest.exists()
    selected_source: Path | None = None
    for source in existing_sources:
        if dest_exists:
            if _sha256(source) == _sha256(dest):
                plans.append(
                    Plan(
                        kind=kind,
                        source=str(source),
                        dest=str(dest),
                        action="skip",
                        reason="destination already has identical content",
                    )
                )
            else:
                plans.append(
                    Plan(
                        kind=kind,
                        source=str(source),
                        dest=str(dest),
                        action="conflict",
                        reason="destination exists with different content",
                    )
                )
            continue
        if selected_source is not None:
            plans.append(
                Plan(
                    kind=kind,
                    source=str(source),
                    dest=str(dest),
                    action="conflict",
                    reason=f"multiple legacy sources found; selected {selected_source}",
                )
            )
            continue

        plans.append(
            Plan(
                kind=kind,
                source=str(source),
                dest=str(dest),
                action="copy",
                reason="migrate legacy file into .Chonos/config layout",
            )
        )
        # first copy plan wins destination ownership; later sources become conflicts/extra
        selected_source = source
    return plans


def _apply_copy(plan: Plan, *, remove_source: bool) -> str:
    source = Path(plan.source)
    dest = Path(plan.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    if remove_source:
        source.unlink()
        return f"copied and removed source: {source} -> {dest}"
    return f"copied: {source} -> {dest}"


def main() -> int:
    args = parse_args()
    dest_db = TODO_DB
    dest_config = get_config_path()

    plans = []
    plans.extend(build_plans(dest_db, _db_candidates(), kind="db"))
    plans.extend(build_plans(dest_config, _config_candidates(), kind="config"))

    applied: list[str] = []
    if args.apply:
        for plan in plans:
            if plan.action != "copy":
                continue
            applied.append(_apply_copy(plan, remove_source=bool(args.remove_source)))

    summary: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "remove_source": bool(args.remove_source),
        "dest_db": str(dest_db),
        "dest_config": str(dest_config),
        "plans": [asdict(p) for p in plans],
        "applied": applied,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Chronos workspace layout migration ({summary['mode']})")
        print(f"dest_db: {summary['dest_db']}")
        print(f"dest_config: {summary['dest_config']}")
        for plan in plans:
            source = plan.source or "N/A"
            print(f"- [{plan.kind}:{plan.action}] {source} -> {plan.dest} | {plan.reason}")
        if applied:
            print("Applied:")
            for note in applied:
                print(f"  - {note}")

    has_conflict = any(p.action == "conflict" for p in plans)
    return 1 if has_conflict else 0


if __name__ == "__main__":
    raise SystemExit(main())
