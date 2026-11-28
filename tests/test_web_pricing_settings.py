# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Pricing and settings. Sample figures only. No price service is contacted."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import urlopen

import pytest

from bittytax.web.backup import list_backups, restore_backup
from bittytax.web.desk_settings import (
    COST_BASIS,
    apply_confirmed_rules,
    convert_dated_amounts,
    feeds_sa108,
    format_clock,
    format_number,
    load_audit,
    load_settings,
    other_gains_display,
    page_model,
    recalculate,
    reports_are_stale,
    reset_data,
    rewrite_tax_csv,
    save_display,
    save_general,
    save_pricing,
    save_tax,
)
from bittytax.web.errors import DeskError
from bittytax.web.koinly_check import store_uploads
from bittytax.web.pack_sections import _cover
from bittytax.web.prices import lookup_gbp, price_transactions
from bittytax.web.sa108 import sa108_view
from bittytax.web.server import bind_server
from bittytax.web.store import Desk
from bittytax.web.tax_views import koinly_page
from bittytax.web.tx_store import import_prices
from bittytax.web.tx_workspace import prepare, read_filters, row_matches, save_row

ROOT = Path(__file__).resolve().parents[1]
HEALTH = (ROOT / "examples" / "web-sample-health-koinly.csv").read_bytes()
SAMPLE = (ROOT / "examples" / "web-sample-koinly.csv").read_bytes()
Quote = Callable[[str, str, str], Optional[Dict[str, str]]]


