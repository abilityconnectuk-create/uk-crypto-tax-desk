# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Transactions workspace and the unconfirmed UK tag set. Sample data only."""

from __future__ import annotations

import csv
import io
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from urllib.request import urlopen

import pytest

from bittytax.tax import TaxCalculator
from bittytax.web.defi_apply import tax_cells
from bittytax.web.errors import DeskError
from bittytax.web.filing import CATEGORIES, readiness
from bittytax.web.futures import relieve_loss
from bittytax.web.server import bind_server
from bittytax.web.store import Desk
from bittytax.web.tag_set import (
    BANNER,
)
from bittytax.web.tag_set import _pool as pool_cells  # pylint: disable=protected-access
from bittytax.web.tag_set import (
    catalogue,
    describe,
    document,
    figures_unchanged,
    rewrite_cells,
    settle,
)
from bittytax.web.tx_store import (
    add_rename,
    add_spam_token,
    delete_price,
    delete_rename,
    delete_spam_token,
    import_prices,
    load_prices,
    update_meta,
)
from bittytax.web.tx_workspace import (
    add_transaction,
    aggregate_rows,
    apply_import,
    build_page,
    explorer_url,
    export_csv,
    export_xlsx,
    open_row,
    prepare,
    read_filters,
    read_table,
    restore_rows,
    row_matches,
    save_row,
    selected_ids,
    soft_delete,
    stage_import,
    warning_total,
)
from bittytax.web.user_rules import apply_bulk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
_WHEN = "2026-06-01 12:00:00"
_CHAIN = "0x" + ("ab" * 32)


