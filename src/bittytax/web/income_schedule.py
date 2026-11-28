# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Income for the main return. It is kept apart from the SA108 capital gains pages.

The trading allowance is a check only. It is not deducted.
The income value is also the token cost the calculator carries forward.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .rules import parameter

LABEL = "awaiting tax review"
_PENCE = Decimal("0.01")
_STAKING = {"staking", "staking-reward"}
_DEFI = {"interest"}
_AIRDROP = {"airdrop"}


def build_schedule(
    report: Optional[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    ending_year: int,
) -> Dict[str, Any]:
    """Staking, DeFi returns and income airdrops, separate from SA108."""
    allowance = _decimal(parameter(ending_year, "trading_misc_allowance") or "1000")
    lines = _from_income(report)
    lines.extend(_airdrops(rows, lines))
    total = sum((_decimal(item["amount"]) for item in lines), Decimal(0))
    return {
        "rows": lines,
        "total": f"{total.quantize(_PENCE)}",
        "allowance": f"{allowance.quantize(_PENCE)}",
        "allowance_deducted": False,
        "above_allowance": total > allowance,
        "label": LABEL,
        "confirmed": False,
        "note": (
            "This schedule is for the main return. It is not part of SA108. "
            "The 1000 trading allowance is a check and is not deducted. "
            "Whether the activity is a trade is an open question. "
            "The income value is carried as the token cost."
        ),
        "open_question": (
            "Where staking, DeFi returns and income airdrops sit on the return, "
            "and whether the trading allowance applies, is an open question for the accountant."
        ),
    }


def _from_income(report: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    if report is None:
        return []
    income = report.get("income") or {}
    lines = []
    for row in income.get("rows") or []:
        kind = str(row.get("income_type") or "")
        category = _category(kind, str(row.get("note") or ""))
        if category is None:
            continue
        amount = _decimal(row.get("amount"))
        lines.append(_line(row, category, amount, _question(category)))
    return lines


def _airdrops(
    rows: Sequence[Mapping[str, Any]], lines: Sequence[Mapping[str, str]]
) -> List[Dict[str, str]]:
    seen = {(item["date"], item["asset"], item["category"]) for item in lines}
    extra = []
    for row in rows:
        if str(row.get("type") or "") != "Airdrop":
            continue
        amount = _decimal(row.get("buy_value"))
        item = {
            "date": str(row.get("date") or ""),
            "asset": str(row.get("buy_asset") or ""),
            "category": "income airdrop",
            "amount": f"{amount.quantize(_PENCE)}",
            "token_cost": f"{amount.quantize(_PENCE)}",
            "note": str(row.get("note") or ""),
            "open_question": _question("income airdrop"),
            "label": LABEL,
        }
        key = (item["date"], item["asset"], item["category"])
        if key in seen:
            continue
        seen.add(key)
        extra.append(item)
    return extra


def _line(row: Mapping[str, Any], category: str, amount: Decimal, question: str) -> Dict[str, str]:
    shown = f"{amount.quantize(_PENCE)}"
    return {
        "date": str(row.get("date") or ""),
        "asset": str(row.get("asset") or ""),
        "category": category,
        "amount": shown,
        "token_cost": shown,
        "note": str(row.get("note") or ""),
        "open_question": question,
        "label": LABEL,
    }


def _category(kind: str, note: str) -> Optional[str]:
    folded = kind.casefold()
    text = note.casefold()
    if folded in _STAKING or "staking" in text:
        return "staking"
    if folded in _DEFI or "defi" in text or "lending" in text:
        return "defi return"
    if folded in _AIRDROP or "airdrop" in text:
        return "income airdrop"
    if folded == "income":
        return "income airdrop" if "airdrop" in text else None
    return None


def _question(category: str) -> str:
    if category == "staking":
        return "Whether staking is a trade is an open question. The allowance is not deducted."
    if category == "defi return":
        return "Whether a DeFi return is income or capital is an open question."
    return (
        "An airdrop is income only when it is for a service. "
        "That is an open question. The value shown is the token cost on the row."
    )


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
