#!/usr/bin/env python3
"""
每日将 Hermes 会话记录增量同步到 NotebookLM（OpenClaw Daily Logs）。

修正版策略：
- NotebookLM 对 .json/.jsonl 直接上传可能返回 400。
- 因此先将会话转成 .md（文本源），再导入 NotebookLM。
- 使用 ledger 做增量，避免重复导入。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

# Ensure restricted environments (e.g. cron) can find ~/.local/bin tools.
_local_bin = os.path.expanduser("~/.local/bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + ":" + os.environ.get("PATH", "")

DEFAULT_NOTEBOOK_ID = "ea0592c6-20ed-4200-82df-a89f16b272ab"  # OpenClaw Daily Logs
DEFAULT_SESSIONS_DIR = Path("/home/ubuntu/.hermes/sessions")
DEFAULT_LEDGER = Path("/home/ubuntu/.chronos/notebooklm_sync_ledger.json")
DEFAULT_EXPORT_DIR = Path("/home/ubuntu/.chronos/notebooklm_exports")


def load_ledger(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {"imported": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"imported": {}}
        imported = data.get("imported", {})
        if not isinstance(imported, dict):
            imported = {}
        return {"imported": imported}
    except Exception:
        return {"imported": {}}


def save_ledger(path: Path, data: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_session_files(sessions_dir: Path) -> List[Path]:
    files = list(sessions_dir.glob("session_*.json")) + list(sessions_dir.glob("*.jsonl"))
    files = [p for p in files if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def _safe_read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def render_markdown(src: Path) -> str:
    """把 Hermes 会话文件转成可检索 markdown 文本。"""
    title = f"Hermes Session Memory - {src.name}"
    lines = [f"# {title}", "", f"- source_file: {src}", f"- exported_at: {datetime.now().isoformat(timespec='seconds')}", ""]

    if src.suffix == ".json":
        payload = _safe_read_json(src)
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if isinstance(msgs, list) and msgs:
                lines.append("## Messages")
                lines.append("")
                for i, m in enumerate(msgs, 1):
                    role = m.get("role", "unknown") if isinstance(m, dict) else "unknown"
                    content = ""
                    if isinstance(m, dict):
                        c = m.get("content", "")
                        if isinstance(c, str):
                            content = c
                        else:
                            content = json.dumps(c, ensure_ascii=False)
                    lines.append(f"### {i}. {role}")
                    lines.append("")
                    lines.append(content[:8000])
                    lines.append("")
                return "\n".join(lines)

    # 默认：按文本逐行写入（适配 jsonl / 异常 json）
    lines.append("## Raw Transcript")
    lines.append("")
    raw = src.read_text(encoding="utf-8", errors="replace")
    # 控制大小，避免单源过大
    lines.append(raw[:200000])
    lines.append("")
    return "\n".join(lines)


def export_to_md(src: Path, export_dir: Path) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    out = export_dir / f"{src.stem}.md"
    out.write_text(render_markdown(src), encoding="utf-8")
    return out


def notebooklm_ready() -> Tuple[bool, str]:
    p = subprocess.run(["notebooklm", "status", "--json"], capture_output=True, text=True)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()
    return True, "ok"


def add_source(notebook_id: str, md_path: Path) -> Tuple[bool, str, str]:
    """Add source with robust fallback.

    Primary: upload markdown file path.
    Fallback: retry via a restricted temporary markdown file with a simple title,
    because some NotebookLM backends intermittently fail file registration with
    "Failed to get SOURCE_ID from registration response" / RPC ADD_SOURCE.
    """
    # 1) Primary path: file upload
    cmd = ["notebooklm", "source", "add", str(md_path), "--type", "text", "-n", notebook_id, "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        try:
            obj = json.loads(p.stdout)
            sid = str(obj.get("source_id", "") or "")
            if not sid and isinstance(obj.get("source"), dict):
                sid = str(obj["source"].get("id", "") or "")
            if sid:
                return True, sid, ""
            return False, "", f"missing source id in response: {p.stdout[:300]}"
        except Exception:
            return False, "", f"invalid json: {p.stdout[:300]}"

    primary_err = (p.stderr or p.stdout).strip()

    # 2) Fallback path: retry through a restricted temp file with a simple title.
    # Never pass rendered session content via argv: local process listings may expose it.
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, "", f"file-read-failed: {e}; primary={primary_err}"

    max_chars = 90000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[truncated by sync script due to size limit]"

    title = md_path.stem[:120]
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", prefix="notebooklm-source-", delete=False) as tmp:
            tmp_name = tmp.name
            tmp.write(text)
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        cmd2 = [
            "notebooklm", "source", "add", tmp_name,
            "--type", "text",
            "--title", title,
            "-n", notebook_id,
            "--json",
        ]
        p2 = subprocess.run(cmd2, capture_output=True, text=True)
    except Exception as exc:
        return False, "", f"primary={primary_err}; fallback-exception={exc}"
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
    if p2.returncode != 0:
        return False, "", f"primary={primary_err}; fallback={(p2.stderr or p2.stdout).strip()}"

    try:
        obj2 = json.loads(p2.stdout)
        sid2 = str(obj2.get("source_id", "") or "")
        if not sid2 and isinstance(obj2.get("source"), dict):
            sid2 = str(obj2["source"].get("id", "") or "")
        if not sid2:
            return False, "", f"fallback-missing-source-id: {p2.stdout[:300]}"
        return True, sid2, ""
    except Exception:
        return False, "", f"fallback-invalid-json: {p2.stdout[:300]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook-id", default=DEFAULT_NOTEBOOK_ID)
    ap.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--export-dir", default=str(DEFAULT_EXPORT_DIR))
    ap.add_argument("--max-files", type=int, default=10)
    args = ap.parse_args()

    sessions_dir = Path(args.sessions_dir)
    ledger_path = Path(args.ledger)
    export_dir = Path(args.export_dir)

    if not sessions_dir.exists():
        print(json.dumps({"ok": False, "error": f"sessions dir not found: {sessions_dir}"}, ensure_ascii=False))
        return 2

    ok, msg = notebooklm_ready()
    if not ok:
        print(json.dumps({"ok": False, "error": f"notebooklm not ready: {msg}"}, ensure_ascii=False))
        return 3

    ledger = load_ledger(ledger_path)
    imported = ledger["imported"]

    files = list_session_files(sessions_dir)
    pending = [f for f in files if str(f) not in imported]
    if args.max_files > 0:
        pending = pending[: args.max_files]

    imported_now, failed = [], []

    for src in pending:
        md = export_to_md(src, export_dir)
        ok, source_id, err = add_source(args.notebook_id, md)
        if ok:
            imported[str(src)] = {
                "source_id": source_id,
                "md_file": str(md),
                "synced_at": datetime.now().isoformat(timespec="seconds"),
            }
            save_ledger(ledger_path, ledger)
            imported_now.append(str(src))
        else:
            failed.append({"path": str(src), "error": err})

    out = {
        "ok": len(failed) == 0,
        "notebook_id": args.notebook_id,
        "scanned": len(files),
        "pending": len(pending),
        "imported_count": len(imported_now),
        "failed_count": len(failed),
        "imported_files": imported_now,
        "failed": failed,
        "ledger": str(ledger_path),
        "export_dir": str(export_dir),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
