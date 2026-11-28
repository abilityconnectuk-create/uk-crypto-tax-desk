# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Analyst verdicts of 28 Nov 2026. Synthetic figures only."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from bittytax.bt_types import AssetSymbol, DisposalType, TaxRules, Timestamp, TrType, Year
from bittytax.constants import TZ_UTC
from bittytax.tax import CalculateCapitalGains
from bittytax.tax_event import TaxEventCapitalGains
from bittytax.transactions import Sell
from bittytax.web.allocation_desk import apply_refund, mark_negligible
from bittytax.web.defi_apply import tax_cells
from bittytax.web.errors import DeskError
from bittytax.web.futures import build_report, relieve_loss
from bittytax.web.income_schedule import build_schedule
from bittytax.web.losses import schedule
from bittytax.web.rules import confirmed_flags, load_rules, load_sa108
from bittytax.web.sa108 import box_13_6, estimates_flag, grouped_crypto_boxes, sa108_view


def test_one_disposal_per_token_per_day_nets_its_slices() -> None:
    figures = grouped_crypto_boxes(
        [
            {
                "asset": "BTC",
                "date_iso": "2025-06-01",
                "disposal_type": "Same Day",
                "gain": "100",
            },
            {
                "asset": "BTC",
                "date_iso": "2025-06-01",
                "disposal_type": "Section 104",
                "gain": "-40",
            },
            {
                "asset": "ETH",
                "date_iso": "2025-06-02",
                "disposal_type": "Section 104",
                "gain": "-15",
            },
            {
                "asset": "GIFT",
                "date_iso": "2025-06-01",
                "disposal_type": "No Gain/No Loss",
                "gain": "0",
            },
        ]
    )
    assert figures is not None
    assert figures["count"] == "3"
    assert figures["gains"] == "60.00"
    assert figures["losses"] == "15.00"


def test_box_13_6_and_box_53() -> None:
    assert box_13_6([]) == ""
    assert box_13_6(["NVC"]) == "NVC"
    assert box_13_6(["NVC", "OTH"]) == "MUL"
    assert estimates_flag([{"type": "Trade", "buy_asset": "BTC", "sell_asset": "ETH"}]) == "X"
    assert estimates_flag([{"type": "Trade", "buy_asset": "BTC", "sell_asset": "GBP"}]) == ""
    view = sa108_view(
        {"summary": {"disposals": "9", "total_gain": "1", "losses": "1"}, "disposals": []},
        2026,
        rows=[{"type": "Trade", "buy_asset": "BTC", "sell_asset": "ETH"}],
        claim_codes=["NVC"],
    )
    assert view is not None
    boxes = {item["box"]: item["value"] for item in view["boxes"]}
    assert boxes["13.6"] == "NVC"
    assert boxes["53"] == "X"


def test_in_year_losses_are_set_against_the_higher_rate_first() -> None:
    gains = CalculateCapitalGains(Year(2025), TaxRules.UK_INDIVIDUAL)
    gains.tax_summary(_event("2024-08-01", "1000"))
    gains.tax_summary(_event("2024-09-01", "-2000"))
    gains.tax_summary(_event("2024-11-01", "5000"))
    gains.tax_estimate_cgt()
    assert gains.cgt_estimate_split["taxable_gain"] == Decimal(0)
    assert gains.cgt_estimate["taxable_gain"] == Decimal(1000)


def test_an_unclaimed_loss_past_the_deadline_is_dropped(tmp_path: Path) -> None:
    class Desk:  # pylint: disable=too-few-public-methods
        def __init__(self) -> None:
            self.root = tmp_path
            self._rows = [{"timestamp": "2020-06-01"}]

        def all_rows(self) -> List[Dict[str, str]]:
            return self._rows

        def report(self, year: int) -> Dict[str, Any]:
            if year == 2021:
                return {"summary": {"total_gain": "0", "losses": "500"}}
            return {"summary": {"total_gain": "0", "losses": "0"}}

    rows = {row["year"]: row for row in schedule(Desk(), today=date(2026, 11, 28))}
    assert rows["2021"]["lost"] == "500.00"
    assert rows["2021"]["carried"] == "0.00"


