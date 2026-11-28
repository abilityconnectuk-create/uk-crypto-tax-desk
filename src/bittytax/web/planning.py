# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""What-if figures. A hypothetical disposal is calculated and then discarded."""

from __future__ import annotations

import csv
import io
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import config
from .dashboard import dashboard, tax_year_containing
from .engine_run import run_tax_year
from .errors import DeskError
from .prices import latest_unit_prices
from .store import Desk

_DISCLAIMER = (
    "This is not advice and it does not place a trade. "
    "It is the calculator's result for a sale that was not made and was not saved."
)


def plan_view(desk: Desk, today: Optional[date] = None) -> Dict[str, Any]:
    """Allowance remaining and unrealised gain, for the year that contains today."""
    board = dashboard(desk, today)
    return {
        "board": board,
        "disclaimer": _DISCLAIMER,
        "result": None,
    }


def hypothetical(
    desk: Desk, asset: str, quantity: str, day: str, proceeds: str = ""
) -> Dict[str, str]:
    """Run one extra disposal against a copy of the records. The ledger is not written."""
    symbol = asset.strip().upper()
    if not symbol:
        raise DeskError("Enter an asset.")
    amount = _positive(quantity, "Enter a quantity greater than zero.")
    when = _day(day)
    year = tax_year_containing(when)
    value = proceeds.strip()
    if not value:
        cached = latest_unit_prices(desk).get(symbol, "")
        if not cached:
            raise DeskError("Enter the proceeds in GBP, or fetch a price for that asset first.")
        unit = _positive(cached, "The cached price is not a number.")
        value = f"{(amount * unit).quantize(Decimal('0.01'))}"
    else:
        _positive(value, "Enter proceeds greater than zero.")
    baseline = desk.report(year)
    before_rows = len(desk.all_rows())
    before_bytes = desk.records_path.read_bytes()
    source = desk.records_path.read_text(encoding="utf-8")
    changed = _with_sale(source, symbol, amount, value, when)
    alt = _run_copy(changed, year)
    if desk.records_path.read_bytes() != before_bytes or len(desk.all_rows()) != before_rows:
        raise DeskError("The hypothetical sale touched the saved records.")
    return {
        "asset": symbol,
        "quantity": f"{amount}",
        "day": when.isoformat(),
        "proceeds": value,
        "label": config.format_tax_year(year),
        "baseline_gain": str(baseline["summary"]["gain"]),
        "hypothetical_gain": str(alt["summary"]["gain"]),
        "difference": _difference(baseline["summary"]["gain"], alt["summary"]["gain"]),
        "disclaimer": _DISCLAIMER,
    }


def _with_sale(text: str, asset: str, quantity: Decimal, proceeds: str, when: date) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise DeskError("Import some records before trying a hypothetical sale.")
    header = rows[0]
    stamp = datetime(when.year, when.month, when.day, 12, 0, 0).strftime("%Y-%m-%dT%H:%M:%S UTC")
    record = {name: "" for name in header}
    record["Type"] = "Trade"
    record["Buy Quantity"] = proceeds
    record["Buy Asset"] = "GBP"
    record["Buy Value in GBP"] = proceeds
    record["Sell Quantity"] = f"{quantity}"
    record["Sell Asset"] = asset
    record["Sell Value in GBP"] = proceeds
    record["Wallet"] = "Hypothetical"
    record["Timestamp"] = stamp
    record["Note"] = "what-if, not saved"
    rows.append([record.get(name, "") for name in header])
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def _run_copy(text: str, year: int) -> Dict[str, Any]:
    descriptor, name = tempfile.mkstemp(suffix=".csv")
    os.close(descriptor)
    path = Path(name)
    try:
        path.write_text(text, encoding="utf-8")
        return run_tax_year(str(path), year)
    finally:
        path.unlink(missing_ok=True)


def _difference(baseline: object, changed: object) -> str:
    gap = Decimal(str(changed)) - Decimal(str(baseline))
    return f"{gap.quantize(Decimal('0.01'))}"


def _positive(value: str, message: str) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise DeskError(message) from exc
    if amount <= 0:
        raise DeskError(message)
    return amount


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DeskError("Enter the date as YYYY-MM-DD.") from exc
