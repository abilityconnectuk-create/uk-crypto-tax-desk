# -*- coding: utf-8 -*-

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..bt_types import TrType, UnmappedType
from ..constants import TZ_UTC
from .datarow import DataRow
from .out_record import TransactionOutRecord
from .wallets import MatchSettings, RegistryEntry, TokenGroup, WalletRegistry, find_entry

# Accounting tool rejects this type, so an unmatched withdrawal cannot be taxed as a gift
# until the user edits the row.
NEEDS_REVIEW_TYPE = "_Needs-Review"

_OUT_TYPES = frozenset({TrType.WITHDRAWAL, TrType.GIFT_SENT, TrType.SPEND, TrType.CHARITY_SENT})
_IN_TYPES = frozenset({TrType.DEPOSIT, TrType.GIFT_RECEIVED})
_DESTINATION_KEYS = ("TxDest", "Receiving Wallet", "To Wallet (read-only)")
_CHAIN_KEYS = ("Chain", "Blockchain", "Network")
_TAG_KEYS = ("Tag", "Label")

_SUMMARY_HEADER = [
    "Action",
    "Date",
    "Asset",
    "Amount",
    "Wallet",
    "Destination",
    "TxHash",
    "Detail",
]
_REVIEW_HEADER = [
    "Date",
    "Asset",
    "Amount",
    "Wallet",
    "Destination",
    "TxHash",
    "Description",
    "Disposition",
]
_REVIEW_NOTE = "review row treated as"


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


@dataclass
class _Leg:
    row: DataRow
    direction: str
    ambiguous: bool
    eligible: bool
    consumed: bool = False
    own_entry: Optional[RegistryEntry] = None
    payee_entry: Optional[RegistryEntry] = None


@dataclass
class _View:
    timestamp: datetime
    asset: str
    quantity: Decimal
    wallet: str
    destination: str
    tx_hash: str


def reconcile_transfers(
    rows: Sequence[DataRow],
    registry: WalletRegistry,
    legacy_gifts: bool = False,
    review_as: str = "reject",
    containers: Optional[Sequence[List[DataRow]]] = None,
) -> ReconcileResult:
    """Pair internal transfers and stop untagged withdrawals becoming gifts.

    Runs on already-parsed rows, across every file in the conversion. Parsers are
    left unchanged, so nothing happens unless the caller invokes it.
    """
    result = ReconcileResult()
    legs = [leg for leg in (_leg_for(row, registry) for row in rows) if leg is not None]
    _pair(legs, registry.match, result)
    _finish_unmatched(legs, legacy_gifts, review_as, result)
    if registry.match.share_pools:
        _share_pools(rows, containers, registry.match, result)
    return result


def count_actions(result: ReconcileResult) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in result.summary:
        counts[row.action] = counts.get(row.action, 0) + 1
    return counts


def reconciliation_report_paths(output_filename: Optional[str]) -> Tuple[str, str]:
    if output_filename:
        stem, _ext = os.path.splitext(output_filename)
        if not stem:
            stem = "BittyTax_Records"
    else:
        stem = "BittyTax_Records"
    return f"{stem}-transfer-summary.csv", f"{stem}-needs-review.csv"


def write_reconciliation_reports(
    result: ReconcileResult, summary_path: str, review_path: str
) -> None:
    _write_csv(summary_path, _SUMMARY_HEADER, [_summary_cells(row) for row in result.summary])
    _write_csv(review_path, _REVIEW_HEADER, [_review_cells(row) for row in result.review])


def format_reconciliation_message(result: ReconcileResult) -> str:
    counts = count_actions(result)
    parts = [
        f"{counts.get('matched', 0)} matched",
        f"{counts.get('own-address', 0)} own-address",
        f"{counts.get('payee-disposal', 0)} payee disposals",
        f"{counts.get('needs-review', 0)} needs review",
        f"{counts.get('legacy-gift', 0)} kept as gifts",
        f"{counts.get('assumed-disposal', 0)} assumed disposals",
        f"{counts.get('assumed-transfer', 0)} assumed transfers",
    ]
    lines = ["transfer reconciliation: " + ", ".join(parts)]
    if counts.get("needs-review", 0):
        lines.append(
            f"{counts['needs-review']} unmatched row(s) are {NEEDS_REVIEW_TYPE}. "
            "bittytax rejects that type unless you pass --review-rows disposal "
            "or --review-rows transfer."
        )
    assumed = counts.get("assumed-disposal", 0) + counts.get("assumed-transfer", 0)
    if assumed:
        lines.append(
            f"WARNING: {assumed} needs-review row(s) were given a tax treatment so a "
            "report can be produced. Confirm each one. Real disposals may be missing "
            "if they were treated as transfers, and gains may be overstated if they "
            "were treated as gifts."
        )
    return "\n".join(lines)


