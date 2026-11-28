# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Missing GBP prices are filled from a local cache. Tests never call a price service."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bittytax.web.prices import fetch_missing, lookup_gbp, missing_slots
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
HEALTH = (ROOT / "examples" / "web-sample-health-koinly.csv").read_bytes()


def _raw_digest(desk: Desk) -> str:
    digest = hashlib.sha256()
    for path in sorted((desk.root / "raw").rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _lookup(calls: List[str]) -> Callable[[str, str], Optional[Dict[str, str]]]:
    def lookup(asset: str, day: str) -> Optional[Dict[str, str]]:
        calls.append(f"{asset}|{day}")
        if asset == "DOGE":
            return {"price": "5", "source": "sample"}
        return None

    return lookup


def test_fetch_fills_a_gap_and_leaves_the_upload_alone(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes("web-sample-health-koinly.csv", HEALTH)
    before = _raw_digest(desk)
    calls: List[str] = []
    result = fetch_missing(desk, _lookup(calls))
    doge = [
        row for row in desk.all_rows() if "DOGE" in row["assets"] and row["buy_asset"] == "DOGE"
    ]
    assert doge
    assert all(row["buy_value"] == "5.00" for row in doge)
    assert _raw_digest(desk) == before
    assert any(item["asset"] == "PEPE" for item in result["missed"])
    assert calls.count("DOGE|2025-05-01") == 1
    again: List[str] = []
    fetch_missing(desk, _lookup(again))
    assert "DOGE|2025-05-01" not in again
    assert "PEPE|2025-05-02" in again


def test_lookup_reads_the_price_library_without_the_network(monkeypatch: Any) -> None:
    class Record:  # pylint: disable=too-few-public-methods
        price_ccy = Decimal("123.45")
        data_source = "sample-source"

    class Pricer:  # pylint: disable=too-few-public-methods
        def __init__(self, price_tool: bool = True) -> None:
            self.price_tool = price_tool

        def get_historical_price(self, asset: object, timestamp: object) -> Record:
            del asset, timestamp
            return Record()

    monkeypatch.setattr("bittytax.web.prices.ValueAsset", Pricer)
    found = lookup_gbp("DOGE", "2025-05-02")
    assert found is not None
    assert found["price"] == "123.45"
    assert found["source"] == "sample-source"
    assert not missing_slots([])
