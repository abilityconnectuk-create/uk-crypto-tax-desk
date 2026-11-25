# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Pending allocation lots. A listed payee payment is not a spend and not a trade.

The local value stays pending until allocation_links names the payment and the
claim rows. Nothing is matched on its own. A named refund from that payee
reduces the lot. A lot that is not linked is listed later and is not written off.
"""

import csv
import os
import re
from decimal import Decimal
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from ..bt_types import TrType
from .datarow import DataRow
from .out_record import TransactionOutRecord
from .reconcile_types import ReconcileResult, ReviewRow, SummaryRow
from .txref import hashes_equal, normalise_tx_hash
from .wallets import AllocationRule, WalletRegistry

_MONEY = Decimal("0.01")
_DEST_KEYS = ("TxDest", "Receiving Wallet", "To Wallet (read-only)")
_SRC_KEYS = ("TxSrc", "Sending Wallet", "From Wallet (read-only)")
_CHAIN_KEYS = ("Chain", "Blockchain", "Network")
_ID_KEYS = ("TxHash", "Id", "TxId", "PaymentId", "Payment Id")
_SKIPPED = frozenset({"_Possible-Spam", "_Ignored"})
_PENDING_RE = re.compile(
    r"pending allocation \| (?P<label>.*?) \| payment (?P<payment>\S+) \| "
    r"asset (?P<asset>\S+) \| gross (?P<gross>-?\d+(?:\.\d+)?) \| "
    r"refunded (?P<refunded>-?\d+(?:\.\d+)?) \| pending (?P<pending>-?\d+(?:\.\d+)?) \| "
    r"linked (?P<linked>yes|no)"
)


class _Lot(NamedTuple):
    row: DataRow
    rule: AllocationRule
    payment: str
    gross: Decimal
    refunded: Decimal


def mark_allocation_payments(
    rows: Sequence[DataRow], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Turn listed asset sends into pending lots, and apply named refunds."""
    if not registry.allocations:
        return
    lots: List[_Lot] = []
    seen = set()
    for row in rows:
        lot = _payment_lot(row, registry)
        if lot is None:
            continue
        identity = (id(lot.rule), _norm_id(lot.payment))
        if identity in seen:
            continue
        seen.add(identity)
        lots.append(lot)
    if not lots:
        return
    refunded: Dict[Tuple[int, str], Decimal] = {}
    for row in rows:
        _take_refund(row, lots, refunded, result)
    for lot in lots:
        applied = refunded.get((id(lot.rule), _norm_id(lot.payment)), Decimal(0))
        _write_payment(lot, applied, result)


