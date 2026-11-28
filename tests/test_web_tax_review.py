# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Tax-review checks stay local, unconfirmed, and explained."""

from __future__ import annotations

import io
import threading
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Tuple
from urllib.request import urlopen

from bittytax.bt_types import AssetSymbol, DisposalType, TaxRules, Timestamp, TrType
from bittytax.constants import TZ_UTC
from bittytax.tax import TaxCalculator
from bittytax.tax_event import TaxEventCapitalGains
from bittytax.transactions import Buy, Sell
from bittytax.web.accountant_pack import build_zip
from bittytax.web.allocation_desk import apply_refund, link_manually, mark_negligible
from bittytax.web.disposal_match import in_thirty_day_window, matching_rule
from bittytax.web.errors import DeskError
from bittytax.web.fees import gain, gas_on_own_pool
from bittytax.web.futures import build_report
from bittytax.web.income_schedule import build_schedule
from bittytax.web.koinly_reconcile import hint_explanations, reconcile
from bittytax.web.losses import claim_deadline, deadline_warning, load_claims, save_claim, schedule
from bittytax.web.missing_cost_report import build_report as missing_report
from bittytax.web.missing_cost_report import in_window
from bittytax.web.pools import separate, stablecoins_merged
from bittytax.web.queues import export_rows, save_gap
from bittytax.web.rule_versions import history, record, refuse_remote_write
from bittytax.web.rules import load_rules
from bittytax.web.server import bind_server
from bittytax.web.spam_filter import (
    drop_rows,
    lookalike_dust,
    lookalike_token,
    read_log,
    restore_row,
)
from bittytax.web.store import Desk
from bittytax.web.valuation import YearEndRateError, annotate_rows, sterling_value

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"


def test_sterling_value_keeps_the_source_and_refuses_a_year_end_rate() -> None:
    valued = sterling_value(
        {
            "quantity": "2",
            "gbp_total": "100",
            "usd_total": "130",
            "fx_source": "event FX rate",
        }
    )
    assert valued["gbp"] == "100.00"
    assert valued["usd"] == "130.00"
    assert valued["price"] == "50"
    assert valued["fx_source"] == "event FX rate"
    assert valued["manual"] == "CRYPTO23000"
    assert valued["confirmed"] == "true"
    try:
        sterling_value(
            {"quantity": "1", "usd_total": "100", "fx_rate": "0.8", "year_end_rate": True}
        )
    except YearEndRateError as exc:
        assert "year-end" in str(exc)
    else:
        raise AssertionError("year-end conversion was accepted")
    rows = annotate_rows(
        [
            {
                "id": "1",
                "timestamp": "2025-08-01",
                "assets": ["BTC"],
                "sell_quantity": "1",
                "sell_value": "10000",
                "note": "USD 12500 FX 0.80 sample rate",
            }
        ]
    )
    assert rows[0]["usd"] == "12500.00"
    assert rows[0]["gbp"] == "10000.00"


def test_reconciliation_names_causes_and_flags_the_remainder() -> None:
    desk = {
        "disposals": Decimal(2),
        "proceeds": Decimal(100),
        "costs": Decimal(40),
        "gains": Decimal(80),
        "losses": Decimal(0),
        "income": Decimal(5),
    }
    koinly = {
        "disposals": Decimal(1),
        "proceeds": Decimal(90),
        "costs": Decimal(50),
        "gains": Decimal(40),
        "losses": Decimal(0),
        "income": Decimal(0),
    }
    hints = hint_explanations(desk, koinly, Decimal(30), Decimal(10))
    result = reconcile(desk, koinly, Decimal("0.01"), hints)
    by_metric = {row["metric"]: row for row in result["rows"]}
    assert by_metric["disposals"]["causes"][0]["cause"] == "matching difference"
    assert by_metric["disposals"]["unexplained"] is False
    assert by_metric["income"]["causes"][0]["cause"] == "rule"
    assert by_metric["proceeds"]["causes"][0]["cause"] == "manual override"
    assert any(item["cause"] == "missing cost" for item in by_metric["gains"]["causes"])
    assert any(item["cause"] == "unexplained" for item in by_metric["gains"]["causes"])
    assert result["unexplained"] is True
    assert result["confirmed"] is False


