import csv
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest

from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.constants import TZ_UTC
from bittytax.conv.bittytax_conv import main
from bittytax.conv.datafile import DataFile
from bittytax.conv.dataparser import DataParser
from bittytax.conv.datarow import DataRow, TxRawPos
from bittytax.conv.out_record import TransactionOutRecord
from bittytax.conv.parsers.koinly import parse_koinly
from bittytax.conv.transfer_match import (
    NEEDS_REVIEW_TYPE,
    ReconcileResult,
    reconcile_transfers,
    reconciliation_report_paths,
    write_reconciliation_reports,
)
from bittytax.conv.wallets import (
    MatchSettings,
    RegistryEntry,
    TokenGroup,
    WalletRegistry,
    WalletRegistryError,
    load_wallet_registry,
    override_match_settings,
)

LEDGER = "0x1111111111111111111111111111111111111111"
PAYEE = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
BASE = datetime(2024, 6, 1, 12, 0, tzinfo=TZ_UTC)

_BASE_COLUMNS = [
    "Date",
    "Type",
    "Sending Wallet",
    "Sent Amount",
    "Sent Currency",
    "Receiving Wallet",
    "Received Amount",
    "Received Currency",
    "Fee Amount",
    "Fee Currency",
    "TxSrc",
    "TxDest",
    "TxHash",
    "Chain",
    "Description",
]

_TAX_HEADER = [
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

_OUT_TYPES = {
    TrType.WITHDRAWAL,
    TrType.GIFT_SENT,
    TrType.SPEND,
    TrType.CHARITY_SENT,
    TrType.LOST,
}


def _settings(
    groups: Optional[Sequence[Sequence[str]]] = None,
    hours: int = 24,
    tolerance: str = "0.02",
    abs_tol: str = "0",
) -> MatchSettings:
    return MatchSettings(
        window=timedelta(hours=hours),
        amount_tolerance=Decimal(tolerance),
        amount_tolerance_abs=Decimal(abs_tol),
        token_groups=[
            TokenGroup(tuple(symbol.upper() for symbol in group)) for group in groups or []
        ],
    )


def _registry(
    own: Optional[Sequence[RegistryEntry]] = None,
    payees: Optional[Sequence[RegistryEntry]] = None,
    groups: Optional[Sequence[Sequence[str]]] = None,
    hours: int = 24,
    tolerance: str = "0.02",
    share_pools: bool = False,
) -> WalletRegistry:
    settings = _settings(groups=groups, hours=hours, tolerance=tolerance)
    settings.share_pools = share_pools
    return WalletRegistry(own=list(own or []), payees=list(payees or []), match=settings)


def _ledger() -> RegistryEntry:
    return RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum")


def _payee() -> RegistryEntry:
    return RegistryEntry(label="Example processor", address=PAYEE, chain="ethereum")


def _coinbase() -> RegistryEntry:
    return RegistryEntry(label="Coinbase", account="Coinbase")


def _make(
    t_type: TrType,
    when: datetime,
    asset: str,
    quantity: str,
    wallet: str,
    tag: str = "",
    destination: str = "",
    source: str = "",
    tx_hash: str = "",
    fee_quantity: Optional[str] = None,
    fee_asset: str = "",
    value: Optional[str] = None,
    include_tag: bool = True,
    chain: str = "",
) -> DataRow:
    columns = list(_BASE_COLUMNS)
    if include_tag:
        columns.insert(2, "Tag")
    values: Dict[str, str] = {column: "" for column in columns}
    if include_tag:
        values["Tag"] = tag
    values["TxDest"] = destination
    values["TxSrc"] = source
    values["TxHash"] = tx_hash
    values["Chain"] = chain
    if t_type in _OUT_TYPES:
        values["Sending Wallet"] = wallet
        values["Sent Amount"] = quantity
        values["Sent Currency"] = asset
    else:
        values["Receiving Wallet"] = wallet
        values["Received Amount"] = quantity
        values["Received Currency"] = asset
    if fee_quantity is not None:
        values["Fee Amount"] = fee_quantity
        values["Fee Currency"] = fee_asset

    raw = [values[column] for column in columns]
    data_row = DataRow(1, raw, columns, "test")
    data_row.timestamp = when
    data_row.parsed = True
    data_row.tx_raw = TxRawPos(
        columns.index("TxHash"), columns.index("TxSrc"), columns.index("TxDest")
    )
    fee_dec = Decimal(fee_quantity) if fee_quantity is not None else None
    value_dec = Decimal(value) if value is not None else None
    if t_type in _OUT_TYPES:
        data_row.t_record = TransactionOutRecord(
            t_type,
            when,
            sell_quantity=Decimal(quantity),
            sell_asset=asset,
            sell_value=value_dec,
            fee_quantity=fee_dec,
            fee_asset=fee_asset,
            wallet=wallet,
        )
    else:
        data_row.t_record = TransactionOutRecord(
            t_type,
            when,
            buy_quantity=Decimal(quantity),
            buy_asset=asset,
            buy_value=value_dec,
            fee_quantity=fee_dec,
            fee_asset=fee_asset,
            wallet=wallet,
        )
    return data_row


def _run(
    *rows: DataRow,
    registry: Optional[WalletRegistry] = None,
    legacy: bool = False,
    review_as: str = "reject",
) -> ReconcileResult:
    row_list = list(rows)
    return reconcile_transfers(
        row_list,
        registry or _registry(),
        legacy_gifts=legacy,
        review_as=review_as,
        containers=[row_list],
    )


def _actions(result: ReconcileResult) -> List[str]:
    return [row.action for row in result.summary]


def test_own_address_send_becomes_a_transfer() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1.25",
        "Exchange",
        destination=LEDGER.lower(),
        tx_hash="0xsynthetic",
        value="100",
    )
    result = _run(sent, registry=_registry(own=[_ledger()]))

    assert sent.t_record is not None
    assert sent.t_record.t_type is TrType.WITHDRAWAL
    assert sent.t_record.sell_value is None
    assert result.review == []
    assert _actions(result) == ["own-address"]
    assert result.summary[0].tx_hash == "0xsynthetic"
    assert result.summary[0].destination == LEDGER.lower()


