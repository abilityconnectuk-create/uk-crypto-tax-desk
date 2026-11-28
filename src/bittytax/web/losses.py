# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Losses brought forward and carried forward. The figures stay unconfirmed."""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List

from .dashboard import tax_year_containing
from .errors import DeskError
from .rules import parameter

_PENCE = Decimal("0.01")
_BANNER = "AWAITING TAX REVIEW"


def allocate(
    gains: Decimal, gross_losses: Decimal, brought: Decimal, allowance: Decimal
) -> Dict[str, Decimal]:
    """Use this year's losses first. Brought-forward losses stop at the annual exempt amount."""
    gain = gains if gains > 0 else Decimal(0)
    gross = gross_losses if gross_losses > 0 else Decimal(0)
    opening = brought if brought > 0 else Decimal(0)
    exempt = allowance if allowance > 0 else Decimal(0)
    used_current = min(gross, gain)
    left = gain - used_current
    used_brought = min(opening, max(left - exempt, Decimal(0)))
    carried = opening - used_brought + (gross - used_current)
    return {
        "gains": gain,
        "gross_losses": gross,
        "used_current": used_current,
        "brought": opening,
        "brought_used": used_brought,
        "carried": carried,
        "allowance": exempt,
    }


def save_opening(root: Path, year: str, amount: str) -> None:
    ending = _year(year)
    figure = _money(amount)
    payload = json.dumps({"year": ending, "amount": f"{figure}"}) + "\n"
    path = root / "opening-losses.json"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def load_opening(root: Path) -> Dict[str, str]:
    path = root / "opening-losses.json"
    if not path.is_file():
        return {"year": "", "amount": ""}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {"year": "", "amount": ""}
    return {"year": str(loaded.get("year", "")), "amount": str(loaded.get("amount", ""))}


def schedule(desk: Any) -> List[Dict[str, str]]:
    """One row per tax year. Box 45 is brought-forward losses used. Box 47 is carried forward."""
    opening = load_opening(desk.root)
    years = _years(desk, opening.get("year", ""))
    brought = Decimal(0)
    rows = []
    for year in years:
        if str(year) == opening.get("year"):
            brought = _decimal(opening.get("amount"))
        figures = _year_figures(desk, year)
        result = allocate(figures["gains"], figures["losses"], brought, _allowance(year))
        rows.append(
            {
                "year": str(year),
                "gains": f"{result['gains'].quantize(_PENCE)}",
                "gross_losses": f"{result['gross_losses'].quantize(_PENCE)}",
                "brought_used": f"{result['brought_used'].quantize(_PENCE)}",
                "carried": f"{result['carried'].quantize(_PENCE)}",
                "allowance": f"{result['allowance'].quantize(_PENCE)}",
                "banner": _BANNER,
            }
        )
        brought = result["carried"]
    return rows


def for_year(desk: Any, year: int) -> Dict[str, str]:
    for row in schedule(desk):
        if row["year"] == str(year):
            return row
    return {}


def _year_figures(desk: Any, year: int) -> Dict[str, Decimal]:
    if not desk.all_rows():
        return {"gains": Decimal(0), "losses": Decimal(0)}
    try:
        report = desk.report(year)
    except DeskError:
        return {"gains": Decimal(0), "losses": Decimal(0)}
    summary = report.get("summary") or {}
    return {"gains": _decimal(summary.get("total_gain")), "losses": _decimal(summary.get("losses"))}


def _allowance(year: int) -> Decimal:
    return _decimal(parameter(year, "annual_exempt_amount"))


def _years(desk: Any, opening_year: str) -> List[int]:
    found = set()
    if opening_year.isdigit():
        found.add(int(opening_year))
    for row in desk.all_rows():
        text = str(row.get("timestamp") or "")[:10]
        try:
            when = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            continue
        found.add(tax_year_containing(when))
    return sorted(found)


def _year(value: str) -> int:
    text = value.strip()
    if not text.isdigit():
        raise DeskError("Enter the tax year the opening losses belong to.")
    return int(text)


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value.strip() or "0")
    except InvalidOperation as exc:
        raise DeskError("Enter the opening losses as a number.") from exc
    if amount < 0:
        raise DeskError("Enter opening losses as a positive number.")
    return amount.quantize(_PENCE)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal(0)