def test_missing_cost_report_does_not_set_worth_equal_to_cost() -> None:
    report = missing_report(
        [
            {
                "date": "2025-05-01",
                "asset": "DOGE",
                "quantity": "10",
                "cost": "0",
                "proceeds": "40",
                "gain": "40",
            },
            {
                "date": "2025-06-01",
                "asset": "BTC",
                "quantity": "1",
                "cost": "100",
                "proceeds": "150",
                "gain": "50",
            },
        ]
    )
    assert report["with_rows"] == "90.00"
    assert report["without_rows"] == "50.00"
    assert report["gap_effect"] == "40.00"
    assert report["worth_used_as_cost"] is False
    assert report["rows"][0]["cost"] == "0"
    assert report["rows"][0]["proceeds"] == "40"
    assert report["rows"][0]["worth_used_as_cost"] == "false"
    paired = missing_report(
        [
            {
                "date": "15/08/2024",
                "asset": "DOGE",
                "quantity": "100",
                "cost": "0",
                "proceeds": "40",
                "gain": "40",
            }
        ],
        [{"date": "2024-08-15", "asset": "DOGE", "quantity": "100", "proceeds": "40"}],
    )
    assert len(paired["rows"]) == 1
    kept = in_window(
        [{"date": "2024-08-15", "asset": "DOGE"}, {"date": "2021-06-01", "asset": "BTC"}],
        "2024-04-06",
        "2025-04-05",
    )
    assert [row["date"] for row in kept] == ["2024-08-15"]


def test_matching_order_crosses_the_tax_year() -> None:
    assert matching_rule("Same Day") == "same day"
    assert matching_rule("Bed & Breakfast (20/04/2025)") == "next 30 days"
    assert matching_rule("Section 104") == "section 104 pool"
    assert in_thirty_day_window(date(2025, 4, 1), date(2025, 4, 20)) is True
    assert in_thirty_day_window(date(2025, 4, 1), date(2025, 5, 2)) is False
    calculator = _finish(
        [
            _sell("ETH", "1", "150", (2025, 4, 1)),
            _buy("ETH", "1", "80", (2025, 4, 20)),
        ]
    )
    events = _gains(calculator)
    assert len(events) == 1
    assert events[0].disposal_type is DisposalType.BED_AND_BREAKFAST
    assert 2025 in calculator.tax_events
    assert events[0].date == date(2025, 4, 1)


def test_same_day_is_used_before_the_30_day_window() -> None:
    calculator = _finish(
        [
            _buy("ETH", "1", "100", (2025, 4, 1), 1),
            _sell("ETH", "1", "150", (2025, 4, 1)),
            _buy("ETH", "1", "80", (2025, 4, 10), 2),
        ]
    )
    events = _gains(calculator)
    assert events[0].disposal_type is DisposalType.SAME_DAY
    assert calculator.holdings[AssetSymbol("ETH")].quantity == Decimal(1)


def test_stablecoins_keep_separate_pools() -> None:
    calculator = _finish(
        [
            _buy("USDT", "2", "2", (2025, 1, 1), 1),
            _buy("USDC", "3", "3", (2025, 1, 2), 2),
            _buy("USDF", "4", "4", (2025, 1, 3), 3),
            _buy("RLUSD", "5", "5", (2025, 1, 4), 4),
        ]
    )
    names = [str(asset) for asset in calculator.holdings]
    assert separate(names) is True
    assert stablecoins_merged(names) is False
    assert stablecoins_merged(["USDT+USDC"]) is True
    assert calculator.holdings[AssetSymbol("USDT")].quantity == Decimal(2)
    assert calculator.holdings[AssetSymbol("USDC")].quantity == Decimal(3)


def test_fees_add_to_cost_and_gas_uses_its_own_pool() -> None:
    assert gain(Decimal(200), Decimal(100), Decimal(5), Decimal(2)) == Decimal(93)
    assert gas_on_own_pool("ETH", "BTC")["kind"] == "token disposal"
    assert gas_on_own_pool("ETH", "BTC")["pool"] == "ETH"
    calculator = _finish(
        [
            _with_fee(_buy("ETH", "1", "100", (2025, 1, 1)), "5"),
            _with_fee(_sell("ETH", "1", "200", (2025, 6, 1)), "2"),
        ]
    )
    event = _gains(calculator)[0]
    assert event.gain == Decimal("93.00")
    gas = _finish(
        [
            _buy("BTC", "1", "1000", (2025, 1, 1), 1),
            _buy("ETH", "1", "200", (2025, 1, 2), 2),
            _sell("ETH", "0.01", "2", (2025, 1, 3)),
        ]
    )
    assert gas.holdings[AssetSymbol("BTC")].quantity == Decimal(1)
    assert gas.holdings[AssetSymbol("ETH")].quantity == Decimal("0.99")


