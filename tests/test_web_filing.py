# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The ready-to-file page lists open items. It does not calculate tax."""

from __future__ import annotations

import io
import re
import shutil
import threading
import zipfile
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Dict
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from bittytax.web.accountant_pack import build_zip
from bittytax.web.errors import DeskError
from bittytax.web.filing import (
    CATEGORIES,
    accepted_gap_items,
    decision_items,
    items_csv,
    koinly_differences,
    load_threshold,
    loss_items,
    missing_items,
    readiness,
    review_items,
    save_threshold,
    summarise,
    transfer_items,
    unconfirmed_names,
)
from bittytax.web.queues import save_gap
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "examples" / "web-sample-review-koinly.csv"
WALLETS = ROOT / "examples" / "web-sample-wallets.yaml"
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
_CSRF = re.compile(r'name="csrf-token" content="([^"]*)"')
_TODAY = date(2026, 11, 28)
_BAND = Decimal("1")


def test_each_category_comes_from_synthetic_records() -> None:
    missing = missing_items(
        [{"date": "2025-06-04", "asset": "ETH", "cost": "0", "proceeds": "40"}], 2026
    )
    assert missing[0]["category"] == "missing_cost"
    assert missing[0]["href"] == "/missing-cost?tax_year=2026"
    assert missing[0]["blocking"] == "yes"

    transfers = transfer_items(
        [
            {
                "asset": "ETH",
                "score": 60,
                "reason": "same asset",
                "out_id": "out-1",
                "in_id": "in-1",
            }
        ],
        [
            {
                "asset": "BTC",
                "when": "2025-06-03 12:00:00",
                "status": "open",
                "detail": "No matching deposit.",
            },
            {"asset": "SOL", "when": "2025-06-03", "status": "closed", "detail": "Done."},
        ],
        2026,
        {"out-1": "2025-06-01 09:00:00"},
    )
    names = [item["name"] for item in transfers]
    assert any(name.startswith("ETH score") for name in names)
    assert any(name.startswith("BTC") for name in names)
    assert all("SOL" not in name for name in names)
    assert transfers[0]["href"] == "/matches?tax_year=2026"

    rules = {
        "defi": {
            "tags": [
                {
                    "id": "rebasing",
                    "label": "Rebasing receipt token",
                    "effect": "accountant_review",
                    "accountant_review": True,
                }
            ]
        }
    }
    reviewed = review_items(
        [{"tag": "rebasing", "timestamp": "2025-08-01", "buy_asset": "stETH", "id": "9"}],
        rules,
        [{"label": "Sample round", "asset": "TOKEN", "accountant_review": True, "refunded": "0"}],
        2026,
    )
    assert reviewed[0]["name"].startswith("Rebasing receipt token")
    assert reviewed[0]["href"] == "/defi?tax_year=2026"
    assert reviewed[1]["href"] == "/allocations"
    assert reviewed[1]["category"] == "accountant_review"

    named = unconfirmed_names(
        {
            "confirmed": False,
            "stub": True,
            "defi": {
                "confirmed": False,
                "tags": [
                    {"id": "rebasing", "label": "Rebasing receipt token", "confirmed": False},
                    {"id": "airdrop", "label": "Airdrop", "confirmed": True},
                ],
            },
            "desk_review": {
                "label": "awaiting tax review",
                "confirmed": False,
                "matching": {"label": "confirmed", "confirmed": False},
            },
        },
        "2024-25 rules",
    )
    assert named[0] == "2024-25 rules"
    assert "DeFi" in named
    assert "Rebasing receipt token" in named
    assert "Airdrop" not in named
    assert "matching" in named
    assert "confirmed" not in named

    differences = koinly_differences(
        [
            {"box": "13.1", "label": "Number of disposals", "value": "3"},
            {"box": "13.4", "label": "Gains", "value": "10.00"},
            {"box": "13.2", "label": "Disposal proceeds", "value": "5.00"},
        ],
        {"box_17": "4.00"},
        {
            "disposals": Decimal(2),
            "proceeds": Decimal("3.98"),
            "gains": Decimal("10.00"),
            "income": Decimal("1.00"),
        },
        _BAND,
        2026,
    )
    by_name = {item["name"]: item for item in differences}
    assert "SA108 box 13.1" in by_name
    assert "SA108 box 13.4" not in by_name
    assert "SA108 box 13.2" in by_name
    assert by_name["SA108 box 13.2"]["href"] == "/koinly?tax_year=2026"
    assert "Income box 17" in by_name
    assert "Difference 3.00" in by_name["Income box 17"]["detail"]

    losses = loss_items(
        [
            {
                "year": "2022",
                "gross_losses": "40.00",
                "claim_status": "unclaimed",
                "warning": "past",
                "deadline": "2026-04-05",
            },
            {
                "year": "2026",
                "gross_losses": "10.00",
                "claim_status": "unclaimed",
                "warning": "near",
                "deadline": "2030-04-05",
            },
            {
                "year": "2024",
                "gross_losses": "10.00",
                "claim_status": "claimed",
                "warning": "past",
                "deadline": "2028-04-05",
            },
            {
                "year": "2027",
                "gross_losses": "10.00",
                "claim_status": "unclaimed",
                "warning": "past",
                "deadline": "2031-04-05",
            },
        ],
        2026,
    )
    loss_names = [item["name"] for item in losses]
    assert loss_names == ["Unclaimed loss 2022", "Unclaimed loss 2026"]
    assert losses[0]["href"] == "/losses"

    waiting = decision_items(
        [
            {
                "asset": "DUST",
                "status": "open",
                "confirmed": "false",
                "claim_date": "2025-06-01",
            },
            {
                "asset": "OLD",
                "status": "claimed",
                "confirmed": "true",
                "claim_date": "2025-06-01",
            },
        ],
        [
            {"asset": "USDC", "refunded": "25", "linked": "no", "label": "Sample payee"},
            {"asset": "USDC", "refunded": "0", "linked": "no"},
        ],
        2026,
    )
    assert waiting[0]["name"] == "Negligible-value claim DUST"
    assert waiting[1]["name"] == "Refund USDC"
    assert all(item["category"] == "awaiting_decision" for item in waiting)
    assert all(item["href"] == "/allocations" for item in waiting)

    gaps = accepted_gap_items(
        [
            {
                "box": "13.7",
                "label": "Real Time Transaction result",
                "note": "The calculator does not store it.",
            }
        ],
        [{"venue": "Sample Venue", "note": "Closed to customers.", "status": "open"}],
        2026,
    )
    assert gaps[0]["kind"] == "accepted gap"
    assert gaps[0]["blocking"] == "no"
    assert "13.7" in gaps[0]["name"]
    assert gaps[1]["name"] == "Sample Venue"
    assert gaps[1]["href"] == "/queues?tax_year=2026"


