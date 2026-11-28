# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Fill missing GBP values from the calculator's price sources. Results stay local."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..bt_types import AssetSymbol, Timestamp
from ..config import config
from ..conv.dataparser import DataParser
from ..price.exceptions import DataSourceError
from ..price.valueasset import ValueAsset
from .desk_settings import FIAT_SOURCES, SOURCES, load_settings
from .store import Desk
from .tx_store import load_prices, price_index

Lookup = Callable[[str, str], Optional[Mapping[str, str]]]
Quote = Callable[[str, str, str], Optional[Mapping[str, str]]]
_PENCE = Decimal("0.01")
_USD = re.compile(r"USD\s+([0-9]+(?:\.[0-9]+)?)")
_LABEL_CUSTOM = "custom price"
_LABEL_USD = "USD converted to GBP"
_WARN = "No reliable price"
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
        stored = _cache_entry(quote)
        cache[_cache_key(slot["asset"], slot["day"])] = stored
        value = _value(slot["quantity"], str(quote["price"]))
        filled.append(
            {
                "row_id": slot["row_id"],
                "field": slot["field"],
                "asset": slot["asset"],
                "day": slot["day"],
                "price": stored["price"],
                "value": value,
                "source": stored["source"],
                "label": stored["label"],
                "at": stored["at"],
            }
        )
        seen.add(key)
    _write_cache(desk, cache)
    desk.apply_price_fills(filled)
    _write_json(desk.root / "price-misses.json", missed)
    return {"filled": filled, "missed": missed, "remaining": missing_slots(desk.all_rows())}


def price_transactions(
    desk: Desk,
    quote: Optional[Quote] = None,
    fx: Optional[Quote] = None,
) -> Dict[str, Any]:
    """Price every gap. Custom GBP prices win. Network access stays in ``quote`` and ``fx``."""
    settings = load_settings(desk.root)
    order = _with_fallbacks(list(settings["source_order"]))
    trusted = {item.upper() for item in settings["trusted_coins"]}
    quoter = quote_source if quote is None else quote
    fxer = fx_usd_gbp if fx is None else fx
    custom = price_index(desk.root)
    cache = _read_cache(desk)
    _cache_custom(cache, desk.root)
    rows = {str(row.get("id") or ""): row for row in desk.all_rows()}
    filled: List[Dict[str, str]] = []
    missed: List[Dict[str, str]] = []
    warnings: Dict[str, str] = {}
    seen = {f"{entry['row_id']}:{entry['field']}" for entry in desk.price_fills()}
    for slot in missing_slots(desk.all_rows()):
        key = f"{slot['row_id']}:{slot['field']}"
        if key in seen:
            continue
        outcome = _one_price(
            slot, rows.get(slot["row_id"], {}), custom, order, trusted, quoter, fxer
        )
        if outcome.get("warning"):
            warnings[slot["row_id"]] = str(outcome["warning"])
            missed.append({**slot, "reason": str(outcome["warning"])})
            continue
        if not outcome.get("price"):
            missed.append({**slot, "reason": "No price returned."})
            continue
        stored = _cache_entry(outcome)
        cache[_cache_key(slot["asset"], slot["day"])] = stored
        if outcome.get("fx_day"):
            cache[_cache_key("USD", str(outcome["fx_day"]))] = _cache_entry(
                {"price": outcome.get("fx_rate", ""), "source": outcome.get("fx_source", "")}
            )
        value = str(outcome.get("value") or _value(slot["quantity"], stored["price"]))
        filled.append({**slot, "price": stored["price"], "value": value, **stored})
        seen.add(key)
    _write_cache(desk, cache)
    _write_json(desk.root / "price-warnings.json", {"rows": warnings})
    desk.apply_price_fills(filled)
    _write_json(desk.root / "price-misses.json", missed)
    return {
        "filled": filled,
        "missed": missed,
        "warnings": warnings,
        "remaining": missing_slots(desk.all_rows()),
    }


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