def test_income_schedule_checks_the_allowance_and_carries_cost() -> None:
    report = {
        "income": {
            "rows": [
                {
                    "date": "2025-07-01",
                    "asset": "ETH",
                    "income_type": "Staking-Reward",
                    "amount": "1000",
                    "note": "staking",
                },
                {
                    "date": "2025-07-02",
                    "asset": "USDC",
                    "income_type": "Interest",
                    "amount": "16",
                    "note": "lending",
                },
            ]
        }
    }
    rows = [
        {
            "type": "Airdrop",
            "date": "2025-07-03",
            "buy_asset": "SAMPLE",
            "buy_value": "25",
            "note": "income airdrop",
        }
    ]
    schedule_view = build_schedule(report, rows, 2026)
    assert schedule_view["allowance_deducted"] is True
    assert schedule_view["total"] == "1041.00"
    assert schedule_view["above_allowance"] is True
    assert schedule_view["box_17"] == "1041.00"
    assert schedule_view["box_18"] == "1000.00"
    assert schedule_view["taxable"] == "41.00"
    assert schedule_view["placement"].startswith("SA100")
    assert {row["token_cost"] for row in schedule_view["rows"]} == {"1000.00", "16.00", "25.00"}
    assert schedule_view["confirmed"] is True


def test_futures_comparison_does_not_change_the_capital_figure() -> None:
    rows = [
        {"type": "Margin-Gain", "buy_value": "25", "note": "perp", "date": "2025-08-01"},
        {"type": "Margin-Fee", "sell_value": "3", "note": "trading fee", "date": "2025-08-01"},
        {"type": "Margin-Fee", "sell_value": "1", "note": "funding fee", "date": "2025-08-02"},
    ]
    plain = build_report(rows, Decimal(100), False)
    compared = build_report(rows, Decimal(100), True)
    assert plain["realised"] == "25.00"
    assert plain["trading_fees"] == "3.00"
    assert plain["funding_fees"] == "1.00"
    assert plain["comparison_gains"] == "100.00"
    assert compared["comparison_gains"] == "125.00"
    assert compared["capital_gains"] == "100.00"
    assert compared["box_17"] == "25.00"
    assert compared["box_18"] == "4.00"
    assert compared["sa108_crypto"] is False
    assert compared["filed_changed"] is False
    assert "SA100" in compared["note"]


def test_spam_filter_logs_and_restores(tmp_path: Path) -> None:
    assert lookalike_token("USD\u0422") is True
    assert lookalike_token("USDT") is False
    known = ["0x1111111111111111111111111111111111111111"]
    poison = "0x1111000000000000000000000000000000001111"
    assert lookalike_dust(poison, known, Decimal("0.00001")) is True
    assert lookalike_dust(poison, known, Decimal(1)) is False
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    original = desk.all_rows()[0]
    assert drop_rows(desk, [original["id"]], "sam") == 1
    assert read_log(desk.root)[0]["reason"]
    restore_row(desk, original["id"], "sam")
    assert desk.transaction(original["id"])["type"] == original["type"]
    assert read_log(desk.root)[0]["restored"] == "yes"


def test_queues_allocations_and_versions(tmp_path: Path) -> None:
    save_gap(tmp_path, "Sample Venue", "open", "Closed to customers.", "sam")
    exported = export_rows(tmp_path, {"needs_review": [], "queue": []})
    assert exported[0]["venue"] == "Sample Venue"
    assert exported[0]["status"] == "open"
    try:
        link_manually(tmp_path, "", "claim-1", "sam")
    except DeskError:
        pass
    else:
        raise AssertionError("a blank payment was linked")
    linked = link_manually(tmp_path, "pay-1", "claim-1", "sam")
    assert linked["confirmed"] == "false"
    refunded = apply_refund("80", "30")
    assert refunded["proceeds"] == "30"
    assert refunded["shortfall_loss"] == "50"
    assert refunded["loss_posted"] == "true"
    assert refunded["stablecoin_acquired_at"] == "30"
    claim = mark_negligible(
        tmp_path, "SAMPLE", "sam", value="0", on_date="2026-06-01", pool_cost="40"
    )
    assert claim["status"] == "claimed"
    assert claim["claim_code"] == "NVC"
    assert claim["loss_posted"] == "true"
    assert claim["loss"] == "40"
    assert claim["whole_pool"] == "true"
    first = record(tmp_path, "sam", "Approved a manual override.", "override")
    second = record(tmp_path, "sam", "Approved another override.", "override")
    assert first["version"] == "1"
    assert second["version"] == "2"
    assert history(tmp_path)[0]["who"] == "sam"
    assert history(tmp_path)[0]["at"]
    try:
        refuse_remote_write("koinly")
    except DeskError as exc:
        assert "does not write to Koinly" in str(exc)
    else:
        raise AssertionError("a Koinly write was allowed")


