# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Queues for unmatched transfers and for venues that are closed or inaccessible.

Each entry has a status and a note. The pack exports the same rows.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .errors import DeskError

LABEL = "awaiting tax review"
_STATUSES = {"open", "noted", "closed"}


def transfers(
    review: Mapping[str, Any], notes: Mapping[str, Mapping[str, str]]
) -> List[Dict[str, str]]:
    """Unmatched transfers from the conversion review, with a stored status and note."""
    rows = []
    for source, kind in (("needs_review", "unmatched transfer"), ("queue", "review queue")):
        for index, item in enumerate(review.get(source) or []):
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id") or f"{kind}-{index}")
            stored = notes.get(identity) or {}
            rows.append(
                {
                    "id": identity,
                    "kind": kind,
                    "status": stored.get("status") or "open",
                    "note": stored.get("note") or "",
                    "when": str(item.get("timestamp") or ""),
                    "asset": str(item.get("asset") or ""),
                    "detail": str(item.get("description") or item.get("note") or ""),
                    "label": LABEL,
                }
            )
    return rows


def gaps(root: Path) -> List[Dict[str, str]]:
    return _read(root)


def save_gap(root: Path, venue: str, status: str, note: str, who: str) -> Dict[str, str]:
    """Add one known gap. A closed or inaccessible venue is named, not guessed at."""
    name = venue.strip()
    if not name:
        raise DeskError("Name the venue that is closed or inaccessible.")
    chosen = status if status in _STATUSES else "open"
    entry = {
        "id": f"gap-{len(_read(root)) + 1}",
        "kind": "known gap",
        "venue": name[:80],
        "status": chosen,
        "note": note.strip()[:500],
        "who": (who or "local")[:80],
        "at": _stamp(),
        "label": LABEL,
    }
    rows = _read(root)
    rows.append(entry)
    _write(root, rows)
    return entry


def save_transfer_note(root: Path, identity: str, status: str, note: str) -> None:
    if not identity.strip():
        raise DeskError("Choose a transfer to update.")
    chosen = status if status in _STATUSES else "open"
    path = root / "transfer-notes.json"
    current = _json_dict(path)
    current[identity] = {"status": chosen, "note": note.strip()[:500]}
    _write_json(path, current)


def export_rows(root: Path, review: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Rows for the accountant pack."""
    notes = _json_dict(root / "transfer-notes.json")
    packed = transfers(review, notes)
    packed.extend(gaps(root))
    return packed


def _read(root: Path) -> List[Dict[str, str]]:
    path = root / "known-gaps.json"
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _write(root: Path, rows: List[Dict[str, str]]) -> None:
    _write_json(root / "known-gaps.json", rows)


def _json_dict(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _write_json(path: Path, payload: object) -> None:
    text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
