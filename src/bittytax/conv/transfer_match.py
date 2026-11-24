# -*- coding: utf-8 -*-

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..bt_types import TrType, UnmappedType
from ..constants import TZ_UTC
from .datarow import DataRow
from .out_record import TransactionOutRecord
from .reconcile_types import ReconcileResult, ReviewRow, SummaryRow
from .txref import hashes_equal, is_confusable_official, related_official
from .wallets import (
    MatchSettings,
    RegistryEntry,
    TokenGroup,
    WalletRegistry,
    find_entry,
    wallet_is_cex,
)

# Accounting tool rejects this type, so an unmatched withdrawal cannot be taxed as a gift
# until the user edits the row.
NEEDS_REVIEW_TYPE = "_Needs-Review"
POSSIBLE_SPAM_TYPE = "_Possible-Spam"

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
class TransferLeg:
    row: DataRow
    direction: str
    ambiguous: bool
    eligible: bool
    consumed: bool = False
    own_entry: Optional[RegistryEntry] = None
    payee_entry: Optional[RegistryEntry] = None
    cex: bool = False


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
    # Imported here so this module can finish loading before the pairing rules
    # import it back.
    from .allocations import (  # pylint: disable=import-outside-toplevel
        apply_allocation_links,
        mark_allocation_payments,
    )
    from .pairing import (  # pylint: disable=import-outside-toplevel,cyclic-import
        classify_before_pair,
        flag_unresolved,
        pair_bridges,
        pair_by_hash,
        pair_cex_chain,
        pair_fuzzy,
        pair_same_wallet_swaps,
        pair_swaps,
    )

    classify_before_pair(rows, registry, result)
    mark_allocation_payments(rows, registry, result)
    legs = [leg for leg in (_leg_for(row, registry) for row in rows) if leg is not None]
    pair_by_hash(legs, registry, result)
    pair_cex_chain(legs, registry, result)
    pair_bridges(legs, registry, result)
    pair_swaps(legs, registry, result)
    pair_same_wallet_swaps(legs, registry, result)
    _pair(legs, registry.match, result)
    pair_fuzzy(legs, registry, result)
    flag_unresolved(legs, registry, result)
    _finish_unmatched(legs, legacy_gifts, review_as, result, registry)
    apply_allocation_links(rows, registry, result)
    if registry.match.share_pools:
        _share_pools(rows, containers, registry.match, result)
    return result


def count_actions(result: ReconcileResult) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in result.summary:
        counts[row.action] = counts.get(row.action, 0) + 1
    return counts


def reconciliation_report_paths(output_filename: Optional[str]) -> Tuple[str, str, str, str]:
    if output_filename:
        stem, _ext = os.path.splitext(output_filename)
        if not stem:
            stem = "BittyTax_Records"
    else:
        stem = "BittyTax_Records"
    return (
        f"{stem}-transfer-summary.csv",
        f"{stem}-needs-review.csv",
        f"{stem}-possible-spam.csv",
        f"{stem}-review-queue.csv",
    )


