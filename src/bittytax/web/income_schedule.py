# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Income for SA100 page TR3, Other UK income. It is kept apart from SA108.

Boxes 17 and 18 are the income and the expenses. Box 21 is the description.
Miscellaneous losses stay with this income. The income value is the token cost.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .rules import parameter

LABEL = "confirmed"
_PENCE = Decimal("0.01")
_INCOME_WORDS = ("income", "service", "quest", "referral", "task")
_STAKING = {"staking", "staking-reward"}
_DEFI = {"interest"}
_AIRDROP = {"airdrop"}


def build_schedule(
    report: Optional[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    ending_year: int,
) -> Dict[str, Any]:
    """Staking, income DeFi returns and income airdrops, on SA100 TR3."""
    allowance = _decimal(parameter(ending_year, "trading_misc_allowance") or "1000")
    lines = _from_income(report)
    lines.extend(_airdrops(rows, lines))
    total = sum((_decimal(item["amount"]) for item in lines), Decimal(0))
    expenses = sum((_decimal(item.get("expenses")) for item in lines), Decimal(0))
    other = _decimal(parameter(ending_year, "self_employment_receipts") or "0")
    applied = _apply_allowance(total, expenses, other, allowance)
    return {
        "rows": lines,
        "total": f"{total.quantize(_PENCE)}",
        "expenses": f"{expenses.quantize(_PENCE)}",
        "allowance": f"{allowance.quantize(_PENCE)}",
        "allowance_deducted": applied["allowance_deducted"],
        "above_allowance": applied["above"],
        "exempt": applied["exempt"],
        "reported": applied["reported"],
        "box_17": applied["box_17"],
        "box_18": applied["box_18"],
        "box_21": "Cryptoasset miscellaneous income",
        "taxable": applied["taxable"],
        "placement": "SA100 page TR3, Other UK income, boxes 17, 18 and 21",
        "label": LABEL,
        "confirmed": True,
        "note": (
            "This schedule is SA100 page TR3, Other UK income. It is not part of SA108. "
            "Box 17 is the income, box 18 is the expenses, and box 21 describes it. "
            "Gross trading and miscellaneous receipts of 1000 or less are exempt and are "
            "not reported. Above that, 1000 is deducted instead of expenses when that is "
            "larger. The deduction cannot create a loss. The allowance is shared with "
            "self-employment receipts. The income value is carried as the token cost. "
            "A miscellaneous loss stays with this type of income."
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
        if category == "income airdrop" and not _income_airdrop(row):
            continue
        if category == "defi return" and _capital_yield(row):
            continue
        amount = _decimal(row.get("amount"))
        lines.append(_line(row, category, amount))
    return lines


def _airdrops(
    rows: Sequence[Mapping[str, Any]], lines: Sequence[Mapping[str, str]]
) -> List[Dict[str, str]]:
    seen = {(item["date"], item["asset"], item["category"]) for item in lines}
    extra = []
    for row in rows:
        if str(row.get("type") or "") != "Airdrop":
            continue
        if not _income_airdrop(row):
            continue
        amount = _decimal(row.get("buy_value"))
        item = {
            "date": str(row.get("date") or ""),
            "asset": str(row.get("buy_asset") or ""),
            "category": "income airdrop",
            "amount": f"{amount.quantize(_PENCE)}",
            "expenses": "0.00",
            "token_cost": f"{amount.quantize(_PENCE)}",
            "note": str(row.get("note") or ""),
            "open_question": "",
            "label": LABEL,
        }
        key = (item["date"], item["asset"], item["category"])
        if key in seen:
            continue
        seen.add(key)
        extra.append(item)
    return extra


def _line(row: Mapping[str, Any], category: str, amount: Decimal) -> Dict[str, str]:
    shown = f"{amount.quantize(_PENCE)}"
    return {
        "date": str(row.get("date") or ""),
        "asset": str(row.get("asset") or ""),
        "category": category,
        "amount": shown,
        "expenses": f"{_decimal(row.get('fees')).quantize(_PENCE)}",
        "token_cost": shown,
        "note": str(row.get("note") or ""),
        "open_question": "",
        "label": LABEL,
    }


def _capital_yield(row: Mapping[str, Any]) -> bool:
    """Yield that sits inside a receipt or share price is capital, not this schedule."""
    text = str(row.get("note") or "").casefold()
    return "receipt" in text or "share price" in text


def _income_airdrop(row: Mapping[str, Any]) -> bool:
    """Income only when the row is tagged as a service, task, quest or referral."""
    tag = str(row.get("tag") or "").casefold()
    note = str(row.get("note") or "").casefold()
    if tag in {"income", "income_airdrop"}:
        return True
    return any(word in note or word in tag for word in _INCOME_WORDS)


def _apply_allowance(
    gross: Decimal, expenses: Decimal, other: Decimal, allowance: Decimal
) -> Dict[str, Any]:
    """Full relief at or below 1000. Above that, deduct 1000 or the expenses."""
    if gross + other <= allowance:
        has_receipts = gross + other > 0
        return {
            "exempt": has_receipts,
            "reported": False,
            "above": False,
            "allowance_deducted": has_receipts,
            "box_17": "0.00",
            "box_18": "0.00",
            "taxable": "0.00",
        }
    elected = allowance if allowance >= expenses else expenses
    deduction = min(elected, gross)
    taxable = gross - deduction
    return {
        "exempt": False,
        "reported": True,
        "above": True,
        "allowance_deducted": elected == allowance,
        "box_17": f"{gross.quantize(_PENCE)}",
        "box_18": f"{deduction.quantize(_PENCE)}",
        "taxable": f"{taxable.quantize(_PENCE)}",
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


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