def test_the_three_statuses() -> None:
    ready = summarise([], 2026, _BAND)
    assert ready["status"] == "Ready"
    assert ready["summary"] == "Nothing is open for this year."
    assert ready["blocking_count"] == 0
    assert ready["gap_count"] == 0
    assert [row["key"] for row in ready["counts"]] == [key for key, _label in CATEGORIES]
    assert all(row["count"] == 0 for row in ready["counts"])

    gaps_only = summarise(
        accepted_gap_items(
            [
                {
                    "box": "13.8",
                    "label": "Tax already paid",
                    "note": "The calculator does not store it.",
                }
            ],
            [],
            2026,
        ),
        2026,
        _BAND,
    )
    assert gaps_only["status"] == "Ready with accepted gaps"
    assert gaps_only["gap_count"] == 1
    assert gaps_only["blocking_count"] == 0

    blocked = summarise(
        missing_items([{"date": "2025-06-04", "asset": "ETH"}], 2026)
        + accepted_gap_items(
            [{"box": "13.7", "label": "Result", "note": "The calculator does not store it."}],
            [],
            2026,
        ),
        2026,
        _BAND,
    )
    assert blocked["status"] == "Not ready"
    assert blocked["blocking_count"] == 1
    assert blocked["gap_count"] == 1
    assert [row["count"] for row in blocked["counts"] if row["key"] == "missing_cost"] == [1]
    assert [row["count"] for row in blocked["counts"] if row["key"] == "accepted_gap"] == [1]


