# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Trace where units came from. This does not change a tax figure."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence


def trace_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """A balance timeline per asset, plus a likely gap when a disposal is short."""
    events: Dict[str, List[Dict[str, str]]] = {}
    balance: Dict[str, Dict[str, Decimal]] = {}
    gaps: List[Dict[str, str]] = []
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or ""))
    for row in ordered:
        _apply(row, events, balance, gaps)
    assets = []
    for asset in sorted(events):
        assets.append({"asset": asset, "events": events[asset], "gaps": _gaps_for(gaps, asset)})
    return assets


def _apply(
    row: Mapping[str, Any],
    events: Dict[str, List[Dict[str, str]]],
    balance: Dict[str, Dict[str, Decimal]],
    gaps: List[Dict[str, str]],
) -> None:
    wallet = str(row.get("wallet") or "unknown wallet")
    when = str(row.get("timestamp") or "")
    kind = str(row.get("type") or "")
    row_id = str(row.get("id") or "")
    common = {"wallet": wallet, "when": when, "kind": kind, "row_id": row_id}
    if row.get("buy_asset") and row.get("buy_quantity"):
        _move(
            events,
            balance,
            None,
            _leg(common, str(row["buy_asset"]), _decimal(row["buy_quantity"]), "in"),
        )
    if row.get("sell_asset") and row.get("sell_quantity"):
        _move(
            events,
            balance,
            gaps,
            _leg(common, str(row["sell_asset"]), -_decimal(row["sell_quantity"]), "out"),
        )


def _leg(common: Mapping[str, str], asset: str, signed: Decimal, direction: str) -> Dict[str, Any]:
    return {**dict(common), "asset": asset, "signed": signed, "direction": direction}


def _move(
    events: Dict[str, List[Dict[str, str]]],
    balance: Dict[str, Dict[str, Decimal]],
    gaps: Optional[List[Dict[str, str]]],
    step: Mapping[str, Any],
) -> None:
    asset = str(step["asset"])
    wallet = str(step["wallet"])
    signed = step["signed"]
    wallet_balance = balance.setdefault(asset, {})
    current = wallet_balance.get(wallet, Decimal(0)) + signed
    wallet_balance[wallet] = current
    events.setdefault(asset, []).append(
        {
            "when": str(step["when"]),
            "wallet": wallet,
            "kind": str(step["kind"]),
            "direction": str(step["direction"]),
            "quantity": f"{abs(signed)}",
            "balance": f"{current}",
            "row_id": str(step["row_id"]),
        }
    )
    if gaps is not None and current < 0:
        gaps.append(_gap(asset, wallet, str(step["when"]), events[asset]))


def _gap(
    asset: str, wallet: str, when: str, history: Sequence[Mapping[str, str]]
) -> Dict[str, str]:
    incoming = [item for item in history if item["direction"] == "in" and item["wallet"] == wallet]
    outgoing_elsewhere = [
        item for item in history if item["direction"] == "out" and item["wallet"] != wallet
    ]
    if not incoming and outgoing_elsewhere:
        return {
            "asset": asset,
            "when": when,
            "wallet": wallet,
            "kind": "unmatched transfer",
            "detail": "Another wallet sent this asset and this wallet has no matching deposit.",
            "href": "/matches",
        }
    if not incoming:
        return {
            "asset": asset,
            "when": when,
            "wallet": wallet,
            "kind": "missing deposit source",
            "detail": "The units leave a wallet that has no earlier deposit in this ledger.",
            "href": "/import/add",
        }
    return {
        "asset": asset,
        "when": when,
        "wallet": wallet,
        "kind": "missing import for a wallet",
        "detail": "The running balance went below zero. A wallet file may be missing.",
        "href": "/import/add",
    }


def _gaps_for(gaps: Sequence[Mapping[str, str]], asset: str) -> List[Dict[str, str]]:
    return [dict(item) for item in gaps if item["asset"] == asset]


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