def _pair(legs: Sequence[_Leg], settings: MatchSettings, result: ReconcileResult) -> None:
    outs = [leg for leg in legs if leg.direction == "out" and leg.eligible]
    ins = [leg for leg in legs if leg.direction == "in" and leg.eligible]
    outs.sort(key=_sort_key)
    ins.sort(key=_sort_key)
    applied: List[Tuple[_Leg, _Leg]] = []
    match_id = 0
    for out in outs:
        chosen = _best_deposit(out, ins, settings, applied)
        if chosen is None:
            continue
        match_id += 1
        _apply_match(out, chosen, match_id, settings, result)
        applied.append((out, chosen))


def _best_deposit(
    out: _Leg,
    ins: Sequence[_Leg],
    settings: MatchSettings,
    applied: Sequence[Tuple[_Leg, _Leg]],
) -> Optional[_Leg]:
    chosen: Optional[_Leg] = None
    chosen_score: Optional[Tuple[Decimal, timedelta, int]] = None
    for incoming in ins:
        if incoming.consumed or not _compatible(out, incoming, settings):
            continue
        if any(
            _is_mirror(prev_out, prev_in, out, incoming, settings) for prev_out, prev_in in applied
        ):
            continue
        score = _score(out, incoming, settings)
        if chosen_score is None or score < chosen_score:
            chosen = incoming
            chosen_score = score
    return chosen


def _compatible(out: _Leg, incoming: _Leg, settings: MatchSettings) -> bool:
    if out.row is incoming.row:
        return False
    if not _wallets_differ(out, incoming):
        return False
    if not _assets_compatible(
        _asset(out.row, "out"), _asset(incoming.row, "in"), settings.token_groups
    ):
        return False
    out_ts = _as_utc(_record(out.row).timestamp)
    in_ts = _as_utc(_record(incoming.row).timestamp)
    if abs(out_ts - in_ts) > settings.window:
        return False
    return _amounts_match(out.row, incoming.row, settings)


def _score(out: _Leg, incoming: _Leg, settings: MatchSettings) -> Tuple[Decimal, timedelta, int]:
    # Closest amount, then closest time. A shared tx hash only breaks a tie,
    # so a small send does not take a worse deposit just because the hash matches.
    same_hash = 0 if _same_hash(out.row, incoming.row) else 1
    out_ts = _as_utc(_record(out.row).timestamp)
    in_ts = _as_utc(_record(incoming.row).timestamp)
    return (_amount_gap(out.row, incoming.row, settings), abs(out_ts - in_ts), same_hash)


def _is_mirror(out_a: _Leg, in_a: _Leg, out_b: _Leg, in_b: _Leg, settings: MatchSettings) -> bool:
    """True when the second pair is the same movement recorded in reverse."""
    if not _wallet(out_a) or not _wallet(in_a) or not _wallet(out_b) or not _wallet(in_b):
        return False
    if _wallet(out_a) != _wallet(in_b) or _wallet(in_a) != _wallet(out_b):
        return False
    if _wallet(out_a) == _wallet(out_b):
        return False
    stamp = _as_utc(_record(out_a.row).timestamp)
    for leg in (in_a, out_b, in_b):
        if _as_utc(_record(leg.row).timestamp) != stamp:
            return False
    if not _assets_compatible(
        _asset(out_a.row, "out"), _asset(out_b.row, "out"), settings.token_groups
    ):
        return False
    qty_a = _quantity(out_a.row, "out")
    qty_b = _quantity(out_b.row, "out")
    if qty_a is None or qty_b is None:
        return False
    return _amounts_close(qty_a, qty_b, settings)


def _wallet(leg: _Leg) -> str:
    return _record(leg.row).wallet.strip().casefold()


