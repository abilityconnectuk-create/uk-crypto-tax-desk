# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Sequence

import yaml

from .conv.txref import hashes_equal


class ReviewQueueError(ValueError):
    """The exception list is missing or not in the expected shape."""


@dataclass
class QueueItem:
    timestamp: str
    asset: str
    quantity: str
    wallet: str
    tx_hash: str
    note: str
    disposition: str = ""


@dataclass
class ReviewException:
    reason: str
    tx_hash: str = ""
    on_date: str = ""
    asset: str = ""
    amount: str = ""
    wallet: str = ""


def load_review_exceptions(path: str) -> List[ReviewException]:
    """Load exceptions from a list file or from the review_exceptions key."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except OSError as exc:
        raise ReviewQueueError(f"Cannot read review exceptions {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ReviewQueueError(f"Review exceptions {path} are not valid YAML: {exc}") from exc

    if loaded is None:
        return []
    if isinstance(loaded, list):
        rows = loaded
    elif isinstance(loaded, dict):
        rows = loaded.get("review_exceptions") or []
    else:
        raise ReviewQueueError(f"Review exceptions {path} must be a list or a mapping")
    if not isinstance(rows, list):
        raise ReviewQueueError("review_exceptions must be a list")
    return [_load_exception(item, index) for index, item in enumerate(rows, start=1)]


def review_queue_failures(
    items: Sequence[QueueItem], exceptions: Sequence[ReviewException]
) -> List[QueueItem]:
    """Queue rows that are not covered by an explicit exception."""
    return [item for item in items if not any(_covers(item, exc) for exc in exceptions)]


def _covers(item: QueueItem, exc: ReviewException) -> bool:
    if exc.tx_hash and item.tx_hash and hashes_equal(exc.tx_hash, item.tx_hash):
        return True
    if not (exc.on_date and exc.asset and exc.amount):
        return False
    if _item_date(item) != exc.on_date:
        return False
    if item.asset.strip().upper() != exc.asset.strip().upper():
        return False
    try:
        same_amount = Decimal(item.quantity) == Decimal(exc.amount)
    except (ArithmeticError, ValueError):
        return False
    wallet_ok = not exc.wallet or item.wallet.strip().casefold() == exc.wallet.strip().casefold()
    return bool(same_amount and wallet_ok)


def _item_date(item: QueueItem) -> str:
    text = item.timestamp.strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    return parsed.date().isoformat()


def _load_exception(value: object, index: int) -> ReviewException:
    if not isinstance(value, dict):
        raise ReviewQueueError(f"review_exceptions[{index}] must be a mapping")
    allowed = {"reason", "tx_hash", "date", "asset", "amount", "wallet"}
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ReviewQueueError(f"review_exceptions[{index}] has unknown keys: {names}")
    reason = _text(value.get("reason"))
    if not reason:
        raise ReviewQueueError(f"review_exceptions[{index}] needs a reason")
    tx_hash = _text(value.get("tx_hash"))
    on_date = _text(value.get("date"))
    asset = _text(value.get("asset")).upper()
    amount = _text(value.get("amount"))
    wallet = _text(value.get("wallet"))
    identified = bool(tx_hash or (on_date and asset and amount))
    if not identified:
        raise ReviewQueueError(
            f"review_exceptions[{index}] needs a tx_hash, or a date, asset and amount"
        )
    return ReviewException(
        reason=reason,
        tx_hash=tx_hash,
        on_date=on_date,
        asset=asset,
        amount=amount,
        wallet=wallet,
    )


def _text(value: Optional[object]) -> str:
    if value is None:
        return ""
    return str(value).strip()