def test_own_account_name_on_the_destination_is_a_transfer() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "2",
        "Ledger",
        destination="coinbase",
    )
    result = _run(sent, registry=_registry(own=[_coinbase()]))
    assert sent.t_record is not None
    assert sent.t_record.t_type is TrType.WITHDRAWAL
    assert _actions(result) == ["own-address"]


def test_source_wallet_is_not_treated_as_the_destination() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "2",
        "Coinbase",
        destination=OTHER,
        tx_hash="0xout",
    )
    result = _run(sent, registry=_registry(own=[_coinbase()]))
    assert sent.t_record is not None
    assert sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result) == ["needs-review"]


def test_spend_and_charity_to_own_address_are_transfers() -> None:
    spend = _make(TrType.SPEND, BASE, "ETH", "0.2", "Ledger", destination=LEDGER, tag="Cost")
    charity = _make(
        TrType.CHARITY_SENT,
        BASE,
        "ETH",
        "0.1",
        "Ledger",
        destination=LEDGER,
        tag="Donation",
    )
    _run(spend, charity, registry=_registry(own=[_ledger()]))
    assert spend.t_record is not None and spend.t_record.t_type is TrType.WITHDRAWAL
    assert charity.t_record is not None and charity.t_record.t_type is TrType.WITHDRAWAL


def test_lost_to_own_address_stays_lost() -> None:
    lost = _make(TrType.LOST, BASE, "ETH", "1", "Ledger", destination=LEDGER, tag="Lost")
    result = _run(lost, registry=_registry(own=[_ledger()]))
    assert lost.t_record is not None
    assert lost.t_record.t_type is TrType.LOST
    assert result.summary == []


