from datetime import datetime, timedelta
from decimal import Decimal

from test_transfer_match import BASE, LEDGER, OTHER, _make, _registry, _run

from bittytax.audit import AuditRecords
from bittytax.bittytax import _do_integrity_check
from bittytax.bt_types import AssetSymbol, TaxRules, Timestamp, TrType
from bittytax.config import config
from bittytax.constants import TZ_UTC
from bittytax.conv.transfer_match import NEEDS_REVIEW_TYPE
from bittytax.conv.wallets import RegistryEntry
from bittytax.review_queue import QueueItem, ReviewException
from bittytax.t_row import TransactionRow
from bittytax.tax import TaxCalculator
from bittytax.transactions import Buy

ROUND = "0x" + "cd" * 32


def _buy(quantity: str, cost: str, when: datetime, t_type: TrType = TrType.TRADE) -> Buy:
    buy = Buy(t_type, Decimal(quantity), AssetSymbol("ETH"), Decimal(cost))
    buy.timestamp = Timestamp(when)
    buy.tid = [int(when.timestamp()), 0]
    return buy


def test_round_trip_does_not_inflate_the_5_april_pool() -> None:
    registry = _registry(own=[RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum")])
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Exchange",
        destination=LEDGER,
        tx_hash=ROUND,
    )
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(minutes=20),
        "ETH",
        "1",
        "Ledger ETH",
        tx_hash=ROUND.upper(),
    )
    extra = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(days=30),
        "ETH",
        "0.4",
        "Ledger ETH",
        value="400",
    )
    leftover = _make(
        TrType.GIFT_SENT,
        BASE + timedelta(days=40),
        "ETH",
        "0.5",
        "Exchange",
        destination=OTHER,
        tx_hash="0x" + "11" * 16,
    )
    result = _run(sent, received, extra, leftover, registry=registry, review_as="conservative")
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record is not None and received.t_record.t_type is TrType.DEPOSIT
    assert extra.t_record is not None and extra.t_record.t_type is TrType.REVIEW_ACQUISITION
    assert leftover.t_record is not None and leftover.t_record.t_type == NEEDS_REVIEW_TYPE
    assert result.queue and result.queue[0].disposition == "review-queue"

    previous = config.ccy
    config.ccy = "USD"
    try:
        opening = _buy("2", "2000", datetime(2024, 6, 1, 12, tzinfo=TZ_UTC))
        acquired = _buy(
            "0.4",
            "400",
            datetime(2024, 7, 1, 12, tzinfo=TZ_UTC),
            TrType.REVIEW_ACQUISITION,
        )
        later = _buy("0.01", "10", datetime(2025, 6, 1, 12, tzinfo=TZ_UTC))
        calculator = TaxCalculator([opening, acquired, later], TaxRules.UK_INDIVIDUAL)
        calculator.pool_same_day()
        calculator.process_section104(True)
        snapshot = calculator.snapshot_payload("conservative")
    finally:
        config.ccy = previous

    pool = snapshot["pools"]["2025"]["assets"]
    eth = next(row for row in pool if row["asset"] == "ETH")
    assert Decimal(eth["quantity"]) == Decimal("2.4")
    assert Decimal(eth["cost"]) == Decimal("2400")
    assert snapshot["currency"] == "USD"
    assert calculator.holdings[AssetSymbol("ETH")].quantity == Decimal("2.41")


def test_unmatched_withdrawal_fails_integrity_until_it_is_excepted() -> None:
    item_hash = "0x" + "22" * 16
    TransactionRow.review_queue = []
    config.config["review_exception_rows"] = []
    try:
        TransactionRow.review_queue = [
            QueueItem(
                timestamp="2024-08-01T12:00:00",
                asset="ETH",
                quantity="0.5",
                wallet="Exchange",
                tx_hash=item_hash,
                note="unmatched withdrawal",
                disposition="review-queue",
            )
        ]
        assert _do_integrity_check(AuditRecords([]), {}) is False
        config.config["review_exception_rows"] = [
            ReviewException(reason="sample leftover checked by hand", tx_hash=item_hash)
        ]
        assert _do_integrity_check(AuditRecords([]), {}) is True
    finally:
        TransactionRow.review_queue = []
        config.config["review_exception_rows"] = []


def test_timestamp_near_5_april_midnight_is_flagged() -> None:
    previous = config.ccy
    config.ccy = "USD"
    try:
        buy = _buy("1", "100", datetime(2025, 4, 5, 23, 30, tzinfo=TZ_UTC))
        calculator = TaxCalculator([buy], TaxRules.UK_INDIVIDUAL)
        calculator.pool_same_day()
        calculator.process_section104(True)
        assert calculator.year_end.flags
        flag = calculator.year_end.flags[0]
        assert flag.utc_tax_year == 2025
        assert flag.london_tax_year == 2026
    finally:
        config.ccy = previous
