# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Pending allocation lots. A link is stored only when someone names both sides.

A refund reduces the pending amount. A negligible-value claim is recorded
for an accountant and does not post a loss.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List

from ..conv.allocations import pending_allocations
from .errors import DeskError

LABEL = "awaiting tax review"


def list_lots(records_path: str) -> List[Dict[str, str]]:
    return pending_allocations(records_path)


def link_manually(root: Path, payment: str, claim: str, who: str) -> Dict[str, str]:
    """Store a link. Blank sides are refused, so nothing is matched on its own."""
    paid = payment.strip()
    claimed = claim.strip()
    if not paid or not claimed:
        raise DeskError("Name the payment and the claim. Lots are not linked on their own.")
    entry = {
        "payment": paid[:80],
        "claim": claimed[:80],
        "who": (who or "local")[:80],
        "label": LABEL,
        "confirmed": "false",
    }
    rows = _read(root, "allocation-links.json")
    rows.append(entry)
    _write(root, "allocation-links.json", rows)
    return entry


def apply_refund(gross: str, refund: str) -> Dict[str, str]:
    """A refund reverses the original payment. It is not a new gain or loss."""
    paid = _money(gross)
    back = _money(refund)
    if back < 0:
        raise DeskError("Enter the refund as a positive amount.")
    if back > paid:
        raise DeskError("The refund is larger than the original payment.")
    pending = paid - back
    return {
        "gross": f"{paid}",
        "refunded": f"{back}",
        "pending": f"{pending}",
        "loss_posted": "false",
        "label": LABEL,
    }


def mark_negligible(root: Path, payment: str, who: str) -> Dict[str, str]:
    """Record a negligible-value claim. It stays accountant-only and posts no loss."""
    paid = payment.strip()
    if not paid:
        raise DeskError("Name the lot. A negligible-value claim is not created on its own.")
    entry = {
        "payment": paid[:80],
        "who": (who or "local")[:80],
        "status": "accountant-only",
        "loss_posted": "false",
        "label": LABEL,
        "confirmed": "false",
        "open_question": "Whether a negligible-value claim can be made is an open question.",
    }
    rows = _read(root, "negligible-claims.json")
    rows.append(entry)
    _write(root, "negligible-claims.json", rows)
    return entry


def negligible_claims(root: Path) -> List[Dict[str, Any]]:
    return _read(root, "negligible-claims.json")


def links(root: Path) -> List[Dict[str, Any]]:
    return _read(root, "allocation-links.json")


def _read(root: Path, name: str) -> List[Dict[str, Any]]:
    path = root / name
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _write(root: Path, name: str, rows: List[Dict[str, Any]]) -> None:
    path = root / name
    text = json.dumps(rows, ensure_ascii=True, indent=2) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _money(value: str) -> Decimal:
    try:
        return Decimal(value.strip() or "0")
    except InvalidOperation as exc:
        raise DeskError("Enter the amount as a number.") from exc