def test_matched_pair_becomes_a_transfer_and_carries_the_link() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1.5", "Exchange", value="200", tx_hash="0xmove")
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(minutes=30),
        "ETH",
        "1.5",
        "Ledger",
        value="200",
        tx_hash="0xmove",
    )
    result = _run(received, sent)

    assert sent.t_record is not None and received.t_record is not None
    assert sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record.t_type is TrType.DEPOSIT
    assert sent.t_record.sell_value is None
    assert received.t_record.buy_value is None
    assert sent.t_record.sell_quantity == Decimal("1.5")
    assert "transfer match 1" in sent.t_record.note
    assert "transfer match 1" in received.t_record.note
    assert _actions(result) == ["matched"]


def test_shortfall_within_tolerance_is_recorded_as_the_fee() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange")
    received = _make(TrType.GIFT_RECEIVED, BASE + timedelta(minutes=5), "ETH", "0.99", "Ledger")
    _run(sent, received)
    assert sent.t_record is not None
    assert sent.t_record.t_type is TrType.WITHDRAWAL
    assert sent.t_record.sell_quantity == Decimal("0.99")
    assert sent.t_record.fee_quantity == Decimal("0.01")
    assert sent.t_record.fee_asset == "ETH"


def test_gross_withdrawal_is_netted_when_the_fee_was_already_recorded() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Exchange",
        fee_quantity="0.01",
        fee_asset="ETH",
    )
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.99", "Ledger")
    _run(sent, received)
    assert sent.t_record is not None
    assert sent.t_record.sell_quantity == Decimal("0.99")
    assert sent.t_record.fee_quantity == Decimal("0.01")
    assert sent.t_record.fee_asset == "ETH"


def test_amount_inside_tolerance_matches_and_outside_does_not() -> None:
    close_out = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange")
    close_in = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.985", "Ledger")
    close = _run(close_out, close_in)
    assert close_out.t_record is not None and close_out.t_record.t_type is TrType.WITHDRAWAL
    assert _actions(close) == ["matched"]

    far_out = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="0xfar")
    far_in = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.90", "Ledger")
    far = _run(far_out, far_in)
    assert far_out.t_record is not None and far_out.t_record.t_type == NEEDS_REVIEW_TYPE
    assert far_in.t_record is not None and far_in.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(far) == ["needs-review", "needs-review"]


def test_time_window_accepts_the_boundary_and_rejects_past_it() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "BTC", "0.5", "Exchange")
    on_time = _make(TrType.GIFT_RECEIVED, BASE + timedelta(hours=24), "BTC", "0.5", "Cold")
    result = _run(sent, on_time)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert _actions(result) == ["matched"]

    early = _make(TrType.GIFT_SENT, BASE, "BTC", "0.5", "Exchange", tx_hash="0xlate")
    late = _make(
        TrType.GIFT_RECEIVED,
        BASE - timedelta(hours=24, seconds=1),
        "BTC",
        "0.5",
        "Cold",
    )
    missed = _run(early, late)
    assert early.t_record is not None and early.t_record.t_type == NEEDS_REVIEW_TYPE
    assert late.t_record is not None and late.t_record.t_type == NEEDS_REVIEW_TYPE
    assert missed.review[0].disposition == "needs-review"
    assert missed.review[0].tx_hash == "0xlate"


def test_deposit_before_withdrawal_still_matches() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "3", "Exchange")
    received = _make(TrType.GIFT_RECEIVED, BASE - timedelta(hours=2), "ETH", "3", "Ledger")
    _run(sent, received)
    assert received.t_record is not None and received.t_record.t_type is TrType.DEPOSIT


def test_same_wallet_does_not_match() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="0xsame")
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Exchange")
    result = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result) == ["needs-review", "needs-review"]