def test_custom_price_overrides_every_source(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    import_prices(desk.root, b"date,asset,gbp\n2025-05-01,DOGE,9\n")

    def quote(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del day, source
        if asset == "DOGE":
            raise AssertionError("a custom price must not ask a source")
        blank: Optional[Dict[str, str]] = None
        return blank

    price_transactions(desk, quote, _no_fx)
    doge = _asset(desk, "DOGE")
    assert Decimal(doge["buy_value"]) == Decimal("9")
    assert doge["value_source"] == "custom price"
    cached = json.loads((desk.root / "price-cache.json").read_bytes().decode("utf-8"))
    entry = cached["DOGE|2025-05-01"]
    assert entry["source"] == "custom price"
    assert entry["label"] == "custom price"
    assert entry["at"].endswith("Z")
    assert b"\r" not in (desk.root / "price-cache.json").read_bytes()


def test_source_order_falls_back_and_labels_the_row(tmp_path: Path, monkeypatch: Any) -> None:
    desk = _desk(tmp_path)
    save_pricing(desk.root, {"source_order": "CoinPaprika", "trusted_coins": "DOGE"}, "sam")
    calls: List[str] = []

    def quote(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del day
        if asset != "DOGE":
            return None
        calls.append(source)
        if source == "CoinGecko":
            return {"price": "4", "source": source}
        return None

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the price library was called")

    monkeypatch.setattr("bittytax.web.prices.lookup_gbp", boom)
    monkeypatch.setattr("bittytax.web.prices.quote_source", boom)
    price_transactions(desk, quote, _no_fx)
    assert calls[0] == "CoinPaprika"
    assert "CoinGecko" in calls
    doge = _asset(desk, "DOGE")
    assert Decimal(doge["buy_value"]) == Decimal("4")
    assert doge["value_source"] == "CoinGecko"
    chosen = read_filters({})
    chosen["value_source"] = "CoinGecko"
    assert row_matches(doge, chosen)
    chosen["value_source"] = "CoinPaprika"
    assert not row_matches(doge, chosen)
    warned = prepare(desk.all_rows(), Decimal(0), "utc")
    assert any(row["value_source"] == "CoinGecko" for row in warned)


def test_untrusted_and_low_liquidity_are_warnings(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    save_pricing(desk.root, {"source_order": "CoinGecko", "trusted_coins": "DOGE"}, "sam")

    def quote(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del day, source
        if asset == "PEPE":
            return {"price": "99", "source": "CoinGecko"}
        if asset == "DOGE":
            return {"price": "3", "source": "CoinGecko", "liquidity": "low"}
        return None

    result = price_transactions(desk, quote, _no_fx)
    pepe = _asset(desk, "PEPE")
    doge = _asset(desk, "DOGE")
    assert pepe["buy_value"] == ""
    assert doge["buy_value"] == ""
    assert pepe["id"] in result["warnings"]
    prepared = prepare(desk.all_rows(), Decimal(0), "utc", result["warnings"])
    flagged = {row["buy_asset"]: row["warnings"] for row in prepared}
    assert "unreliable_price" in flagged["PEPE"]
    assert "unreliable_price" in flagged["DOGE"]


def test_a_reliable_untrusted_price_is_kept(tmp_path: Path) -> None:
    desk = _desk(tmp_path)

    def quote(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del day
        if asset == "PEPE" and source == "CoinGecko":
            return {"price": "2", "source": source, "reliable": "yes"}
        return None

    price_transactions(desk, quote, _no_fx)
    pepe = _asset(desk, "PEPE")
    assert Decimal(pepe["buy_value"]) == Decimal("4.00")
    assert pepe["value_source"] == "CoinGecko"


def test_usd_price_is_converted_on_the_transaction_date(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    save_pricing(desk.root, {"source_order": "CryptoCompare", "trusted_coins": "DOGE"}, "sam")

    def quote(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del day
        if asset == "DOGE":
            return {"price": "10", "currency": "USD", "source": source, "reliable": "yes"}
        return None

    def fx(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del asset
        if source == "Frankfurter":
            return {"price": "0.80", "source": source}
        assert day == "2025-05-01"
        return None

    price_transactions(desk, quote, fx)
    doge = _asset(desk, "DOGE")
    assert Decimal(doge["buy_value"]) == Decimal("8.00")
    assert doge["value_source"] == "USD converted to GBP"
    cached = json.loads((desk.root / "price-cache.json").read_bytes().decode("utf-8"))
    assert cached["USD|2025-05-01"]["price"] == "0.80"
    assert cached["DOGE|2025-05-01"]["label"] == "USD converted to GBP"


def test_a_usd_total_on_the_row_uses_that_days_rate(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    _set_note(desk, "PEPE", "USD 4 sample")
    calls: List[str] = []

    def quote(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del day, source
        calls.append(asset)
        blank: Optional[Dict[str, str]] = None
        return blank

    def fx(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
        del asset, day
        if source == "Frankfurter":
            return {"price": "0.50", "source": "Frankfurter"}
        return None

    price_transactions(desk, quote, fx)
    pepe = _asset(desk, "PEPE")
    assert Decimal(pepe["buy_value"]) == Decimal("2.00")
    assert pepe["value_source"] == "USD converted to GBP"
    assert "PEPE" not in calls


def test_lookup_still_reads_the_library_stand_in(monkeypatch: Any) -> None:
    class Record:  # pylint: disable=too-few-public-methods
        price_ccy = Decimal("1.25")
        data_source = "sample-source"

    class Pricer:  # pylint: disable=too-few-public-methods
        def __init__(self, price_tool: bool = True) -> None:
            self.price_tool = price_tool

        def get_historical_price(self, asset: object, timestamp: object) -> Record:
            del asset, timestamp
            return Record()

    monkeypatch.setattr("bittytax.web.prices.ValueAsset", Pricer)
    found = lookup_gbp("DOGE", "2025-05-02")
    assert found is not None
    assert found["price"] == "1.25"


def test_general_settings_lock_and_cover(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes("sample.csv", SAMPLE)
    save_general(
        desk.root,
        {
            "base_currency": "GBP",
            "koinly_currency": "USD",
            "home_country": "UK",
            "timezone": "Europe/London",
            "report_language": "en-GB",
            "lock_before": "2025-01-01",
            "dust": "1.50",
            "name": "Ada Example",
            "utr": "1234567890",
        },
        "sam",
    )
    stored = load_settings(desk.root)
    assert stored["base_currency"] == "GBP"
    assert stored["home_country"] == "UK"
    assert stored["timezone"] == "Europe/London"
    assert stored["report_language"] == "en-GB"
    assert stored["utr"] == "1234567890"
    assert b"\r" not in (desk.root / "desk-settings.json").read_bytes()
    early = next(row for row in desk.all_rows() if row["date"] < "2025-01-01")
    later = next(row for row in desk.all_rows() if row["date"] >= "2025-01-01")
    with pytest.raises(DeskError, match="locked"):
        save_row(desk, early["id"], {"tag": "", "reviewed": "no"}, "sam")
    save_row(desk, later["id"], {"tag": "", "reviewed": "no"}, "sam")
    cover = _cover(None, {"summary": {}}, {}, 2026, desk)
    assert "Name: Ada Example" in cover
    assert "UTR: 1234567890" in cover
    with pytest.raises(DeskError, match="fixed"):
        save_general(desk.root, {"tax_year_start": "1 January", **_general()}, "sam")


def test_reset_requires_the_word_and_backs_up_first(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes("sample.csv", SAMPLE)
    with pytest.raises(DeskError, match="RESET"):
        reset_data(desk.root, "reset", "sam")
    assert (desk.root / "ledger.json").is_file()
    reset_data(desk.root, "RESET", "sam")
    assert not (desk.root / "ledger.json").is_file()
    backups = list_backups(desk.root)
    assert backups
    restore_backup(desk.root, backups[-1]["name"], "sam")
    assert (desk.root / "ledger.json").is_file()
    audit = load_audit(desk.root)
    assert any(item["key"] == "reset" and item["at"].endswith("Z") for item in audit)


def test_tax_rules_audit_and_stale_reports(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    save_tax(
        desk.root,
        {
            "income_airdrops": "on",
            "income_forks": "off",
            "income_mining": "on",
            "income_rewards": "on",
            "cashback_cost": "market",
            "cost_basis_method": COST_BASIS,
            "other_gains_as_capital": "yes",
        },
        "sam",
    )
    stored = load_settings(desk.root)
    assert stored["cashback_cost"] == "market"
    assert stored["income_airdrops"] == "on"
    assert stored["other_gains_as_capital"] is True
    assert reports_are_stale(desk.root)
    audit = load_audit(desk.root)
    cashback = next(item for item in audit if item["key"] == "cashback_cost")
    assert cashback["old"] == "nil"
    assert cashback["new"] == "market"
    assert cashback["at"].endswith("Z")
    with pytest.raises(DeskError, match="share pooling"):
        save_tax(desk.root, {"cost_basis_method": "fifo", "cashback_cost": "nil"}, "sam")
    switches = page_model(desk.root)["switches"]
    assert {item["status"] for item in switches} == {"unconfirmed"}
    assert {item["year"] for item in switches} == {"2024-25", "2025-26", "2026-27"}
    assert apply_confirmed_rules(desk.root, Path("missing.csv"), 2026) is None
    text = (
        "Type,Buy Quantity,Buy Asset,Buy Value,Sell Quantity,Sell Asset,Sell Value,"
        "Fee Quantity,Fee Asset,Fee Value,Wallet,Timestamp,Note\n"
        "Cashback,1,BTC,10,,,,,,,,w,2025-06-01 12:00:00 UTC,\n"
        "Airdrop,1,ETH,5,,,,,,,,w,2025-06-01 12:00:00 UTC,\n"
    )
    rewritten = rewrite_tax_csv(text, {"cashback_cost": "nil", "income_airdrops": "off"})
    assert "Cashback,1,BTC,0," in rewritten
    assert rewritten.splitlines()[2].startswith("Deposit,")
    report = {"summary": {"gain": "1"}, "disposals": []}
    before = sa108_view(report, 2026)
    after = sa108_view(report, 2026)
    assert before is not None and after is not None
    assert before["boxes"] == after["boxes"]
    assert feeds_sa108("other_gains_as_capital") is False
    shown = other_gains_display(
        [{"type": "Margin-Gain", "gain": "3"}], stored["other_gains_as_capital"]
    )
    assert shown["in_sa108"] == "no"
    assert shown["shown"] == "yes"

    class Quiet:
        def __init__(self, root: Path) -> None:
            self.root = root

        def all_rows(self) -> List[Dict[str, str]]:
            return [{"date": "2025-06-01", "id": "1"}]

        def report(self, year: int) -> Dict[str, int]:
            return {"year": year}

    result = recalculate(Quiet(desk.root), "sam")
    assert result["stale"] is False
    assert reports_are_stale(desk.root) is False


def test_a_failed_recalculate_leaves_reports_stale(tmp_path: Path) -> None:
    save_tax(tmp_path, {"cashback_cost": "market", "cost_basis_method": COST_BASIS}, "sam")

    class Boom:
        def __init__(self, root: Path) -> None:
            self.root = root

        def all_rows(self) -> List[Dict[str, str]]:
            return [{"date": "2025-06-01"}]

        def report(self, year: int) -> None:
            del year
            raise DeskError("A row has no value in the local currency.")

    result = recalculate(Boom(tmp_path), "sam")
    assert result["stale"] is True
    assert reports_are_stale(tmp_path)


def test_display_formats(tmp_path: Path) -> None:
    save_display(
        tmp_path,
        {"number_format": "1.234,56", "time_format": "12-hour", "compact_small": "yes"},
        "sam",
    )
    assert format_number("1234.5", "1.234,56") == "1.234,50"
    assert format_number("1234.5", "1 234.56") == "1 234.50"
    assert format_number("0.004", "1,234.56", compact=True) == "<0.01"
    assert format_clock("2025-06-01 15:04:00", "12-hour") == "2025-06-01 03:04 pm"
    assert format_clock("2025-06-01 15:04:00", "24-hour") == "2025-06-01 15:04"
    model = page_model(tmp_path)
    assert model["values"]["compact_small"] is True
    assert model["number_preview"] == "1.234,50"


def test_koinly_usd_rows_convert_on_their_date(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    complete = "Date,Type,Sent Currency,Gain,Net Value\n2025-06-01,Sell,DOGE,10,20\n"
    rates = {"2025-06-01": Decimal("0.80")}
    converted, count = convert_dated_amounts(complete, rates)
    assert count == 1
    assert "8.00" in converted
    assert "16.00" in converted
    untouched, skipped = convert_dated_amounts(complete, {})
    assert skipped == 0
    assert untouched == complete
    cache = {"USD|2025-06-01": {"price": "0.80", "source": "Frankfurter", "at": "t", "label": "x"}}
    (desk.root).mkdir(parents=True, exist_ok=True)
    (desk.root / "price-cache.json").write_bytes((json.dumps(cache) + "\n").encode("utf-8"))
    store_uploads(desk.root, 2026, b"", complete.encode("utf-8"))
    page = koinly_page(desk, 2026)
    assert page["conversion_label"] == "USD converted to GBP"
    assert feeds_sa108("other_gains_as_capital") is False


def test_settings_page_renders_each_tab(tmp_path: Path) -> None:
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/config", timeout=30) as response:
            page = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    for text in (
        "General",
        "Tax rules",
        "Transactions",
        "Display",
        "Base currency",
        "Home country",
        "6 April",
        "Europe/London",
        "en-GB",
        "Dust threshold",
        "UTR",
        "Type RESET",
        "Recalculate",
        "airdrops",
        "forks",
        "mining",
        "rewards",
        "Treat other gains as capital gains",
        "Cashback cost basis",
        "HMRC share pooling",
        "section 104",
        "unconfirmed",
        "Spam tokens",
        "Custom prices",
        "Source order",
        "Trusted coins",
        "Number format",
        "Time format",
        "Compact small amounts",
        "Settings audit",
        "AWAITING TAX REVIEW",
        "Loan drawdown",
    ):
        assert text in page
    assert 'action="/config"' not in page


def _desk(tmp_path: Path) -> Desk:
    desk = Desk(tmp_path)
    desk.import_bytes("health.csv", HEALTH)
    return desk


def _set_note(desk: Desk, symbol: str, note: str) -> None:
    path = desk.root / "ledger.json"
    ledger = json.loads(path.read_bytes())
    for stored in ledger["rows"]:
        cells = stored["cells"]
        if symbol in {cells[2], cells[5]}:
            cells[12] = note
    path.write_bytes((json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _asset(desk: Desk, symbol: str) -> Dict[str, Any]:
    return next(row for row in desk.all_rows() if row.get("buy_asset") == symbol)


def _no_fx(asset: str, day: str, source: str) -> Optional[Dict[str, str]]:
    del asset, day, source
    blank: Optional[Dict[str, str]] = None
    return blank


def _general() -> Dict[str, str]:
    return {
        "base_currency": "GBP",
        "koinly_currency": "USD",
        "home_country": "UK",
        "timezone": "Europe/London",
        "report_language": "en-GB",
        "lock_before": "",
        "dust": "0",
        "name": "",
        "utr": "",
    }