def test_the_threshold_defaults_to_one_pound_and_the_file_stays_binary(tmp_path: Path) -> None:
    assert load_threshold(tmp_path) == _BAND
    boxes = [{"box": "13.4", "label": "Gains", "value": "10.00"}]
    income = {"box_17": "2.00"}
    koinly = {"gains": Decimal("9.00"), "income": Decimal("1.00")}
    quiet = koinly_differences(boxes, income, koinly, _BAND, 2026)
    assert not quiet
    loud = koinly_differences(
        boxes,
        {"box_17": "2.02"},
        {"gains": Decimal("8.99"), "income": Decimal("1.00")},
        _BAND,
        2026,
    )
    assert {item["name"] for item in loud} == {"SA108 box 13.4", "Income box 17"}
    counted = koinly_differences(
        [{"box": "13.1", "value": "2"}],
        {},
        {"disposals": Decimal(1)},
        _BAND,
        2026,
    )
    assert counted[0]["name"] == "SA108 box 13.1"
    with pytest.raises(DeskError, match="positive"):
        save_threshold(tmp_path, "-1")
    save_threshold(tmp_path, "2.5")
    stored = (tmp_path / "ready-threshold.json").read_bytes()
    assert b"\r" not in stored
    assert load_threshold(tmp_path) == Decimal("2.50")


def test_readiness_uses_synthetic_year_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = tmp_path / "ruleset"
    monkeypatch.setenv("UK_CRYPTO_TAX_DESK_RULES", str(rules))
    desk = Desk(tmp_path / "data")
    ready = readiness(desk, 2025, _TODAY)
    assert ready["status"] == "Ready"
    assert ready["label"] == "2024/25"

    _yaml(
        rules / "sa108" / "2024-25.yaml",
        """
tax_year: "2024-25"
ending_year: 2025
confirmed: true
boxes:
  - box: "13.7"
    label: Real Time Transaction result
    confirmed: true
    note: The calculator does not store it.
""",
    )
    with_gaps = readiness(desk, 2025, _TODAY)
    assert with_gaps["status"] == "Ready with accepted gaps"
    assert any(item["kind"] == "accepted gap" for item in with_gaps["items"])

    _yaml(
        rules / "rules" / "2024-25.yaml",
        """
tax_year: "2024-25"
ending_year: 2025
confirmed: false
defi:
  confirmed: false
  tags:
    - id: rebasing
      label: Rebasing receipt token
      confirmed: false
      accountant_review: true
""",
    )
    blocked = readiness(desk, 2025, _TODAY)
    assert blocked["status"] == "Not ready"
    shown = [item["name"] for item in blocked["items"]]
    assert "2024-25 rules" in shown
    assert "DeFi" in shown
    assert "Rebasing receipt token" in shown


def test_readiness_reads_claims_refunds_and_known_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = tmp_path / "ruleset"
    monkeypatch.setenv("UK_CRYPTO_TAX_DESK_RULES", str(rules))
    _yaml(
        rules / "rules" / "2025-26.yaml",
        """
tax_year: "2025-26"
ending_year: 2026
confirmed: true
""",
    )
    desk = Desk(tmp_path / "data")
    (desk.root / "negligible-claims.json").write_bytes(
        b'[{"asset": "DUST", "status": "open", "confirmed": "false", '
        b'"claim_date": "2025-06-01"}]\n'
    )
    note = (
        "pending allocation | Sample round | payment pay-1 | asset USDC | "
        "gross 40 | refunded 25 | pending 15 | linked no"
    )
    (desk.records_path).write_bytes(f"Note\n{note}\n".encode("utf-8"))
    save_gap(desk.root, "Sample Venue", "open", "Closed to customers.", "sam")
    board = readiness(desk, 2026, _TODAY)
    names = [item["name"] for item in board["items"]]
    assert "Negligible-value claim DUST" in names
    assert "Refund USDC" in names
    assert "Sample Venue" in names
    assert board["status"] == "Not ready"
    venue = next(item for item in board["items"] if item["name"] == "Sample Venue")
    assert venue["kind"] == "accepted gap"


