# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Fee treatment. Acquisition fees are added to cost. Disposal fees are an allowable cost.

Box 13.2 stays the gross proceeds. Swap fees are split in half. Gas paid in a
token is a disposal of that token, on that token's own pool.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict

LABEL = "confirmed"


def gain(
    proceeds: Decimal, cost: Decimal, acquisition_fee: Decimal, disposal_fee: Decimal
) -> Decimal:
    """Gain after acquisition fees in the cost and disposal fees as an allowable cost."""
    allowable = cost + acquisition_fee
    net_proceeds = proceeds - disposal_fee
    return net_proceeds - allowable


def gas_on_own_pool(fee_asset: str, disposed_asset: str) -> Dict[str, str]:
    """A token fee is its own disposal when the fee asset is not the asset sold."""
    fee = fee_asset.strip().upper()
    sold = disposed_asset.strip().upper()
    if not fee:
        return {"kind": "none", "pool": "", "label": LABEL}
    if fee == sold:
        return {"kind": "same-pool fee", "pool": fee, "label": LABEL}
    return {"kind": "token disposal", "pool": fee, "label": LABEL}
