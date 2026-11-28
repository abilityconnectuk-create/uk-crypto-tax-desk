# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Own-wallet pairs use the calculator's withdrawal and deposit types."""

from __future__ import annotations

import csv
import io
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from bittytax.web.matching import decide, match_view, propose, save_settings
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

_HEADER = [
    "Date",
    "Type",
    "Tag",
    "Sending Wallet",
    "Sent Amount",
    "Sent Currency",
    "Sent Cost Basis",
    "Receiving Wallet",
    "Received Amount",
    "Received Currency",
    "Received Cost Basis",
    "Fee Amount",
    "Fee Currency",
    "Gain (GBP)",
    "Net Value (GBP)",
    "Fee Value (GBP)",
    "TxSrc",
    "TxDest",
    "TxHash",
    "Description",
]


def _csv(rows: list) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_HEADER)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _line(
    when: str,
    kind: str,
    tag: str,
    wallet: str,
    amount: str,
    asset: str,
    value: str,
    note: str,
    side: str,
) -> list:
    sent = ["", "", ""]
    received = ["", "", ""]
    if side == "out":
        sent = [wallet, amount, asset]
    else:
        received = [wallet, amount, asset]
    return [
        when,
        kind,
        tag,
        sent[0],
        sent[1],
        sent[2],
        "",
        received[0],
        received[1],
        received[2],
        "",
        "",
        "",
        "",
        value,
        "",
        "",
        "",
        note,
        note,
    ]


def test_high_confidence_pairs_become_transfers_and_can_be_unmatched(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    payload = _csv(
        [
            _line(
                "2025-07-01 10:00:00",
                "crypto_withdrawal",
                "Gift",
                "Wallet Alpha",
                "1",
                "ETH",
                "2000",
                "sample-out",
                "out",
            ),
            _line(
                "2025-07-01 11:00:00",
                "crypto_deposit",
                "",
                "Wallet Beta",
                "1",
                "ETH",
                "2000",
                "sample-in",
                "in",
            ),
            _line(
                "2025-08-01 10:00:00",
                "crypto_withdrawal",
                "Gift",
                "Wallet Alpha",
                "2",
                "ETH",
                "3000",
                "sample-late-out",
                "out",
            ),
            _line(
                "2025-08-02 22:00:00",
                "crypto_deposit",
                "",
                "Wallet Beta",
                "2",
                "ETH",
                "3000",
                "sample-late-in",
                "in",
            ),
        ]
    )
    desk.import_bytes("pairs.csv", payload)
    view = match_view(desk)
    assert len(view["matched"]) == 1
    assert view["suggested"]
    assert view["suggested"][0]["score"] < view["settings"]["auto_score"]
    matched = view["matched"][0]
    out_row = desk.transaction(matched["out_id"])
    in_row = desk.transaction(matched["in_id"])
    assert out_row["type"] == "Withdrawal"
    assert in_row["type"] == "Deposit"
    assert out_row["tag"] == "own_wallet_transfer"
    before = desk.report(2026)["summary"]["disposals"]
    decide(desk, matched["out_id"], matched["in_id"], "unmatch", "sam")
    restored = desk.transaction(matched["out_id"])
    assert restored["type"] == "Gift-Sent"
    after = desk.report(2026)["summary"]["disposals"]
    assert int(after) == int(before) + 1
    log = (desk.root / "events.jsonl").read_text(encoding="utf-8")
    assert "match-auto" in log
    assert "match-unmatch" in log


def test_fee_tolerance_and_same_wallet_are_respected(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    save_settings(desk.root, {"window_hours": "48", "fee_percent": "2", "auto_score": "80"})
    payload = _csv(
        [
            _line(
                "2025-07-03 10:00:00",
                "crypto_withdrawal",
                "",
                "Wallet Alpha",
                "1",
                "BTC",
                "1000",
                "fee-out",
                "out",
            ),
            _line(
                "2025-07-03 12:00:00",
                "crypto_deposit",
                "",
                "Wallet Beta",
                "0.99",
                "BTC",
                "1000",
                "fee-in",
                "in",
            ),
            _line(
                "2025-07-04 10:00:00",
                "crypto_withdrawal",
                "",
                "Wallet Alpha",
                "5",
                "BTC",
                "5000",
                "same-out",
                "out",
            ),
            _line(
                "2025-07-04 11:00:00",
                "crypto_deposit",
                "",
                "Wallet Alpha",
                "5",
                "BTC",
                "5000",
                "same-in",
                "in",
            ),
        ]
    )
    desk.import_bytes("fees.csv", payload)
    rows = desk.all_rows()
    pairs = propose(rows, {"window_hours": 48, "fee_percent": "2", "fee_abs": "0"})
    assets = {item["asset"] for item in pairs}
    assert "BTC" in assets
    assert all(item["out_wallet"] != item["in_wallet"] for item in pairs)


def test_accept_and_reject_are_on_the_page(tmp_path: Path) -> None:
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/matches", timeout=30) as response:
            page = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "Suggested matches" in page
    assert "Accept" in page
    assert "Reject" in page
    assert "Unmatch" in page
    assert "not a new tax rule" in page