def test_loss_deadlines_and_claim_status(tmp_path: Path) -> None:
    assert claim_deadline(2025) == "2029-04-05"
    assert deadline_warning("2026-04-05", date(2026, 11, 28)) == "past"
    assert deadline_warning("2027-01-15", date(2026, 11, 28)) == "near"
    assert deadline_warning("2030-04-05", date(2026, 11, 28)) == ""
    desk = Desk(tmp_path)
    save_claim(desk.root, "2026", "claimed")
    assert load_claims(desk.root)["2026"] == "claimed"
    assert not schedule(desk)
    rules = load_rules(2026)
    assert rules is not None
    assert rules["desk_review"]["matching"]["confirmed"] is True
    assert rules["desk_review"]["sterling"]["confirmed"] is True
    assert rules["desk_review"]["pooling"]["confirmed"] is True
    rebasing = next(tag for tag in rules["defi"]["tags"] if tag["id"] == "rebasing")
    assert rebasing["confirmed"] is False
    assert rebasing["accountant_review"] is True


def test_accountant_pack_contains_the_review_sections(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    blob = build_zip(desk, 2025)
    expected = {
        "cover.txt",
        "method-statement.txt",
        "disposal-schedule.csv",
        "pool-snapshots.csv",
        "income-schedule.csv",
        "koinly-reconciliation.csv",
        "missing-cost.csv",
        "known-gaps.csv",
        "open-questions.txt",
        "box-53-note.txt",
    }
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert expected <= set(archive.namelist())
        cover = archive.read("cover.txt").decode("utf-8")
        assert "Box 13.1" in cover
        assert "Box 45" in cover
        assert "Box 47" in cover
        assert "Box 51 inputs" in cover
        assert "Gains before the split" in cover
        assert "Gains on or after the split" in cover
        method = archive.read("method-statement.txt").decode("utf-8")
        assert "CRYPTO23000" in method
        assert "CRYPTO22200" in method
        assert "USDT" in method
        schedule_csv = archive.read("disposal-schedule.csv").decode("utf-8")
        assert "matching_rule" in schedule_csv
        note = archive.read("box-53-note.txt").decode("utf-8")
        assert "Box 53" in note
        assert "AWAITING TAX REVIEW" in cover


def test_review_pages_render(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        koinly = _get(port, "/koinly?tax_year=2026")
        missing = _get(port, "/missing-cost?tax_year=2026")
        matches = _get(port, "/matches?tax_year=2025")
        income = _get(port, "/income?tax_year=2026")
        losses = _get(port, "/losses")
        prices = _get(port, "/prices")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "Reconciliation" in koinly or "No Koinly file" in koinly
    assert "Box 13.1" in koinly
    assert "Missing cost" in missing
    assert "worth equal to cost" in missing
    assert "Disposal matching" in matches
    assert "same day" in matches
    assert "next 30 days" in matches
    assert "section 104 pool" in matches
    assert "Income" in income
    assert "Token cost" in income
    assert "trading allowance" in income.casefold() or "Trading allowance" in income
    assert "Four-year deadline" in losses
    assert "USD" in prices and "GBP" in prices


def test_no_module_calls_koinly() -> None:
    root = ROOT / "src" / "bittytax"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").casefold()
        assert "koinly.com" not in text
        assert "api.koinly" not in text


def _get(port: int, path: str) -> str:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=120) as response:
        return response.read().decode("utf-8")


def _finish(transactions: List[object]) -> TaxCalculator:
    calculator = TaxCalculator(transactions, TaxRules.UK_INDIVIDUAL)  # type: ignore[arg-type]
    calculator.pool_same_day()
    calculator.match_sell(DisposalType.SAME_DAY)
    calculator.match_buyback(DisposalType.BED_AND_BREAKFAST)
    calculator.process_section104(True)
    return calculator


def _gains(calculator: TaxCalculator) -> List[TaxEventCapitalGains]:
    found = []
    for year in calculator.tax_events.values():
        for event in year:
            if isinstance(event, TaxEventCapitalGains):
                found.append(event)
    return found


def _with_fee(trade: object, fee: str) -> object:
    trade.fee_value = Decimal(fee)  # type: ignore[attr-defined]
    return trade


def _buy(asset: str, quantity: str, cost: str, when: Tuple[int, int, int], serial: int = 0) -> Buy:
    year, month, day = when
    buy = Buy(TrType.TRADE, Decimal(quantity), AssetSymbol(asset), Decimal(cost))
    buy.timestamp = Timestamp(datetime(year, month, day, tzinfo=TZ_UTC))
    buy.tid = [year, month, day, serial]
    return buy


def _sell(
    asset: str, quantity: str, proceeds: str, when: Tuple[int, int, int], serial: int = 9
) -> Sell:
    year, month, day = when
    sell = Sell(TrType.SPEND, Decimal(quantity), AssetSymbol(asset), Decimal(proceeds))
    sell.timestamp = Timestamp(datetime(year, month, day, tzinfo=TZ_UTC))
    sell.tid = [year, month, day, serial]
    return sell
