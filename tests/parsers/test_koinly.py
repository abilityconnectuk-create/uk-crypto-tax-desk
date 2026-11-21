from decimal import Decimal
from typing import Dict, List

import pytest

from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.conv.dataparser import DataParser
from bittytax.conv.datarow import DataRow
from bittytax.conv.parsers.koinly import parse_koinly, parse_koinly_bulk_edit

config.ccy = "GBP"

# Header of the Koinly "Bulk edit in Excel" transactions export. Currency and wallet cells carry
# a ";<id>" suffix and the value is held in "Net Value (read-only)", see:
# https://support.koinly.io/en/articles/9490043-bulk-edit-in-excel
HEADER = [
    "ID (read-only)",
    "Parent ID (read-only)",
    "Date (UTC)",
    "Type",
    "Tag",
    "From Wallet (read-only)",
    "From Wallet ID",
    "From Amount",
    "From Currency",
    "To Wallet (read-only)",
    "To Wallet ID",
    "To Amount",
    "To Currency",
    "Fee Amount",
    "Fee Currency",
    "Net Worth Amount",
    "Net Worth Currency",
    "Fee Worth Amount",
    "Fee Worth Currency",
    "Net Value (read-only)",
    "Fee Value (read-only)",
    "Value Currency (read-only)",
    "Deleted",
    "From Source (read-only)",
    "To Source (read-only)",
    "Negative Balances (read-only)",
    "Missing Rates (read-only)",
    "Missing Cost Basis (read-only)",
    "Synced To Accounting At (UTC read-only)",
    "TxSrc",
    "TxDest",
    "TxHash",
    "Description",
]


def _parse(**cells: str) -> DataRow:
    parser = DataParser.match_header(HEADER, 0)
    assert parser.row_handler is parse_koinly_bulk_edit

    values: Dict[str, str] = {col: "" for col in HEADER}
    values.update(cells)
    row: List[str] = [values[col] for col in HEADER]

    data_row = DataRow(1, row, parser.in_header, "Koinly")
    parse_koinly_bulk_edit(data_row, parser)
    return data_row


