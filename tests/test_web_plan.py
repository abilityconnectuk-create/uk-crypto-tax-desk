# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""A what-if sale is calculated on a copy and is not written to the ledger."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from bittytax.web.planning import hypothetical, plan_view
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (ROOT / "examples" / "web-sample-koinly.csv").read_bytes()


def test_hypothetical_sale_is_not_saved(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes("web-sample-koinly.csv", SAMPLE)
    before = len(desk.all_rows())
    result = hypothetical(desk, "btc", "0.1", "2025-12-01", "4000")
    assert result["asset"] == "BTC"
    assert "not advice" in result["disclaimer"]
    assert result["difference"] != ""
    assert len(desk.all_rows()) == before
    assert "what-if" not in desk.records_path.read_text(encoding="utf-8")
    board = plan_view(desk, date(2025, 12, 1))
    assert board["board"]["tax_year"] == 2026
    assert "not advice" in board["disclaimer"]
