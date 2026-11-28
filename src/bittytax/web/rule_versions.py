# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Who approved a rule change or a manual override, and when.

The log stays on this computer. Nothing here writes to Koinly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .errors import DeskError
from .events import append_event

LABEL = "awaiting tax review"


def record(root: Path, who: str, summary: str, kind: str = "override") -> Dict[str, str]:
    """Append the next version. ``who`` and the time are stored with it."""
    prior = history(root)
    entry = {
        "version": str(len(prior) + 1),
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "who": (who or "local")[:80],
        "kind": kind[:40],
        "summary": summary[:500],
        "label": LABEL,
        "confirmed": "false",
    }
    path = root / "rule-versions.jsonl"
    line = json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    append_event(
        root,
        "rule-version",
        entry["who"],
        entry["summary"],
        {"version": entry["version"]},
    )
    return entry


def history(root: Path) -> List[Dict[str, Any]]:
    path = root / "rule-versions.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def refuse_remote_write(target: str) -> None:
    """This desk never writes to Koinly, or to any other remote tax service."""
    if "koinly" in target.casefold():
        raise DeskError("This desk does not write to Koinly.")
    raise DeskError("This desk does not write to a remote tax service.")