def test_token_variant_pairs_only_when_configured() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "WETH", "2", "Exchange")
    received = _make(TrType.GIFT_RECEIVED, BASE + timedelta(minutes=10), "ETH", "2", "Ledger")
    missed = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(missed) == ["needs-review", "needs-review"]

    grouped_out = _make(TrType.GIFT_SENT, BASE, "WETH", "2", "Exchange")
    grouped_in = _make(TrType.GIFT_RECEIVED, BASE, "cbBTC", "2", "Ledger")
    # A symbol from the other group must not pair.
    wrong = _run(
        grouped_out, grouped_in, registry=_registry(groups=[["ETH", "WETH"], ["BTC", "CBBTC"]])
    )
    assert grouped_out.t_record is not None and grouped_out.t_record.t_type == NEEDS_REVIEW_TYPE
    assert grouped_in.t_record is not None and grouped_in.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(wrong) == ["needs-review", "needs-review"]

    eth_out = _make(TrType.GIFT_SENT, BASE, "WETH", "2", "Exchange", tx_hash="0xwrap")
    eth_in = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "2", "Ledger", tx_hash="0xwrap")
    paired = _run(eth_out, eth_in, registry=_registry(groups=[["ETH", "WETH"], ["BTC", "cbBTC"]]))
    assert eth_out.t_record is not None and eth_out.t_record.t_type is TrType.WITHDRAWAL
    assert eth_in.t_record is not None and eth_in.t_record.t_type is TrType.DEPOSIT
    assert eth_out.t_record.sell_asset == "WETH"
    assert eth_in.t_record.buy_asset == "ETH"
    assert _actions(paired) == ["matched"]


def test_unmatched_withdrawal_is_review_not_a_gift() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "4",
        "Exchange",
        destination=OTHER,
        tx_hash="0xreview",
    )
    result = _run(sent)
    assert sent.t_record is not None
    assert sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert len(result.review) == 1
    item = result.review[0]
    assert item.disposition == "needs-review"
    assert item.asset == "ETH"
    assert item.quantity == Decimal("4")
    assert item.wallet == "Exchange"
    assert item.destination == OTHER
    assert item.tx_hash == "0xreview"
    assert item.timestamp == BASE


def test_legacy_flag_keeps_the_gift() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "4", "Exchange", destination=OTHER, tx_hash="0xold")
    result = _run(sent, legacy=True)
    assert sent.t_record is not None
    assert sent.t_record.t_type is TrType.GIFT_SENT
    assert result.review[0].disposition == "kept-as-gift"
    assert _actions(result) == ["legacy-gift"]


def test_payee_send_stays_a_disposal_even_when_a_deposit_matches() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Exchange",
        destination=PAYEE.upper(),
        tx_hash="0xpay",
        value="50",
    )
    received = _make(TrType.GIFT_RECEIVED, BASE + timedelta(minutes=1), "ETH", "1", "Somewhere")
    result = _run(sent, received, registry=_registry(payees=[_payee()]))
    assert sent.t_record is not None and sent.t_record.t_type is TrType.GIFT_SENT
    assert sent.t_record.sell_value == Decimal("50")
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result) == ["payee-disposal", "needs-review"]


def test_explicit_gift_is_not_matched_or_sent_to_review() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tag="Gift", destination=OTHER)
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Friend", tag="Gift")
    result = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.GIFT_SENT
    assert received.t_record is not None and received.t_record.t_type is TrType.GIFT_RECEIVED
    assert result.summary == []
    assert result.review == []


def test_explicit_gift_to_own_address_is_still_a_transfer() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tag="Gift", destination=LEDGER)
    result = _run(sent, registry=_registry(own=[_ledger()]))
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert _actions(result) == ["own-address"]


def test_chain_mismatch_does_not_count_as_own_address() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Exchange",
        destination=LEDGER,
        chain="bitcoin",
        tx_hash="0xchain",
    )
    result = _run(sent, registry=_registry(own=[_ledger()]))
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result) == ["needs-review"]


def test_gift_without_a_tag_column_is_left_alone() -> None:
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Exchange",
        destination=OTHER,
        include_tag=False,
    )
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Ledger", include_tag=False)
    result = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.GIFT_SENT
    assert received.t_record is not None and received.t_record.t_type is TrType.GIFT_RECEIVED
    assert result.summary == []


