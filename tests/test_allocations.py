import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

from test_reconcile_koinly import _pack, _snapshot, _wallets
from test_transfer_match import BASE, _make, _registry, _run

from bittytax.bt_types import AssetSymbol, Note, TaxRules, Timestamp, TrType
from bittytax.constants import TZ_UTC
from bittytax.conv.allocations import pending_allocations
from bittytax.conv.wallets import AllocationLink, AllocationRefund, AllocationRule
from bittytax.reconcile_koinly import build_report
from bittytax.t_row import TransactionRow
from bittytax.tax import TaxCalculator
from bittytax.tax_event import TaxEventCapitalGains, TaxEventIncome
from bittytax.transactions import Buy, Sell

PAYEE = "0x0000000000000000000000000000000000000c01"
OTHER = "0x0000000000000000000000000000000000000c02"
PAY = "0x" + "aa" * 32
REFUND = "0x" + "bb" * 32
TGE = "0x" + "cc" * 32
VEST = "0x" + "dd" * 32
STRAY = "0x" + "ee" * 32


def _rule(*refunds: AllocationRefund) -> AllocationRule:
    return AllocationRule(
        label="Example sale",
        chain="ethereum",
        payees=(PAYEE,),
        assets=("USDT", "USDC"),
        refunds=refunds,
    )


def test_unlinked_allocation_stays_pending_until_a_manual_link(tmp_path: Path) -> None:
    registry = _registry()
    registry.allocations = [_rule()]
    paid = _make(
        TrType.GIFT_SENT,
        BASE,
        "USDT",
        "1000",
        "Ledger",
        destination=PAYEE,
        tx_hash=PAY,
        value="800",
        chain="ethereum",
    )
    other_chain = _make(
        TrType.GIFT_SENT,
        BASE,
        "USDT",
        "1000",
        "Ledger",
        destination=PAYEE,
        tx_hash=STRAY,
        value="800",
        chain="sui",
    )
    token = _make(TrType.GIFT_SENT, BASE, "BTC", "1", "Ledger", destination=PAYEE, chain="ethereum")
    claim = _make(TrType.GIFT_RECEIVED, BASE, "WIDGET", "100", "Ledger", tx_hash=TGE)
    held = _run(paid, other_chain, token, claim, registry=registry)

    assert paid.t_record is not None
    assert paid.t_record.t_type is TrType.ALLOCATION
    assert paid.t_record.t_type is not TrType.SPEND
    assert paid.t_record.t_type is not TrType.TRADE
    assert paid.t_record.sell_value == Decimal("800")
    assert "pending 800.00" in paid.t_record.note
    assert "linked no" in paid.t_record.note
    assert other_chain.t_record is not None and other_chain.t_record.t_type is not TrType.ALLOCATION
    assert token.t_record is not None and token.t_record.t_type is not TrType.ALLOCATION
    assert claim.t_record is not None and claim.t_record.t_type is not TrType.ALLOCATION_CLAIM
    assert "allocation-link" not in [row.action for row in held.summary]
    assert all(item.tx_hash != PAY for item in held.queue)

    records = tmp_path / "records.csv"
    _write_records(records, paid.t_record.note)
    lots = pending_allocations(str(records))
    assert len(lots) == 1
    assert lots[0]["label"] == "Example sale"
    assert lots[0]["payment"] == PAY
    assert lots[0]["pending"] == "800.00"
    assert lots[0]["linked"] == "no"
    report = _report(tmp_path, records)
    assert report["pending_allocations"] == lots
    assert report["passed"] is True
    assert "negligible" not in str(report).casefold()

    linked = _registry()
    linked.allocations = [_rule()]
    linked.allocation_links = [AllocationLink(payments=(PAY,), asset="WIDGET", claims=(TGE, VEST))]
    again = _make(
        TrType.GIFT_SENT,
        BASE,
        "USDT",
        "1000",
        "Ledger",
        destination=PAYEE,
        tx_hash=PAY,
        value="800",
        chain="ethereum",
    )
    first = _make(TrType.GIFT_RECEIVED, BASE, "WIDGET", "100", "Ledger", tx_hash=TGE)
    second = _make(TrType.GIFT_RECEIVED, BASE, "WIDGET", "400", "Ledger", tx_hash=VEST)
    _run(again, first, second, registry=linked)
    assert again.t_record is not None and "linked yes" in again.t_record.note
    assert "pending 0.00" in again.t_record.note
    assert first.t_record is not None and first.t_record.t_type is TrType.ALLOCATION_CLAIM
    assert second.t_record is not None and second.t_record.t_type is TrType.ALLOCATION_CLAIM
    assert first.t_record.buy_value == Decimal("160.00")
    assert second.t_record.buy_value == Decimal("640.00")
    assert first.t_record.t_type is not TrType.TRADE
    _write_records(records, again.t_record.note)
    assert not pending_allocations(str(records))


