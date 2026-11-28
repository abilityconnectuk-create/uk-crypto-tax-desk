# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""DeFi tags reach the calculator only when the rule file says confirmed."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from bittytax.web.defi_apply import pending_preview, tax_cells
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
GENERIC = (
    "Type,Buy Quantity,Buy Asset,Buy Value in GBP,Sell Quantity,Sell Asset,"
    "Sell Value in GBP,Fee Quantity,Fee Asset,Fee Value in GBP,Wallet,Timestamp,Note,Raw Data\n"
    "Deposit,1,ETH,100,,,,,,,Sample,2025-06-01 12:00:00,staking sample,\n"
)


def test_an_unconfirmed_tag_does_not_change_the_type() -> None:
    row = {
        "tag": "staking_reward",
        "wallet": "Sample",
        "timestamp": "2025-06-01 12:00:00",
        "buy_asset": "ETH",
        "sell_asset": "",
        "cells": ["Deposit", "1", "ETH"],
    }
    assert tax_cells(row)[0] == "Deposit"
    collateral = {
        "tag": "collateral_deposit",
        "wallet": "Sample",
        "timestamp": "2025-06-01 12:00:00",
        "buy_asset": "",
        "sell_asset": "ETH",
        "cells": ["Spend", "1"],
    }
    assert tax_cells(collateral)[0] == "Spend"


def test_confirmed_false_leaves_the_report_and_a_true_copy_does_not(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "desk")
    desk.import_bytes("generic.csv", GENERIC.encode("utf-8"))
    row = desk.all_rows()[0]
    desk.reclassify_many(
        [{"row_id": row["id"], "type": "Deposit", "tag": "staking_reward", "why": "sample tag"}],
        "sam",
    )
    before = desk.report(2026)
    assert desk.all_rows()[0]["type"] == "Deposit"
    assert str(before["income"]["amount"]) in {"0.00", "0", ""}
    previews = pending_preview(desk, 2026)
    staking = next(item for item in previews if item["tag"] == "staking_reward")
    assert staking["banner"] == "AWAITING TAX REVIEW"
    assert staking["changed"] == "yes"
    rules = tmp_path / "rules"
    shutil.copytree(ROOT / "config", rules)
    target = rules / "rules" / "2025-26.yaml"
    text = target.read_text(encoding="utf-8")
    old = (
        "    - id: staking_reward\n"
        "      label: Staking reward\n"
        "      effect: miscellaneous_income\n"
        "      confirmed: false\n"
    )
    new = old.replace("confirmed: false", "confirmed: true", 1)
    assert old in text
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    previous = os.environ.get("UK_CRYPTO_TAX_DESK_RULES")
    os.environ["UK_CRYPTO_TAX_DESK_RULES"] = str(rules)
    try:
        after = desk.report(2026)
    finally:
        if previous is None:
            os.environ.pop("UK_CRYPTO_TAX_DESK_RULES", None)
        else:
            os.environ["UK_CRYPTO_TAX_DESK_RULES"] = previous
    assert desk.all_rows()[0]["type"] == "Deposit"
    assert str(after["income"]["amount"]) != str(before["income"]["amount"])
