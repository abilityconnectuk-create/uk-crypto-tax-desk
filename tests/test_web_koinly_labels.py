# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The editable Koinly table sets types and tags. Unknown labels stay in review."""

from __future__ import annotations

from pathlib import Path

from bittytax.web.koinly_labels import unknown_labels
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "examples" / "web-sample-koinly-full.csv"


def test_listed_labels_map_and_an_unknown_label_is_kept_for_review(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(FULL.name, FULL.read_bytes())
    rows = {row["note"]: row for row in desk.all_rows()}
    assert rows["made up staking reward"]["type"] == "Staking-Reward"
    assert rows["made up staking reward"]["tag"] == "staking_reward"
    assert rows["made up airdrop"]["type"] == "Airdrop"
    assert rows["made up airdrop"]["tag"] == "airdrop"
    assert rows["made up lending interest"]["type"] == "Interest"
    assert rows["made up cost"]["type"] == "Spend"
    assert rows["made up gift"]["type"] == "Gift-Sent"
    assert rows["made up lost coin"]["type"] == "Lost"
    assert rows["made up swap"]["type"] == "Trade"
    transfer = [row for row in desk.all_rows() if "internal transfer" in row["note"]]
    assert {row["type"] for row in transfer} == {"Withdrawal", "Deposit"}
    assert {row["tag"] for row in transfer} == {"own_wallet_transfer"}
    mystery = rows["made up unknown label"]
    assert mystery["type"].startswith("_")
    assert "mystery prize" in unknown_labels(desk.root)