def _apply_match(
    out: _Leg,
    incoming: _Leg,
    match_id: int,
    settings: MatchSettings,
    result: ReconcileResult,
) -> None:
    view = _view(out)
    detail = _align_quantities(out.row, incoming.row, settings)
    own = f"; own address {out.own_entry.label}" if out.own_entry else ""
    note = f"transfer match {match_id}{own}"
    _reclassify_out(out.row, note)
    _reclassify_in(incoming.row, note)
    out.consumed = True
    incoming.consumed = True
    in_view = _view(incoming)
    result.summary.append(
        _summary(
            "matched",
            view,
            f"match {match_id} with {in_view.quantity} {in_view.asset} "
            f"in {in_view.wallet or 'unknown wallet'}; {detail}{own}",
        )
    )


def _finish_unmatched(
    legs: Sequence[_Leg], legacy_gifts: bool, review_as: str, result: ReconcileResult
) -> None:
    for leg in legs:
        if leg.consumed:
            continue
        view = _view(leg)
        record = _record(leg.row)
        if leg.direction == "out" and leg.own_entry is not None:
            _record_own_address(leg, view, result)
        elif leg.direction == "out" and leg.payee_entry is not None:
            _record_payee(leg, view, result)
        elif leg.ambiguous and record.t_type in (TrType.GIFT_SENT, TrType.GIFT_RECEIVED):
            _record_unmatched(leg, view, legacy_gifts, review_as, result)


def _record_own_address(leg: _Leg, view: _View, result: ReconcileResult) -> None:
    assert leg.own_entry is not None
    label = leg.own_entry.label
    changed = _reclassify_out(leg.row, f"own address: {label}")
    if changed:
        detail = f"destination {label}; no deposit matched; reclassified to Withdrawal"
    else:
        detail = f"destination {label}; already a withdrawal; no deposit matched"
    result.summary.append(_summary("own-address", view, detail))


def _record_payee(leg: _Leg, view: _View, result: ReconcileResult) -> None:
    assert leg.payee_entry is not None
    type_name = _type_name(_record(leg.row).t_type)
    result.summary.append(
        _summary(
            "payee-disposal",
            view,
            f"destination {leg.payee_entry.label}; left as {type_name}",
        )
    )


def _record_unmatched(
    leg: _Leg, view: _View, legacy_gifts: bool, review_as: str, result: ReconcileResult
) -> None:
    kind = "withdrawal" if leg.direction == "out" else "deposit"
    # Read the original description before the note is rewritten.
    description = _description(leg.row)
    if legacy_gifts:
        kept = "Gift-Sent" if leg.direction == "out" else "Gift-Received"
        result.summary.append(
            _summary(
                "legacy-gift",
                view,
                f"untagged {kind} left as {kept} (--legacy-gift-withdrawals)",
            )
        )
        result.review.append(_review(leg, view, "kept-as-gift", description))
        return

    treatment = review_as if review_as in ("disposal", "transfer") else "reject"
    if treatment == "reject":
        _record(leg.row).t_type = UnmappedType(NEEDS_REVIEW_TYPE)
        _append_note(_record(leg.row), f"needs review: unmatched {kind}")
        result.summary.append(
            _summary("needs-review", view, f"untagged {kind} set to {NEEDS_REVIEW_TYPE}")
        )
        result.review.append(_review(leg, view, "needs-review", description))
        return

    _apply_review_type(leg, treatment)
    action = "assumed-disposal" if treatment == "disposal" else "assumed-transfer"
    result.summary.append(
        _summary(
            action, view, f"untagged {kind} treated as {treatment} so a report can be produced"
        )
    )
    result.review.append(_review(leg, view, action, description))


def _apply_review_type(leg: _Leg, treatment: str) -> None:
    record = _record(leg.row)
    if leg.direction == "out":
        record.t_type = TrType.GIFT_SENT if treatment == "disposal" else TrType.WITHDRAWAL
        if treatment == "transfer":
            record.sell_value = None
    else:
        record.t_type = TrType.GIFT_RECEIVED if treatment == "disposal" else TrType.DEPOSIT
        if treatment == "transfer":
            record.buy_value = None
    _append_note(record, f"{_REVIEW_NOTE} {treatment}")


