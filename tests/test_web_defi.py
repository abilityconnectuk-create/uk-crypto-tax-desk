# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""DeFi tags follow the reviewed rule file. The receipt-token case stays with the accountant."""

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


def test_defi_rules_confirm_the_reviewed_tags_and_leave_rebasing() -> None:
    for year in (2025, 2026):
        rules = load_rules(year)
        assert rules is not None
        tags = {tag["id"]: tag for tag in rules["defi"]["tags"]}
        assert tags["own_wallet_transfer"]["effect"] == "not_disposal"
        assert tags["own_wallet_transfer"]["confirmed"] is True
        assert tags["collateral_deposit"]["protocols"]["Morpho Blue"] == "not_disposal"
        assert tags["rebasing"]["confirmed"] is False
        assert tags["rebasing"]["accountant_review"] is True
        assert tags["lender_loan"]["effect"] == "disposal_for_a_right"
    stub = load_rules(2027)
    assert stub is not None
    assert confirmed_flags(stub) and all(flag is False for flag in confirmed_flags(stub))
    titles = [item["title"] for item in accountant_review_items(2026)]
    assert titles == ["Rebasing receipt token"]
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
    assert "Rebasing receipt token" in page
    assert "AWAITING TAX REVIEW" in page
