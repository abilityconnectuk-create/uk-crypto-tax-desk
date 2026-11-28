# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Data-health list: fix these rows before relying on a report."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..config import config

_OUT_TYPES = {"Withdrawal"}


def analyse(
    rows: Sequence[Mapping[str, Any]],
    review_lists: Mapping[str, Sequence[Mapping[str, str]]],
    missing_cost: Sequence[Mapping[str, str]],
) -> Dict[str, Any]:
    """Group ledger problems. Each issue links to a transaction when one matches."""
    issues: List[Dict[str, str]] = []
    issues.extend(_missing_cost(rows, missing_cost))
    issues.extend(_unmatched(rows, review_lists.get("queue", ())))
    issues.extend(_duplicates(rows))
    issues.extend(_missing_prices(rows))
    issues.extend(_negative_balances(rows))
    counts: Dict[str, int] = {}
    for issue in issues:
        counts[issue["category"]] = counts.get(issue["category"], 0) + 1
    return {"issues": issues, "counts": counts, "total": len(issues)}


def _missing_cost(
    rows: Sequence[Mapping[str, Any]], missing_cost: Sequence[Mapping[str, str]]
) -> List[Dict[str, str]]:
    found = []
    for item in missing_cost:
        match = _first_match(rows, str(item.get("date", "")), str(item.get("asset", "")))
        quantity = str(item.get("quantity", ""))
        asset = str(item.get("asset", ""))
        found.append(
            _issue(
                "missing-cost",
                f"Missing cost basis for {quantity} {asset}".strip(),
                f"On {item.get('date', '')}. The calculator has no pooled cost for this disposal.",
                match,
            )
        )
    return found


def _unmatched(
    rows: Sequence[Mapping[str, Any]], queue: Sequence[Mapping[str, str]]
) -> List[Dict[str, str]]:
    found = []
    for item in queue:
        asset = str(item.get("asset", ""))
        match = _first_match(rows, str(item.get("timestamp", "")), asset, _OUT_TYPES)
        if match is None:
            match = _first_match(rows, str(item.get("timestamp", "")), asset)
        detail = str(item.get("description") or item.get("disposition") or "")
        found.append(
            _issue(
                "unmatched-transfer",
                f"Unmatched transfer out of {item.get('quantity', '')} {asset}".strip(),
                detail or "A transfer out has no matching transfer in.",
                match,
            )
        )
    return found


def _duplicates(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    groups: Dict[Tuple[str, ...], List[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_duplicate_key(row), []).append(row)
    found = []
    for group in groups.values():
        if len(group) < 2:
            continue
        for row in group:
            found.append(
                _issue(
                    "duplicate",
                    f"Likely duplicate {row.get('type', '')} on {row.get('date', '')}",
                    "Same type, time, amounts, assets and wallet as another row.",
                    row,
                )
            )
    return found


def _missing_prices(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    found = []
    for row in rows:
        gaps = []
        for side, asset_key, qty_key, value_key in (
            ("buy", "buy_asset", "buy_quantity", "buy_value"),
            ("sell", "sell_asset", "sell_quantity", "sell_value"),
            ("fee", "fee_asset", "fee_quantity", "fee_value"),
        ):
            asset = str(row.get(asset_key, "") or "")
            if not asset or _is_fiat(asset):
                continue
            if _decimal(row.get(qty_key)) == 0:
                continue
            if str(row.get(value_key, "") or "").strip():
                continue
            gaps.append(f"{side} {asset}")
        if not gaps:
            continue
        found.append(
            _issue(
                "missing-price",
                f"Missing GBP value for {', '.join(gaps)}",
                f"On {row.get('date', '')}. The row has a quantity and no local-currency value.",
                row,
            )
        )
    return found


def _negative_balances(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    ordered = sorted(rows, key=lambda row: (str(row.get("timestamp", "")), str(row.get("id", ""))))
    balances: Dict[str, Decimal] = {}
    found = []
    for row in ordered:
        for asset, delta in _deltas(row):
            if _is_fiat(asset):
                continue
            current = balances.get(asset, Decimal(0)) + delta
            balances[asset] = current
            if current < 0:
                found.append(
                    _issue(
                        "negative-balance",
                        f"Negative {asset} balance ({_qty(current)})",
                        f"After {row.get('date', '')} the running quantity is below zero.",
                        row,
                    )
                )
    return found


def _deltas(row: Mapping[str, Any]) -> List[Tuple[str, Decimal]]:
    changes = []
    buy_asset = str(row.get("buy_asset", "") or "")
    sell_asset = str(row.get("sell_asset", "") or "")
    fee_asset = str(row.get("fee_asset", "") or "")
    if buy_asset:
        changes.append((buy_asset, _decimal(row.get("buy_quantity"))))
    if sell_asset:
        changes.append((sell_asset, -_decimal(row.get("sell_quantity"))))
    if fee_asset:
        changes.append((fee_asset, -_decimal(row.get("fee_quantity"))))
    return changes


def _duplicate_key(row: Mapping[str, Any]) -> Tuple[str, ...]:
    fields = (
        "type",
        "timestamp",
        "buy_quantity",
        "buy_asset",
        "sell_quantity",
        "sell_asset",
        "fee_quantity",
        "fee_asset",
        "wallet",
    )
    return tuple(str(row.get(name, "") or "") for name in fields)


def _first_match(
    rows: Sequence[Mapping[str, Any]],
    when: str,
    asset: str,
    types: Optional[Set[str]] = None,
) -> Optional[Mapping[str, Any]]:
    day = when[:10]
    for row in rows:
        if (
            day
            and str(row.get("date", "")) != day
            and not str(row.get("timestamp", "")).startswith(day)
        ):
            continue
        if types is not None and str(row.get("type", "")) not in types:
            continue
        assets = set(row.get("assets") or [])
        assets.update(
            str(row.get(name, "") or "") for name in ("buy_asset", "sell_asset", "fee_asset")
        )
        assets.discard("")
        if asset and asset not in assets:
            continue
        return row
    return None


def _issue(
    category: str, summary: str, detail: str, row: Optional[Mapping[str, Any]]
) -> Dict[str, str]:
    href = ""
    row_id = ""
    if row is not None:
        row_id = str(row.get("id", ""))
        href = _href(row_id)
    return {
        "category": category,
        "summary": summary,
        "detail": detail,
        "href": href,
        "row_id": row_id,
    }


def _href(row_id: str) -> str:
    if ":" not in row_id:
        return ""
    import_id, seq = row_id.split(":", 1)
    return f"/transactions/{import_id}/{seq}"


def _is_fiat(asset: str) -> bool:
    return asset in set(config.fiat_list)


def _decimal(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def _qty(value: Decimal) -> str:
    return f"{value.normalize():f}"