def test_the_ledger_feeds_missing_cost_transfers_and_koinly(tmp_path: Path) -> None:
    shutil.copy(WALLETS, tmp_path / "wallets.yaml")
    desk = Desk(tmp_path)
    desk.import_bytes(REVIEW.name, REVIEW.read_bytes())
    trade = next(row for row in desk.all_rows() if row["type"] == "Trade")
    desk.reclassify(trade["id"], "Trade", "Sample rebase row.", "sam", "rebasing")
    folder = desk.root / "koinly" / "2026"
    folder.mkdir(parents=True)
    (folder / "complete.csv").write_bytes(
        b"Type,Sent Currency,Cost,Proceeds,Gain\n"
        b"crypto_withdrawal,ETH,0,1,1\n"
        b"reward,ETH,0,80,80\n"
    )
    board = readiness(desk, 2026, _TODAY, _BAND)
    categories = {item["category"] for item in board["items"]}
    assert "missing_cost" in categories
    assert "transfer" in categories
    assert "accountant_review" in categories
    assert "koinly_difference" in categories
    assert any(item["name"].startswith("Rebasing") for item in board["items"])
    assert any("Income box 17" == item["name"] for item in board["items"])
    text = items_csv(board)
    assert "\r" not in text
    assert text.startswith("tax_year,status,category,name,detail,page,kind\n")
    assert "Missing cost" in text


def test_the_page_print_view_csv_nav_and_cover(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    blob = build_zip(desk, 2026)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        cover = archive.read("cover.txt").decode("utf-8")
    assert "Ready to file: Not ready." in cover

    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    browser = _Client(f"http://127.0.0.1:{port}")
    try:
        home = browser.get("/")
        page = browser.get("/ready?tax_year=2026")
        printed = browser.get("/ready/print?tax_year=2026")
        downloaded = browser.get_bytes("/ready.csv?tax_year=2026")
        browser.post("/ready/threshold", {"tax_year": "2026", "threshold": "5"})
        saved = browser.get("/ready?tax_year=2026")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()

    assert "Ready to file" in home
    assert "Open the checklist" in home
    assert "Not ready" in page
    assert "2024/25" in page
    assert "2025/26" in page
    assert "2026/27" in page
    assert "Print view" in page
    assert "Download CSV" in page
    assert "does not write to Koinly" in page
    assert "Save threshold" not in printed
    assert "Print view" in printed
    assert "Not ready" in printed
    assert "BittyTax" in printed
    assert "Affero" in printed
    assert b"\r" not in downloaded
    assert downloaded.startswith(b"tax_year,status,category,name,detail,page,kind\n")
    assert b"Not ready" in downloaded
    assert 'value="5.00"' in saved


def _yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.strip().encode("utf-8") + b"\n")


class _Client:
    """Sends the session cookie and the form token."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie = ""
        self.csrf = ""

    def get(self, path: str) -> str:
        return self._open(Request(self.base + path)).decode("utf-8")

    def get_bytes(self, path: str) -> bytes:
        return self._open(Request(self.base + path))

    def post(self, path: str, fields: Dict[str, str]) -> str:
        payload = dict(fields)
        payload["csrf"] = self.csrf
        body = urlencode(payload).encode("utf-8")
        request = Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._open(request).decode("utf-8")

    def _open(self, request: Request) -> bytes:
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.headers.get("Set-Cookie")
                if raw:
                    self.cookie = str(raw).split(";", 1)[0].strip()
                body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{exc.code} {detail}") from exc
        found = _CSRF.search(body.decode("utf-8", errors="replace"))
        if found:
            self.csrf = found.group(1)
        return body