def test_filters_group_page_and_explorer(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    _manual(
        desk,
        "Deposit",
        buy_qty="2",
        buy_asset="ZZZ",
        buy_value="40",
        wallet="ethereum",
        when="2025-04-05 23:30:00",
        note="boundary row",
    )
    later = _manual(
        desk,
        "Trade",
        buy_qty="1",
        buy_asset="QQQ",
        buy_value="9",
        sell_qty="3",
        sell_asset="ZZZ",
        sell_value="30",
        wallet="Sample Wallet",
        when="2026-08-01 09:00:00",
        note="later trade",
    )
    _meta(
        desk,
        later,
        {
            "tag": "swap",
            "tx_hash": _CHAIN,
            "sender": "from-wallet",
            "recipient": "to-wallet",
            "description": "sample swap note",
            "contract": "made-up-contract",
            "method": "sampleMethod",
            "chain": "ethereum",
        },
    )
    page = build_page(desk, {"per": ["25"]})
    assert page["total"] == 2
    assert page["per_page"] == 25
    assert [group["date"] for group in page["groups"]] == ["2026-08-01", "2025-04-05"]
    assert build_page(desk, {"per": ["50"]})["per_page"] == 50
    assert build_page(desk, {"per": ["100"]})["per_page"] == 100
    uk = build_page(desk, {"clock": ["uk"], "tax_quick": ["2026"]})
    assert uk["filters"]["date_from"] == "2025-04-06"
    assert uk["filters"]["date_to"] == "2026-04-05"
    assert uk["total"] == 1
    assert uk["rows"][0]["filter_date"] == "2025-04-06"
    uk_hit = build_page(
        desk, {"clock": ["uk"], "date_from": ["2025-04-06"], "date_to": ["2025-04-06"]}
    )
    assert uk_hit["total"] == 1
    utc_year = build_page(desk, {"clock": ["utc"], "tax_quick": ["2025"]})
    assert utc_year["total"] == 1
    assert utc_year["rows"][0]["filter_date"] == "2025-04-05"
    prepared = prepare(desk.all_rows(), Decimal(0), "utc")
    trade = next(row for row in prepared if row["id"] == later)
    assert trade["tx_class"] == "exchange"
    assert trade["explorer"].startswith("https://etherscan.io/tx/")
    assert "sample-tx-btc-buy" not in trade["explorer"]
    deposit = next(row for row in prepared if row["id"] != later)
    assert deposit["tx_class"] == "crypto_deposit"
    assert deposit["explorer"] == ""
    coinbase = dict(trade)
    coinbase["wallet"] = "Coinbase"
    coinbase["chain"] = ""
    assert explorer_url(coinbase) == ""
    filters = {
        "wallet": "Sample Wallet",
        "asset": "QQQ",
        "tx_class": "exchange",
        "tag": "swap",
        "manual": "yes",
        "description": "swap",
        "sender": "from-wallet",
        "recipient": "to-wallet",
        "tx_hash": _CHAIN[2:8],
        "contract": "made-up",
        "method": "sampleMethod",
        "import_source": "Manual entry",
        "import_type": "Manual",
        "amount_min": "1",
        "amount_max": "5",
        "net_min": "9",
        "net_max": "30",
        "gain_min": "0",
        "gain_max": "0",
    }
    chosen = {key: "" for key in read_filters({})}
    chosen.update(filters)
    assert row_matches(trade, chosen)
    chosen["fee_min"] = "1"
    assert not row_matches(trade, chosen)


def test_page_lists_the_workspace(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/transactions", timeout=30) as response:
            page = response.read().decode("utf-8")
        with urlopen(f"http://127.0.0.1:{port}/config", timeout=30) as response:
            settings = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    for text in (
        "Wallet",
        "transactions",
        'name="per"',
        "Reclassify",
        'name="row_id"',
        "Add transaction",
        "Add filter",
        "awaiting owner approval",
        "Crypto deposit",
        "UK time",
        "6 Apr 2025 to 5 Apr 2026",
        "Tx hash",
        "Import source",
        "Deleted",
    ):
        assert text in page
    assert "awaiting owner approval" in settings
    assert "Spam tokens" in settings
    assert "Custom prices" in settings


def test_row_editor_audits_a_gbp_override(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    row_id = _manual(desk, "Deposit", buy_qty="1", buy_asset="ZZZ", buy_value="10")
    original = (desk.root / "ledger.json").read_bytes()
    save_row(desk, row_id, {"tag": "reward", "reviewed": "yes", "gbp": "12"}, "sam")
    edited = open_row(desk, row_id)
    assert edited["tag"] == "reward"
    assert edited["reviewed"] is True
    assert edited["buy_value"] == "12"
    audit = edited["audit"]
    value = next(entry for entry in audit if entry["field"] == "buy_value")
    assert value["old"] == "10"
    assert value["new"] == "12"
    assert value["at"]
    save_row(desk, row_id, {"tag": "reward", "reviewed": "yes", "gbp": ""}, "sam")
    cleared = open_row(desk, row_id)
    assert cleared["buy_value"] == "10"
    blank = next(
        entry for entry in cleared["audit"] if entry["field"] == "buy_value" and entry["new"] == ""
    )
    assert blank["old"] == "12"
    assert (desk.root / "ledger.json").read_bytes() == original


def test_add_transaction_tabs(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    with pytest.raises(DeskError, match="YYYY-MM-DD"):
        add_transaction(desk, {"kind": "deposit", "buy_quantity": "1", "buy_asset": "ZZZ"}, "sam")
    deposit = add_transaction(
        desk,
        {
            "kind": "deposit",
            "buy_quantity": "1",
            "buy_asset": "ZZZ",
            "to_wallet": "Sample Wallet",
            "date": "2026-06-02",
            "description": "made up deposit",
        },
        "sam",
    )
    withdrawal = add_transaction(
        desk,
        {
            "kind": "withdrawal",
            "sell_quantity": "1",
            "sell_asset": "ZZZ",
            "from_wallet": "Sample Wallet",
            "date": "2026-06-03",
        },
        "sam",
    )
    trade = add_transaction(
        desk,
        {
            "kind": "trade",
            "buy_quantity": "2",
            "buy_asset": "QQQ",
            "sell_quantity": "1",
            "sell_asset": "ZZZ",
            "from_wallet": "Sample Wallet",
            "date": "2026-06-04",
            "fiat_value": "15",
        },
        "sam",
    )
    transfer = add_transaction(
        desk,
        {
            "kind": "transfer",
            "sell_quantity": "1",
            "sell_asset": "QQQ",
            "from_wallet": "Sample Wallet",
            "to_wallet": "Other Wallet",
            "date": "2026-06-05",
        },
        "sam",
    )
    assert len(deposit) == 1
    assert len(withdrawal) == 1
    assert len(trade) == 1
    assert len(transfer) == 2
    rows = {row["id"]: row for row in desk.all_rows()}
    assert rows[deposit[0]]["manual"] is True
    assert rows[trade[0]]["buy_value"] == "15"
    assert {rows[item]["type"] for item in transfer} == {"Withdrawal", "Deposit"}
    page = build_page(desk, {"manual": ["yes"]})
    assert all(row["manual"] for row in page["rows"])


def test_bulk_spam_delete_aggregate_and_reimport(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    first = _manual(desk, "Deposit", buy_qty="1", buy_asset="ZZZ", buy_value="4", wallet="Left")
    second = _manual(desk, "Deposit", buy_qty="2", buy_asset="ZZZ", buy_value="6", wallet="Left")
    other = _manual(desk, "Deposit", buy_qty="1", buy_asset="QQQ", buy_value="8", wallet="Right")
    apply_bulk(desk, [first], "tag", "", "reward", "Sample tag.", "sam")
    assert desk.transaction(first)["tag"] == "reward"
    apply_bulk(desk, [other], "spam", "", "", "Sample spam.", "sam")
    assert desk.transaction(other)["type"] == "_Possible-Spam"
    ids = selected_ids(
        desk,
        {"scope": ["filtered"], "wallet": ["Left"]},
        {"scope": ["filtered"], "wallet": ["Left"]},
    )
    assert set(ids) == {first, second}
    soft_delete(desk, [second], "sam")
    visible = {row["id"] for row in build_page(desk, {})["rows"]}
    assert second not in visible
    deleted = build_page(desk, {"deleted": ["yes"]})
    assert [row["id"] for row in deleted["rows"]] == [second]
    assert second not in desk.records_path.read_text(encoding="utf-8")
    restore_rows(desk, [second], "sam")
    assert second in {row["id"] for row in build_page(desk, {})["rows"]}
    created = aggregate_rows(desk, [first, second], "sam")
    assert desk.transaction(created)["buy_quantity"] in {"3", "3.0"}
    assert desk.transaction(created)["manual"] is True
    body = export_csv(prepare(desk.all_rows(), Decimal(0), "utc"), {})
    assert b"\r" not in body
    parsed = read_table(body, "rows.csv")
    for item in parsed:
        if item["id"] == created:
            item["buy_value"] = "77"
    edited = _csv_bytes(parsed)
    staged = stage_import(desk, edited, "edited.csv")
    assert any(field["new"] == "77" for change in staged for field in change["fields"])
    assert desk.transaction(created)["buy_value"] == "10"
    apply_import(desk, "sam")
    assert desk.transaction(created)["buy_value"] == "77"
    sheet = export_xlsx(prepare(desk.all_rows(), Decimal(0), "utc"), {})
    assert sheet[:2] == b"PK"
    assert any(item["id"] == created for item in read_table(sheet, "rows.xlsx"))


def test_warnings_and_ready_count(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    _manual(
        desk, "Spend", sell_qty="1", sell_asset="ZZZ", sell_value="20", when="2026-06-01 10:00:00"
    )
    _manual(desk, "Deposit", buy_qty="1", buy_asset="QQQ", when="2026-06-02 10:00:00")
    _manual(
        desk,
        "Spend",
        sell_qty="1",
        sell_asset="QQQ",
        sell_value="3",
        when="2026-06-03 10:00:00",
    )
    _manual(
        desk, "Spend", sell_qty="1", sell_asset="MMM", sell_value="0.01", when="2026-07-01 10:00:00"
    )
    rows = prepare(desk.all_rows(), Decimal(0), "utc")
    codes = {code for row in rows for code in row["warnings"]}
    assert "missing_purchase" in codes
    assert "missing_price" in codes
    assert "missing_cost_basis" in codes
    assert "missing_acquisition_cost" in codes
    dusty = prepare(desk.all_rows(), Decimal("1"), "utc")
    tiny = next(row for row in dusty if row["sell_asset"] == "MMM")
    assert tiny["warnings"] == []
    blank = next(row for row in dusty if row["buy_asset"] == "QQQ" and not row["buy_value"])
    assert "missing_price" in blank["warnings"]
    assert warning_total(desk, 2027) >= 1
    view = readiness(desk, 2027, today=date(2026, 11, 28))
    count = next(item for item in view["counts"] if item["key"] == "tx_warning")
    assert count["count"] == warning_total(desk, 2027)
    assert any(item["href"] == "/transactions?warning=any" for item in view["items"])
    empty = readiness(Desk(tmp_path / "empty"), 2027, today=date(2026, 11, 28))
    assert [row["key"] for row in empty["counts"]] == [key for key, _label in CATEGORIES]
    assert next(item for item in empty["counts"] if item["key"] == "tx_warning")["count"] == 0


def test_tag_set_is_unconfirmed_and_does_not_change_figures(tmp_path: Path) -> None:
    for ending, slug in ((2025, "2024-25"), (2026, "2025-26"), (2027, "2026-27")):
        loaded = document(ending)
        raw = (ROOT / "config" / "tags" / f"{slug}.yaml").read_bytes()
        assert b"\r" not in raw
        assert loaded["confirmed"] is False
        assert loaded["banner"] == BANNER
        assert loaded["cashback_cost"]["value"] == "nil"
        assert loaded["cashback_cost"]["confirmed"] is False
        for tag in loaded["tags"]:
            assert tag["confirmed"] is False
            assert tag["sources"][0]["url"].startswith("https://")
    desk = Desk(tmp_path)
    row_id = _manual(desk, "Deposit", buy_qty="1", buy_asset="ZZZ", buy_value="10")
    before = desk.records_path.read_bytes()
    update_meta(desk.root, row_id, {"tag": "reward"})
    desk.refresh_records()
    assert desk.records_path.read_bytes() == before
    row = desk.transaction(row_id)
    assert figures_unchanged(row, row["cells"])
    assert tax_cells(row)[0] == row["cells"][0]
    assert describe("reward", 2027, row)["banner"] == BANNER
    assert "box 17" in describe("reward", 2027, row)["box"]
    salary = describe("salary", 2027, row)
    assert salary["flag"] == "employment"
    assert "not yet confirmed" in salary["box"]


def test_forced_tag_treatments() -> None:
    cells = _cells("Deposit", "1", "ZZZ", "10", "", "", "")
    assert rewrite_cells(_tagged("salary"), cells, force=True) == cells
    assert rewrite_cells(_tagged("airdrop"), cells, force=True) == cells
    income = rewrite_cells(_tagged("airdrop", airdrop_choice="income"), cells, force=True)
    assert income[0] == "Income"
    market = rewrite_cells(_tagged("airdrop", airdrop_choice="market"), cells, force=True)
    assert market[0] == "Airdrop" and market[3] == "10"
    nil = rewrite_cells(_tagged("airdrop", airdrop_choice="nil"), cells, force=True)
    assert nil[0] == "Airdrop" and nil[3] == "0"
    forked = rewrite_cells(_tagged("fork"), cells, force=True)
    assert forked[0] == "Fork" and forked[3] == "10"
    assert rewrite_cells(_tagged("loan"), cells, force=True)[0] == "Loan"
    assert rewrite_cells(_tagged("margin_loan"), cells, force=True)[0] == "Loan"
    repay = _cells("Withdrawal", "", "", "", "1", "ZZZ", "10")
    assert rewrite_cells(_tagged("loan_repay"), repay, force=True)[0] == "Loan-Repayment"
    assert rewrite_cells(_tagged("margin_repay"), repay, force=True)[0] == "Loan-Repayment"
    assert rewrite_cells(_tagged("cost"), repay, force=True)[0] == "Spend"
    assert rewrite_cells(_tagged("other_fee"), repay, force=True)[0] == "Spend"
    assert rewrite_cells(_tagged("tax"), repay, force=True)[0] == "Spend"
    loan_fee = rewrite_cells(_tagged("loan_fee"), repay, force=True)
    assert loan_fee[0] == "Spend" and "memo" in loan_fee[12].casefold()
    margin_fee = rewrite_cells(_tagged("margin_fee"), repay, force=True)
    assert margin_fee[0] == "Spend" and margin_fee[0] != "Margin-Fee"
    gain = rewrite_cells(
        _tagged("realised_pnl"), _cells("Deposit", "1", "GBP", "4", "", "", ""), force=True
    )
    assert gain[0] == "Margin-Gain"
    loss = rewrite_cells(
        _tagged("realised_pnl", sell_value="-4"),
        _cells("Withdrawal", "", "", "", "1", "GBP", "-4"),
        force=True,
    )
    assert loss[0] == "Margin-Loss"
    assert rewrite_cells(_tagged("futures_fee"), repay, force=True)[0] == "Margin-Fee"
    funding = rewrite_cells(_tagged("funding_fee"), repay, force=True)
    assert funding[0] == "Margin-Fee" and "funding" in funding[12].casefold()
    relieved = relieve_loss(Decimal("40"), Decimal("0"))
    assert relieved["used"] == "0.00"
    swap = rewrite_cells(
        _tagged("swap"), _cells("Trade", "1", "QQQ", "10", "1", "ZZZ", "10"), force=True
    )
    assert swap[0] == "Trade"
    assert rewrite_cells(_tagged("payment"), repay, force=True)[0] == "Spend"
    assert rewrite_cells(_tagged("dust"), repay, force=True)[0] == "Spend"
    assert rewrite_cells(_tagged("multi_trade"), swap, force=True)[0] == "Trade"
    pool = rewrite_cells(_tagged("pool_in", pool_same_token="yes"), repay, force=True)
    assert pool[0] == "Spend"
    kept = pool_cells(
        {
            "pool_protocol": "made-up-pool",
            "pool_same_token": "yes",
            "pool_receipt": "",
            "pool_restricted": "yes",
        },
        list(repay),
        {"protocols": {"made-up-pool": "not_disposal"}},
        True,
    )
    assert kept[0] == "Withdrawal"
    gift = rewrite_cells(_tagged("gift"), repay, force=True)
    assert gift[0] == "Gift-Sent"
    spouse = rewrite_cells(_tagged("gift", spouse="yes"), repay, force=True)
    assert spouse[0] == "Gift-Spouse"
    assert rewrite_cells(_tagged("donation"), repay, force=True)[0] == "Charity-Sent"
    assert rewrite_cells(_tagged("lost"), repay, force=True)[0] == "_Ignored"
    assert rewrite_cells(_tagged("stolen"), repay, force=True)[0] == "_Ignored"
    cash = rewrite_cells(_tagged("cashback"), cells, force=True)
    assert cash[0] == "Cashback" and cash[3] == "0"
    assert TaxCalculator.INCOME_TYPES
    assert "Cashback" not in {str(kind) for kind in TaxCalculator.INCOME_TYPES}
    refund = rewrite_cells(_tagged("fee_refund"), cells, force=True)
    assert refund[0] == "Deposit" and refund[3] == "0"
    linked_id = "sample:0000"
    fee_cells = _cells("Trade", "1", "QQQ", "10", "1", "ZZZ", "10")
    fee_cells[9] = "2"
    refund_row = _tagged("fee_refund", linked_fee=linked_id, buy_value="1")
    revised = settle(
        [{"id": linked_id, "tag": ""}, refund_row],
        [fee_cells, rewrite_cells(refund_row, cells, force=True)],
        force=True,
    )
    assert revised[0][9] == "1"
    assert revised[1][0] == "_Ignored"
    plain = _cells("Deposit", "1", "ZZZ", "10", "", "", "")
    assert rewrite_cells({"timestamp": _WHEN}, plain, force=True) == plain
    assert catalogue()
    assert all(item["confirmed"] is False for item in catalogue())


def test_settings_lists_are_deletable(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    row_id = _manual(desk, "Deposit", buy_qty="2", buy_asset="ZZZ", wallet="Sample Source")
    added = add_rename(desk.root, "Sample Source", "ZZZ", "YYY")
    assert desk.transaction(row_id)["buy_asset"] == "YYY"
    delete_rename(desk.root, added["id"])
    assert desk.transaction(row_id)["buy_asset"] == "ZZZ"
    token = add_spam_token(desk.root, "ZZZ")
    assert tax_cells(desk.transaction(row_id))[0] == "_Possible-Spam"
    assert desk.transaction(row_id)["type"] == "Deposit"
    delete_spam_token(desk.root, token["id"])
    assert tax_cells(desk.transaction(row_id))[0] == "Deposit"
    payload = b"date,asset,gbp\n2026-06-01,ZZZ,4\n"
    assert b"\r" not in payload
    import_prices(desk.root, payload)
    desk.refresh_records()
    assert desk.transaction(row_id)["buy_value"] == "8"
    price_id = next(item["id"] for item in load_prices(desk.root))
    delete_price(desk.root, price_id)
    desk.refresh_records()
    assert desk.transaction(row_id)["buy_value"] == ""


def _csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _manual(desk: Desk, kind: str, **fields: str) -> str:
    cells = _cells(
        kind,
        fields.get("buy_qty", ""),
        fields.get("buy_asset", ""),
        fields.get("buy_value", ""),
        fields.get("sell_qty", ""),
        fields.get("sell_asset", ""),
        fields.get("sell_value", ""),
    )
    cells[10] = fields.get("wallet", "Sample Wallet")
    cells[11] = fields.get("when", _WHEN)
    cells[12] = fields.get("note", "made up")
    return desk.append_manual([cells], "sam")[0]


def _meta(desk: Desk, row_id: str, fields: Dict[str, str]) -> None:
    update_meta(desk.root, row_id, fields)


def _cells(
    kind: str,
    buy_qty: str,
    buy_asset: str,
    buy_value: str,
    sell_qty: str,
    sell_asset: str,
    sell_value: str,
) -> List[str]:
    return [
        kind,
        buy_qty,
        buy_asset,
        buy_value,
        sell_qty,
        sell_asset,
        sell_value,
        "",
        "",
        "",
        "Sample Wallet",
        _WHEN,
        "made up",
    ]


def _tagged(tag: str, **extra: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {"tag": tag, "timestamp": _WHEN}
    row.update(extra)
    return row