def test_existing_withdrawal_and_deposit_keep_their_quantities() -> None:
    sent = _make(
        TrType.WITHDRAWAL,
        BASE,
        "ETH",
        "1",
        "Exchange",
        fee_quantity="0.01",
        fee_asset="ETH",
        include_tag=False,
    )
    received = _make(TrType.DEPOSIT, BASE, "ETH", "0.99", "Ledger", include_tag=False)
    result = _run(sent, received)
    assert sent.t_record is not None and received.t_record is not None
    assert sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record.t_type is TrType.DEPOSIT
    assert sent.t_record.sell_quantity == Decimal("1")
    assert sent.t_record.fee_quantity == Decimal("0.01")
    assert received.t_record.buy_quantity == Decimal("0.99")
    assert _actions(result) == ["matched"]
    assert result.review == []


def test_closest_amount_beats_a_shared_tx_hash() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="0xhash")
    closer = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Ledger", tx_hash="")
    hashed = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.99", "Cold", tx_hash="0xHASH")
    _run(sent, closer, hashed)
    assert closer.t_record is not None and closer.t_record.t_type is TrType.DEPOSIT
    assert hashed.t_record is not None and hashed.t_record.t_type == NEEDS_REVIEW_TYPE
    assert sent.t_record is not None and sent.t_record.sell_quantity == Decimal("1")


def test_trade_rows_are_ignored() -> None:
    trade = _make(TrType.TRADE, BASE, "ETH", "1", "Exchange")
    # A trade is not an in or out leg. Give it both sides so a careless classifier could grab it.
    assert trade.t_record is not None
    trade.t_record.buy_quantity = Decimal("1")
    trade.t_record.buy_asset = "BTC"
    result = _run(trade)
    assert trade.t_record.t_type is TrType.TRADE
    assert result.summary == []


