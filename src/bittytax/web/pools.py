# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""One section 104 pool per asset. Stablecoins are not merged into each other."""

from __future__ import annotations

from typing import Sequence

LABEL = "confirmed"
MANUAL = "CRYPTO22200"
STABLECOINS = ("USDT", "USDC", "USDF", "RLUSD")


def separate(assets: Sequence[str]) -> bool:
    """True when every name is its own pool and no two stablecoins share a name."""
    cleaned = [item.strip().upper() for item in assets if item and item.strip()]
    if len(cleaned) != len(set(cleaned)):
        return False
    return all("/" not in item and "+" not in item and "," not in item for item in cleaned)


def stablecoins_merged(assets: Sequence[str]) -> bool:
    """True when a pool name joins two stablecoins. That must not happen."""
    for item in assets:
        upper = item.strip().upper()
        found = [name for name in STABLECOINS if name in upper]
        if len(found) > 1:
            return True
    return False
