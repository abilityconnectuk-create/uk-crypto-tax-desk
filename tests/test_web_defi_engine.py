# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""A confirmed DeFi tag can change the calculator type. Rebasing cannot."""

from __future__ import annotations

from pathlib import Path

from bittytax.web.defi_apply import pending_preview, tax_cells
from bittytax.web.store import Desk

GENERIC = (
    "Type,Buy Quantity,Buy Asset,Buy Value in GBP,Sell Quantity,Sell Asset,"
    "Sell Value in GBP,Fee Quantity,Fee Asset,Fee Value in GBP,Wallet,Timestamp,Note,Raw Data\n"
    "Deposit,1,ETH,100,,,,,,,Sample,2025-06-01 12:00:00,staking sample,\n"
)


def test_a_confirmed_staking_tag_is_income_and_rebasing_is_not() -> None:
    row = {
        "tag": "staking_reward",
        "wallet": "Sample",
        "timestamp": "2025-06-01 12:00:00",
        "buy_asset": "ETH",
        "sell_asset": "",
        "cells": ["Deposit", "1", "ETH"],
    }
    assert tax_cells(row)[0] == "Staking-Reward"
    rebase = {
        "tag": "rebasing",
        "wallet": "Sample",
        "timestamp": "2025-06-01 12:00:00",
        "buy_asset": "ETH",
        "sell_asset": "",
        "cells": ["Deposit", "1", "ETH"],
    }
    assert tax_cells(rebase)[0] == "Deposit"
    morpho = {
        "tag": "collateral_deposit",
        "wallet": "Morpho Blue",
        "timestamp": "2025-06-01 12:00:00",
        "buy_asset": "",
        "sell_asset": "ETH",
        "cells": ["Spend", "1"],
    }
    assert tax_cells(morpho)[0] == "Withdrawal"


def test_a_confirmed_staking_tag_reaches_the_income_total(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "desk")
    desk.import_bytes("generic.csv", GENERIC.encode("utf-8"))
    row = desk.all_rows()[0]
    desk.reclassify_many(
        [{"row_id": row["id"], "type": "Deposit", "tag": "staking_reward", "why": "sample tag"}],
        "sam",
    )
    report = desk.report(2026)
    assert desk.all_rows()[0]["type"] == "Deposit"
    assert str(report["income"]["amount"]) == "100.00"
    previews = pending_preview(desk, 2026)
    assert all(item["tag"] != "staking_reward" for item in previews)