def _leg_for(row: DataRow, registry: WalletRegistry) -> Optional[_Leg]:
    if row.failure is not None or row.t_record is None:
        return None
    direction = _direction(row.t_record.t_type)
    if direction is None:
        return None
    quantity = _quantity(row, direction)
    if quantity is None or quantity <= 0 or not _asset(row, direction):
        return None

    tag = _tag(row)
    ambiguous = tag == "" and row.t_record.t_type in (TrType.GIFT_SENT, TrType.GIFT_RECEIVED)
    own, payee = _registry_hits(row, registry, direction)
    if not _should_track(row.t_record.t_type, direction, ambiguous, own, payee):
        return None
    return _Leg(
        row=row,
        direction=direction,
        ambiguous=ambiguous,
        eligible=_is_eligible(row.t_record.t_type, direction, ambiguous, own, payee),
        own_entry=own,
        payee_entry=payee,
    )


def _registry_hits(
    row: DataRow, registry: WalletRegistry, direction: str
) -> Tuple[Optional[RegistryEntry], Optional[RegistryEntry]]:
    if direction != "out":
        return None, None
    chain = _chain(row)
    candidates = _destination_candidates(row)
    own = find_entry(registry.own, *candidates, chain=chain)
    # A row already typed as a withdrawal is left alone for payees. The payee list
    # stops disposals being rewritten into transfers; it does not rewrite exchange exports.
    if own is not None or row.t_record is None or row.t_record.t_type is TrType.WITHDRAWAL:
        return own, None
    return own, _find_payee(row, registry.payees, candidates, chain)


def _should_track(
    t_type: Union[TrType, UnmappedType],
    direction: str,
    ambiguous: bool,
    own: Optional[RegistryEntry],
    payee: Optional[RegistryEntry],
) -> bool:
    if direction == "in":
        if t_type is TrType.DEPOSIT:
            return True
        return bool(t_type is TrType.GIFT_RECEIVED and ambiguous)
    if t_type is TrType.WITHDRAWAL:
        return True
    if t_type is TrType.GIFT_SENT:
        return bool(ambiguous or own or payee)
    if t_type in (TrType.SPEND, TrType.CHARITY_SENT):
        return bool(own or payee)
    return False


def _is_eligible(
    t_type: Union[TrType, UnmappedType],
    direction: str,
    ambiguous: bool,
    own: Optional[RegistryEntry],
    payee: Optional[RegistryEntry],
) -> bool:
    if payee is not None:
        return False
    if direction == "in":
        return True
    if t_type is TrType.WITHDRAWAL or own is not None:
        return True
    return bool(ambiguous and t_type is TrType.GIFT_SENT)


def _align_quantities(out_row: DataRow, in_row: DataRow, settings: MatchSettings) -> str:
    """Net a reclassified withdrawal down to the amount that arrived.

    Rows that were already withdrawals are not rewritten, so exchange exports keep
    the quantities their parsers produced.
    """
    out = _record(out_row)
    incoming = _record(in_row)
    if out.t_type is TrType.WITHDRAWAL:
        return "quantities left unchanged"
    sent = out.sell_quantity
    received = incoming.buy_quantity
    if sent is None or received is None or sent <= received:
        return "quantities left unchanged"
    if not _assets_compatible(out.sell_asset, incoming.buy_asset, settings.token_groups):
        return "quantities left unchanged"

    shortfall = sent - received
    out.sell_quantity = received
    fee = out.fee_quantity if out.fee_quantity is not None else Decimal(0)
    fee_same = fee > 0 and _assets_compatible(out.fee_asset, out.sell_asset, settings.token_groups)
    if fee <= 0:
        out.fee_quantity = shortfall
        out.fee_asset = out.sell_asset
        return f"shortfall {_fmt_qty(shortfall)} {out.sell_asset} recorded as the transfer fee"
    if fee_same and _amounts_close(fee, shortfall, settings):
        return "withdrawal quantity netted so the same-asset fee is not counted twice"
    return "withdrawal quantity set to the amount received; recorded fee left as-is"


def _reclassify_out(row: DataRow, note: str) -> bool:
    record = _record(row)
    changed = record.t_type is not TrType.WITHDRAWAL
    if changed:
        record.t_type = TrType.WITHDRAWAL
        record.sell_value = None
    _append_note(record, note)
    return changed


