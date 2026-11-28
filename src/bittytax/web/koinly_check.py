# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Compare the desk with a Koinly capital-gains summary and complete tax report."""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

_PENCE = Decimal("0.01")
_TOTALS = {"total", "totals", "grand total", "subtotal", "sum"}
_FIAT = {"GBP", "EUR", "USD", "AUD", "NZD", "CAD", "PLN", "DKK", "NOK", "SEK", "CHF"}


def compare_year(
    report: Mapping[str, Any],
    summary_csv: str,
    complete_csv: str,
) -> Dict[str, Any]:
    """Desk totals beside Koinly totals. A difference of at least one penny is highlighted."""
    summary = parse_summary(summary_csv) if summary_csv.strip() else {}
    complete_totals, complete_rows = (
        parse_complete(complete_csv) if complete_csv.strip() else ({}, [])
    )
    koinly = summary or complete_totals
    desk = _desk_assets(report)
    assets = sorted(set(desk) | set(koinly))
    rows = [_asset_row(asset, desk.get(asset), koinly.get(asset)) for asset in assets]
    return {
        "rows": rows,
        "desk": _sum_side(desk),
        "koinly": _sum_side(koinly),
        "source": "summary" if summary else "complete",
        "complete_rows": complete_rows,
        "has_summary": bool(summary),
        "has_complete": bool(complete_rows) or bool(complete_totals),
    }


def parse_summary(text: str) -> Dict[str, Dict[str, Decimal]]:
    """Per-asset totals from a Koinly capital-gains summary CSV."""
    totals: Dict[str, Dict[str, Decimal]] = {}
    for raw in _dicts(text):
        asset = _cell(raw, "asset", "currency", "currency name")
        if not asset or asset.casefold() in _TOTALS:
            continue
        _add(
            totals,
            asset,
            _money(raw, "cost basis", "cost"),
            _money(raw, "proceeds"),
            _money(raw, "gain"),
        )
    return totals


def parse_complete(text: str) -> Tuple[Dict[str, Dict[str, Decimal]], List[Dict[str, str]]]:
    """Per-asset totals and the disposal lines from a Koinly complete tax report."""
    totals: Dict[str, Dict[str, Decimal]] = {}
    rows: List[Dict[str, str]] = []
    for raw in _dicts(text):
        gain_text = _text(raw, "gain")
        if gain_text == "":
            continue
        asset = _disposed_asset(raw)
        if not asset:
            continue
        cost = _money(raw, "sent cost basis", "cost basis", "cost")
        proceeds = _money(raw, "net value", "proceeds")
        gain = _decimal(gain_text)
        _add(totals, asset, cost, proceeds, gain)
        rows.append(
            {
                "date": _text(raw, "date"),
                "asset": asset,
                "type": _text(raw, "type"),
                "amount": _text(raw, "sent amount", "amount"),
                "cost": f"{cost}",
                "proceeds": f"{proceeds}",
                "gain": f"{gain}",
                "description": _text(raw, "description"),
            }
        )
    return totals, rows


def store_uploads(root: Path, tax_year: int, summary: bytes, complete: bytes) -> Path:
    """Keep the uploads in the data directory. Callers must not commit them with the source."""
    folder = root / "koinly" / str(tax_year)
    folder.mkdir(parents=True, exist_ok=True)
    if summary:
        (folder / "summary.csv").write_bytes(summary)
    if complete:
        (folder / "complete.csv").write_bytes(complete)
    return folder


def load_uploads(root: Path, tax_year: int) -> Tuple[str, str]:
    folder = root / "koinly" / str(tax_year)
    return _read(folder / "summary.csv"), _read(folder / "complete.csv")


def _desk_assets(report: Mapping[str, Any]) -> Dict[str, Dict[str, Decimal]]:
    totals: Dict[str, Dict[str, Decimal]] = {}
    for row in report.get("disposals", []):
        cost = _decimal(row.get("cost")) + _decimal(row.get("fees"))
        _add(
            totals,
            str(row.get("asset", "")),
            cost,
            _decimal(row.get("proceeds")),
            _decimal(row.get("gain")),
        )
    return totals


def _asset_row(
    asset: str,
    desk: Optional[Mapping[str, Decimal]],
    koinly: Optional[Mapping[str, Decimal]],
) -> Dict[str, Any]:
    desk_side = desk or {"cost": Decimal(0), "proceeds": Decimal(0), "gain": Decimal(0)}
    koinly_side = koinly or {"cost": Decimal(0), "proceeds": Decimal(0), "gain": Decimal(0)}
    diffs = {name: koinly_side[name] - desk_side[name] for name in ("cost", "proceeds", "gain")}
    return {
        "asset": asset,
        "desk_cost": f"{desk_side['cost']}",
        "desk_proceeds": f"{desk_side['proceeds']}",
        "desk_gain": f"{desk_side['gain']}",
        "koinly_cost": f"{koinly_side['cost']}",
        "koinly_proceeds": f"{koinly_side['proceeds']}",
        "koinly_gain": f"{koinly_side['gain']}",
        "diff_cost": f"{diffs['cost']}",
        "diff_proceeds": f"{diffs['proceeds']}",
        "diff_gain": f"{diffs['gain']}",
        "different": any(abs(value) >= _PENCE for value in diffs.values()),
    }


def _sum_side(assets: Mapping[str, Mapping[str, Decimal]]) -> Dict[str, str]:
    totals = {"cost": Decimal(0), "proceeds": Decimal(0), "gain": Decimal(0)}
    for row in assets.values():
        for name in totals:
            totals[name] += row[name]
    return {name: f"{value}" for name, value in totals.items()}


def _add(
    totals: Dict[str, Dict[str, Decimal]],
    asset: str,
    cost: Decimal,
    proceeds: Decimal,
    gain: Decimal,
) -> None:
    bucket = totals.setdefault(
        asset, {"cost": Decimal(0), "proceeds": Decimal(0), "gain": Decimal(0)}
    )
    bucket["cost"] += cost
    bucket["proceeds"] += proceeds
    bucket["gain"] += gain


def _disposed_asset(raw: Mapping[str, str]) -> str:
    sent = _text(raw, "sent currency")
    received = _text(raw, "received currency")
    if sent and sent not in _FIAT:
        return sent
    if received and received not in _FIAT:
        return received
    return sent or received


def _dicts(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return []
    rows = []
    for raw in reader:
        packed = {}
        for key, value in raw.items():
            if key is None:
                continue
            packed[_canon(key)] = "" if value is None else value.strip()
        rows.append(packed)
    return rows


def _canon(name: str) -> str:
    text = name.strip().casefold()
    text = re.sub(r"\s*\([a-z]{3}\)", "", text)
    return " ".join(text.split())


def _cell(raw: Mapping[str, str], *names: str) -> str:
    for name in names:
        if raw.get(name):
            return raw[name]
    return ""


def _text(raw: Mapping[str, str], *names: str) -> str:
    return _cell(raw, *names)


def _money(raw: Mapping[str, str], *names: str) -> Decimal:
    for name in names:
        if name in raw and raw[name] != "":
            return _decimal(raw[name])
    return Decimal(0)


def _decimal(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", "").replace("£", "")
    if not text or text in {"-", "—"}:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig")
