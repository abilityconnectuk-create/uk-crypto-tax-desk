# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Compare one tax year with an imported Koinly file.

Each difference is given a named cause, or marked unexplained.
The files are read locally. This module does not contact Koinly.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import DeskError
from .koinly_check import compare_year, parse_complete, parse_summary

LABEL = "awaiting tax review"
METRICS = ("disposals", "proceeds", "costs", "gains", "losses", "income")
CAUSES = ("rule", "matching difference", "missing cost", "manual override", "unexplained")
_PENCE = Decimal("0.01")
_INCOME_TYPES = ("reward", "staking", "airdrop", "interest", "income", "mining")


def load_tolerance(root: Path) -> Decimal:
    path = root / "koinly-tolerance.json"
    if not path.is_file():
        return _PENCE
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _PENCE
    if not isinstance(loaded, dict):
        return _PENCE
    return _positive(str(loaded.get("tolerance") or "0.01"))


def save_tolerance(root: Path, raw: str) -> Decimal:
    amount = _positive(raw)
    path = root / "koinly-tolerance.json"
    payload = json.dumps({"tolerance": f"{amount}"}) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return amount


def desk_metrics(report: Mapping[str, Any]) -> Dict[str, Decimal]:
    """Figures from the calculator for one year."""
    summary = report.get("summary") or {}
    income = report.get("income") or {}
    disposals = report.get("disposals") or []
    gains = Decimal(0)
    losses = Decimal(0)
    for row in disposals:
        gain = _decimal(row.get("gain"))
        if gain >= 0:
            gains += gain
        else:
            losses += -gain
    count = summary.get("disposals")
    return {
        "disposals": _decimal(count if count not in (None, "") else len(disposals)),
        "proceeds": _decimal(summary.get("proceeds")),
        "costs": _decimal(summary.get("allowable_costs")),
        "gains": gains if disposals else _decimal(summary.get("total_gain")),
        "losses": losses if disposals else _decimal(summary.get("losses")),
        "income": _decimal(income.get("amount")),
    }


def koinly_metrics(summary_csv: str, complete_csv: str) -> Dict[str, Optional[Decimal]]:
    """Figures from the imported files. A missing count stays unknown."""
    _summary = parse_summary(summary_csv) if summary_csv.strip() else {}
    _complete, rows = parse_complete(complete_csv) if complete_csv.strip() else ({}, [])
    source = rows or []
    if not source and not _summary:
        return {name: None for name in METRICS}
    if source:
        return _from_rows(source)
    totals = _sum_assets(_summary)
    return {
        "disposals": None,
        "proceeds": totals["proceeds"],
        "costs": totals["cost"],
        "gains": totals["gains"],
        "losses": totals["losses"],
        "income": Decimal(0),
    }


def reconcile(
    desk: Mapping[str, Decimal],
    koinly: Mapping[str, Optional[Decimal]],
    tolerance: Decimal,
    explanations: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, Any]:
    """Name a cause for each difference, and flag anything left over."""
    band = tolerance if tolerance >= 0 else _PENCE
    rows = []
    unexplained = False
    for metric in METRICS:
        other = koinly.get(metric)
        ours = desk.get(metric, Decimal(0))
        if other is None:
            rows.append(_unknown(metric, ours))
            unexplained = True
            continue
        difference = ours - other
        used, left = _apply(metric, difference, band, explanations or [])
        flag = abs(left) > band
        unexplained = unexplained or flag
        rows.append(
            {
                "metric": metric,
                "desk": _show(ours),
                "koinly": _show(other),
                "difference": _show(difference),
                "causes": used,
                "unexplained": flag,
                "remainder": _show(left),
            }
        )
    return {
        "rows": rows,
        "tolerance": _show(band),
        "unexplained": unexplained,
        "label": LABEL,
        "confirmed": False,
        "causes": list(CAUSES),
    }


def hint_explanations(
    desk: Mapping[str, Decimal],
    koinly: Mapping[str, Optional[Decimal]],
    missing_cost_effect: Decimal,
    manual_override_amount: Decimal,
) -> List[Dict[str, object]]:
    """Causes the desk can name from its own records. The rest stays unexplained."""
    found: List[Dict[str, object]] = []
    disposal_gap = _count_gap(desk.get("disposals", Decimal(0)), koinly.get("disposals"))
    if disposal_gap != 0:
        found.append(
            {"metric": "disposals", "cause": "matching difference", "amount": disposal_gap}
        )
    income_gap = _money_gap(desk.get("income", Decimal(0)), koinly.get("income"))
    if income_gap != 0:
        found.append({"metric": "income", "cause": "rule", "amount": income_gap})
    if missing_cost_effect != 0:
        found.append({"metric": "gains", "cause": "missing cost", "amount": missing_cost_effect})
        found.append({"metric": "costs", "cause": "missing cost", "amount": -missing_cost_effect})
    if manual_override_amount != 0:
        found.append(
            {"metric": "proceeds", "cause": "manual override", "amount": manual_override_amount}
        )
    return found


