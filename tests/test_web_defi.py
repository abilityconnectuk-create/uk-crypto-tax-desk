# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""DeFi tags are notes. They stay unconfirmed and do not change the calculator type."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from bittytax.web.errors import DeskError
from bittytax.web.rules import accountant_review_items, confirmed_flags, load_rules
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"


def test_defi_rules_stay_unconfirmed_and_name_the_review_items() -> None:
    for year in (2025, 2026):
        rules = load_rules(year)
        assert rules is not None
        flags = confirmed_flags(rules["defi"])
        assert flags and all(flag is False for flag in flags)
        tags = {tag["id"]: tag for tag in rules["defi"]["tags"]}
        assert tags["own_wallet_transfer"]["effect"] == "not_disposal"
        assert tags["collateral_deposit"]["default"] == "accountant_review"
        assert tags["collateral_deposit"]["protocols"] == {}
    titles = [item["title"] for item in accountant_review_items(2026)]
    assert "Loan drawdown" in titles
    assert "Airdrop" in titles
    assert "Rebasing receipt token" in titles
    assert "Own-wallet transfer" not in titles


def test_a_tag_is_logged_without_changing_the_type(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    gift = next(row for row in desk.all_rows() if "sample gift" in row["note"])
    before = gift["type"]
    desk.reclassify(gift["id"], before, "Sample loan drawdown.", "sam", "loan_drawdown")
    updated = desk.transaction(gift["id"])
    assert updated["type"] == before
    assert updated["tag"] == "loan_drawdown"
    assert "repayment obligation" in updated["tag_note"]
    with pytest.raises(DeskError):
        desk.reclassify(gift["id"], before, "No new tag.", "sam", "")


def test_health_page_lists_defi_review_items(tmp_path: Path) -> None:
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/health?tax_year=2026", timeout=30) as response:
            page = response.read().decode()
    finally:
        httpd.shutdown()
    assert "Loan drawdown" in page
    assert "AWAITING TAX REVIEW" in page