def _reclassify_in(row: DataRow, note: str) -> None:
    record = _record(row)
    if record.t_type is not TrType.DEPOSIT:
        record.t_type = TrType.DEPOSIT
        record.buy_value = None
    _append_note(record, note)


def _amounts_match(out_row: DataRow, in_row: DataRow, settings: MatchSettings) -> bool:
    return any(
        _amounts_close(left, right, settings)
        for left in _out_amounts(out_row, settings)
        for right in _in_amounts(in_row, settings)
    )


def _amount_gap(out_row: DataRow, in_row: DataRow, settings: MatchSettings) -> Decimal:
    gaps = [
        abs(left - right)
        for left in _out_amounts(out_row, settings)
        for right in _in_amounts(in_row, settings)
    ]
    if not gaps:
        return Decimal("Infinity")
    return min(gaps)


def _out_amounts(row: DataRow, settings: MatchSettings) -> List[Decimal]:
    record = _record(row)
    quantity = record.sell_quantity
    if quantity is None:
        return []
    amounts = [quantity]
    if _fee_in_asset(record.sell_asset, record, settings) and quantity > _fee_value(record):
        amounts.append(quantity - _fee_value(record))
    return amounts


def _in_amounts(row: DataRow, settings: MatchSettings) -> List[Decimal]:
    record = _record(row)
    quantity = record.buy_quantity
    if quantity is None:
        return []
    amounts = [quantity]
    if _fee_in_asset(record.buy_asset, record, settings):
        amounts.append(quantity + _fee_value(record))
    return amounts


def _fee_in_asset(asset: str, record: TransactionOutRecord, settings: MatchSettings) -> bool:
    return _fee_value(record) > 0 and _assets_compatible(
        record.fee_asset, asset, settings.token_groups
    )


def _fee_value(record: TransactionOutRecord) -> Decimal:
    if record.fee_quantity is None:
        return Decimal(0)
    return record.fee_quantity


def _amounts_close(left: Decimal, right: Decimal, settings: MatchSettings) -> bool:
    if left <= 0 or right <= 0:
        return False
    limit = max(left, right) * settings.amount_tolerance + settings.amount_tolerance_abs
    return abs(left - right) <= limit


def _assets_compatible(left: str, right: str, groups: Sequence[TokenGroup]) -> bool:
    first = left.strip().upper()
    second = right.strip().upper()
    if not first or not second:
        return False
    if first == second:
        return True
    return any(first in group.symbols and second in group.symbols for group in groups)


def _group_for(asset: str, groups: Sequence[TokenGroup]) -> Optional[TokenGroup]:
    symbol = asset.strip().upper()
    for group in groups:
        if symbol in group.symbols:
            return group
    return None


def _canonical(asset: str, settings: MatchSettings) -> str:
    group = _group_for(asset, settings.token_groups)
    if group is None:
        return asset
    return group.canonical()


def _find_payee(
    row: DataRow,
    entries: Sequence[RegistryEntry],
    candidates: Sequence[str],
    chain: str,
) -> Optional[RegistryEntry]:
    by_destination = find_entry(entries, *candidates, chain=chain)
    if by_destination is not None:
        return by_destination
    record = _record(row)
    text = f"{record.note} {_description(row)}".casefold()
    wallet = record.wallet.strip().casefold()
    asset = _asset(row, "out").strip().upper()
    for entry in entries:
        if entry.description and entry.description.casefold() in text:
            return entry
        if (
            entry.wallet
            and entry.asset
            and entry.wallet.strip().casefold() == wallet
            and entry.asset.strip().upper() == asset
        ):
            return entry
    return None


def _share_pools(
    rows: Sequence[DataRow],
    containers: Optional[Sequence[List[DataRow]]],
    settings: MatchSettings,
    result: ReconcileResult,
) -> None:
    homes = list(containers) if containers is not None else []
    if not homes and isinstance(rows, list):
        homes = [rows]
    for row in list(rows):
        deposit = _convert_wrap(row, settings)
        if deposit is None:
            continue
        _insert_after(homes, row, deposit)
        result.summary.append(
            _summary(
                "wrap",
                _view_record(row, "out"),
                "wrap or unwrap treated as a transfer in the "
                f"{_canonical(_record(row).sell_asset, settings)} pool",
            )
        )
    for home in homes:
        for row in home:
            _rename_record(row, settings)