def apply_allocation_links(
    rows: Sequence[DataRow], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Split named pending lots onto the claim rows. Unnamed rows stay pending."""
    if not registry.allocation_links:
        return
    for link in registry.allocation_links:
        chosen = _open_lots(rows, link.payments)
        claims = _claim_rows(rows, link.asset, link.claims)
        if not chosen or not claims:
            continue
        pool = sum((item[1] for item in chosen), Decimal(0))
        _split_cost(claims, link.asset, pool)
        for row, _pending in chosen:
            record = _record(row)
            record.note = _mark_linked(record.note)
            _drop_review(result, _row_ids(row))
            _log(
                result,
                row,
                "allocation-link",
                record.sell_asset or link.asset,
                record.sell_quantity or Decimal(0),
                f"pending value {_money(pool)} split onto {link.asset} by quantity",
            )


def pending_allocations(records_path: str) -> List[Dict[str, str]]:
    """Lots that are still open. A linked lot is omitted and is not a loss."""
    if not records_path or not os.path.isfile(records_path):
        return []
    found: List[Dict[str, str]] = []
    with open(records_path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            match = _PENDING_RE.search(row.get("Note") or "")
            if match is None or match.group("linked") == "yes":
                continue
            pending = Decimal(match.group("pending"))
            if pending <= 0:
                continue
            found.append(
                {
                    "label": match.group("label"),
                    "payment": match.group("payment"),
                    "asset": match.group("asset"),
                    "gross": match.group("gross"),
                    "refunded": match.group("refunded"),
                    "pending": match.group("pending"),
                    "linked": "no",
                }
            )
    return found


def _payment_lot(row: DataRow, registry: WalletRegistry) -> Optional[_Lot]:
    record = row.t_record
    if record is None or row.failure is not None or str(record.t_type) in _SKIPPED:
        return None
    if not _is_outgoing(record):
        return None
    rule = _matching_rule(row, record.sell_asset, registry)
    if rule is None:
        return None
    return _Lot(row, rule, _primary_id(row), record.sell_value or Decimal(0), Decimal(0))


def _matching_rule(row: DataRow, asset: str, registry: WalletRegistry) -> Optional[AllocationRule]:
    symbol = asset.strip().upper()
    chain = _chain(row)
    destinations = {_norm_address(item) for item in _candidates(row, _DEST_KEYS, "dest")}
    for rule in registry.allocations:
        if symbol not in {item.upper() for item in rule.assets}:
            continue
        if rule.chain and chain and rule.chain.casefold() != chain.casefold():
            continue
        payees = {_norm_address(item) for item in rule.payees}
        if destinations & payees:
            return rule
    return None


def _take_refund(
    row: DataRow,
    lots: Sequence[_Lot],
    refunded: Dict[Tuple[int, str], Decimal],
    result: ReconcileResult,
) -> None:
    record = row.t_record
    if record is None or not _is_incoming(record):
        return
    ids = _row_ids(row)
    chain = _chain(row)
    sources = {_norm_address(item) for item in _candidates(row, _SRC_KEYS, "src")}
    for lot in lots:
        if lot.rule.chain and chain and lot.rule.chain.casefold() != chain.casefold():
            continue
        payees = {_norm_address(item) for item in lot.rule.payees}
        if not sources & payees:
            continue
        for refund in lot.rule.refunds:
            if not any(_ids_match(item, refund.refund) for item in ids):
                continue
            if not _ids_match(refund.payment, lot.payment):
                continue
            value = record.buy_value or Decimal(0)
            key = (id(lot.rule), _norm_id(lot.payment))
            refunded[key] = refunded.get(key, Decimal(0)) + value
            _write_refund(row, lot.payment, value, result)
            return


def _write_payment(lot: _Lot, refunded: Decimal, result: ReconcileResult) -> None:
    record = _record(lot.row)
    pending = lot.gross - refunded
    if pending < 0:
        pending = Decimal(0)
    record.t_type = TrType.ALLOCATION
    record.buy_quantity = None
    record.buy_asset = ""
    record.buy_value = None
    record.note = _append(
        record.note,
        _clause(
            lot.rule.label, lot.payment, record.sell_asset, lot.gross, refunded, pending, False
        ),
    )
    detail = f"{lot.rule.label}; pending local value {_money(pending)}"
    if refunded:
        detail = f"{detail}; refunded {_money(refunded)}"
    _log(result, lot.row, "allocation-payment", record.sell_asset, record.sell_quantity, detail)


def _write_refund(row: DataRow, payment: str, value: Decimal, result: ReconcileResult) -> None:
    record = _record(row)
    record.t_type = TrType.REVIEW_ACQUISITION
    record.sell_quantity = None
    record.sell_asset = ""
    record.sell_value = None
    record.note = _append(
        record.note, f"allocation refund | payment {payment} | value {_money(value)}"
    )
    _log(
        result,
        row,
        "allocation-refund",
        record.buy_asset,
        record.buy_quantity,
        f"refund of {payment} reduces that pending lot by {_money(value)}",
    )


def _open_lots(rows: Sequence[DataRow], payments: Sequence[str]) -> List[Tuple[DataRow, Decimal]]:
    chosen = []
    for row in rows:
        record = row.t_record
        if record is None:
            continue
        match = _PENDING_RE.search(record.note or "")
        if match is None or match.group("linked") == "yes":
            continue
        if not any(_ids_match(match.group("payment"), payment) for payment in payments):
            continue
        chosen.append((row, Decimal(match.group("pending"))))
    return chosen


def _claim_rows(
    rows: Sequence[DataRow], asset: str, claims: Sequence[str]
) -> List[Tuple[DataRow, Decimal]]:
    found = []
    for row in rows:
        record = row.t_record
        if record is None or str(record.t_type) in _SKIPPED:
            continue
        if "allocation cost |" in (record.note or "") or _PENDING_RE.search(record.note or ""):
            continue
        if not any(_ids_match(item, claim) for item in _row_ids(row) for claim in claims):
            continue
        quantity = _quantity_for(record, asset)
        if quantity is None or quantity <= 0:
            continue
        found.append((row, quantity))
    return found


def _split_cost(claims: Sequence[Tuple[DataRow, Decimal]], asset: str, pool: Decimal) -> None:
    total = sum((quantity for _row, quantity in claims), Decimal(0))
    if total <= 0:
        return
    used = Decimal(0)
    last = len(claims) - 1
    for index, (row, quantity) in enumerate(claims):
        if index == last:
            share = (pool - used).quantize(_MONEY)
        else:
            share = (pool * quantity / total).quantize(_MONEY)
            used += share
        record = _record(row)
        symbol = record.buy_asset or record.sell_asset or asset
        record.t_type = TrType.ALLOCATION_CLAIM
        record.buy_quantity = quantity
        record.buy_asset = symbol
        record.buy_value = share
        record.sell_quantity = None
        record.sell_asset = ""
        record.sell_value = None
        record.note = _append(
            record.note, f"allocation cost | asset {symbol} | value {_money(share)}"
        )


def _quantity_for(record: TransactionOutRecord, asset: str) -> Optional[Decimal]:
    symbol = asset.strip().casefold()
    if record.buy_asset.strip().casefold() == symbol and record.buy_quantity is not None:
        return record.buy_quantity
    if record.sell_asset.strip().casefold() == symbol and record.sell_quantity is not None:
        return record.sell_quantity
    return None


def _is_outgoing(record: TransactionOutRecord) -> bool:
    if not record.sell_asset or record.sell_quantity is None or record.sell_quantity <= 0:
        return False
    return not (record.buy_asset and record.buy_quantity)


def _is_incoming(record: TransactionOutRecord) -> bool:
    if not record.buy_asset or record.buy_quantity is None or record.buy_quantity <= 0:
        return False
    return not (record.sell_asset and record.sell_quantity)


def _clause(
    label: str,
    payment: str,
    asset: str,
    gross: Decimal,
    refunded: Decimal,
    pending: Decimal,
    linked: bool,
) -> str:
    flag = "yes" if linked else "no"
    return (
        f"pending allocation | {label} | payment {payment} | asset {asset} | "
        f"gross {_money(gross)} | refunded {_money(refunded)} | "
        f"pending {_money(pending)} | linked {flag}"
    )


def _mark_linked(note: str) -> str:
    match = _PENDING_RE.search(note)
    if match is None:
        return note
    replacement = _clause(
        match.group("label"),
        match.group("payment"),
        match.group("asset"),
        Decimal(match.group("gross")),
        Decimal(match.group("refunded")),
        Decimal(0),
        True,
    )
    return note[: match.start()] + replacement + note[match.end() :]


def _drop_review(result: ReconcileResult, ids: Sequence[str]) -> None:
    result.review = [item for item in result.review if not _listed(item, ids)]
    result.queue = [item for item in result.queue if not _listed(item, ids)]


def _listed(item: ReviewRow, ids: Sequence[str]) -> bool:
    return any(_ids_match(item.tx_hash, value) for value in ids)


def _log(
    result: ReconcileResult,
    row: DataRow,
    action: str,
    asset: str,
    quantity: Optional[Decimal],
    detail: str,
) -> None:
    record = _record(row)
    result.summary.append(
        SummaryRow(
            action=action,
            timestamp=record.timestamp,
            asset=asset,
            quantity=quantity or Decimal(0),
            wallet=record.wallet,
            destination=_first(_candidates(row, _DEST_KEYS, "dest")),
            tx_hash=_primary_id(row),
            detail=detail,
        )
    )


def _primary_id(row: DataRow) -> str:
    ids = _row_ids(row)
    if ids:
        return ids[0]
    return f"row-{row.line_num}"


def _row_ids(row: DataRow) -> List[str]:
    found: List[str] = []
    if row.tx_raw is not None:
        _add(found, _at(row, row.tx_raw.tx_hash_pos))
    for key in _ID_KEYS:
        if key in row.row_dict:
            _add(found, row.row_dict[key])
    return found


def _candidates(row: DataRow, keys: Sequence[str], end: str) -> Tuple[str, ...]:
    found: List[str] = []
    if row.tx_raw is not None:
        pos = row.tx_raw.tx_dest_pos if end == "dest" else row.tx_raw.tx_src_pos
        _add(found, _at(row, pos))
    for key in keys:
        if key in row.row_dict:
            _add(found, row.row_dict[key])
    return tuple(found)


def _chain(row: DataRow) -> str:
    for key in _CHAIN_KEYS:
        if key in row.row_dict and row.row_dict[key].strip():
            return row.row_dict[key].strip()
    return ""


def _ids_match(left: str, right: str) -> bool:
    if hashes_equal(left, right):
        return True
    folded = _norm_id(left)
    return bool(folded) and folded == _norm_id(right)


def _norm_id(value: str) -> str:
    return normalise_tx_hash(value)


def _norm_address(value: str) -> str:
    return "".join(value.split()).casefold()


def _money(value: Decimal) -> str:
    return format(value.quantize(_MONEY), "f")


def _append(note: str, text: str) -> str:
    if note:
        return f"{note} | {text}"
    return text


def _record(row: DataRow) -> TransactionOutRecord:
    if row.t_record is None:
        raise RuntimeError("Missing t_record")
    return row.t_record


def _at(row: DataRow, pos: Optional[int]) -> str:
    if pos is None or pos < 0 or pos >= len(row.row):
        return ""
    return row.row[pos].strip()


def _add(found: List[str], text: str) -> None:
    cleaned = text.strip()
    if cleaned and cleaned not in found:
        found.append(cleaned)


def _first(values: Sequence[str]) -> str:
    if values:
        return values[0]
    return ""