def _apply(
    metric: str,
    difference: Decimal,
    tolerance: Decimal,
    explanations: Sequence[Mapping[str, object]],
) -> Tuple[List[Dict[str, str]], Decimal]:
    left = difference
    used = []
    for item in explanations:
        if str(item.get("metric")) != metric or abs(left) <= tolerance:
            continue
        cause = str(item.get("cause") or "")
        if cause not in CAUSES or cause == "unexplained":
            continue
        amount = _decimal(item.get("amount"))
        if amount == 0 or (left > 0) != (amount > 0):
            continue
        take = amount if abs(amount) <= abs(left) else left
        used.append({"cause": cause, "amount": _show(take)})
        left -= take
    if abs(left) > tolerance:
        used.append({"cause": "unexplained", "amount": _show(left)})
    return used, left


def _unknown(metric: str, ours: Decimal) -> Dict[str, Any]:
    return {
        "metric": metric,
        "desk": _show(ours),
        "koinly": "",
        "difference": "",
        "causes": [{"cause": "unexplained", "amount": ""}],
        "unexplained": True,
        "remainder": "",
    }


def _from_rows(rows: Sequence[Mapping[str, str]]) -> Dict[str, Optional[Decimal]]:
    proceeds = costs = gains = losses = income = Decimal(0)
    disposals = 0
    for row in rows:
        kind = str(row.get("type") or "").casefold()
        gain = _decimal(row.get("gain"))
        if any(name in kind for name in _INCOME_TYPES):
            income += _decimal(row.get("proceeds")) or _decimal(row.get("gain"))
            continue
        disposals += 1
        proceeds += _decimal(row.get("proceeds"))
        costs += _decimal(row.get("cost"))
        if gain >= 0:
            gains += gain
        else:
            losses += -gain
    return {
        "disposals": Decimal(disposals),
        "proceeds": proceeds,
        "costs": costs,
        "gains": gains,
        "losses": losses,
        "income": income,
    }


def _sum_assets(assets: Mapping[str, Mapping[str, Decimal]]) -> Dict[str, Decimal]:
    proceeds = costs = gains = losses = Decimal(0)
    for row in assets.values():
        proceeds += row.get("proceeds", Decimal(0))
        costs += row.get("cost", Decimal(0))
        gain = row.get("gain", Decimal(0))
        if gain >= 0:
            gains += gain
        else:
            losses += -gain
    return {"proceeds": proceeds, "cost": costs, "gains": gains, "losses": losses}


def _count_gap(ours: Decimal, other: Optional[Decimal]) -> Decimal:
    if other is None:
        return Decimal(0)
    return ours - other


def _money_gap(ours: Decimal, other: Optional[Decimal]) -> Decimal:
    if other is None:
        return Decimal(0)
    return ours - other


def _show(value: Decimal) -> str:
    if value == value.to_integral():
        return str(int(value)) if value == int(value) else f"{value.quantize(_PENCE)}"
    return f"{value.quantize(_PENCE)}"


def _positive(raw: str) -> Decimal:
    try:
        amount = Decimal(raw.strip() or "0")
    except InvalidOperation as exc:
        raise DeskError("Enter the tolerance as a number.") from exc
    if amount < 0:
        raise DeskError("Enter the tolerance as a positive number.")
    return amount


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def page_bits(
    report: Optional[Mapping[str, Any]],
    summary_csv: str,
    complete_csv: str,
    tolerance: Decimal,
    missing_cost_effect: Decimal,
    manual_override_amount: Decimal,
) -> Dict[str, Any]:
    """Comparison plus the named-cause reconciliation for one year."""
    comparison = None
    if summary_csv.strip() or complete_csv.strip():
        comparison = compare_year(report or {"disposals": []}, summary_csv, complete_csv)
    if report is None:
        return {"comparison": comparison, "reconciliation": None}
    desk = desk_metrics(report)
    koinly = koinly_metrics(summary_csv, complete_csv)
    hints = hint_explanations(desk, koinly, missing_cost_effect, manual_override_amount)
    return {
        "comparison": comparison,
        "reconciliation": reconcile(desk, koinly, tolerance, hints),
    }
