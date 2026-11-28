# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Pending allocation lots. A link is stored only when someone names both sides.

A refund is a disposal of the right, not a reversal of the payment.
A negligible-value claim is the whole section 104 pool and posts the loss.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def apply_refund(gross: str, refund: str, tokens_delivered: str = "no") -> Dict[str, str]:
    """Section 22 disposal of the right. Proceeds are the refund. Cost is its share.

    The stablecoin that comes back is acquired at market value. A shortfall
    with no tokens delivered is an allowable loss.
    """
    paid = _money(gross)
    back = _money(refund)
    if back < 0:
        raise DeskError("Enter the refund as a positive amount.")
    if back > paid:
        raise DeskError("The refund is larger than the original payment.")
    share = (back / paid) if paid else Decimal(0)
    cost = paid * share
    gain = back - cost
    delivered = str(tokens_delivered).strip().casefold() in {"yes", "true", "1"}
    shortfall = Decimal(0) if delivered else paid - back
    pending = paid - back if delivered else Decimal(0)
    return {
        "gross": f"{paid}",
        "refunded": f"{back}",
        "proceeds": f"{back}",
        "cost": f"{cost}",
        "gain": f"{gain}",
        "stablecoin_acquired_at": f"{back}",
        "pending": f"{pending}",
        "shortfall_loss": f"{shortfall}",
        "loss_posted": "true" if shortfall > 0 else "false",
        "treatment": "section 22 disposal of the right",
        "label": LABEL,
    }


def mark_negligible(
    root: Path,
    asset: str,
    who: str,
    value: Optional[str] = None,
    on_date: str = "",
    pool_cost: Optional[str] = None,
    earlier_date: str = "",
    lost_key: bool = False,
) -> Dict[str, str]:
    """Deemed disposal and a fresh acquisition of the whole section 104 pool.

    The date is the claim date, or an earlier date when the asset was already
    negligible, no more than two years before the start of that tax year.
    The loss goes in SA108 box 13.5 and the claim code goes in box 13.6.
    """
    name = asset.strip()
    if not name:
        raise DeskError("Name the asset. The claim is the whole section 104 pool.")
    if value is None or pool_cost is None or not on_date.strip():
        raise DeskError("Enter the value, the pool cost and the claim date.")
    claim_on = _date(on_date)
    deemed = _date(earlier_date) if earlier_date.strip() else claim_on
    earliest = _earliest_deemed(claim_on)
    if deemed < earliest or deemed > claim_on:
        raise DeskError(
            "The deemed date cannot be more than two years before the tax year of the claim."
        )
    worth = _money(value)
    cost = _money(pool_cost)
    if worth < 0 or cost < 0:
        raise DeskError("Enter the value and the pool cost as positive amounts or zero.")
    loss = cost - worth if cost > worth else Decimal(0)
    entry = {
        "asset": name[:80],
        "payment": name[:80],
        "value": f"{worth}",
        "pool_cost": f"{cost}",
        "date": deemed.isoformat(),
        "claim_date": claim_on.isoformat(),
        "loss": f"{loss}",
        "reacquired_at": f"{worth}",
        "loss_posted": "true",
        "claim_code": "NVC",
        "box_13_5": f"{loss}",
        "who": (who or "local")[:80],
        "lost_key": "true" if lost_key else "false",
        "attested": "true",
        "status": "claimed",
        "label": LABEL,
        "confirmed": "true",
        "whole_pool": "true",
    }
    rows = _read(root, "negligible-claims.json")
    rows.append(entry)
    _write(root, "negligible-claims.json", rows)
    return entry


def negligible_claims(root: Path) -> List[Dict[str, Any]]:
    return _read(root, "negligible-claims.json")


def claim_codes(root: Path) -> List[str]:
    """Codes for box 13.6. One negligible-value claim supplies its code."""
    found: List[str] = []
    for row in negligible_claims(root):
        code = str(row.get("claim_code") or "")
        if row.get("loss_posted") == "true" and code and code not in found:
            found.append(code)
    return found


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


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise DeskError("Enter the date as YYYY-MM-DD.") from exc


def _earliest_deemed(claim_on: date) -> date:
    """Two years before 6 April of the tax year in which the claim is made."""
    start_year = claim_on.year if (claim_on.month, claim_on.day) >= (4, 6) else claim_on.year - 1
    return date(start_year - 2, 4, 6)


def _money(value: str) -> Decimal:
    try:
        return Decimal(value.strip() or "0")
    except InvalidOperation as exc:
        raise DeskError("Enter the amount as a number.") from exc
