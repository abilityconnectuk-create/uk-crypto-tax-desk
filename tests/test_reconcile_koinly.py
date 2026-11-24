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
                "capital_gains": "100",
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
