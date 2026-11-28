# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Disposals that have no cost basis, and the totals with and without them.

A gap is left open. Market value is not written in as the cost.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence

LABEL = "confirmed"
_PENCE = Decimal("0.01")


def in_window(rows: Sequence[Mapping[str, Any]], start: str, end: str) -> List[Mapping[str, Any]]:
    """Keep shortfalls whose date falls inside the tax year being shown."""
    if not start or not end:
        return list(rows)
    kept = []
    for row in rows:
        day = str(row.get("date") or "")[:10]
        if start <= day <= end:
            kept.append(row)
    return kept


def effect_total(missing_rows: Sequence[Mapping[str, Any]]) -> Decimal:
    """Gain effect of pool shortfalls: the proceeds that were left at zero cost."""
    return sum((_money(row.get("proceeds")) for row in missing_rows), Decimal(0))


def build_report(
    disposals: Sequence[Mapping[str, Any]],
    missing_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """List each disposal with no cost, and the gain totals with and without those rows."""
    gaps = list(missing_rows or [])
    listed: List[Dict[str, str]] = []
    gap_gain = Decimal(0)
    kept_gain = Decimal(0)
    all_gain = Decimal(0)
    for row in disposals:
        gain = _money(row.get("gain"))
        all_gain += gain
        if _no_cost(row, gaps):
            effect = gain
            gap_gain += effect
            listed.append(_line(row, effect))
        else:
            kept_gain += gain
    for row in gaps:
        if _already(listed, row):
            continue
        effect = _money(row.get("proceeds"))
        listed.append(_engine_line(row, effect))
    return {
        "rows": listed,
        "with_rows": f"{all_gain.quantize(_PENCE)}",
        "without_rows": f"{kept_gain.quantize(_PENCE)}",
        "gap_effect": f"{gap_gain.quantize(_PENCE)}",
        "worth_used_as_cost": False,
        "label": LABEL,
        "confirmed": True,
        "note": (
            "These disposals have no cost basis. The gain is the calculator's figure "
            "at the cost it has, including zero. Worth is not written in as the cost."
        ),
    }


def _line(row: Mapping[str, Any], effect: Decimal) -> Dict[str, str]:
    cost = _text(row.get("cost"))
    proceeds = _text(row.get("proceeds"))
    return {
        "date": str(row.get("date") or row.get("date_iso") or ""),
        "asset": str(row.get("asset") or ""),
        "quantity": str(row.get("quantity") or ""),
        "cost": cost or "0.00",
        "proceeds": proceeds or "0.00",
        "gain": _text(row.get("gain")) or "0.00",
        "gain_effect": f"{effect.quantize(_PENCE)}",
        "worth_used_as_cost": "false",
        "source": "disposal",
    }


def _engine_line(row: Mapping[str, Any], effect: Decimal) -> Dict[str, str]:
    return {
        "date": str(row.get("date") or ""),
        "asset": str(row.get("asset") or ""),
        "quantity": str(row.get("quantity") or ""),
        "cost": "0.00",
        "proceeds": _text(row.get("proceeds")) or "0.00",
        "gain": f"{effect.quantize(_PENCE)}",
        "gain_effect": f"{effect.quantize(_PENCE)}",
        "worth_used_as_cost": "false",
        "source": "pool shortfall",
    }


def _no_cost(row: Mapping[str, Any], gaps: Sequence[Mapping[str, Any]]) -> bool:
    cost = _money(row.get("cost"))
    proceeds = _money(row.get("proceeds"))
    if cost == 0 and proceeds != 0:
        return True
    asset = str(row.get("asset") or "")
    when = str(row.get("date_iso") or row.get("date") or "")
    for gap in gaps:
        if str(gap.get("asset") or "") == asset and str(gap.get("date") or "")[:10] == when[:10]:
            return cost == 0
    return False


def _already(listed: Sequence[Mapping[str, str]], row: Mapping[str, Any]) -> bool:
    asset = str(row.get("asset") or "")
    when = _day(str(row.get("date") or ""))
    return any(item["asset"] == asset and _day(item["date"]) == when for item in listed)


def _day(value: str) -> str:
    text = value.strip()[:10]
    parts = text.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return text


def _text(value: object) -> str:
    return str(value or "").strip()


def _money(value: object) -> Decimal:
    try:
        return Decimal(_text(value).replace(",", "") or "0")
    except InvalidOperation:
        return Decimal(0)
