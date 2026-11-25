"""Synthetic histories only. No real addresses."""

import time
from datetime import timedelta

from test_transfer_match import BASE, _make, _registry, _run

from bittytax.bt_types import TrType
from bittytax.conv.transfer_match import count_actions

# Far inside a minute, and far inside the 38 minutes seen on a large history.
_BUDGET_SECONDS = 30


def test_pairing_50k_rows_stays_within_the_time_budget() -> None:
    rows = []
    for index in range(25000):
        when = BASE + timedelta(hours=index * 200)
        asset = f"T{index % 100}"
        rows.append(_make(TrType.GIFT_SENT, when, asset, "1", "Exchange"))
        rows.append(_make(TrType.GIFT_RECEIVED, when + timedelta(minutes=5), asset, "1", "Ledger"))
    started = time.perf_counter()
    result = _run(*rows, registry=_registry())
    elapsed = time.perf_counter() - started
    assert count_actions(result).get("matched", 0) == 25000
    assert elapsed < _BUDGET_SECONDS
    assert rows[0].t_record is not None and rows[0].t_record.t_type is TrType.WITHDRAWAL
    assert rows[-1].t_record is not None and rows[-1].t_record.t_type is TrType.DEPOSIT