def test_report_files_list_the_review_columns(tmp_path: Path) -> None:
    sent = _make(
        TrType.GIFT_SENT, BASE, "ETH", "4", "Exchange", destination=OTHER, tx_hash="0xreview"
    )
    result = _run(sent)
    summary_path = tmp_path / "out-transfer-summary.csv"
    review_path = tmp_path / "out-needs-review.csv"
    write_reconciliation_reports(result, str(summary_path), str(review_path))

    with open(review_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == [
        "Date",
        "Asset",
        "Amount",
        "Wallet",
        "Destination",
        "TxHash",
        "Description",
        "Disposition",
    ]
    assert rows[1][1:] == ["ETH", "4", "Exchange", OTHER, "0xreview", "", "needs-review"]

    stem, review, spam = reconciliation_report_paths(str(tmp_path / "records.xlsx"))
    assert stem.endswith("records-transfer-summary.csv")
    assert review.endswith("records-needs-review.csv")
    assert spam.endswith("records-possible-spam.csv")


def test_example_registry_loads() -> None:
    registry = load_wallet_registry("examples/wallets.example.yaml")
    assert len(registry.own) == 3
    assert registry.own[0].address == LEDGER
    assert registry.payees[0].label == "Example processor"
    assert registry.payees[1].description == "card purchase"
    assert registry.payees[2].asset == "BTC"
    assert registry.match.window == timedelta(hours=24)
    assert registry.match.amount_tolerance == Decimal("0.02")
    assert registry.match.share_pools is False
    assert registry.match.token_groups[0].symbols == ("ETH", "WETH")
    assert registry.match.token_groups[1].symbols == ("BTC", "CBBTC")


def test_registry_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "wallets.yaml"
    path.write_text("own:\n  - label: X\n    addr: '0x1'\n", encoding="utf-8")
    with pytest.raises(WalletRegistryError):
        load_wallet_registry(str(path))


def test_override_window_and_tolerance() -> None:
    settings = MatchSettings()
    override_match_settings(settings, window_hours="12", amount_tolerance="0.05")
    assert settings.window == timedelta(hours=12)
    assert settings.amount_tolerance == Decimal("0.05")
    with pytest.raises(WalletRegistryError):
        override_match_settings(settings, amount_tolerance="-1")


def test_koinly_tax_report_own_address_is_rewritten() -> None:
    config.ccy = "GBP"
    parser = DataParser.match_header(_TAX_HEADER, 0)
    assert parser.all_handler is parse_koinly
    values: Dict[str, str] = {column: "" for column in _TAX_HEADER}
    values.update(
        {
            "Date": "2024-06-01 12:00:00",
            "Type": "crypto_withdrawal",
            "Sending Wallet": "Exchange",
            "Sent Amount": "1.25",
            "Sent Currency": "ETH",
            "Net Value (GBP)": "100",
            "TxDest": LEDGER,
            "TxHash": "0xparsed",
            "Description": "synthetic",
        }
    )
    data_row = DataRow(1, [values[column] for column in _TAX_HEADER], parser.in_header, "Koinly")
    parse_koinly([data_row], parser)
    assert data_row.t_record is not None
    parsed_type = data_row.t_record.t_type
    assert parsed_type is TrType.GIFT_SENT

    result = reconcile_transfers([data_row], _registry(own=[_ledger()]))
    assert data_row.t_record.t_type is TrType.WITHDRAWAL
    assert data_row.t_record.wallet == "Exchange"
    assert result.review == []
    assert result.summary[0].action == "own-address"
    assert result.summary[0].tx_hash == "0xparsed"


def test_cli_writes_transfers_and_the_review_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.ccy = "GBP"
    DataFile.data_files.clear()
    DataFile.data_files_ordered.clear()
    try:
        wallet_path = tmp_path / "wallets.yaml"
        wallet_path.write_text(
            "own:\n  - label: Ledger ETH\n    chain: ethereum\n" f'    address: "{LEDGER}"\n',
            encoding="utf-8",
        )
        source = tmp_path / "koinly.csv"
        with open(source, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(_TAX_HEADER)
            writer.writerow(
                [
                    "2024-06-01 12:00:00",
                    "crypto_withdrawal",
                    "",
                    "Exchange",
                    "1.25",
                    "ETH",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "100",
                    "",
                    "",
                    LEDGER,
                    "0xown",
                    "synthetic own send",
                ]
            )
            writer.writerow(
                [
                    "2024-06-02 12:00:00",
                    "crypto_withdrawal",
                    "",
                    "Exchange",
                    "0.4",
                    "ETH",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "40",
                    "",
                    "",
                    OTHER,
                    "0xunknown",
                    "synthetic external send",
                ]
            )
        output = tmp_path / "records.csv"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "bittytax_conv",
                str(source),
                "--format",
                "CSV",
                "--wallets",
                str(wallet_path),
                "-o",
                str(output),
            ],
        )
        main()

        with open(output, encoding="utf-8", newline="") as handle:
            records = list(csv.DictReader(handle))
        types = [row["Type"] for row in records]
        assert types == ["Withdrawal", NEEDS_REVIEW_TYPE]

        review_path = tmp_path / "records-needs-review.csv"
        with open(review_path, encoding="utf-8", newline="") as handle:
            review = list(csv.DictReader(handle))
        assert len(review) == 1
        assert review[0]["Asset"] == "ETH"
        assert review[0]["Amount"] == "0.4"
        assert review[0]["Wallet"] == "Exchange"
        assert review[0]["Destination"] == OTHER
        assert review[0]["TxHash"] == "0xunknown"
        assert review[0]["Disposition"] == "needs-review"
    finally:
        DataFile.data_files.clear()
        DataFile.data_files_ordered.clear()


def test_unmatched_deposit_is_review_not_a_gift() -> None:
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE,
        "ETH",
        "2",
        "Ledger",
        tx_hash="0xin",
    )
    received.row_dict["Description"] = "from somewhere"
    result = _run(received)
    assert received.t_record is not None
    assert received.t_record.t_type == NEEDS_REVIEW_TYPE
    assert result.review[0].disposition == "needs-review"
    assert result.review[0].description == "from somewhere"
    assert result.review[0].wallet == "Ledger"


