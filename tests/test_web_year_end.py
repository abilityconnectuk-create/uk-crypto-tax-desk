# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The year-end card is information only, and only from 1 February to 5 April."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from bittytax.web.present import build_home
from bittytax.web.store import Desk
from bittytax.web.year_end import dismiss_year_end, in_year_end_window, year_end_card


def test_the_window_is_february_to_5_april() -> None:
    assert in_year_end_window(date(2026, 2, 1))
    assert in_year_end_window(date(2026, 4, 5))
    assert not in_year_end_window(date(2026, 1, 31))
    assert not in_year_end_window(date(2026, 4, 6))
    assert not in_year_end_window(date(2026, 11, 28))


def test_the_card_is_dismissible_and_does_not_suggest_a_trade(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    today = date(2026, 2, 2)
    card = year_end_card(desk, today)
    assert card is not None
    assert card["note"] == "This is not advice and it does not suggest a trade."
    assert card["banner"] == "AWAITING TAX REVIEW"
    assert "allowance_remaining" in card
    assert card["losses_available"]
    view = build_home(desk, 0, "all", "", "value", "desc", today)
    assert view["year_end"]["note"] == card["note"]
    dismiss_year_end(desk.root, today)
    assert year_end_card(desk, today) is None
    assert build_home(desk, 0, "all", "", "value", "desc", today)["year_end"] is None
