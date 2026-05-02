"""Timezone helpers with a fixed-offset fallback for Windows runtimes without tzdata."""
from __future__ import annotations

from datetime import timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_shanghai_tz():
    try:
        return ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8))


def get_utc_tz():
    try:
        return ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        return timezone.utc