def test_allocation_refund_reduces_only_the_named_payment() -> None:
    registry = _registry()
    registry.allocations = [_rule(AllocationRefund(payment=PAY, refund=REFUND))]
    paid = _make(
        TrType.GIFT_SENT,
        BASE,
        "USDT",
        "1000",
        "Ledger",
        destination=PAYEE,
        tx_hash=PAY,
        value="800",
        chain="ethereum",
    )
    refund = _make(
        TrType.GIFT_RECEIVED,
        BASE,
        "USDT",
        "200",
        "Ledger",
        source=PAYEE,
        tx_hash=REFUND,
        value="160",
        chain="ethereum",
    )
    stray = _make(
        TrType.GIFT_RECEIVED,
        BASE,
        "USDT",
        "50",
        "Ledger",
        source=PAYEE,
        tx_hash=STRAY,
        value="40",
        chain="ethereum",
    )
    _run(paid, refund, stray, registry=registry)
    assert paid.t_record is not None
    assert "refunded 160.00" in paid.t_record.note
    assert "pending 640.00" in paid.t_record.note
    assert refund.t_record is not None
    assert refund.t_record.t_type is TrType.REVIEW_ACQUISITION
    assert "allocation refund" in refund.t_record.note
    assert refund.t_record.t_type is not TrType.INCOME
    assert stray.t_record is not None and "allocation refund" not in stray.t_record.note

    unnamed = _registry()
    unnamed.allocations = [_rule(AllocationRefund(payment=PAY, refund=REFUND))]
    still = _make(
        TrType.GIFT_SENT,
        BASE,
        "USDT",
        "1000",
        "Ledger",
        destination=PAYEE,
        tx_hash=PAY,
        value="800",
        chain="ethereum",
    )
    wrong = _make(
        TrType.GIFT_RECEIVED,
        BASE,
        "USDT",
        "200",
        "Ledger",
        source=OTHER,
        tx_hash=REFUND,
        value="160",
        chain="ethereum",
    )
    _run(still, wrong, registry=unnamed)
    assert still.t_record is not None and "pending 800.00" in still.t_record.note
    assert wrong.t_record is not None and "allocation refund" not in wrong.t_record.note


def test_allocation_short_pool_keeps_the_local_value() -> None:
    header = TransactionRow.HEADER
    values = {column: "" for column in header}
    values.update(
        {
            "Type": "Allocation",
            "Sell Quantity": "100",
            "Sell Asset": "USDT",
            "Sell Value": "80",
            "Wallet": "Ledger",
            "Timestamp": "2024-06-01T12:00:00",
        }
    )
    imported = TransactionRow([values[column] for column in header], 1)
    imported.parse()
    assert imported.t_record is not None and imported.t_record.sell is not None
    assert imported.t_record.sell.t_type is TrType.ALLOCATION
    assert imported.t_record.sell.disposal is True
    assert imported.t_record.sell.proceeds == Decimal("80")
    assert imported.t_record.buy is None

    empty = _calculator(_allocation("100", "80", 1))
    event = _gain(empty)
    assert event.proceeds == Decimal("80.00")
    assert event.cost == Decimal("0.00")
    assert event.proceeds != event.cost
    assert not empty.missing_cost_basis

    short = _calculator(_buy("40", "20", 1), _allocation("100", "80", 2))
    event = _gain(short)
    assert event.proceeds == Decimal("80.00")
    assert event.cost == Decimal("20.00")
    assert not short.missing_cost_basis
    holding = short.holdings[AssetSymbol("USDT")]
    assert holding.quantity == 0
    assert holding.cost == 0

    claim = Buy(TrType.ALLOCATION_CLAIM, Decimal("500"), AssetSymbol("WIDGET"), Decimal("640"))
    claim.timestamp = Timestamp(datetime(2024, 8, 1, tzinfo=TZ_UTC))
    claim.tid = [8, 0]
    claim.note = Note("allocation cost")
    funded = _calculator(claim)
    funded.process_income()
    assert funded.holdings[AssetSymbol("WIDGET")].cost == Decimal("640")
    income = [
        item
        for year in funded.tax_events.values()
        for item in year
        if isinstance(item, TaxEventIncome)
    ]
    assert income == []


def _allocation(quantity: str, proceeds: str, day: int) -> Sell:
    sell = Sell(TrType.ALLOCATION, Decimal(quantity), AssetSymbol("USDT"), Decimal(proceeds))
    sell.timestamp = Timestamp(datetime(2024, 6, day, tzinfo=TZ_UTC))
    sell.tid = [100 + day, 0]
    return sell


def _buy(quantity: str, cost: str, day: int) -> Buy:
    buy = Buy(TrType.TRADE, Decimal(quantity), AssetSymbol("USDT"), Decimal(cost))
    buy.timestamp = Timestamp(datetime(2024, 1, day, tzinfo=TZ_UTC))
    buy.tid = [day, 0]
    return buy


def _calculator(*transactions: object) -> TaxCalculator:
    calculator = TaxCalculator(list(transactions), TaxRules.UK_INDIVIDUAL)  # type: ignore[arg-type]
    calculator.pool_same_day()
    calculator.process_section104(True)
    return calculator


def _gain(calculator: TaxCalculator) -> TaxEventCapitalGains:
    events = [
        event
        for year in calculator.tax_events.values()
        for event in year
        if isinstance(event, TaxEventCapitalGains)
    ]
    assert len(events) == 1
    return events[0]


def _write_records(path: Path, note: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Type", "Wallet", "Timestamp", "Note"])
        writer.writerow(["Deposit", "Ledger", "2024-06-01T12:00:00 UTC", ""])
        writer.writerow(["Withdrawal", "Exchange", "2024-06-02T12:00:00 UTC", ""])
        writer.writerow(["Deposit", "Ledger", "2024-06-02T12:00:00 UTC", note])


def _report(folder: Path, records: Path) -> Dict[str, Any]:
    koinly = folder / "koinly"
    koinly.mkdir(exist_ok=True)
    _pack(koinly)
    snapshot = folder / "snapshot.json"
    wallets = folder / "wallets.yaml"
    _snapshot(snapshot)
    _wallets(wallets)
    return build_report(
        koinly_dir=str(koinly),
        records_path=str(records),
        snapshot_path=str(snapshot),
        wallets_path=str(wallets),
        review_rows="conservative",
    )