def _convert_wrap(row: DataRow, settings: MatchSettings) -> Optional[DataRow]:
    if row.t_record is None or row.t_record.t_type is not TrType.TRADE:
        return None
    record = row.t_record
    if not record.sell_asset or not record.buy_asset:
        return None
    if _group_for(record.sell_asset, settings.token_groups) is None:
        return None
    if not _assets_compatible(record.sell_asset, record.buy_asset, settings.token_groups):
        return None
    if record.sell_quantity is None or record.buy_quantity is None:
        return None
    if not _amounts_close(record.sell_quantity, record.buy_quantity, settings):
        return None

    canonical = _canonical(record.sell_asset, settings)
    sent = record.sell_quantity
    received = record.buy_quantity
    net = received if sent >= received else sent
    record.t_type = TrType.WITHDRAWAL
    record.sell_asset = canonical
    record.sell_quantity = net
    record.sell_value = None
    record.buy_quantity = None
    record.buy_asset = ""
    record.buy_value = None
    if sent > received and (record.fee_quantity is None or record.fee_quantity <= 0):
        record.fee_quantity = sent - received
        record.fee_asset = canonical
    elif record.fee_asset:
        record.fee_asset = _canonical(record.fee_asset, settings)
    _append_note(record, f"wrap treated as a transfer in the {canonical} pool")

    deposit = DataRow(row.line_num, [], [], row.worksheet_name)
    deposit.timestamp = record.timestamp
    deposit.parsed = True
    deposit.t_record = TransactionOutRecord(
        TrType.DEPOSIT,
        record.timestamp,
        buy_quantity=net,
        buy_asset=canonical,
        wallet=record.wallet,
        note=f"wrap treated as a transfer in the {canonical} pool",
    )
    return deposit


def _rename_record(row: DataRow, settings: MatchSettings) -> None:
    if row.t_record is None:
        return
    record = row.t_record
    if record.buy_asset:
        record.buy_asset = _canonical(record.buy_asset, settings)
    if record.sell_asset:
        record.sell_asset = _canonical(record.sell_asset, settings)
    if record.fee_asset:
        record.fee_asset = _canonical(record.fee_asset, settings)


def _insert_after(containers: Sequence[List[DataRow]], anchor: DataRow, new_row: DataRow) -> None:
    for container in containers:
        for index, existing in enumerate(container):
            if existing is anchor:
                container.insert(index + 1, new_row)
                return


def _view_record(row: DataRow, direction: str) -> _View:
    record = _record(row)
    quantity = _quantity(row, direction)
    if quantity is None:
        quantity = Decimal(0)
    return _View(
        timestamp=_as_utc(record.timestamp),
        asset=_asset(row, direction),
        quantity=quantity,
        wallet=record.wallet,
        destination=_destination(row),
        tx_hash=_tx_hash(row),
    )


def _wallets_differ(out: _Leg, incoming: _Leg) -> bool:
    left = _record(out.row).wallet.strip().casefold()
    right = _record(incoming.row).wallet.strip().casefold()
    if not left or not right:
        return True
    return left != right


def _same_hash(left: DataRow, right: DataRow) -> bool:
    first = _tx_hash(left)
    second = _tx_hash(right)
    return bool(first) and first.casefold() == second.casefold()


def _direction(t_type: Union[TrType, UnmappedType]) -> Optional[str]:
    if t_type in _OUT_TYPES:
        return "out"
    if t_type in _IN_TYPES:
        return "in"
    return None


def _sort_key(leg: _Leg) -> Tuple[datetime, str, Decimal, str, str]:
    view = _view(leg)
    return (view.timestamp, view.asset, view.quantity, view.wallet, view.tx_hash)


def _view(leg: _Leg) -> _View:
    record = _record(leg.row)
    quantity = _quantity(leg.row, leg.direction)
    if quantity is None:
        raise RuntimeError("Missing quantity")
    return _View(
        timestamp=_as_utc(record.timestamp),
        asset=_asset(leg.row, leg.direction),
        quantity=quantity,
        wallet=record.wallet,
        destination=_destination(leg.row),
        tx_hash=_tx_hash(leg.row),
    )


