# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Append-only desk log. Values that look like secrets are not stored."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

_SECRET = re.compile(
    r"(0x[0-9a-fA-F]{8,}|[A-Za-z0-9+/_=-]{32,}|api[_-]?key|secret|password)",
    re.IGNORECASE,
)


def scrub(value: object) -> str:
    """Hide tokens and long secrets. Ordinary words stay readable."""
    return _SECRET.sub("redacted", str(value))[:500]


def append_event(
    root: Path,
    kind: str,
    who: str,
    summary: str,
    detail: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one line. The file mode is 0600."""
    entry = {
        "id": uuid.uuid4().hex[:12],
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "who": scrub(who or "local")[:80],
        "kind": scrub(kind)[:40],
        "summary": scrub(summary),
        "detail": _detail(detail or {}),
    }
    path = root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return entry


def read_events(root: Path) -> List[Dict[str, Any]]:
    """Return every stored event, oldest first."""
    path = root / "events.jsonl"
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


def _detail(detail: Mapping[str, Any]) -> Dict[str, str]:
    packed = {}
    for key, value in list(detail.items())[:12]:
        packed[scrub(key)[:40]] = scrub(value)
    return packed
