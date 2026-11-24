# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List


@dataclass
class SummaryRow:
    action: str
    timestamp: datetime
    asset: str
    quantity: Decimal
    wallet: str
    destination: str
    tx_hash: str
    detail: str


@dataclass
class ReviewRow:
    timestamp: datetime
    asset: str
    quantity: Decimal
    wallet: str
    destination: str
    tx_hash: str
    description: str
    disposition: str


@dataclass
class ReconcileResult:
    summary: List[SummaryRow] = field(default_factory=list)
    review: List[ReviewRow] = field(default_factory=list)
    spam: List[ReviewRow] = field(default_factory=list)
    queue: List[ReviewRow] = field(default_factory=list)
    match_seq: int = 0
