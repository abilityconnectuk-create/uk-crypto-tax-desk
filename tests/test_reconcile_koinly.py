import csv
import json
from decimal import Decimal
from pathlib import Path

from bittytax.reconcile_koinly import build_report


def _write(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerows(rows)


def _pack(folder: Path, eth_quantity: str = "2") -> None:
    _write(
        folder / "transaction_history.csv",
        [
            [
                "Date",
                "Type",
                "Sending Wallet",
                "Sent Amount",
                "Sent Currency",
                "Receiving Wallet",
                "Received Amount",
                "Received Currency",
                "Tag",
            ],
            ["2024-06-01 12:00:00", "crypto_deposit", "", "", "", "Ledger", "1", "ETH", ""],
            ["2024-06-02 12:00:00", "transfer", "Exchange", "1", "ETH", "Ledger", "1", "ETH", ""],
            ["2024-06-03 12:00:00", "crypto_deposit", "", "", "", "Example Bot", "1", "ETH", ""],
        ],
    )
    _write(
        folder / "end_of_year_holdings.csv",
        [
            ["Balances as at 5 April 2025 23:59"],
            ["Currency", "Amount", "Cost"],
            ["ETH", eth_quantity, "2000"],
            ["DOGE", "0.000000001", "0"],
        ],
    )
    _write(
        folder / "income.csv",
        [["Date", "Income"], ["2024-06-01", "10"]],
    )
    _write(
        folder / "other_gains.csv",
        [["Date", "Gain"], ["2024-08-01", "5"]],
    )
    _write(
        folder / "capital_gains.csv",
        [["Date", "Gain", "Missing cost"], ["2024-09-01", "100", "9"]],
    )


def _engine(path: Path) -> None:
    _write(
        path,
        [
            ["Type", "Wallet", "Timestamp", "Note"],
            ["Deposit", "Ledger", "2024-06-01T12:00:00 UTC", ""],
            ["Withdrawal", "Exchange", "2024-06-02T12:00:00 UTC", ""],
            ["Deposit", "Ledger", "2024-06-02T12:00:00 UTC", ""],
        ],
    )


def _snapshot(path: Path, quantity: str = "2") -> None:
    payload = {
        "currency": "USD",
        "timezone": "Europe/London",
        "review_rows": "conservative",
        "pools": {
            "2025": {
                "as_of": "2025-04-05",
                "assets": [
                    {"asset": "ETH", "quantity": quantity, "cost": "2000", "fees": "0"},
                ],
            }
        },
        "years": {
            "2025": {
                "income": "10",
                "margin_net": "5",
                "capital_gains": "95",
                "missing_cost": "3",
            }
        },
        "timezone_flags": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wallets(path: Path) -> None:
    path.write_text(
        "count_exceptions:\n"
        "  - reason: sample bot wallet excluded from the count\n"
        "    wallet: Example Bot\n"
        "official_assets: [USDT, BTC, ETH]\n"
        "match:\n"
        "  share_pools: false\n",
        encoding="utf-8",
    )


def test_reconciliation_passes_and_y4_does_not_fail_the_report(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _pack(koinly)
    records = tmp_path / "records.csv"
    snapshot = tmp_path / "snapshot.json"
    wallets = tmp_path / "wallets.yaml"
    _engine(records)
    _snapshot(snapshot)
    _wallets(wallets)
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
        wallets_path=str(wallets),
        review_rows="conservative",
    )
    assert report["passed"] is True
    assert report["review_rows"] == "conservative"
    assert report["checks"]["Y4"]["affects_result"] is False
    assert report["checks"]["Y4"]["rows"][0]["engine_missing_cost"] == "3"
    assert Decimal(report["checks"]["Y4"]["rows"][0]["koinly_missing_cost"]) == Decimal("9")
    assert report["count_exceptions"][0]["wallet"] == "Example Bot"
    assert report["share_pools"] is False


def test_reconciliation_names_a_failing_pool_quantity(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _pack(koinly, eth_quantity="3")
    records = tmp_path / "records.csv"
    snapshot = tmp_path / "snapshot.json"
    wallets = tmp_path / "wallets.yaml"
    _engine(records)
    _snapshot(snapshot, quantity="2")
    _wallets(wallets)
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
        wallets_path=str(wallets),
    )
    assert report["passed"] is False
    assert "P1" in report["failing_checks"]
    failure = report["checks"]["P1"]["failures"][0]
    assert failure["asset"] == "ETH"
    assert failure["koinly"] == "3"
    assert failure["engine"] == "2"


def test_iso_holdings_title_is_5_april_not_4_may(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _write(
        koinly / "holdings.csv",
        [
            ["Balances as at 2025-04-05 23:59"],
            ["Currency", "Amount", "Cost"],
            ["BTC", "1", "10"],
        ],
    )
    _write(koinly / "transaction_history.csv", [["Date", "Type", "Wallet"]])
    records = tmp_path / "records.csv"
    _write(records, [["Type", "Wallet", "Timestamp", "Note"]])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps({"pools": {"2025": {"as_of": "2025-04-05", "assets": []}}, "years": {}}),
        encoding="utf-8",
    )
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
    )
    holding = report["holdings_as_of"][0]
    assert holding["as_of"] == "2025-04-05"
    assert holding["tax_year"] == "2025"
    assert holding["tax_year_label"] == "2024/25"


def test_official_name_maps_bitcoin_and_keeps_a_lookalike_apart(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _write(
        koinly / "holdings.csv",
        [
            ["Balances as at 5 April 2025 23:59"],
            ["Currency", "Amount", "Cost"],
            ["BTC (Bitcoin)", "1", "10"],
            ["BTC (Bear Trump Coin)", "1", "4"],
        ],
    )
    _write(koinly / "transaction_history.csv", [["Date", "Type", "Wallet"]])
    records = tmp_path / "records.csv"
    _write(records, [["Type", "Wallet", "Timestamp", "Note"]])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "pools": {
                    "2025": {
                        "as_of": "2025-04-05",
                        "assets": [{"asset": "BTC", "quantity": "1", "cost": "10", "fees": "0"}],
                    }
                },
                "years": {},
            }
        ),
        encoding="utf-8",
    )
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
    )
    failed = {row["asset"] for row in report["checks"]["P1"]["failures"]}
    assert "BTC" not in failed
    assert "BTC (Bear Trump Coin)" in failed

    wallets = tmp_path / "wallets.yaml"
    wallets.write_text("asset_names:\n  BTC:\n    - Satoshi\n", encoding="utf-8")
    overridden = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
        wallets_path=str(wallets),
    )
    overridden_assets = {row["asset"] for row in overridden["checks"]["P1"]["failures"]}
    assert "BTC (Bitcoin)" in overridden_assets