def _summary(action: str, view: _View, detail: str) -> SummaryRow:
    return SummaryRow(
        action=action,
        timestamp=view.timestamp,
        asset=view.asset,
        quantity=view.quantity,
        wallet=view.wallet,
        destination=view.destination,
        tx_hash=view.tx_hash,
        detail=detail,
    )


def _review(
    leg: _Leg, view: _View, disposition: str, description: Optional[str] = None
) -> ReviewRow:
    return ReviewRow(
        timestamp=view.timestamp,
        asset=view.asset,
        quantity=view.quantity,
        wallet=view.wallet,
        destination=view.destination,
        tx_hash=view.tx_hash,
        description=_description(leg.row) if description is None else description,
        disposition=disposition,
    )


def _description(row: DataRow) -> str:
    for key in ("Description", "description", "Note"):
        if key in row.row_dict and row.row_dict[key].strip():
            return row.row_dict[key].strip()
    if row.t_record is not None and row.t_record.note:
        return row.t_record.note
    return ""


def _record(row: DataRow) -> TransactionOutRecord:
    if row.t_record is None:
        raise RuntimeError("Missing t_record")
    return row.t_record


def _quantity(row: DataRow, direction: str) -> Optional[Decimal]:
    record = _record(row)
    if direction == "out":
        return record.sell_quantity
    return record.buy_quantity


def _asset(row: DataRow, direction: str) -> str:
    record = _record(row)
    if direction == "out":
        return record.sell_asset
    return record.buy_asset


def _tag(row: DataRow) -> Optional[str]:
    for key in _TAG_KEYS:
        if key in row.row_dict:
            return row.row_dict[key].strip()
    return None


def _chain(row: DataRow) -> str:
    for key in _CHAIN_KEYS:
        if key in row.row_dict and row.row_dict[key].strip():
            return row.row_dict[key].strip()
    return ""


def _destination_candidates(row: DataRow) -> Tuple[str, ...]:
    values: List[str] = []
    if row.tx_raw is not None:
        _add_text(values, _at(row, row.tx_raw.tx_dest_pos))
    for key in _DESTINATION_KEYS:
        if key in row.row_dict:
            _add_text(values, row.row_dict[key])
    return tuple(values)


def _destination(row: DataRow) -> str:
    candidates = _destination_candidates(row)
    if candidates:
        return candidates[0]
    return ""


def _tx_hash(row: DataRow) -> str:
    if row.tx_raw is not None:
        raw = _at(row, row.tx_raw.tx_hash_pos)
        if raw:
            return raw
    if "TxHash" in row.row_dict:
        return row.row_dict["TxHash"].strip()
    return ""


def _at(row: DataRow, pos: Optional[int]) -> str:
    if pos is None or pos < 0 or pos >= len(row.row):
        return ""
    return row.row[pos].strip()


def _add_text(values: List[str], text: str) -> None:
    cleaned = text.strip()
    if cleaned and cleaned not in values:
        values.append(cleaned)


def _append_note(record: TransactionOutRecord, note: str) -> None:
    if record.note:
        record.note = f"{record.note} | {note}"
    else:
        record.note = note


def _type_name(t_type: Union[TrType, UnmappedType]) -> str:
    if isinstance(t_type, TrType):
        return t_type.value
    return str(t_type)


def _as_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=TZ_UTC)
    return timestamp.astimezone(TZ_UTC)


def _fmt_qty(quantity: Decimal) -> str:
    return f"{quantity.normalize():f}"


def _summary_cells(row: SummaryRow) -> List[str]:
    return [
        row.action,
        _fmt_ts(row.timestamp),
        row.asset,
        _fmt_qty(row.quantity),
        row.wallet,
        row.destination,
        row.tx_hash,
        row.detail,
    ]


def _review_cells(row: ReviewRow) -> List[str]:
    return [
        _fmt_ts(row.timestamp),
        row.asset,
        _fmt_qty(row.quantity),
        row.wallet,
        row.destination,
        row.tx_hash,
        row.description,
        row.disposition,
    ]


def _fmt_ts(timestamp: datetime) -> str:
    return f"{_as_utc(timestamp):%Y-%m-%d %H:%M:%S} UTC"


def _write_csv(path: str, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
