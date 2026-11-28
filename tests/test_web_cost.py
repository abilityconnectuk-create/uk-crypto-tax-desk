# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The cost-basis page traces units and does not rewrite the ledger."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from bittytax.web.cost_basis import trace_rows
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "examples" / "web-sample-review-koinly.csv"


def test_a_sale_with_no_deposit_is_a_missing_source() -> None:
    rows = [
        {
            "id": "a:1",
            "timestamp": "2025-06-04 12:00:00",
            "wallet": "Example Exchange",
            "type": "Trade",
            "sell_asset": "ETH",
            "sell_quantity": "1",
            "buy_asset": "GBP",
            "buy_quantity": "3000",
        }
    ]
    traced = trace_rows(rows)
    eth = next(item for item in traced if item["asset"] == "ETH")
    assert eth["gaps"][0]["kind"] == "missing deposit source"
    assert eth["gaps"][0]["href"] == "/import/add"


def test_an_unmatched_send_into_a_short_wallet_points_at_matches() -> None:
    rows = [
        {
            "id": "a:1",
            "timestamp": "2025-06-03 12:00:00",
            "wallet": "Wallet Alpha",
            "type": "Withdrawal",
            "sell_asset": "ETH",
            "sell_quantity": "1",
        },
        {
            "id": "a:2",
            "timestamp": "2025-06-03 13:00:00",
            "wallet": "Wallet Beta",
            "type": "Trade",
            "sell_asset": "ETH",
            "sell_quantity": "1",
            "buy_asset": "GBP",
            "buy_quantity": "3000",
        },
    ]
    traced = trace_rows(rows)
    eth = next(item for item in traced if item["asset"] == "ETH")
    kinds = [gap["kind"] for gap in eth["gaps"]]
    assert "unmatched transfer" in kinds
    assert all(gap["href"] in {"/matches", "/import/add"} for gap in eth["gaps"])


def test_the_page_does_not_rewrite_rows(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(REVIEW.name, REVIEW.read_bytes())
    before = [(row["id"], row["type"]) for row in desk.all_rows()]
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        with urlopen(f"http://127.0.0.1:{port}/cost") as response:
            page = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert "AWAITING TAX REVIEW" in page
    assert "missing deposit source" in page
    assert "Nothing on this page changes the calculator." in page
    after = [(row["id"], row["type"]) for row in desk.all_rows()]
    assert after == before
