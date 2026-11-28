# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Small date wording shared by the pages. Nothing here reads the ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def ago(stamp: str, now: Optional[datetime] = None) -> str:
    """A short age for an import time, such as ``3 days ago``."""
    if not stamp:
        return ""
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp
    moment = now or datetime.now(timezone.utc)
    seconds = int((moment - then).total_seconds())
    if seconds < 45:
        return "just now"
    steps = (
        (60, "minute"),
        (3600, "hour"),
        (86400, "day"),
        (86400 * 7, "week"),
        (86400 * 30, "month"),
        (86400 * 365, "year"),
    )
    chosen = "minute"
    count = max(1, seconds // 60)
    for size, label in steps:
        if seconds >= size:
            chosen = label
            count = max(1, seconds // size)
    suffix = "" if count == 1 else "s"
    return f"{count} {chosen}{suffix} ago"
