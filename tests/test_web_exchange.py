# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Synthetic exchange exports are checked against Koinly and stay out of the ledger."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bittytax.web.errors import DeskError
from bittytax.web.exchange_csv import accept_row, compare, identify, open_gaps, page, stage_import
from bittytax.web.filing import CATEGORIES, readiness
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
_CSRF = re.compile(r'name="csrf-token" content="([^"]*)"')
_COINBASE = [
    "ID",
    "Timestamp",
    "Transaction Type",
    "Asset",
    "Quantity Transacted",
    "Price Currency",
    "Price at Transaction",
    "Subtotal",
    "Total (inclusive of fees and/or spread)",
    "Fees and/or Spread",
    "Notes",
]
_KOINLY = SAMPLE.read_text(encoding="utf-8").splitlines()[0].split(",")


def test_coinbase_ku_coin_and_revolut_headers_parse(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    coinbase = stage_import(desk, "coinbase-statement.csv", _one_buy(), "local")
    assert coinbase["exchange"] == "Coinbase"
    assert coinbase["format"] == "Coinbase"
    trades = stage_import(desk, "kucoin-spot.csv", _kucoin_trades(), "local")
    assert trades["exchange"] == "KuCoin"
    assert trades["format"] == "KuCoin Trades"
    deposits = stage_import(desk, "kucoin-deposits.csv", _kucoin_deposits(), "local")
    assert deposits["format"] == "KuCoin Deposits"
    assert deposits["skipped"] == 1
    withdrawals = stage_import(desk, "kucoin-withdrawals.csv", _kucoin_withdrawals(), "local")
    assert withdrawals["format"] == "KuCoin Withdrawals"
    revolut = stage_import(desk, "revolut-statement.csv", _revolut(), "local")
    assert revolut["exchange"] == "Revolut X"
    assert revolut["format"] == "Revolut"
    stored = json.loads((desk.root / "exchanges.json").read_bytes().decode("utf-8"))
    by_format = {item["format"]: item for item in stored["imports"]}
    buy = by_format["Coinbase"]["rows"][0]["cells"]
    assert buy[0] == "Trade"
    assert Decimal(buy[1]) == Decimal("0.5")
    assert buy[2] == "ETH"
    assert Decimal(buy[4]) == Decimal("1000")
    assert buy[5] == "GBP"
    spot = [row for row in by_format["KuCoin Trades"]["rows"] if row["kind"] == "mapped"]
    assert spot[0]["cells"][0] == "Trade"
    assert Decimal(spot[0]["cells"][1]) == Decimal("0.5")
    assert spot[0]["cells"][2] == "ETH"
    assert Decimal(spot[0]["cells"][4]) == Decimal("1000")
    assert spot[0]["cells"][5] == "USDT"
    held = [row for row in by_format["KuCoin Trades"]["rows"] if row["kind"] == "unclear"]
    assert held and "HOLD" in held[0]["description"]
    deposit = by_format["KuCoin Deposits"]["rows"]
    assert len(deposit) == 1
    assert deposit[0]["cells"][0] == "Deposit"
    assert Decimal(deposit[0]["cells"][1]) == Decimal("0.2")
    withdrawal = by_format["KuCoin Withdrawals"]["rows"][0]["cells"]
    assert withdrawal[0] == "Withdrawal"
    assert Decimal(withdrawal[4]) == Decimal("0.4")
    revolut_rows = by_format["Revolut"]["rows"]
    mapped = [row["cells"] for row in revolut_rows if row["kind"] == "mapped"]
    assert {cells[0] for cells in mapped} == {"Trade", "Deposit"}
    assert any(row["kind"] == "unclear" and "Mystery" in row["description"] for row in revolut_rows)
    assert not desk.all_rows()


def test_an_unknown_header_names_the_columns_and_a_koinly_file_is_refused() -> None:
    unknown = identify("notes.csv", b"Foo,Bar\n1,2\n")
    assert unknown["ok"] == "no"
    assert "Unknown header" in unknown["error"]
    assert "Foo" in unknown["error"] and "Bar" in unknown["error"]
    assert "Revolut X statement" in unknown["error"]
    koinly = identify(SAMPLE.name, SAMPLE.read_bytes())
    assert koinly["ok"] == "no"
    assert "Koinly" in koinly["error"]
    assert "does not import" in koinly["error"]


def test_notes_that_fail_do_not_keep_the_cell_text(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    stage_import(desk, "coinbase.csv", _bad_note(), "local")
    stored = json.loads((desk.root / "exchanges.json").read_text(encoding="utf-8"))
    row = stored["imports"][0]["rows"][0]
    assert row["kind"] == "unclear"
    assert row["description"] == "Unexpected Notes."
    assert "0x" not in row["description"]


def test_the_same_bytes_are_imported_once_and_the_file_has_no_carriage_return(
    tmp_path: Path,
) -> None:
    desk = Desk(tmp_path)
    first = stage_import(desk, "kucoin-deposits.csv", _kucoin_deposits(), "local")
    second = stage_import(desk, "kucoin-deposits.csv", _kucoin_deposits(), "local")
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["id"] == first["id"]
    raw = (desk.root / "exchanges.json").read_bytes()
    assert b"\r" not in raw
    stored = json.loads(raw.decode("utf-8"))
    assert len(stored["imports"]) == 1


def test_coinbase_is_compared_with_koinly_by_time_asset_amount_and_direction(
    tmp_path: Path,
) -> None:
    desk = _loaded(tmp_path)
    view = compare(desk, 2026, "Coinbase")
    titles = [row["title"] for row in view["open"]]
    assert view["matched"] == 2, titles
    assert "Fee mismatch: Trade 400 GBP for 0.2 ETH" in titles
    assert "Amount mismatch: Trade 800 GBP for 0.4 ETH" in titles
    assert "Exchange only: Deposit 0.1 ETH" in titles
    assert "Exchange only: Deposit 1.5 BTC" in titles
    assert "Koinly only: Airdrop 3 LTC" in titles
    assert "Koinly only: Lost 1.5 BTC" in titles
    assert "Exchange only: Trade 40 GBP for 0.02 ETH" in titles
    assert "Koinly only: Trade 40 GBP for 0.02 ETH" in titles
    assert any(title.startswith("Needs a type: Convert Dust") for title in titles)
    assert not any("0.5 ETH" in title for title in titles)
    assert not any("0.01 ETH" in title for title in titles)
    assert not any("SOL" in title for title in titles)
    assert not any("0.3" in title for title in titles)
    fee = next(row for row in view["open"] if row["kind"] == "fee")
    assert "Exchange fee 1 GBP" in fee["detail"]
    assert "Koinly fee 4 GBP" in fee["detail"]
    assert fee["href"].startswith("/transactions/")
    amount = next(row for row in view["open"] if row["kind"] == "amount")
    assert "0.4 ETH" in amount["detail"] and "0.25 ETH" in amount["detail"]
    missing = next(row for row in view["open"] if row["title"] == "Exchange only: Deposit 0.1 ETH")
    assert missing["href"].startswith("/review?")
    older = compare(desk, 2025, "Coinbase")
    assert [row["title"] for row in older["open"]] == ["Exchange only: Deposit 0.3 ETH"]
    assert desk.all_rows()
    assert all("Accepted from the exchange file" not in row["note"] for row in desk.all_rows())


def test_a_second_export_of_the_same_trade_does_not_open_another_gap(tmp_path: Path) -> None:
    desk = _loaded(tmp_path)
    before = compare(desk, 2026, "Coinbase")
    stage_import(desk, "coinbase-again.csv", _one_buy("cb-copy"), "local")
    after = compare(desk, 2026, "Coinbase")
    assert after["matched"] == before["matched"]
    assert [row["title"] for row in after["open"]] == [row["title"] for row in before["open"]]


def test_accepting_a_mapped_row_adds_it_to_the_ledger_and_clears_the_gap(tmp_path: Path) -> None:
    desk = _loaded(tmp_path)
    before = len(desk.all_rows())
    gap = next(row for row in compare(desk, 2026, "Coinbase")["open"] if "0.1 ETH" in row["title"])
    ledger_id = accept_row(desk, gap["id"], "local", {})
    assert ledger_id in {row["id"] for row in desk.all_rows()}
    assert len(desk.all_rows()) == before + 1
    assert all("0.1 ETH" not in row["title"] for row in compare(desk, 2026, "Coinbase")["open"])
    accepted = next(row for row in desk.all_rows() if row["id"] == ledger_id)
    assert accepted["type"] == "Deposit"
    assert accepted["wallet"] == "Coinbase"
    koinly = desk.root / "koinly"
    assert not koinly.exists()


def test_an_unclear_row_needs_a_chosen_type_before_it_can_be_accepted(tmp_path: Path) -> None:
    desk = _loaded(tmp_path)
    gap = next(row for row in compare(desk, 2026, "Coinbase")["open"] if row["kind"] == "unclear")
    try:
        accept_row(desk, gap["id"], "local", {})
    except DeskError as exc:
        assert "not guessed" in str(exc)
    else:
        raise AssertionError("an unclear row was accepted without a type")
    ledger_id = accept_row(
        desk,
        gap["id"],
        "local",
        {"type": "Deposit", "buy_asset": "eth", "buy_quantity": "0.01"},
    )
    accepted = next(row for row in desk.all_rows() if row["id"] == ledger_id)
    assert accepted["type"] == "Deposit"
    assert accepted["buy_asset"] == "ETH"
    assert Decimal(accepted["buy_quantity"]) == Decimal("0.01")
    assert "Convert Dust" in accepted["note"]
    try:
        accept_row(
            desk,
            gap["id"],
            "local",
            {"type": "Deposit", "buy_asset": "ETH", "buy_quantity": "0.01"},
        )
    except DeskError as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("the same row was accepted twice")


def test_ku_coin_and_revolut_match_a_koinly_row_for_that_wallet(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    stage_import(desk, "kucoin-spot.csv", _kucoin_match(), "local")
    desk.import_bytes(
        "koinly.csv",
        _koinly_rows([_trade("KuCoin", "0.5", "1000", "USDT", "1", "2025-06-01 12:00:00")]),
    )
    view = compare(desk, 2026, "KuCoin")
    assert view["matched"] == 1
    assert not view["open"]
    stage_import(desk, "revolut.csv", _revolut_buy(), "local")
    stored = json.loads((desk.root / "exchanges.json").read_text(encoding="utf-8"))
    revolut = next(item for item in stored["imports"] if item["format"] == "Revolut")
    stamp = str(revolut["rows"][0]["timestamp"])[:19].replace("T", " ")
    desk.import_bytes(
        "revolut-koinly.csv", _koinly_rows([_trade("Revolut", "0.5", "1000", "GBP", "2", stamp)])
    )
    checked = compare(desk, 2026, "Revolut X")
    assert checked["matched"] == 1, [row["title"] for row in checked["open"]]


def test_ready_to_file_counts_exchange_gaps_and_an_empty_desk_has_none(tmp_path: Path) -> None:
    empty = readiness(Desk(tmp_path), 2026)
    assert [row["key"] for row in empty["counts"]] == [key for key, _label in CATEGORIES]
    assert next(row["count"] for row in empty["counts"] if row["key"] == "exchange_gap") == 0
    desk = _loaded(tmp_path)
    board = readiness(desk, 2026)
    count = next(row["count"] for row in board["counts"] if row["key"] == "exchange_gap")
    assert count == len(open_gaps(desk, 2026))
    assert count > 0
    assert any(item["category"] == "exchange_gap" for item in board["items"])
    assert board["status"] == "Not ready"
    shown = page(desk, 2026, "Coinbase")
    assert shown["exchange"] == "Coinbase"
    assert any(item["name"] == "KuCoin" for item in shown["exchanges"])


def test_the_import_page_comparison_review_and_ready_page(tmp_path: Path) -> None:
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    browser = _Client(f"http://127.0.0.1:{port}")
    try:
        wallets = browser.get("/import")
        assert "Exchange records" in wallets
        assert 'id="exchange-drop"' in wallets
        koinly = browser.post_file("/import", "koinly.csv", _koinly_scenario())
        assert "Imported" in koinly
        assert "rows imported" in koinly
        imported = browser.post_file("/import", "coinbase-statement.csv", _coinbase_scenario(False))
        assert "Exchange vs Koinly" in imported
        assert "rows imported" not in imported
        compared = browser.get("/exchanges?tax_year=2026&exchange=Coinbase")
        assert "Amount mismatch" in compared
        assert "Fee mismatch" in compared
        assert "Exchange only" in compared
        assert "Koinly only" in compared
        assert "Needs a type" in compared
        assert "Spot Orders_Filled Orders" in compared
        assert "Deposit_Withdrawal History_Deposit History" in compared
        assert "Symbol, Type, Quantity, Price, Value, Fees, Date" in compared
        ready = browser.get("/ready?tax_year=2026")
        assert "Exchange gaps" in ready
        review = browser.get("/review?tax_year=2026")
        assert "Add to the tax report" in review
        row_id = _row_with(review, "0.1")
        browser.post("/review/exchange", {"row_id": row_id, "tax_year": "2026"})
        cleared = browser.get("/exchanges?tax_year=2026&exchange=Coinbase")
        assert "Deposit 0.1 ETH" not in cleared
        assert "1 row has been added to the tax report." in cleared
        refused = browser.post_file("/import/exchange", "notes.csv", b"Foo,Bar\n1,2\n")
        assert "Unknown header" in refused
        assert "Foo" in refused and "Bar" in refused
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    desk = Desk(tmp_path)
    assert any(row["type"] == "Deposit" and row["buy_asset"] == "ETH" for row in desk.all_rows())


def _loaded(tmp_path: Path) -> Desk:
    desk = Desk(tmp_path)
    desk.import_bytes("koinly.csv", _koinly_scenario())
    stage_import(desk, "coinbase-statement.csv", _coinbase_scenario(True), "local")
    return desk


def _row_with(page_html: str, quantity: str) -> str:
    for row_id, body in re.findall(r'<tr id="row-([^"]+)">([\s\S]*?)</tr>', page_html):
        if f">{quantity}<" in body and "Add to the tax report" in body:
            return str(row_id)
    raise AssertionError("the review page did not offer that exchange row")


def _coinbase_scenario(include_old: bool) -> bytes:
    rows = [
        _buy("cb-match", "2025-06-01 12:00:00 UTC", "0.5", "1000", "2"),
        _buy("cb-fee", "2025-06-01 15:00:00 UTC", "0.2", "400", "1"),
        _buy("cb-amount", "2025-06-02 12:00:00 UTC", "0.4", "800", "0"),
        _receive("cb-only", "2025-06-03 12:00:00 UTC", "ETH", "0.1"),
        _buy("cb-near", "2025-06-05 12:00:00 UTC", "0.01", "20", "0"),
        _buy("cb-far", "2025-06-06 12:00:00 UTC", "0.02", "40", "0"),
        _receive("cb-direction", "2025-06-07 12:00:00 UTC", "BTC", "1.5"),
        _dust("cb-dust", "2025-06-08 12:00:00 UTC"),
    ]
    if include_old:
        rows.append(_receive("cb-old", "2025-03-01 12:00:00 UTC", "ETH", "0.3"))
    return _coinbase(rows)


def _koinly_scenario() -> bytes:
    return _koinly_rows(
        [
            _trade("Coinbase", "0.5", "1000", "GBP", "2", "2025-06-01 12:00:00"),
            _trade("Coinbase", "0.2", "400", "GBP", "4", "2025-06-01 15:00:00"),
            _trade("Coinbase", "0.25", "500", "GBP", "0", "2025-06-02 12:00:00"),
            _trade("Coinbase", "0.01", "20", "GBP", "0", "2025-06-05 12:01:00"),
            _trade("Coinbase", "0.02", "40", "GBP", "0", "2025-06-06 12:10:00"),
            _deposit("Coinbase", "LTC", "3", "2025-06-04 12:00:00", "Airdrop"),
            _withdrawal("Coinbase", "1.5", "2025-06-07 12:00:00", "Lost"),
            _deposit("Kraken", "SOL", "1", "2025-06-01 12:00:00", "Airdrop"),
        ]
    )


def _trade(
    wallet: str, received: str, sent: str, quote: str, fee: str, when: str
) -> Dict[str, str]:
    return {
        "Date": when,
        "Type": "exchange",
        "Sending Wallet": wallet,
        "Sent Amount": sent,
        "Sent Currency": quote,
        "Receiving Wallet": wallet,
        "Received Amount": received,
        "Received Currency": "ETH",
        "Fee Amount": fee if Decimal(fee) else "",
        "Fee Currency": quote if Decimal(fee) else "",
        "Net Value (GBP)": "1000",
        "Fee Value (GBP)": fee if Decimal(fee) else "",
        "Description": "Synthetic trade",
    }


def _withdrawal(wallet: str, amount: str, when: str, tag: str) -> Dict[str, str]:
    return {
        "Date": when,
        "Type": "crypto_withdrawal",
        "Tag": tag,
        "Sending Wallet": wallet,
        "Sent Amount": amount,
        "Sent Currency": "BTC",
        "Net Value (GBP)": "1000",
        "Description": "Synthetic withdrawal",
    }


def _deposit(wallet: str, asset: str, amount: str, when: str, tag: str) -> Dict[str, str]:
    return {
        "Date": when,
        "Type": "crypto_deposit",
        "Tag": tag,
        "Receiving Wallet": wallet,
        "Received Amount": amount,
        "Received Currency": asset,
        "Net Value (GBP)": "1000",
        "Description": "Synthetic deposit",
    }


def _one_buy(row_id: str = "cb-1") -> bytes:
    return _coinbase([_buy(row_id, "2025-06-01 12:00:00 UTC", "0.5", "1000", "2")])


def _bad_note() -> bytes:
    return _coinbase(
        [
            [
                "cb-bad",
                "2025-06-01 12:00:00 UTC",
                "Buy",
                "ETH",
                "0.1",
                "GBP",
                "2000.00",
                "200.00",
                "200.00",
                "1.00",
                "sent to 0x1234567890abcdef",
            ]
        ]
    )


def _buy(row_id: str, when: str, quantity: str, subtotal: str, fee: str) -> List[str]:
    total = Decimal(subtotal) + Decimal(fee)
    note = f"Bought {quantity} ETH for £{Decimal(subtotal):.2f} GBP"
    return [
        row_id,
        when,
        "Buy",
        "ETH",
        quantity,
        "GBP",
        "2000.00",
        f"{Decimal(subtotal):.2f}",
        f"{total:.2f}",
        f"{Decimal(fee):.2f}",
        note,
    ]


def _receive(row_id: str, when: str, asset: str, quantity: str) -> List[str]:
    return [
        row_id,
        when,
        "Receive",
        asset,
        quantity,
        "GBP",
        "2000.00",
        "200.00",
        "200.00",
        "0.00",
        f"Received {asset}",
    ]


def _dust(row_id: str, when: str) -> List[str]:
    return [
        row_id,
        when,
        "Convert Dust",
        "ETH",
        "0.01",
        "GBP",
        "2000.00",
        "20.00",
        "20.00",
        "0.00",
        "Converted dust",
    ]


def _coinbase(rows: Sequence[Sequence[str]]) -> bytes:
    return b"Transactions\nUser,Sample Person,\n" + _csv([_COINBASE, *rows])


def _kucoin_trades() -> bytes:
    header = [
        "UID",
        "Account Type",
        "Order ID",
        "Symbol",
        "Side",
        "Order Type",
        "Avg. Filled Price",
        "Filled Amount",
        "Filled Volume",
        "Filled Volume (USDT)",
        "Filled Time(UTC+00:00)",
        "Fee",
        "Tax",
        "Maker/Taker",
        "Fee Currency",
    ]
    return _csv(
        [
            header,
            [
                "sample-uid",
                "main",
                "order-sample-1",
                "ETH-USDT",
                "BUY",
                "LIMIT",
                "2000",
                "0.5",
                "1000",
                "1000",
                "2025-06-01 12:00:00",
                "1",
                "0",
                "Taker",
                "USDT",
            ],
            [
                "sample-uid",
                "main",
                "order-sample-2",
                "ETH-USDT",
                "HOLD",
                "LIMIT",
                "2000",
                "0.1",
                "200",
                "200",
                "2025-06-02 12:00:00",
                "1",
                "0",
                "Taker",
                "USDT",
            ],
        ]
    )


def _kucoin_match() -> bytes:
    text = _kucoin_trades().decode("utf-8").splitlines()
    return (text[0] + "\n" + text[1] + "\n").encode("utf-8")


def _kucoin_deposits() -> bytes:
    return _csv(
        [
            [
                "UID",
                "Account Type",
                "Time(UTC+00:00)",
                "Coin",
                "Amount",
                "Fee",
                "Hash",
                "Deposit Address",
                "Transfer Network",
                "Status",
                "Remarks",
            ],
            [
                "sample-uid",
                "main",
                "2025-06-04 09:00:00",
                "ETH",
                "0.2",
                "0",
                "sample-hash",
                "sample-deposit-address",
                "ETH",
                "SUCCESS",
                "sample deposit",
            ],
            [
                "sample-uid",
                "main",
                "2025-06-04 10:00:00",
                "ETH",
                "0.3",
                "0",
                "sample-hash-2",
                "sample-deposit-address",
                "ETH",
                "PROCESSING",
                "pending",
            ],
        ]
    )


def _kucoin_withdrawals() -> bytes:
    return _csv(
        [
            [
                "UID",
                "Account Type",
                "Time(UTC+00:00)",
                "Coin",
                "Amount",
                "Fee",
                "Hash",
                "Withdrawal Address/Account",
                "Transfer Network",
                "Status",
                "Remarks",
            ],
            [
                "sample-uid",
                "main",
                "2025-06-05 09:00:00",
                "ETH",
                "0.4",
                "0.01",
                "sample-hash",
                "sample-withdrawal-address",
                "ETH",
                "SUCCESS",
                "sample withdrawal",
            ],
        ]
    )


def _revolut() -> bytes:
    return _csv(
        [
            ["Symbol", "Type", "Quantity", "Price", "Value", "Fees", "Date"],
            ["ETH", "Buy", "0.5", "£2000.00", "£1000.00", "£2.00", "2025-06-01 12:00:00"],
            ["ETH", "Receive", "0.1", "£2000.00", "£200.00", "£0.00", "2025-06-02 12:00:00"],
            ["ETH", "Mystery", "0.2", "£2000.00", "£400.00", "£0.00", "2025-06-03 12:00:00"],
        ]
    )


def _revolut_buy() -> bytes:
    text = _revolut().decode("utf-8").splitlines()
    return (text[0] + "\n" + text[1] + "\n").encode("utf-8")


def _koinly_rows(rows: Sequence[Mapping[str, str]]) -> bytes:
    table = [_KOINLY]
    for item in rows:
        table.append([item.get(name, "") for name in _KOINLY])
    return _csv(table)


def _csv(rows: Sequence[Sequence[str]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


class _Client:
    """Sends the session cookie and the form token."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie = ""
        self.csrf = ""

    def get(self, path: str) -> str:
        return self._open(Request(self.base + path))

    def post(self, path: str, fields: Dict[str, str]) -> str:
        payload = dict(fields)
        payload["csrf"] = self.csrf
        body = urlencode(payload).encode("utf-8")
        request = Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._open(request)

    def post_file(self, path: str, filename: str, data: bytes) -> str:
        boundary = "----deskboundaryexchange"
        csrf = b""
        if self.csrf:
            csrf = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csrf"\r\n\r\n'
                f"{self.csrf}\r\n"
            ).encode("utf-8")
        body = (
            csrf
            + (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode("utf-8")
            + data
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        request = Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return self._open(request)

    def _open(self, request: Request) -> str:
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.headers.get("Set-Cookie")
                if raw:
                    self.cookie = str(raw).split(";", 1)[0].strip()
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{exc.code} {detail}") from exc
        found = _CSRF.search(body)
        if found:
            self.csrf = found.group(1)
        return body