def test_deposit_reward() -> None:
    data_row = _parse(
        **{
            "Date (UTC)": "2025-01-03 22:19:40",
            "Type": "deposit",
            "Tag": "reward",
            "To Wallet (read-only)": "Flare (FLR);flare",
            "To Amount": "8121.9422723224",
            "To Currency": "WFLR;9546698",
            "Net Value (read-only)": "224.7162634542",
            "Value Currency (read-only)": "GBP;1",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.STAKING_REWARD
    assert data_row.t_record.buy_quantity == Decimal("8121.9422723224")
    assert data_row.t_record.buy_asset == "WFLR"
    assert data_row.t_record.buy_value == Decimal("224.7162634542")
    assert data_row.t_record.wallet == "Flare (FLR)"


def test_trade() -> None:
    data_row = _parse(
        **{
            "Date (UTC)": "2025-02-01 10:00:00",
            "Type": "trade",
            "From Amount": "1",
            "From Currency": "BTC;1",
            "To Amount": "15",
            "To Currency": "ETH;2",
            "Net Value (read-only)": "50000",
            "Value Currency (read-only)": "GBP",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.TRADE
    assert data_row.t_record.buy_quantity == Decimal("15")
    assert data_row.t_record.buy_asset == "ETH"
    assert data_row.t_record.sell_quantity == Decimal("1")
    assert data_row.t_record.sell_asset == "BTC"


def test_withdrawal_cost() -> None:
    data_row = _parse(
        **{
            "Date (UTC)": "2025-02-02 10:00:00",
            "Type": "withdrawal",
            "Tag": "Cost",
            "From Amount": "0.5",
            "From Currency": "ETH;2",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.SPEND
    assert data_row.t_record.sell_quantity == Decimal("0.5")
    assert data_row.t_record.sell_asset == "ETH"


def test_untagged_deposit_is_transfer() -> None:
    data_row = _parse(
        **{
            "Date (UTC)": "2025-02-05 10:00:00",
            "Type": "deposit",
            "To Amount": "2",
            "To Currency": "ADA;5",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.DEPOSIT


def test_unknown_tag_is_unmapped() -> None:
    data_row = _parse(
        **{
            "Date (UTC)": "2025-02-07 10:00:00",
            "Type": "deposit",
            "Tag": "SomeTag",
            "To Amount": "1",
            "To Currency": "XYZ;9",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == "_SomeTag"


TAX_HEADER = [
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


def _parse_tax_report(**cells: str) -> DataRow:
    parser = DataParser.match_header(TAX_HEADER, 0)
    assert parser.all_handler is parse_koinly

    values: Dict[str, str] = {col: "" for col in TAX_HEADER}
    values.update(cells)
    row: List[str] = [values[col] for col in TAX_HEADER]
    data_row = DataRow(1, row, parser.in_header, "Koinly")
    parse_koinly([data_row], parser)
    return data_row


def test_tax_report_untagged_withdrawal_stays_gift_sent() -> None:
    # The parser itself keeps the historical mapping. Transfer matching is a later stage.
    data_row = _parse_tax_report(
        **{
            "Date": "2024-06-01 12:00:00",
            "Type": "crypto_withdrawal",
            "Sending Wallet": "Exchange",
            "Sent Amount": "1.5",
            "Sent Currency": "ETH",
            "Net Value (GBP)": "100",
            "TxDest": "0x3333333333333333333333333333333333333333",
            "TxHash": "0xabc",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.GIFT_SENT
    assert data_row.t_record.sell_quantity == Decimal("1.5")
    assert data_row.t_record.sell_asset == "ETH"
    assert data_row.t_record.wallet == "Exchange"


def test_tax_report_untagged_deposit_stays_gift_received() -> None:
    data_row = _parse_tax_report(
        **{
            "Date": "2024-06-01 13:00:00",
            "Type": "crypto_deposit",
            "Receiving Wallet": "Ledger",
            "Received Amount": "1.5",
            "Received Currency": "ETH",
            "Net Value (GBP)": "100",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.GIFT_RECEIVED
    assert data_row.t_record.buy_quantity == Decimal("1.5")
    assert data_row.t_record.buy_asset == "ETH"


def test_thousands_separators_are_parsed() -> None:
    data_row = _parse_tax_report(
        **{
            "Date": "2024-06-01 12:00:00",
            "Type": "crypto_withdrawal",
            "Sending Wallet": "Exchange",
            "Sent Amount": "1,234.56",
            "Sent Currency": "ETH",
            "Fee Amount": "1,000",
            "Fee Currency": "ETH",
            "Net Value (GBP)": "2,000.50",
            "TxDest": "0x3333333333333333333333333333333333333333",
        }
    )
    assert data_row.t_record is not None
    assert data_row.failure is None
    assert data_row.t_record.sell_quantity == Decimal("1234.56")
    assert data_row.t_record.fee_quantity == Decimal("1000")
    assert data_row.t_record.sell_value == Decimal("2000.50")


def test_local_currency_value_stays_equal_to_the_quantity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        DataParser,
        "convert_currency",
        lambda _value, _currency, _timestamp: Decimal("99.01"),
    )
    fiat = _parse_tax_report(
        **{
            "Date": "2024-06-01 12:00:00",
            "Type": "fiat_withdrawal",
            "Sending Wallet": "Bank",
            "Sent Amount": "100",
            "Sent Currency": "GBP",
            "Net Value (GBP)": "127.40",
        }
    )
    assert fiat.t_record is not None
    assert fiat.t_record.sell_quantity == Decimal("100")
    assert fiat.t_record.sell_value == Decimal("100")

    crypto = _parse_tax_report(
        **{
            "Date": "2024-06-01 12:00:00",
            "Type": "crypto_withdrawal",
            "Sending Wallet": "Exchange",
            "Sent Amount": "2",
            "Sent Currency": "ETH",
            "Net Value (GBP)": "127.40",
        }
    )
    assert crypto.t_record is not None
    assert crypto.t_record.sell_value == Decimal("99.01")


def test_liquidity_fee_and_multi_trade_tags() -> None:
    added = _parse_tax_report(
        **{
            "Date": "2024-06-01 12:00:00",
            "Type": "crypto_withdrawal",
            "Tag": "Liquidity in",
            "Sending Wallet": "Exchange",
            "Sent Amount": "3",
            "Sent Currency": "ETH",
        }
    )
    assert added.t_record is not None
    assert added.t_record.t_type is TrType.SPEND
    assert "Koinly tag: Liquidity in" in added.t_record.note

    removed = _parse_tax_report(
        **{
            "Date": "2024-06-02 12:00:00",
            "Type": "crypto_deposit",
            "Tag": "Liquidity out",
            "Receiving Wallet": "Exchange",
            "Received Amount": "3",
            "Received Currency": "ETH",
        }
    )
    assert removed.t_record is not None
    assert removed.t_record.t_type is TrType.GIFT_RECEIVED

    fee = _parse_tax_report(
        **{
            "Date": "2024-06-03 12:00:00",
            "Type": "crypto_withdrawal",
            "Tag": "Other fee",
            "Sending Wallet": "Exchange",
            "Sent Amount": "0.01",
            "Sent Currency": "ETH",
        }
    )
    assert fee.t_record is not None
    assert fee.t_record.t_type is TrType.SPEND

    multi = _parse_tax_report(
        **{
            "Date": "2024-06-04 12:00:00",
            "Type": "crypto_withdrawal",
            "Tag": "Multi trade",
            "Sending Wallet": "Exchange",
            "Sent Amount": "1",
            "Sent Currency": "ETH",
            "Received Amount": "2",
            "Received Currency": "BTC",
        }
    )
    assert multi.t_record is not None
    assert multi.t_record.t_type is TrType.TRADE
    assert multi.t_record.sell_asset == "ETH"
    assert multi.t_record.buy_asset == "BTC"


def test_value_falls_back_to_net_worth() -> None:
    data_row = _parse(
        **{
            "Date (UTC)": "2025-02-04 10:00:00",
            "Type": "deposit",
            "Tag": "reward",
            "To Amount": "5",
            "To Currency": "DOT;4",
            "Net Worth Amount": "200",
            "Net Worth Currency": "GBP",
        }
    )
    assert data_row.t_record is not None
    assert data_row.t_record.buy_value == Decimal("200")
