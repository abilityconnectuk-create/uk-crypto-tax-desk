# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Look-alike tokens and dust from a look-alike address.

Dropped rows are logged. Restore puts the earlier type back.
The filter does not contact Koinly.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Set

from ..conv.txref import related_official
from .convert import POSSIBLE_SPAM
from .errors import DeskError

LABEL = "awaiting tax review"
OFFICIAL = ("BTC", "ETH", "SOL", "USDT", "USDC", "USDF", "RLUSD")
_DUST = Decimal("0.001")
_ADDRESS = re.compile(r"(0x[0-9A-Fa-f]{16,}|bc1[a-z0-9]{10,})")


def candidates(rows: Sequence[Mapping[str, Any]], known: Sequence[str]) -> List[Dict[str, str]]:
    """Rows the filter would drop, with the reason."""
    owned = set(known) | _established(rows)
    found = []
    for row in rows:
        if str(row.get("type") or "") == POSSIBLE_SPAM:
            continue
        reason = _reason(row, owned)
        if not reason:
            continue
        found.append(
            {
                "row_id": str(row.get("id") or ""),
                "asset": ", ".join(row.get("assets") or []),
                "when": str(row.get("timestamp") or ""),
                "reason": reason,
                "label": LABEL,
            }
        )
    return found


def drop_rows(desk: Any, row_ids: Sequence[str], who: str) -> int:
    """Mark the rows as possible spam and append a log line for each one."""
    by_id = {str(row["id"]): row for row in desk.all_rows()}
    changes = []
    logs = []
    for row_id in row_ids:
        row = by_id.get(row_id)
        if row is None or str(row.get("type")) == POSSIBLE_SPAM:
            continue
        changes.append(
            {
                "row_id": row_id,
                "type": POSSIBLE_SPAM,
                "why": "Look-alike filter.",
            }
        )
        logs.append(
            {
                "row_id": row_id,
                "before_type": str(row.get("type") or ""),
                "reason": _reason(row, set()) or "look-alike",
                "who": who or "local",
                "restored": "no",
            }
        )
    if not changes:
        return 0
    desk.reclassify_many(changes, who or "local")
    _append_logs(desk.root, logs)
    return len(changes)


def restore_row(desk: Any, row_id: str, who: str) -> None:
    """Put back the type from the drop log."""
    logs = read_log(desk.root)
    chosen = None
    for entry in reversed(logs):
        if entry.get("row_id") == row_id and entry.get("restored") != "yes":
            chosen = entry
            break
    if chosen is None:
        raise DeskError("That row is not in the filter log.")
    before = str(chosen.get("before_type") or "")
    if not before:
        raise DeskError("The filter log has no earlier type for that row.")
    desk.reclassify_many(
        [{"row_id": row_id, "type": before, "why": "Restore a filtered row."}],
        who or "local",
    )
    chosen["restored"] = "yes"
    chosen["restored_by"] = who or "local"
    _write_log(desk.root, logs)


def read_log(root: Path) -> List[Dict[str, str]]:
    path = root / "spam-filter.json"
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def lookalike_token(symbol: str) -> bool:
    """True when the ticker imitates an official asset without being that asset."""
    return related_official(symbol, OFFICIAL) is not None


def lookalike_dust(address: str, known: Sequence[str], quantity: Decimal) -> bool:
    """True for a dust amount from an address that copies the ends of a known one."""
    if quantity >= _DUST or not address:
        return False
    for item in known:
        if not item or item == address:
            continue
        if len(item) < 8 or len(address) < 8:
            continue
        if item[:4] == address[:4] and item[-4:] == address[-4:]:
            return True
    return False


def _reason(row: Mapping[str, Any], known: Set[str]) -> str:
    for asset in row.get("assets") or []:
        if lookalike_token(str(asset)):
            return "look-alike token"
    quantity = _decimal(row.get("buy_quantity") or row.get("sell_quantity"))
    for address in _ADDRESS.findall(_blob(row)):
        if lookalike_dust(address, list(known), quantity):
            return "dust from a look-alike address"
    return ""


def _established(rows: Sequence[Mapping[str, Any]]) -> Set[str]:
    found = set()
    for row in rows:
        quantity = _decimal(row.get("buy_quantity") or row.get("sell_quantity"))
        if quantity < _DUST:
            continue
        found.update(_ADDRESS.findall(_blob(row)))
    return found


def _blob(row: Mapping[str, Any]) -> str:
    return " ".join(
        str(row.get(name) or "") for name in ("note", "wallet", "buy_asset", "sell_asset")
    )


def _append_logs(root: Path, logs: Sequence[Mapping[str, str]]) -> None:
    current = read_log(root)
    current.extend(dict(item) for item in logs)
    _write_log(root, current)


def _write_log(root: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path = root / "spam-filter.json"
    payload = json.dumps(list(rows), ensure_ascii=True, indent=2) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