def test_income_uses_value_before_token_amount(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _write(koinly / "transaction_history.csv", [["Date", "Type", "Wallet"]])
    _write(
        koinly / "income.csv",
        [["Date", "Amount", "Value (USD)"], ["2024-06-01", "1000", "10"]],
    )
    records = tmp_path / "records.csv"
    _write(records, [["Type", "Wallet", "Timestamp", "Note"]])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps({"pools": {}, "years": {"2025": {"income": "10", "margin_net": "0"}}}),
        encoding="utf-8",
    )
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
    )
    assert report["checks"]["Y1"]["passed"] is True


def test_net_gains_include_futures_with_margin(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _write(koinly / "transaction_history.csv", [["Date", "Type", "Wallet"]])
    _write(koinly / "capital_gains.csv", [["Date", "Gain"], ["2024-09-01", "100"]])
    _write(koinly / "other_gains.csv", [["Date", "Gain"], ["2024-08-01", "40"]])
    records = tmp_path / "records.csv"
    _write(records, [["Type", "Wallet", "Timestamp", "Note"]])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "pools": {},
                "years": {"2025": {"capital_gains": "60", "margin_net": "40", "income": "0"}},
            }
        ),
        encoding="utf-8",
    )
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
    )
    assert report["checks"]["Y3"]["passed"] is True
    assert report["checks"]["Y2"]["passed"] is True
    assert "futures" in report["checks"]["Y3"]["definition"]


def test_missing_cost_comes_from_indicator_or_blank_cost(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _write(koinly / "transaction_history.csv", [["Date", "Type", "Wallet"]])
    _write(
        koinly / "capital_gains.csv",
        [
            [
                "Date",
                "Asset",
                "Cost basis (USD)",
                "Proceeds (USD)",
                "Gain (USD)",
                "Missing cost basis",
            ],
            ["2024-09-01", "ETH", "0", "40", "40", "Yes"],
            ["2024-10-01", "BTC", "100", "150", "50", "No"],
            ["2024-11-01", "SOL", "", "7", "7", "Yes"],
            ["2024-12-01", "DOGE", "0", "9", "9", "No"],
        ],
    )
    records = tmp_path / "records.csv"
    _write(records, [["Type", "Wallet", "Timestamp", "Note"]])
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps({"pools": {}, "years": {"2025": {"missing_cost": "0", "capital_gains": "0"}}}),
        encoding="utf-8",
    )
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
    )
    row = report["checks"]["Y4"]["rows"][0]
    assert Decimal(row["koinly_missing_cost"]) == Decimal(47)
    assert report["checks"]["Y4"]["affects_result"] is False
    assert "proceeds" in report["checks"]["Y4"]["definition"]
    assert "Y4" not in report["failing_checks"]


def test_counts_follow_the_converter_for_transfers_income_and_margin(tmp_path: Path) -> None:
    koinly = tmp_path / "koinly"
    koinly.mkdir()
    _write(
        koinly / "transaction_history.csv",
        [
            [
                "Date",
                "Type",
                "Tag",
                "Sending Wallet",
                "Sent Amount",
                "Receiving Wallet",
                "Received Amount",
            ],
            ["2024-06-01", "transfer", "", "Exchange", "1", "Ledger;99", "1"],
            ["2024-06-02", "crypto_deposit", "Reward", "", "", "Ledger;99", "2"],
            ["2024-06-03", "crypto_withdrawal", "Realized gain", "Exchange", "3", "", ""],
            ["2024-06-04", "buy", "", "Exchange", "4", "Exchange", "1"],
        ],
    )
    _write(
        koinly / "bulk_edit.csv",
        [
            [
                "Date (UTC)",
                "Type",
                "Tag",
                "From Wallet (read-only)",
                "From Amount",
                "To Wallet (read-only)",
                "To Amount",
            ],
            ["2024-06-05", "transfer", "", "Exchange", "1", "Cold", "1"],
        ],
    )
    records = tmp_path / "records.csv"
    _write(
        records,
        [
            ["Type", "Wallet"],
            ["Withdrawal", "Exchange"],
            ["Deposit", "Ledger"],
            ["Staking-Reward", "Ledger"],
            ["Margin-Loss", "Exchange"],
            ["Trade", "Exchange"],
            ["Trade", "Cold"],
        ],
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"pools": {}, "years": {}}), encoding="utf-8")
    report = build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
    )
    assert report["checks"]["T1"]["passed"] is True, report["checks"]["T1"]
    assert report["checks"]["T2"]["passed"] is True, report["checks"]["T2"]
    assert "To wallet" in " ".join(report["notes"])
