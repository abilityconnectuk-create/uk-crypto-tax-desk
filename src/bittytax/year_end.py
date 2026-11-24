# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

import csv
import datetime
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

import dateutil.tz

from .config import config
from .constants import TZ_UTC

LONDON = dateutil.tz.gettz("Europe/London")


@dataclass
class PoolSnap:
    asset: str
    quantity: Decimal
    cost: Decimal
    fees: Decimal


@dataclass
class TimezoneFlag:
    timestamp_utc: str
    timestamp_london: str
    utc_tax_year: int
    london_tax_year: int
    asset: str
    quantity: str
    wallet: str


@dataclass
class YearEndTracker:
    """Section 104 balances at the end of each tax year, and boundary warnings."""

    currency: str
    snapshots: Dict[int, Dict[str, PoolSnap]] = field(default_factory=dict)
    flags: List[TimezoneFlag] = field(default_factory=list)
    _open_year: Optional[int] = None

    def observe(self, transaction: Any, holdings: Mapping[Any, Any]) -> None:
        """Record a timezone warning, and close a tax year before a later transaction."""
        self._flag(transaction)
        year = tax_year_of(_as_london(transaction.timestamp).date())
        if self._open_year is None:
            self._open_year = year
            return
        if year <= self._open_year:
            return
        for closed in range(self._open_year, year):
            self.snapshots[closed] = _copy_holdings(holdings)
        self._open_year = year

    def close(self, holdings: Mapping[Any, Any]) -> None:
        """Snapshot the tax year that is still open after the last transaction."""
        if self._open_year is None or self._open_year in self.snapshots:
            return
        self.snapshots[self._open_year] = _copy_holdings(holdings)

    def _flag(self, transaction: Any) -> None:
        timestamp = transaction.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=TZ_UTC)
        utc_date = timestamp.astimezone(TZ_UTC).date()
        london_date = timestamp.astimezone(LONDON).date()
        utc_year = tax_year_of(utc_date)
        london_year = tax_year_of(london_date)
        if utc_year == london_year:
            return
        quantity = getattr(transaction, "quantity", Decimal(0))
        wallet = str(getattr(transaction, "wallet", ""))
        self.flags.append(
            TimezoneFlag(
                timestamp_utc=f"{timestamp.astimezone(TZ_UTC):%Y-%m-%d %H:%M:%S} UTC",
                timestamp_london=f"{timestamp.astimezone(LONDON):%Y-%m-%d %H:%M:%S %Z}",
                utc_tax_year=utc_year,
                london_tax_year=london_year,
                asset=str(getattr(transaction, "asset", "")),
                quantity=f"{quantity:f}",
                wallet=wallet,
            )
        )


def tax_year_of(day: datetime.date) -> int:
    """UK tax year number. 2024-06-01 belongs to the year ending 5 April 2025."""
    if day > config.get_tax_year_end(day.year):
        return day.year + 1
    return day.year


def tax_year_label(year: int) -> str:
    """Label for the year ending 5 April. 2025 is 2024/25."""
    return f"{year - 1}/{year % 100:02d}"


def _as_london(timestamp: datetime.datetime) -> datetime.datetime:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=TZ_UTC)
    if LONDON is None:
        raise RuntimeError("Europe/London timezone is not available")
    return timestamp.astimezone(LONDON)


def _copy_holdings(holdings: Mapping[Any, Any]) -> Dict[str, PoolSnap]:
    snapped: Dict[str, PoolSnap] = {}
    for asset, holding in holdings.items():
        name = str(asset)
        snapped[name] = PoolSnap(
            asset=name,
            quantity=Decimal(holding.quantity),
            cost=Decimal(holding.cost),
            fees=Decimal(holding.fees),
        )
    return snapped


def _dec(value: Decimal) -> str:
    return f"{value:f}"


def build_snapshot_payload(
    tracker: YearEndTracker,
    year_totals: Mapping[int, Mapping[str, Decimal]],
    missing_rows: Sequence[Mapping[str, str]],
    review_rows: str,
) -> Dict[str, Any]:
    pools: Dict[str, Any] = {}
    for year in sorted(tracker.snapshots):
        assets = []
        for name in sorted(tracker.snapshots[year]):
            pool = tracker.snapshots[year][name]
            assets.append(
                {
                    "asset": pool.asset,
                    "quantity": _dec(pool.quantity),
                    "cost": _dec(pool.cost),
                    "fees": _dec(pool.fees),
                }
            )
        pools[str(year)] = {
            "as_of": config.get_tax_year_end(year).isoformat(),
            "assets": assets,
        }

    years: Dict[str, Any] = {}
    for year, totals in sorted(year_totals.items()):
        years[str(year)] = {key: _dec(value) for key, value in totals.items()}

    return {
        "currency": tracker.currency,
        "timezone": "Europe/London",
        "review_rows": review_rows,
        "pools": pools,
        "years": years,
        "missing_cost_rows": list(missing_rows),
        "timezone_flags": [
            {
                "timestamp_utc": flag.timestamp_utc,
                "timestamp_london": flag.timestamp_london,
                "utc_tax_year": flag.utc_tax_year,
                "london_tax_year": flag.london_tax_year,
                "asset": flag.asset,
                "quantity": flag.quantity,
                "wallet": flag.wallet,
            }
            for flag in tracker.flags
        ],
    }


def write_snapshot(path: str, payload: Mapping[str, Any]) -> str:
    """Write the JSON snapshot and a CSV of pool rows. Returns the JSON path."""
    json_path = path
    if path.endswith(os.sep) or os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        json_path = os.path.join(path, "section104-snapshot.json")
    folder = os.path.dirname(json_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    stem, _ext = os.path.splitext(json_path)
    _write_pool_csv(f"{stem}-pools.csv", payload)
    _write_flag_csv(f"{stem}-timezone-flags.csv", payload)
    return json_path


def _write_pool_csv(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["Tax Year", "As Of", "Asset", "Quantity", "Pooled Cost", "Fees", "Currency"]
        )
        currency = str(payload.get("currency", ""))
        pools = payload.get("pools", {})
        for year in sorted(pools):
            block = pools[year]
            for asset in block.get("assets", []):
                writer.writerow(
                    [
                        year,
                        block.get("as_of", ""),
                        asset.get("asset", ""),
                        asset.get("quantity", ""),
                        asset.get("cost", ""),
                        asset.get("fees", ""),
                        currency,
                    ]
                )


def _write_flag_csv(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "Timestamp UTC",
                "Timestamp London",
                "UTC Tax Year",
                "London Tax Year",
                "Asset",
                "Quantity",
                "Wallet",
            ]
        )
        for flag in payload.get("timezone_flags", []):
            writer.writerow(
                [
                    flag.get("timestamp_utc", ""),
                    flag.get("timestamp_london", ""),
                    flag.get("utc_tax_year", ""),
                    flag.get("london_tax_year", ""),
                    flag.get("asset", ""),
                    flag.get("quantity", ""),
                    flag.get("wallet", ""),
                ]
            )
