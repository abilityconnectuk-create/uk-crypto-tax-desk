# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Sterling value of one event at the time it happens.

The price and the foreign-exchange source are stored with the event.
A dollar total is never converted at a single year-end rate.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import DeskError

_PENCE = Decimal("0.01")
_USD = re.compile(r"USD\s+([0-9]+(?:\.[0-9]+)?)")
_RATE = re.compile(r"FX\s+([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9 ._-]{1,40})")
MANUAL = "CRYPTO23000"
LABEL = "confirmed"


class YearEndRateError(DeskError):
    """A dollar total must not be converted at one year-end rate."""


def sterling_value(event: Mapping[str, object]) -> Dict[str, str]:
    """GBP for one event, with the price and the source that produced it."""
    if _flag(event.get("year_end_rate")):
        raise YearEndRateError(
            "A USD total is not converted at a single year-end rate. "
            "Value the event in GBP at the time it happens."
        )
    quantity = _decimal(event.get("quantity"))
    gbp_total = _optional(event.get("gbp_total"))
    usd_total = _optional(event.get("usd_total"))
    unit_gbp = _optional(event.get("unit_gbp"))
    fx_rate = _optional(event.get("fx_rate"))
    source = str(event.get("fx_source") or "").strip()
    gbp, price, used = _choose(quantity, gbp_total, usd_total, unit_gbp, fx_rate, source)
    return {
        "gbp": "" if gbp is None else f"{gbp.quantize(_PENCE)}",
        "usd": "" if usd_total is None else f"{usd_total.quantize(_PENCE)}",
        "price": "" if price is None else f"{price}",
        "fx_source": used,
        "manual": MANUAL,
        "label": LABEL,
        "confirmed": "true",
    }


def annotate_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """USD and GBP side by side. USD is taken from the row, not from a year-end rate."""
    annotated = []
    for row in rows:
        gbp_total = _first_money(row, ("sell_value", "buy_value", "fee_value"))
        usd_total, fx_rate, named = _from_note(str(row.get("note") or ""))
        quantity = _first_money(row, ("sell_quantity", "buy_quantity", "fee_quantity"))
        asset = ", ".join(row.get("assets") or [])
        try:
            valued = sterling_value(
                {
                    "quantity": quantity,
                    "gbp_total": gbp_total,
                    "usd_total": usd_total,
                    "fx_rate": fx_rate,
                    "fx_source": named or "transaction GBP value",
                    "year_end_rate": False,
                }
            )
        except YearEndRateError:
            valued = sterling_value(
                {"quantity": quantity, "gbp_total": gbp_total, "fx_source": "transaction GBP value"}
            )
        valued.update(
            {
                "when": str(row.get("timestamp") or row.get("date") or ""),
                "asset": asset,
                "row_id": str(row.get("id") or ""),
            }
        )
        annotated.append(valued)
    return annotated


def record_valuations(root: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Store the price and the source. The file stays in the data directory."""
    payload = {
        "manual": MANUAL,
        "label": LABEL,
        "confirmed": True,
        "rows": list(rows),
    }
    path = root / "valuations.json"
    _write(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


def _choose(
    quantity: Decimal,
    gbp_total: Optional[Decimal],
    usd_total: Optional[Decimal],
    unit_gbp: Optional[Decimal],
    fx_rate: Optional[Decimal],
    source: str,
) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
    if gbp_total is not None:
        price = (gbp_total / quantity) if quantity else gbp_total
        return gbp_total, price, source or "transaction GBP value"
    if usd_total is not None and fx_rate is not None:
        gbp = usd_total * fx_rate
        price = (gbp / quantity) if quantity else fx_rate
        return gbp, price, source or "event FX rate"
    if unit_gbp is not None:
        return quantity * unit_gbp, unit_gbp, source or "unit GBP price"
    return None, None, ""


def _from_note(note: str) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
    usd = _USD.search(note)
    rate = _RATE.search(note)
    usd_total = Decimal(usd.group(1)) if usd else None
    fx_rate = Decimal(rate.group(1)) if rate else None
    named = rate.group(2).strip() if rate else ""
    return usd_total, fx_rate, named


def _first_money(row: Mapping[str, Any], names: Sequence[str]) -> Optional[Decimal]:
    for name in names:
        amount = _optional(row.get(name))
        if amount is not None and amount != 0:
            return amount
    return None


def _flag(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes"}


def _optional(value: object) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    return _decimal(value)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _write(path: Path, text: str) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