def quote_source(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
    """One source from the calculator's price library. This is the network boundary."""
    saved = _source_config()
    try:
        config.config["data_source_select"] = {asset: [source]}
        if asset.upper() in {code.upper() for code in config.FIAT_LIST} or asset.upper() == "USD":
            config.config["data_source_fiat"] = [source]
        else:
            config.config["data_source_crypto"] = [source]
        found = lookup_gbp(asset, day)
    finally:
        config.config.update(saved)
    if not found:
        return None
    found["source"] = source
    found["label"] = source
    return found


def fx_usd_gbp(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
    """GBP for one USD on ``day``, from one fiat source."""
    del asset
    if source not in FIAT_SOURCES:
        return None
    found = quote_source("USD", day, source)
    if not found:
        return None
    found["label"] = _LABEL_USD
    return found


def load_price_warnings(root: Path) -> Dict[str, str]:
    path = root / "price-warnings.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_bytes().decode("utf-8"))
    rows = loaded.get("rows") if isinstance(loaded, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): str(value) for key, value in rows.items()}


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


def _one_price(
    slot: Mapping[str, str],
    row: Mapping[str, Any],
    custom: Mapping[Tuple[str, str], Decimal],
    order: Sequence[str],
    trusted: Set[str],
    quote: Quote,
    fx: Quote,
) -> Dict[str, str]:
    """Custom price, then a dated USD conversion, then the source order."""
    asset = slot["asset"]
    day = slot["day"]
    owned = custom.get((day, asset.casefold()))
    if owned is not None:
        return {
            "price": f"{owned}",
            "value": _value(slot["quantity"], f"{owned}"),
            "source": _LABEL_CUSTOM,
            "label": _LABEL_CUSTOM,
        }
    if _usd_field(row) == slot["field"]:
        converted = _convert_usd(row, day, order, fx)
        if converted:
            return converted
        if _usd_amount(row) is not None:
            return {"warning": _WARN}
    found = _from_sources(asset, day, order, trusted, quote, fx)
    if found:
        return found
    if asset.upper() not in trusted:
        return {"warning": _WARN}
    return {}


def _from_sources(
    asset: str,
    day: str,
    order: Sequence[str],
    trusted: Set[str],
    quote: Quote,
    fx: Quote,
) -> Dict[str, str]:
    refused = False
    for source in order:
        found = quote(asset, day, source)
        if not found or not found.get("price"):
            continue
        if not _accepted(asset, found, trusted):
            refused = True
            continue
        if str(found.get("currency") or "GBP").upper() == "USD":
            rate = _fx_rate(day, order, fx)
            if rate is None:
                continue
            unit = _decimal(found["price"]) * rate[0]
            return {
                "price": f"{unit}",
                "source": str(found.get("source") or source),
                "label": _LABEL_USD,
                "fx_day": day,
                "fx_rate": f"{rate[0]}",
                "fx_source": rate[1],
            }
        return {
            "price": str(found["price"]),
            "source": str(found.get("source") or source),
            "label": str(found.get("label") or found.get("source") or source),
        }
    if refused:
        return {"warning": _WARN}
    return {}


def _convert_usd(
    row: Mapping[str, Any], day: str, order: Sequence[str], fx: Quote
) -> Dict[str, str]:
    amount = _usd_amount(row)
    rate = _fx_rate(day, order, fx)
    if amount is None or rate is None:
        return {}
    total = (amount * rate[0]).quantize(_PENCE)
    return {
        "price": f"{rate[0]}",
        "value": f"{total}",
        "source": rate[1],
        "label": _LABEL_USD,
        "fx_day": day,
        "fx_rate": f"{rate[0]}",
        "fx_source": rate[1],
    }


def _fx_rate(day: str, order: Sequence[str], fx: Quote) -> Optional[Tuple[Decimal, str]]:
    for source in order:
        if source not in FIAT_SOURCES:
            continue
        found = fx("USD", day, source)
        if found and found.get("price"):
            return _decimal(found["price"]), str(found.get("source") or source)
    return None


def _accepted(asset: str, found: Mapping[str, str], trusted: Set[str]) -> bool:
    low = str(found.get("liquidity") or "").casefold() == "low"
    marked = found.get("reliable")
    if marked is None or marked == "":
        reliable = asset.upper() in trusted and not low
    else:
        reliable = str(marked).casefold() in {"1", "true", "yes"}
    if low and not reliable:
        return False
    if asset.upper() not in trusted and not reliable:
        return False
    return True


def _usd_amount(row: Mapping[str, Any]) -> Optional[Decimal]:
    match = _USD.search(str(row.get("note") or ""))
    if not match:
        return None
    return Decimal(match.group(1))


def _usd_field(row: Mapping[str, Any]) -> str:
    if _usd_amount(row) is None:
        return ""
    if row.get("sell_asset") and not str(row.get("sell_value") or ""):
        return "sell_value"
    if row.get("buy_asset") and not str(row.get("buy_value") or ""):
        return "buy_value"
    return ""


def _with_fallbacks(order: Sequence[str]) -> List[str]:
    chosen: List[str] = []
    for source in list(order) + list(SOURCES):
        if source not in chosen:
            chosen.append(source)
    return chosen


def _source_config() -> Dict[str, Any]:
    return {
        "data_source_select": dict(config.config.get("data_source_select") or {}),
        "data_source_crypto": list(config.config.get("data_source_crypto") or []),
        "data_source_fiat": list(config.config.get("data_source_fiat") or []),
    }


def _cache_custom(cache: Dict[str, Dict[str, str]], root: Path) -> None:
    for item in load_prices(root):
        cache[_cache_key(item["asset"], item["date"])] = _cache_entry(
            {"price": item["gbp"], "source": _LABEL_CUSTOM, "label": _LABEL_CUSTOM}
        )


def _cache_entry(quote: Mapping[str, str]) -> Dict[str, str]:
    source = str(quote.get("source") or "")
    return {
        "price": str(quote.get("price") or ""),
        "source": source,
        "at": str(quote.get("at") or _stamp()),
        "label": str(quote.get("label") or source or "market price"),
    }


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
    _write_json(desk.root / "price-cache.json", cache)


def _write_json(path: Any, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)


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