def write_reconciliation_reports(
    result: ReconcileResult,
    summary_path: str,
    review_path: str,
    spam_path: str = "",
    queue_path: str = "",
) -> None:
    _write_csv(summary_path, _SUMMARY_HEADER, [_summary_cells(row) for row in result.summary])
    _write_csv(review_path, _REVIEW_HEADER, [_review_cells(row) for row in result.review])
    if spam_path:
        _write_csv(spam_path, _REVIEW_HEADER, [_review_cells(row) for row in result.spam])
    if queue_path:
        _write_csv(queue_path, _REVIEW_HEADER, [_review_cells(row) for row in result.queue])


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
        f"{counts.get('assumed-market', 0)} market acquisitions",
        f"{counts.get('possible-spam', 0)} possible spam",
    ]
    lines = ["transfer reconciliation: " + ", ".join(parts)]
    if result.queue:
        lines.append(
            f"{len(result.queue)} row(s) are on the review queue. The year-end "
            "integrity check fails until this queue is empty or every leftover is "
            "listed in review_exceptions."
        )
    elif counts.get("needs-review", 0):
        lines.append(
            f"{counts['needs-review']} unmatched row(s) are {NEEDS_REVIEW_TYPE}. "
            "The default --review-as conservative acquires unmatched deposits at "
            "market value and leaves unmatched withdrawals on the review queue. "
            "bittytax then fails the integrity check until that queue is empty "
            "or every leftover is on an explicit exception list."
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


def _pair(legs: Sequence[TransferLeg], settings: MatchSettings, result: ReconcileResult) -> None:
    outs = [leg for leg in legs if leg.direction == "out" and leg.eligible and not leg.consumed]
    ins = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    outs.sort(key=_sort_key)
    ins.sort(key=_sort_key)
    # Imported here: pairing already imports this module while it is loading.
    from .pairing import LegIndex  # pylint: disable=import-outside-toplevel,cyclic-import

    index = LegIndex(ins, settings)
    # A mirror is the same timestamp recorded in reverse, so only that instant is checked.
    applied_at: Dict[datetime, List[Tuple[TransferLeg, TransferLeg]]] = {}
    for out in outs:
        if out.consumed:
            continue
        stamp = _as_utc(_record(out.row).timestamp)
        chosen = _best_deposit(
            out,
            index.asset_window(out, settings.window),
            settings,
            applied_at.get(stamp, ()),
        )
        if chosen is None:
            continue
        result.match_seq += 1
        _apply_match(out, chosen, result.match_seq, settings, result)
        applied_at.setdefault(stamp, []).append((out, chosen))


def _best_deposit(
    out: TransferLeg,
    ins: Sequence[TransferLeg],
    settings: MatchSettings,
    applied: Sequence[Tuple[TransferLeg, TransferLeg]],
) -> Optional[TransferLeg]:
    chosen: Optional[TransferLeg] = None
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


def _compatible(out: TransferLeg, incoming: TransferLeg, settings: MatchSettings) -> bool:
    if out.row is incoming.row:
        return False
    if not _wallets_differ(out, incoming):
        return False
    if not _assets_compatible(
        _asset(out.row, "out"), _asset(incoming.row, "in"), settings.token_groups, settings
    ):
        return False
    out_ts = _as_utc(_record(out.row).timestamp)
    in_ts = _as_utc(_record(incoming.row).timestamp)
    if abs(out_ts - in_ts) > _time_limit(out, incoming, settings):
        return False
    return _amounts_match(out.row, incoming.row, settings)


def _time_limit(out: TransferLeg, incoming: TransferLeg, settings: MatchSettings) -> timedelta:
    if out.cex or incoming.cex:
        return min(settings.window, settings.cex_window)
    return settings.window


def _score(
    out: TransferLeg, incoming: TransferLeg, settings: MatchSettings
) -> Tuple[Decimal, timedelta, int]:
    # Closest amount, then closest time. A shared transaction hash only breaks a tie,
    # so a small send does not take a worse deposit just because the hash matches.
    same_hash = 0 if _same_hash(out.row, incoming.row) else 1
    out_ts = _as_utc(_record(out.row).timestamp)
    in_ts = _as_utc(_record(incoming.row).timestamp)
    return (_amount_gap(out.row, incoming.row, settings), abs(out_ts - in_ts), same_hash)


def _is_mirror(
    out_a: TransferLeg,
    in_a: TransferLeg,
    out_b: TransferLeg,
    in_b: TransferLeg,
    settings: MatchSettings,
) -> bool:
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
        _asset(out_a.row, "out"), _asset(out_b.row, "out"), settings.token_groups, settings
    ):
        return False
    qty_a = _quantity(out_a.row, "out")
    qty_b = _quantity(out_b.row, "out")
    if qty_a is None or qty_b is None:
        return False
    return _amounts_close(qty_a, qty_b, settings)


def _wallet(leg: TransferLeg) -> str:
    return _record(leg.row).wallet.strip().casefold()


def record_transfer_pair(
    out: TransferLeg,
    incoming: TransferLeg,
    settings: MatchSettings,
    result: ReconcileResult,
    how: str,
    action: str = "matched",
) -> None:
    """Pair two legs and log the rule that decided it."""
    result.match_seq += 1
    _apply_match(out, incoming, result.match_seq, settings, result, how, action)


