"""OS-level scheduler helpers for Chronos-managed one-shot jobs."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .paths import CONFIG_DIR


try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - non-posix
    fcntl = None


_CRONTAB_LOCK = Path(os.getenv("CHRONOS_CRONTAB_LOCK_PATH", str(CONFIG_DIR / "locks" / "crontab.lock"))).expanduser()


def supports_system_scheduler() -> bool:
    return os.name == 'posix' and shutil.which("crontab") is not None


def build_job_name(prefix: str, occurrence_id: int) -> str:
    return f"chronos_{prefix}_{occurrence_id}"


def build_job_command(python_bin: str, script_path: Path, action: str, occurrence_id: int) -> str:
    parts = [python_bin, str(script_path), action, "--occurrence-id", str(occurrence_id)]
    return " ".join(shlex.quote(part) for part in parts)


def build_action_command(python_bin: str, script_path: Path, action: str) -> str:
    return " ".join(shlex.quote(part) for part in (python_bin, str(script_path), action))


def _read_current_crontab() -> list[str]:
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        stdout = (result.stdout or "").lower()
        if "no crontab for" in stderr or "no crontab for" in stdout:
            return []
        raise RuntimeError(result.stderr or result.stdout or "failed to read crontab")
    return result.stdout.splitlines()


def _write_crontab(lines: list[str]) -> None:
    payload = "\n".join(lines).rstrip() + "\n"
    subprocess.run(
        ["crontab", "-"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def _job_marker(job_name: str) -> str:
    return f"# {job_name}"


@contextmanager
def _crontab_lock():
    """Cross-process lock for crontab read-modify-write cycles."""
    if fcntl is None:
        yield
        return
    _CRONTAB_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(_CRONTAB_LOCK, "a+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def create_once_job(*, job_name: str, command: str, run_at: datetime) -> None:
    if not supports_system_scheduler():
        raise RuntimeError("system scheduler requires Linux crontab")
    cron_expr = f"{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *"
    line = f"{cron_expr} {command} {_job_marker(job_name)}"
    with _crontab_lock():
        lines = [existing for existing in _read_current_crontab() if _job_marker(job_name) not in existing]
        lines.append(line)
        _write_crontab(lines)


def remove_job(job_name: str) -> bool:
    if not job_name or not supports_system_scheduler():
        return False
    marker = _job_marker(job_name)
    with _crontab_lock():
        lines = _read_current_crontab()
        filtered = [line for line in lines if marker not in line]
        if len(filtered) == len(lines):
            return True
        _write_crontab(filtered)
    return True