def test_only_an_income_airdrop_is_listed_and_the_allowance_is_applied() -> None:
    rows = [
        {
            "type": "Airdrop",
            "date": "2025-07-03",
            "buy_asset": "SAMPLE",
            "buy_value": "25",
            "note": "income airdrop for a quest",
        },
        {
            "type": "Airdrop",
            "date": "2025-07-04",
            "buy_asset": "DUST",
            "buy_value": "5",
            "note": "passive holder drop",
        },
    ]
    shown = build_schedule(None, rows, 2026)
    assert [row["asset"] for row in shown["rows"]] == ["SAMPLE"]
    assert shown["exempt"] is True
    assert shown["reported"] is False
    assert shown["box_17"] == "0.00"
    empty = build_schedule(None, [], 2026)
    assert empty["exempt"] is False
    assert empty["box_17"] == "0.00"
    above = build_schedule(
        {
            "income": {
                "rows": [
                    {
                        "date": "2025-07-01",
                        "asset": "ETH",
                        "income_type": "Staking-Reward",
                        "amount": "2500",
                    }
                ]
            }
        },
        [],
        2026,
    )
    assert above["box_17"] == "2500.00"
    assert above["box_18"] == "1000.00"
    assert above["taxable"] == "1500.00"


def test_futures_losses_stay_with_miscellaneous_income() -> None:
    rows = [
        {"type": "Margin-Loss", "sell_value": "40", "note": "perp", "date": "2025-08-01"},
        {"type": "Margin-Fee", "sell_value": "5", "note": "trading fee", "date": "2025-08-01"},
        {
            "type": "Margin-Gain",
            "buy_value": "90",
            "note": "recognised exchange",
            "date": "2025-08-03",
        },
    ]
    report = build_report(rows, Decimal(100), True)
    assert report["box_17"] == "0.00"
    assert report["miscellaneous_loss"] == "45.00"
    assert report["other_property"] == "90.00"
    assert report["capital_gains"] == "100.00"
    assert report["comparison_gains"] == "60.00"
    relief = relieve_loss(Decimal("45"), Decimal("20"))
    assert relief["used"] == "20.00"
    assert relief["remaining"] == "25.00"
    assert relief["taxable"] == "0.00"


def test_a_refund_posts_the_shortfall_and_a_delivered_token_does_not(tmp_path: Path) -> None:
    short = apply_refund("80", "30", "no")
    assert short["loss_posted"] == "true"
    assert short["shortfall_loss"] == "50"
    kept = apply_refund("80", "30", "yes")
    assert kept["loss_posted"] == "false"
    assert kept["pending"] == "50"
    claim = mark_negligible(
        tmp_path,
        "SAMPLE",
        "sam",
        value="0",
        on_date="2026-06-01",
        pool_cost="10",
        earlier_date="2024-04-06",
        lost_key=True,
    )
    assert claim["lost_key"] == "true"
    try:
        mark_negligible(
            tmp_path,
            "SAMPLE",
            "sam",
            value="0",
            on_date="2026-06-01",
            pool_cost="10",
            earlier_date="2024-04-05",
        )
    except DeskError as exc:
        assert "two years" in str(exc)
    else:
        raise AssertionError("an early deemed date was accepted")


def test_pool_and_lender_tags_use_the_reviewed_types() -> None:
    common = {"timestamp": "2025-06-01 12:00:00", "wallet": "Sample"}
    pool = {
        **common,
        "tag": "lp_deposit",
        "buy_asset": "LP",
        "sell_asset": "ETH",
        "cells": ["Deposit", "1"],
    }
    assert tax_cells(pool)[0] == "Trade"
    lender = {
        **common,
        "tag": "lender_loan",
        "buy_asset": "",
        "sell_asset": "USDC",
        "cells": ["Spend", "1"],
    }
    assert tax_cells(lender)[0] == "Spend"
    liquidated = {
        **common,
        "tag": "liquidation",
        "wallet": "Morpho Blue",
        "buy_asset": "",
        "sell_asset": "ETH",
        "cells": ["Deposit", "1"],
    }
    assert tax_cells(liquidated)[0] == "Spend"
    rules = load_rules(2026)
    assert rules is not None
    assert "section 38" in rules["desk_review"]["fees"]["note"]
    assert rules["defi"]["tags"]
    stub = load_sa108(2027)
    assert stub is not None
    assert confirmed_flags(stub) and all(flag is False for flag in confirmed_flags(stub))
    rebasing = next(tag for tag in rules["defi"]["tags"] if tag["id"] == "rebasing")
    assert rebasing["effect"] == "accountant_review"
    assert rebasing["confirmed"] is False


def _event(day: str, proceeds: str) -> TaxEventCapitalGains:
    year, month, number = (int(part) for part in day.split("-"))
    sell = Sell(TrType.SPEND, Decimal(1), AssetSymbol("BTC"), Decimal(proceeds))
    sell.timestamp = Timestamp(datetime(year, month, number, tzinfo=TZ_UTC))
    return TaxEventCapitalGains(DisposalType.SECTION_104, None, sell, Decimal(0), Decimal(0))
