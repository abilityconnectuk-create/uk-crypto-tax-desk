# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Brought-forward losses stop at the annual exempt amount. The boxes stay unconfirmed."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

from bittytax.web.losses import allocate, for_year, save_opening
from bittytax.web.sa108 import sa108_view
from bittytax.web.store import Desk


def test_brought_forward_losses_stop_at_the_allowance() -> None:
    result = allocate(Decimal(5000), Decimal(0), Decimal(4000), Decimal(3000))
    assert result["brought_used"] == Decimal(2000)
    assert result["carried"] == Decimal(2000)
    unused = allocate(Decimal(1000), Decimal(2500), Decimal(0), Decimal(3000))
    assert unused["used_current"] == Decimal(1000)
    assert unused["carried"] == Decimal(1500)


def test_opening_losses_fill_boxes_45_and_47(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    save_opening(desk.root, "2026", "4000")
    if os.name == "posix":
        assert os.stat(desk.root / "opening-losses.json").st_mode & 0o777 == 0o600
    row = for_year(desk, 2026)
    assert row["brought_used"] == "0.00"
    assert row["carried"] == "4000.00"
    assert row["banner"] == "AWAITING TAX REVIEW"
    report: Dict[str, Any] = {"summary": {}, "disposals": []}
    view = sa108_view(report, 2026, row)
    assert view is not None
    boxes = {item["box"]: item for item in view["boxes"]}
    assert boxes["45"]["value"] == "0.00"
    assert boxes["47"]["value"] == "4000.00"
    assert boxes["45"]["confirmed"] == "no"
    assert view["banner"] == "AWAITING TAX REVIEW"
