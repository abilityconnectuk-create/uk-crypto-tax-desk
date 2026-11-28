# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Fill missing GBP values from the calculator's price sources. Results stay local."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..bt_types import AssetSymbol, Timestamp
from ..config import config
from ..conv.dataparser import DataParser
from ..price.exceptions import DataSourceError
from ..price.valueasset import ValueAsset
from .store import Desk

Lookup = Callable[[str, str], Optional[Mapping[str, str]]]
_PENCE = Decimal("0.01")
_SIDES = (
    ("buy_value", "buy_asset", "buy_quantity"),
    ("sell_value", "sell_asset", "sell_quantity"),
    ("fee_value", "fee_asset", "fee_quantity"),
)


def missing_slots(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Rows that still have a crypto quantity and no GBP value."""
    found = []
    for row in rows:
        for field, asset_key, qty_key in _SIDES:
            asset = str(row.get(asset_key, "") or "")
            if not asset or asset in set(config.fiat_list):
                continue
            quantity = _decimal(row.get(qty_key))
            if quantity == 0 or str(row.get(field, "") or "").strip():
                continue
            found.append(
                {
                    "row_id": str(row.get("id", "")),
                    "field": field,
                    "asset": asset,
                    "quantity": f"{quantity}",
                    "day": str(row.get("date", "")),
                    "href": _href(str(row.get("id", ""))),
                }
            )
    return found


def fetch_missing(desk: Desk, lookup: Optional[Lookup] = None) -> Dict[str, Any]:
    """Price what is missing. ``lookup`` defaults to the calculator's price sources."""
    chosen = lookup_gbp if lookup is None else lookup
    cache = _read_cache(desk)
    slots = missing_slots(desk.all_rows())
    filled: List[Dict[str, str]] = []
    missed: List[Dict[str, str]] = []
    seen = {f"{entry['row_id']}:{entry['field']}" for entry in desk.price_fills()}
    for slot in slots:
        key = f"{slot['row_id']}:{slot['field']}"
        if key in seen:
            continue
        cached = cache.get(_cache_key(slot["asset"], slot["day"]))
        quote = dict(cached) if cached else chosen(slot["asset"], slot["day"])
        if not quote or not quote.get("price"):
            missed.append({**slot, "reason": "No price returned."})
            continue
        cache[_cache_key(slot["asset"], slot["day"])] = {
            "price": str(quote["price"]),
            "source": str(quote.get("source", "")),
        }
        value = _value(slot["quantity"], str(quote["price"]))
        filled.append(
            {
                "row_id": slot["row_id"],
                "field": slot["field"],
                "asset": slot["asset"],
                "day": slot["day"],
                "price": str(quote["price"]),
                "value": value,
                "source": str(quote.get("source", "")),
                "at": _stamp(),
            }
        )
        seen.add(key)
    _write_cache(desk, cache)
    desk.apply_price_fills(filled)
    (desk.root / "price-misses.json").write_text(
        json.dumps(missed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"filled": filled, "missed": missed, "remaining": missing_slots(desk.all_rows())}


def lookup_gbp(asset: str, day: str) -> Optional[Dict[str, str]]:
    """One historical GBP price. Network access happens only through this function."""
    when = DataParser.parse_timestamp(f"{day}T12:00:00")
    try:
        record = ValueAsset(price_tool=True).get_historical_price(
            AssetSymbol(asset), Timestamp(when)
        )
    except (DataSourceError, OSError, ValueError):
        return None
    if record.price_ccy is None:
        return None
    return {"price": f"{record.price_ccy}", "source": str(record.data_source or "price")}


def latest_unit_prices(desk: Desk) -> Dict[str, str]:
    """Newest cached unit price for each asset. Nothing is fetched."""
    best: Dict[str, Tuple[str, str]] = {}
    for key, entry in _read_cache(desk).items():
        if "|" not in key or not isinstance(entry, dict):
            continue
        asset, day = key.split("|", 1)
        price = str(entry.get("price", "") or "")
        if not price:
            continue
        current = best.get(asset)
        if current is None or day > current[0]:
            best[asset] = (day, price)
    return {asset: price for asset, (day, price) in best.items()}


def load_misses(desk: Desk) -> List[Dict[str, str]]:
    path = desk.root / "price-misses.json"
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
    return loaded


def _value(quantity: str, price: str) -> str:
    amount = (_decimal(quantity) * _decimal(price)).quantize(_PENCE)
    return f"{amount}"


def _cache_key(asset: str, day: str) -> str:
    return f"{asset}|{day}"


def _read_cache(desk: Desk) -> Dict[str, Dict[str, str]]:
    path = desk.root / "price-cache.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _write_cache(desk: Desk, cache: Mapping[str, Mapping[str, str]]) -> None:
    path = desk.root / "price-cache.json"
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _href(row_id: str) -> str:
    if ":" not in row_id:
        return ""
    import_id, seq = row_id.split(":", 1)
    return f"/transactions/{import_id}/{seq}"


def _decimal(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
