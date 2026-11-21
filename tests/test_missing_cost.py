from datetime import datetime
from decimal import Decimal

from bittytax.bt_types import AssetSymbol, Note, TaxRules, Timestamp, TrType
from bittytax.constants import TZ_UTC
from bittytax.tax import TaxCalculator
from bittytax.tax_event import TaxEventCapitalGains
from bittytax.transactions import Buy, Sell


def _buy(quantity: str, cost: str, day: int) -> Buy:
    buy = Buy(TrType.TRADE, Decimal(quantity), AssetSymbol("ETH"), Decimal(cost))
    buy.timestamp = Timestamp(datetime(2024, 1, day, tzinfo=TZ_UTC))
    buy.tid = [day, 0]
    return buy


def _sell(quantity: str, proceeds: str, day: int) -> Sell:
    sell = Sell(TrType.SPEND, Decimal(quantity), AssetSymbol("ETH"), Decimal(proceeds))
    sell.timestamp = Timestamp(datetime(2024, 6, day, tzinfo=TZ_UTC))
    sell.tid = [100 + day, 0]
    return sell


def _run(*transactions: object) -> TaxCalculator:
    calculator = TaxCalculator(list(transactions), TaxRules.UK_INDIVIDUAL)  # type: ignore[arg-type]
    calculator.pool_same_day()
    calculator.process_section104(True)
    return calculator


def test_over_disposal_does_not_make_pool_cost_negative() -> None:
    calculator = _run(_buy("1", "100", 1), _sell("5", "500", 1))
    holding = calculator.holdings[AssetSymbol("ETH")]
    assert holding.quantity == 0
    assert holding.cost == 0
    assert holding.cost >= 0
    assert len(calculator.missing_cost_basis) == 1
    missing = calculator.missing_cost_basis[0]
    assert missing.asset == "ETH"
    assert missing.quantity_short == Decimal("4")
    assert missing.proceeds == Decimal("400")
    assert missing.date == datetime(2024, 6, 1, tzinfo=TZ_UTC).date()
    events = [
        event
        for year in calculator.tax_events.values()
        for event in year
        if isinstance(event, TaxEventCapitalGains)
    ]
    assert events[-1].cost == Decimal("100.00")
    assert events[-1].proceeds == Decimal("500.00")


def test_disposal_inside_the_pool_has_no_missing_cost() -> None:
    calculator = _run(_buy("2", "200", 1), _sell("1", "150", 1))
    holding = calculator.holdings[AssetSymbol("ETH")]
    assert holding.quantity == Decimal("1")
    assert holding.cost == Decimal("100")
    assert not calculator.missing_cost_basis


def test_liquid_rebase_carries_cost_to_the_later_sale() -> None:
    bought = _buy("1", "1000", 1)
    removed = Sell(TrType.TRADE, Decimal("1"), AssetSymbol("ETH"), Decimal("1000"))
    removed.timestamp = Timestamp(datetime(2024, 2, 1, 0, 0, tzinfo=TZ_UTC))
    removed.disposal = False
    removed.note = Note("liquid staking rebase stETH into ETH")
    removed.tid = [2, 0]
    added = Buy(TrType.TRADE, Decimal("0.9"), AssetSymbol("ETH"), Decimal("1000"))
    added.timestamp = Timestamp(datetime(2024, 2, 1, 0, 1, tzinfo=TZ_UTC))
    added.acquisition = False
    added.note = Note("liquid staking rebase stETH into ETH")
    added.tid = [2, 1]
    sold = _sell("0.9", "1500", 2)
    calculator = _run(bought, removed, added, sold)
    holding = calculator.holdings[AssetSymbol("ETH")]
    assert holding.quantity == 0
    assert holding.cost == 0
    events = [
        event
        for year in calculator.tax_events.values()
        for event in year
        if isinstance(event, TaxEventCapitalGains)
    ]
    assert events[-1].cost == Decimal("1000.00")
    assert events[-1].gain == Decimal("500.00")
    assert len(calculator.liquid_staking) == 1
    assert "stETH" in calculator.liquid_staking[0].detail
