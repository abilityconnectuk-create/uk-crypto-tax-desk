# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Data-health checks use invented rows only."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.request import urlopen

from bittytax.web.health import analyse
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SAMPLE = ROOT / "examples" / "web-sample-review-koinly.csv"
HEALTH_SAMPLE = ROOT / "examples" / "web-sample-health-koinly.csv"
WALLETS = ROOT / "examples" / "web-sample-wallets.yaml"


def test_analyse_flags_duplicates_missing_prices_and_negative_balances() -> None:
    rows = [
        _row("a:0000", "Deposit", "2025-05-01T12:00:00 UTC", buy=("1", "DOGE", "")),
        _row("a:0001", "Deposit", "2025-05-01T12:00:00 UTC", buy=("1", "DOGE", "")),
        _row("a:0002", "Deposit", "2025-05-02T12:00:00 UTC", buy=("2", "PEPE", "")),
        _row(
            "a:0003",
            "Trade",
            "2025-05-03T12:00:00 UTC",
            buy=("10", "GBP", "10"),
            sell=("5", "DOGE", "10"),
        ),
    ]
    report = analyse(rows, {"queue": []}, [])
    categories = {issue["category"] for issue in report["issues"]}
    assert "duplicate" in categories
    assert "missing-price" in categories
    assert "negative-balance" in categories
    linked = [issue for issue in report["issues"] if issue["category"] == "duplicate"]
    assert linked[0]["href"].startswith("/transactions/")


def test_fiat_without_a_price_is_not_a_missing_price() -> None:
    rows = [_row("a:0000", "Deposit", "2025-05-01T12:00:00 UTC", buy=("20", "GBP", ""))]
    report = analyse(rows, {"queue": []}, [])
    assert not report["issues"]


def test_imported_review_file_links_missing_cost_and_unmatched_transfers(tmp_path: Path) -> None:
    shutil.copy(WALLETS, tmp_path / "wallets.yaml")
    desk = Desk(tmp_path)
    desk.import_bytes(REVIEW_SAMPLE.name, REVIEW_SAMPLE.read_bytes())
    health = desk.health(2026)
    assert any(issue["category"] == "missing-cost" for issue in health["issues"])
    unmatched = [issue for issue in health["issues"] if issue["category"] == "unmatched-transfer"]
    assert unmatched
    assert unmatched[0]["href"].startswith("/transactions/")
    assert desk.health_count(2026) == health["total"]
    assert desk.health_count(2026) > 0


def test_health_page_shows_the_badge(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(HEALTH_SAMPLE.name, HEALTH_SAMPLE.read_bytes())
    assert desk.health(2026)["total"] > 0
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/health?tax_year=2026", timeout=60) as response:
            page = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "missing-price" in page
    assert 'class="badge"' in page


def _row(
    row_id: str,
    kind: str,
    timestamp: str,
    buy: Tuple[str, str, str] = ("", "", ""),
    sell: Tuple[str, str, str] = ("", "", ""),
) -> Dict[str, Any]:
    assets = [asset for asset in (buy[1], sell[1]) if asset]
    return {
        "id": row_id,
        "type": kind,
        "timestamp": timestamp,
        "date": timestamp[:10],
        "wallet": "Sample Wallet",
        "buy_quantity": buy[0],
        "buy_asset": buy[1],
        "buy_value": buy[2],
        "sell_quantity": sell[0],
        "sell_asset": sell[1],
        "sell_value": sell[2],
        "fee_quantity": "",
        "fee_asset": "",
        "fee_value": "",
        "assets": assets,
        "note": "",
    }
