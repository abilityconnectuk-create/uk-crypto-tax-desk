# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The home page uses the tax year that contains today, and cached prices only."""

from __future__ import annotations

import json
import threading
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.request import urlopen

from bittytax.web import DEFAULT_TAX_YEAR
from bittytax.web.dashboard import dashboard, tax_year_containing
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (ROOT / "examples" / "web-sample-koinly.csv").read_bytes()


def test_today_in_november_2026_is_the_year_ending_april_2027() -> None:
    assert tax_year_containing(date(2026, 11, 28)) == 2027
    assert tax_year_containing(date(2026, 4, 5)) == 2026
    assert tax_year_containing(date(2026, 4, 6)) == 2027
    assert DEFAULT_TAX_YEAR == 2026


def test_dashboard_uses_the_configured_allowance_and_a_cached_price(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes("web-sample-koinly.csv", SAMPLE)
    (desk.root / "price-cache.json").write_text(
        json.dumps({"BTC|2025-12-01": {"price": "40000", "source": "sample"}}) + "\n",
        encoding="utf-8",
    )
    board = dashboard(desk, date(2025, 12, 1))
    assert board["tax_year"] == 2026
    assert board["allowance"] == "3000"
    assert board["banner"] == "AWAITING TAX REVIEW"
    assert "does not carry losses forward" in board["losses_carried"]
    btc = _asset(board, "BTC")
    assert Decimal(btc["quantity"]) > 0
    assert (
        btc["value"] == f"{(Decimal(btc['quantity']) * Decimal('40000')).quantize(Decimal('0.01'))}"
    )
    assert btc["unrealised"] != ""
    eth = _asset(board, "ETH")
    assert eth["value"] == ""
    remaining = Decimal(board["allowance"]) - Decimal(board["allowance_used"])
    assert Decimal(board["allowance_remaining"]) == remaining


def test_home_page_names_the_year_that_contains_today(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes("web-sample-koinly.csv", SAMPLE)
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=60) as response:
            page = response.read().decode()
    finally:
        httpd.shutdown()
    assert "AWAITING TAX REVIEW" in page
    assert "2026/27" in page
    assert "does not carry losses forward" in page


def _asset(board: Dict[str, Any], name: str) -> Dict[str, str]:
    found = [row for row in board["holdings"] if row["asset"] == name]
    assert found
    return found[0]
