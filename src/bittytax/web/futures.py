# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Futures and perpetuals on a non-recognised exchange.

They are miscellaneous income on SA100 page TR3, boxes 17 and 18.
They are not SA108 crypto boxes. A loss only reduces later miscellaneous
income of the same type. A comparison switch does not change either return.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

LABEL = "confirmed"
_PENCE = Decimal("0.01")
PLACEMENT = (
    "Non-recognised exchange and DeFi perpetuals are miscellaneous income. "
    "Realised result less trading fees and funding fees goes on SA100 page TR3, "
    "boxes 17 and 18. A loss only reduces later miscellaneous income of the same type. "
    "It is not an SA108 crypto figure. A recognised exchange or an FCA-authorised "
    "broker would be capital in SA108 other property, boxes 14 to 22."
)


def load_toggle(root: Path) -> bool:
    path = root / "futures-toggle.json"
    if not path.is_file():
        return False
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not isinstance(loaded, dict):
        return False
    return bool(loaded.get("treat_other_gains_as_capital"))


def save_toggle(root: Path, enabled: bool) -> None:
    path = root / "futures-toggle.json"
    payload = json.dumps({"treat_other_gains_as_capital": enabled}) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def build_report(
    rows: Sequence[Mapping[str, Any]],
    capital_gains: Decimal,
    treat_as_capital: bool,
) -> Dict[str, Any]:
    """Realised result, trading fees and funding fees, plus an unchanged capital figure."""
    realised = Decimal(0)
    trading = Decimal(0)
    funding = Decimal(0)
    other_property = Decimal(0)
    lines: List[Dict[str, str]] = []
    for row in rows:
        kind = str(row.get("type") or "")
        amount = _amount(row, kind)
        bucket = _bucket(kind, str(row.get("note") or ""))
        if bucket == "realised":
            realised += amount
        elif bucket == "funding":
            funding += abs(amount)
        elif bucket == "trading":
            trading += abs(amount)
        elif bucket == "capital_other":
            other_property += amount
        else:
            continue
        lines.append(
            {
                "date": str(row.get("date") or ""),
                "bucket": bucket,
                "amount": f"{amount.quantize(_PENCE)}",
                "note": str(row.get("note") or ""),
            }
        )
    comparison = capital_gains + realised if treat_as_capital else capital_gains
    fees = trading + funding
    net = realised - fees
    income = realised if realised > 0 else Decimal(0)
    box_18 = min(fees, income)
    box_17 = income
    loss = fees - box_18
    if realised < 0:
        loss += -realised
    return {
        "rows": lines,
        "realised": f"{realised.quantize(_PENCE)}",
        "trading_fees": f"{trading.quantize(_PENCE)}",
        "funding_fees": f"{funding.quantize(_PENCE)}",
        "net": f"{net.quantize(_PENCE)}",
        "box_17": f"{box_17.quantize(_PENCE)}",
        "box_18": f"{box_18.quantize(_PENCE)}",
        "miscellaneous_loss": f"{loss.quantize(_PENCE)}",
        "placement": "SA100 TR3 boxes 17 and 18",
        "sa108_crypto": False,
        "other_property": f"{other_property.quantize(_PENCE)}",
        "other_property_boxes": "SA108 boxes 14 to 22",
        "capital_gains": f"{capital_gains.quantize(_PENCE)}",
        "comparison_gains": f"{comparison.quantize(_PENCE)}",
        "treat_as_capital": treat_as_capital,
        "filed_changed": False,
        "label": LABEL,
        "confirmed": True,
        "open_question": "",
        "note": PLACEMENT,
    }


def relieve_loss(loss: Decimal, later_income: Decimal) -> Dict[str, str]:
    """A futures loss reduces only later miscellaneous income of the same type."""
    available = loss if loss > 0 else Decimal(0)
    income = later_income if later_income > 0 else Decimal(0)
    used = min(available, income)
    return {
        "used": f"{used.quantize(_PENCE)}",
        "remaining": f"{(available - used).quantize(_PENCE)}",
        "taxable": f"{(income - used).quantize(_PENCE)}",
    }


def _bucket(kind: str, note: str) -> str:
    folded = kind.casefold()
    text = note.casefold()
    if "recognised exchange" in text or "fca-authorised" in text or "fca authorised" in text:
        return "capital_other"
    if folded in {"margin-gain", "margin-loss"}:
        return "realised"
    if folded in {"margin-fee", "margin-fee-rebate"}:
        if "funding" in text:
            return "funding"
        return "trading"
    if "funding" in text and "fee" in text:
        return "funding"
    return ""


def _amount(row: Mapping[str, Any], kind: str) -> Decimal:
    if kind == "Margin-Loss":
        return -_decimal(row.get("sell_value") or row.get("proceeds"))
    if kind == "Margin-Fee":
        return -_decimal(row.get("sell_value") or row.get("fee_value"))
    if kind == "Margin-Gain":
        return _decimal(row.get("buy_value") or row.get("gain"))
    if kind == "Margin-Fee-Rebate":
        return _decimal(row.get("buy_value"))
    return _decimal(row.get("sell_value") or row.get("buy_value"))


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