def _apply_match(
    out: TransferLeg,
    incoming: TransferLeg,
    match_id: int,
    settings: MatchSettings,
    result: ReconcileResult,
    how: str = "",
    action: str = "matched",
) -> None:
    view = _view(out)
    detail = _align_quantities(out.row, incoming.row, settings)
    own = f"; own address {out.own_entry.label}" if out.own_entry else ""
    note = f"transfer match {match_id}{own}"
    if how:
        note = f"{note}; {how}"
    _reclassify_out(out.row, note)
    _reclassify_in(incoming.row, note)
    out.consumed = True
    incoming.consumed = True
    in_view = _view(incoming)
    prefix = f"{how}; " if how else ""
    result.summary.append(
        _summary(
            action,
            view,
            f"{prefix}match {match_id} with {in_view.quantity} {in_view.asset} "
            f"in {in_view.wallet or 'unknown wallet'}; {detail}{own}",
        )
    )


def _finish_unmatched(
    legs: Sequence[TransferLeg],
    legacy_gifts: bool,
    review_as: str,
    result: ReconcileResult,
    registry: WalletRegistry,
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
            _record_unmatched(leg, view, legacy_gifts, review_as, result, registry)


def _record_own_address(leg: TransferLeg, view: _View, result: ReconcileResult) -> None:
    assert leg.own_entry is not None
    label = leg.own_entry.label
    changed = _reclassify_out(leg.row, f"own address: {label}")
    if changed:
        detail = f"destination {label}; no deposit matched; reclassified to Withdrawal"
    else:
        detail = f"destination {label}; already a withdrawal; no deposit matched"
    result.summary.append(_summary("own-address", view, detail))


def _record_payee(leg: TransferLeg, view: _View, result: ReconcileResult) -> None:
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
    leg: TransferLeg,
    view: _View,
    legacy_gifts: bool,
    review_as: str,
    result: ReconcileResult,
    registry: WalletRegistry,
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

    if _is_spam(view.asset, registry):
        _record(leg.row).t_type = UnmappedType(POSSIBLE_SPAM_TYPE)
        _append_note(_record(leg.row), "possible spam")
        result.summary.append(
            _summary("possible-spam", view, f"untagged {kind} listed as possible spam")
        )
        result.spam.append(_review(leg, view, "possible-spam", description))
        return

    if review_as == "conservative":
        _queue_or_acquire(leg, view, kind, description, result)
        return

    treatment = review_as if review_as in ("disposal", "transfer", "market") else "reject"
    if treatment == "reject":
        _mark_needs_review(leg, view, kind, description, result, queue=False)
        return

    _apply_review_type(leg, treatment)
    action = {
        "disposal": "assumed-disposal",
        "transfer": "assumed-transfer",
        "market": "assumed-market",
    }[treatment]
    result.summary.append(
        _summary(
            action, view, f"untagged {kind} treated as {treatment} so a report can be produced"
        )
    )
    result.review.append(_review(leg, view, action, description))


def _queue_or_acquire(
    leg: TransferLeg,
    view: _View,
    kind: str,
    description: str,
    result: ReconcileResult,
) -> None:
    if leg.direction == "out":
        _mark_needs_review(leg, view, kind, description, result, queue=True)
        return
    _apply_review_type(leg, "market")
    result.summary.append(
        _summary(
            "assumed-market",
            view,
            f"untagged {kind} acquired at market value; cost stays on the row",
        )
    )
    result.review.append(_review(leg, view, "assumed-market", description))


def _mark_needs_review(
    leg: TransferLeg,
    view: _View,
    kind: str,
    description: str,
    result: ReconcileResult,
    queue: bool,
) -> None:
    record = _record(leg.row)
    record.t_type = UnmappedType(NEEDS_REVIEW_TYPE)
    _append_note(record, f"needs review: unmatched {kind}")
    if view.tx_hash:
        _append_note(record, f"tx {view.tx_hash}")
    action = "review-queue" if queue else "needs-review"
    detail = (
        f"untagged {kind} left on the review queue"
        if queue
        else f"untagged {kind} set to {NEEDS_REVIEW_TYPE}"
    )
    result.summary.append(_summary(action, view, detail))
    item = _review(leg, view, action, description)
    result.review.append(item)
    if queue:
        result.queue.append(item)


def _apply_review_type(leg: TransferLeg, treatment: str) -> None:
    record = _record(leg.row)
    if leg.direction == "out":
        if treatment == "disposal":
            record.t_type = TrType.GIFT_SENT
        else:
            # transfer and market both leave the send as a non-disposal
            record.t_type = TrType.WITHDRAWAL
            record.sell_value = None
    elif treatment == "market":
        record.t_type = TrType.REVIEW_ACQUISITION
    elif treatment == "disposal":
        record.t_type = TrType.GIFT_RECEIVED
    else:
        record.t_type = TrType.DEPOSIT
        record.buy_value = None
    _append_note(record, f"{_REVIEW_NOTE} {treatment}")


def _leg_for(row: DataRow, registry: WalletRegistry) -> Optional[TransferLeg]:
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
    return TransferLeg(
        row=row,
        direction=direction,
        ambiguous=ambiguous,
        eligible=_is_eligible(row.t_record.t_type, direction, ambiguous, own, payee),
        own_entry=own,
        payee_entry=payee,
        cex=wallet_is_cex(row.t_record.wallet, registry.own),
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
    if not _assets_compatible(out.sell_asset, incoming.buy_asset, settings.token_groups, settings):
        return "quantities left unchanged"

    shortfall = sent - received
    out.sell_quantity = received
    fee = out.fee_quantity if out.fee_quantity is not None else Decimal(0)
    fee_same = fee > 0 and _assets_compatible(
        out.fee_asset, out.sell_asset, settings.token_groups, settings
    )
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
        record.fee_asset, asset, settings.token_groups, settings
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


def assets_compatible(left: str, right: str, settings: MatchSettings) -> bool:
    return _assets_compatible(left, right, settings.token_groups, settings)


def amounts_match(out_row: DataRow, in_row: DataRow, settings: MatchSettings) -> bool:
    return _amounts_match(out_row, in_row, settings)


def amount_gap_of(out_row: DataRow, in_row: DataRow, settings: MatchSettings) -> Decimal:
    return _amount_gap(out_row, in_row, settings)


def _assets_compatible(
    left: str,
    right: str,
    groups: Sequence[TokenGroup],
    settings: Optional[MatchSettings] = None,
) -> bool:
    first = left.strip().upper()
    second = right.strip().upper()
    if not first or not second:
        return False
    if first == second:
        return True
    if _blocked_symbol(left, settings) or _blocked_symbol(right, settings):
        return False
    return _pair_group(first, second, groups) is not None


def _blocked_symbol(symbol: str, settings: Optional[MatchSettings]) -> bool:
    official = settings.official_assets if settings is not None else ("USDT", "BTC", "ETH")
    blocked = settings.blocked_symbols if settings is not None else ()
    patterns = settings.blocked_patterns if settings is not None else ()
    raw = symbol.strip().upper()
    if not raw or raw in {item.upper() for item in official}:
        return False
    if is_confusable_official(symbol, official) or related_official(symbol, official):
        return True
    if raw in {item.upper() for item in blocked}:
        return True
    folded = symbol.casefold()
    return any(pattern and pattern in folded for pattern in patterns)


def _pair_group(
    left: str, right: str, groups: Sequence[TokenGroup]
) -> Optional[Tuple[str, TokenGroup]]:
    """Prefer a liquid pairing over a one-to-one wrap when both exist."""
    first = left.strip().upper()
    second = right.strip().upper()
    wrap: Optional[TokenGroup] = None
    for group in groups:
        names = set(group.all_symbols())
        if first not in names or second not in names:
            continue
        liquid_names = set(group.liquid_symbols)
        if group.liquid or first in liquid_names or second in liquid_names:
            return ("liquid", group)
        if wrap is None and first in group.symbols and second in group.symbols:
            wrap = group
    if wrap is None:
        return None
    return ("wrap", wrap)


def _is_spam(asset: str, registry: WalletRegistry) -> bool:
    symbol = asset.strip().upper()
    if symbol in {item.upper() for item in registry.spam_symbols}:
        return True
    folded = asset.casefold()
    return any(pattern and pattern in folded for pattern in registry.spam_patterns)


def _matches_explicit_payee(entry: RegistryEntry, row: DataRow) -> bool:
    if entry.tx_hash and _tx_hash(row).casefold() == entry.tx_hash.casefold():
        return True
    if not (entry.on_date and entry.asset and entry.amount):
        return False
    record = _record(row)
    if _as_utc(record.timestamp).date().isoformat() != entry.on_date:
        return False
    if record.sell_asset.strip().upper() != entry.asset.strip().upper():
        return False
    if record.sell_quantity is None:
        return False
    return record.sell_quantity == Decimal(entry.amount)


def _canonical(asset: str, settings: MatchSettings, value: Optional[Decimal] = None) -> str:
    if _blocked_symbol(asset, settings):
        return asset.strip()
    if related_official(asset, settings.official_assets) and (value is None or value == 0):
        return asset.strip()
    symbol = asset.strip().upper()
    for group in settings.token_groups:
        if symbol in group.all_symbols():
            return group.canonical()
    return asset


def _find_payee(
    row: DataRow,
    entries: Sequence[RegistryEntry],
    candidates: Sequence[str],
    chain: str,
) -> Optional[RegistryEntry]:
    by_destination = find_entry(entries, *candidates, chain=chain)
    if by_destination is not None:
        return by_destination
    for entry in entries:
        if _matches_explicit_payee(entry, row):
            return entry
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
        if row.t_record is not None and str(row.t_record.t_type) == POSSIBLE_SPAM_TYPE:
            continue
        if _mark_liquid_trade(row, settings):
            result.summary.append(
                _summary(
                    "liquid-stake",
                    _view_record(row, "out"),
                    str(_record(row).note),
                )
            )
            continue
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
    record = row.t_record
    paired = (
        _pair_group(record.sell_asset, record.buy_asset, settings.token_groups)
        if record is not None and record.sell_asset and record.buy_asset
        else None
    )
    quantities_ok = (
        record is not None
        and record.t_type is TrType.TRADE
        and record.sell_quantity is not None
        and record.buy_quantity is not None
        and paired is not None
        and paired[0] == "wrap"
        and _amounts_close(record.sell_quantity, record.buy_quantity, settings)
    )
    if record is None or not quantities_ok:
        return None
    if record.sell_quantity is None or record.buy_quantity is None:
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


def _mark_liquid_trade(row: DataRow, settings: MatchSettings) -> bool:
    if row.t_record is None or row.t_record.t_type is not TrType.TRADE:
        return False
    record = row.t_record
    if not record.sell_asset or not record.buy_asset:
        return False
    paired = _pair_group(record.sell_asset, record.buy_asset, settings.token_groups)
    if paired is None or paired[0] != "liquid":
        return False
    group = paired[1]
    _append_note(
        record,
        f"liquid staking rebase {record.sell_asset}/{record.buy_asset} into {group.canonical()}",
    )
    return True


def _rename_record(row: DataRow, settings: MatchSettings) -> None:
    if row.t_record is None:
        return
    if str(row.t_record.t_type) == POSSIBLE_SPAM_TYPE:
        return
    record = row.t_record
    if record.buy_asset:
        record.buy_asset = _canonical(record.buy_asset, settings, record.buy_value)
    if record.sell_asset:
        record.sell_asset = _canonical(record.sell_asset, settings, record.sell_value)
    if record.fee_asset:
        record.fee_asset = _canonical(record.fee_asset, settings, record.fee_value)


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


def _wallets_differ(out: TransferLeg, incoming: TransferLeg) -> bool:
    left = _record(out.row).wallet.strip().casefold()
    right = _record(incoming.row).wallet.strip().casefold()
    if not left or not right:
        return True
    return left != right


def _same_hash(left: DataRow, right: DataRow) -> bool:
    return hashes_equal(_tx_hash(left), _tx_hash(right))


def _direction(t_type: Union[TrType, UnmappedType]) -> Optional[str]:
    if t_type in _OUT_TYPES:
        return "out"
    if t_type in _IN_TYPES:
        return "in"
    return None


def _sort_key(leg: TransferLeg) -> Tuple[datetime, str, Decimal, str, str]:
    view = _view(leg)
    return (view.timestamp, view.asset, view.quantity, view.wallet, view.tx_hash)


def _view(leg: TransferLeg) -> _View:
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
    leg: TransferLeg, view: _View, disposition: str, description: Optional[str] = None
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