def test_legacy_flag_keeps_an_unmatched_deposit() -> None:
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "2", "Ledger")
    result = _run(received, legacy=True)
    assert received.t_record is not None
    assert received.t_record.t_type is TrType.GIFT_RECEIVED
    assert result.review[0].disposition == "kept-as-gift"


def test_payee_matches_description_when_destination_is_blank() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "0.4", "Exchange", destination="")
    assert sent.t_record is not None
    sent.t_record.note = "Card purchase coffee"
    sent.row_dict["Description"] = "Card purchase coffee"
    payee = RegistryEntry(label="Card", description="card purchase")
    result = _run(sent, registry=_registry(payees=[payee]))
    assert sent.t_record.t_type is TrType.GIFT_SENT
    assert _actions(result) == ["payee-disposal"]
    assert result.review == []


def test_payee_matches_wallet_and_asset_when_destination_is_blank() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "BTC", "0.2", "Example Exchange", destination="")
    payee = RegistryEntry(label="Cash-out", wallet="Example Exchange", asset="BTC")
    result = _run(sent, registry=_registry(payees=[payee]))
    assert sent.t_record is not None and sent.t_record.t_type is TrType.GIFT_SENT
    assert _actions(result) == ["payee-disposal"]


def test_blank_destination_without_a_payee_rule_is_still_reviewed() -> None:
    sent = _make(
        TrType.GIFT_SENT, BASE, "ETH", "0.4", "Exchange", destination="", tx_hash="0xblank"
    )
    sent.row_dict["Description"] = "to an external account"
    result = _run(sent)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert result.review[0].destination == ""
    assert result.review[0].description == "to an external account"
    assert result.review[0].tx_hash == "0xblank"


def test_mirror_movement_is_paired_only_once() -> None:
    out_a = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "WalletA")
    in_b = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "WalletB")
    out_b = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "WalletB")
    in_a = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "WalletA")
    result = _run(out_a, in_b, out_b, in_a)
    assert _actions(result).count("matched") == 1
    assert out_a.t_record is not None and out_a.t_record.t_type is TrType.WITHDRAWAL
    assert in_b.t_record is not None and in_b.t_record.t_type is TrType.DEPOSIT
    assert out_b.t_record is not None and out_b.t_record.t_type == NEEDS_REVIEW_TYPE
    assert in_a.t_record is not None and in_a.t_record.t_type == NEEDS_REVIEW_TYPE


def test_review_as_transfer_writes_a_transfer_instead_of_needs_review() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", destination=OTHER)
    result = _run(sent, review_as="transfer")
    assert sent.t_record is not None
    assert sent.t_record.t_type is TrType.WITHDRAWAL
    assert "review row treated as transfer" in sent.t_record.note
    assert result.review[0].disposition == "assumed-transfer"
    assert _actions(result) == ["assumed-transfer"]


def test_share_pools_turns_a_wrap_into_a_transfer_of_the_canonical_asset() -> None:
    trade = _make(TrType.TRADE, BASE, "WETH", "1", "Wallet")
    assert trade.t_record is not None
    trade.t_record.sell_asset = "WETH"
    trade.t_record.sell_quantity = Decimal("1")
    trade.t_record.buy_asset = "ETH"
    trade.t_record.buy_quantity = Decimal("1")
    later = _make(TrType.SPEND, BASE + timedelta(days=1), "WBTC", "0.5", "Wallet", tag="Cost")
    rows = [trade, later]
    result = reconcile_transfers(
        rows,
        _registry(groups=[["ETH", "WETH"], ["BTC", "WBTC"]], share_pools=True),
        containers=[rows],
    )
    assert trade.t_record.t_type is TrType.WITHDRAWAL
    assert trade.t_record.sell_asset == "ETH"
    assert trade.t_record.buy_asset == ""
    assert len(rows) == 3
    deposit = rows[1].t_record
    assert deposit is not None and deposit.t_type is TrType.DEPOSIT
    assert deposit.buy_asset == "ETH"
    assert deposit.buy_quantity == Decimal("1")
    assert later.t_record is not None and later.t_record.sell_asset == "BTC"
    assert "wrap" in _actions(result)


