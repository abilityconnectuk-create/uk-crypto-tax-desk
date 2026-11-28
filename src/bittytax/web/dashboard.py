# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Home-page figures for the tax year that contains today. Rules stay unconfirmed."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from ..config import config
from .errors import DeskError
from .prices import latest_unit_prices
from .rules import parameter
from .store import Desk

_PENCE = Decimal("0.01")


def tax_year_containing(day: date) -> int:
    """Ending year of the UK tax year that contains ``day``. 6 April 2026 is 2027."""
    start = (config.start_of_year_month, config.start_of_year_day)
    if (day.month, day.day) >= start:
        return day.year + 1
    return day.year


def dashboard(desk: Desk, today: Optional[date] = None) -> Dict[str, Any]:
    """Holdings, this year's realised gain, and the configured annual exempt amount."""
    day = date.today() if today is None else today
    year = tax_year_containing(day)
    report: Optional[Dict[str, Any]] = None
    note = ""
    if desk.all_rows():
        try:
            report = desk.report(year)
        except DeskError as exc:
            note = str(exc)
    holdings = _holdings(report, latest_unit_prices(desk))
    allowance = parameter(year, "annual_exempt_amount")
    used = _allowance_used(report)
    return {
        "tax_year": year,
        "label": config.format_tax_year(year),
        "holdings": holdings,
        "realised": "" if report is None else str(report["summary"]["gain"]),
        "allowance": allowance,
        "allowance_used": used,
        "allowance_remaining": _remaining(allowance, used),
        "losses_carried": "The calculator does not carry losses forward.",
        "note": note,
        "banner": "AWAITING TAX REVIEW",
    }


def _holdings(report: Optional[Dict[str, Any]], prices: Dict[str, str]) -> List[Dict[str, str]]:
    rows = [] if report is None else list(report.get("holdings", []))
    costs = [_decimal(row.get("cost")) for row in rows]
    peak = max(costs) if costs else Decimal(0)
    valued = []
    for row, cost in zip(rows, costs):
        asset = str(row.get("asset", ""))
        price = prices.get(asset, "")
        value = ""
        unrealised = ""
        if price:
            amount = (_decimal(row.get("quantity")) * _decimal(price)).quantize(_PENCE)
            value = f"{amount}"
            unrealised = f"{(amount - cost).quantize(_PENCE)}"
        width = 0
        if peak > 0:
            width = int((cost / peak * Decimal(100)).quantize(Decimal("1")))
        valued.append(
            {
                "asset": asset,
                "quantity": str(row.get("quantity", "")),
                "cost": str(row.get("cost", "")),
                "price": price,
                "value": value,
                "unrealised": unrealised,
                "bar": str(width),
            }
        )
    return valued


def _allowance_used(report: Optional[Dict[str, Any]]) -> str:
    if report is None:
        return ""
    estimate = report.get("estimate", {})
    used = _decimal(estimate.get("before", {}).get("allowance_used"))
    after = estimate.get("after") or {}
    if after:
        used += _decimal(after.get("allowance_used"))
    return f"{used.quantize(_PENCE)}"


def _remaining(allowance: str, used: str) -> str:
    if not allowance or not used:
        return ""
    return f"{(_decimal(allowance) - _decimal(used)).quantize(_PENCE)}"


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