def test_share_pools_leaves_a_wrap_as_a_trade_when_disabled() -> None:
    trade = _make(TrType.TRADE, BASE, "WETH", "1", "Wallet")
    assert trade.t_record is not None
    trade.t_record.sell_asset = "WETH"
    trade.t_record.sell_quantity = Decimal("1")
    trade.t_record.buy_asset = "ETH"
    trade.t_record.buy_quantity = Decimal("1")
    _run(trade, registry=_registry(groups=[["ETH", "WETH"]], share_pools=False))
    assert trade.t_record.t_type is TrType.TRADE
    assert trade.t_record.sell_asset == "WETH"
    assert trade.t_record.buy_asset == "ETH"


def test_spam_symbols_are_separated_from_needs_review() -> None:
    real = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", destination=OTHER)
    spam = _make(TrType.GIFT_SENT, BASE, "SCAMCLAIM", "9", "Exchange", destination=OTHER)
    registry = _registry()
    registry.spam_symbols = ["SCAMCLAIM"]
    registry.spam_patterns = ["claim"]
    result = _run(real, spam, registry=registry)
    assert real.t_record is not None and real.t_record.t_type == NEEDS_REVIEW_TYPE
    assert spam.t_record is not None and str(spam.t_record.t_type) == "_Possible-Spam"
    assert len(result.review) == 1
    assert result.review[0].asset == "ETH"
    assert len(result.spam) == 1
    assert result.spam[0].asset == "SCAMCLAIM"
    assert result.spam[0].disposition == "possible-spam"


def test_payee_rule_can_target_one_transaction() -> None:
    by_hash = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="0xabc")
    lookalike = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="0xother")
    by_amount = _make(TrType.GIFT_SENT, BASE, "BTC", "0.5", "Exchange")
    other_amount = _make(TrType.GIFT_SENT, BASE, "BTC", "0.6", "Exchange")
    registry = _registry(
        payees=[
            RegistryEntry(label="Hashed", tx_hash="0xABC"),
            RegistryEntry(label="Dated", on_date="2024-06-01", asset="BTC", amount="0.5"),
        ]
    )
    result = _run(by_hash, lookalike, by_amount, other_amount, registry=registry)
    assert by_hash.t_record is not None and by_hash.t_record.t_type is TrType.GIFT_SENT
    assert by_amount.t_record is not None and by_amount.t_record.t_type is TrType.GIFT_SENT
    assert lookalike.t_record is not None and lookalike.t_record.t_type == NEEDS_REVIEW_TYPE
    assert other_amount.t_record is not None and other_amount.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result).count("payee-disposal") == 2


def test_liquid_staking_group_keeps_the_original_symbol_in_the_note() -> None:
    trade = _make(TrType.TRADE, BASE, "stETH", "0.9", "Wallet")
    assert trade.t_record is not None
    trade.t_record.sell_asset = "ETH"
    trade.t_record.sell_quantity = Decimal("1")
    trade.t_record.buy_asset = "stETH"
    trade.t_record.buy_quantity = Decimal("0.9")
    rows = [trade]
    registry = _registry(share_pools=True)
    registry.match.token_groups = [TokenGroup(("ETH", "STETH"), liquid=True)]
    result = reconcile_transfers(rows, registry, containers=[rows])
    assert trade.t_record.t_type is TrType.TRADE
    assert trade.t_record.sell_asset == "ETH"
    assert trade.t_record.buy_asset == "ETH"
    assert "liquid staking rebase ETH/stETH into ETH" in trade.t_record.note
    assert "liquid-stake" in _actions(result)
