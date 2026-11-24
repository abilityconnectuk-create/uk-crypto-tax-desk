# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..bt_types import TrType, UnmappedType
from ..config import config
from ..constants import TZ_UTC
from .datarow import DataRow
from .out_record import TransactionOutRecord
from .transfer_match import (
    NEEDS_REVIEW_TYPE,
    POSSIBLE_SPAM_TYPE,
    ReconcileResult,
    ReviewRow,
    SummaryRow,
    TransferLeg,
    amount_gap_of,
    amounts_match,
    assets_compatible,
    record_transfer_pair,
)
from .txref import (
    hash_kind,
    hashes_equal,
    is_confusable_official,
    normalise_tx_hash,
    redact_account_numbers,
    related_official,
)
from .wallets import (
    AddressRule,
    MatchSettings,
    OfficialContract,
    ReceiptTokenRule,
    WalletRegistry,
    find_entry,
)

_DEST_KEYS = ("TxDest", "Receiving Wallet", "To Wallet (read-only)")
_SRC_KEYS = ("TxSrc", "Sending Wallet", "From Wallet (read-only)")
_CHAIN_KEYS = ("Chain", "Blockchain", "Network")
_CONTRACT_KEYS = (
    "ContractAddress",
    "TokenContractAddress",
    "Token Contract",
    "TokenContract",
    "Contract",
)
_BUILTIN_LP_SYMBOLS = frozenset({"SLP", "BPT", "UNI-V2"})
_BUILTIN_LP_PATTERNS = ("-lp", "lp-", "/lp", "_lp", "uni-v2", "receipt")
_CONTRACT_IN_NAME = re.compile(r"\((0x[0-9a-fA-F]{8,})\)")
_FEE_WORD = re.compile(r"\b(fee|gas)\b")
_DUST_RATIO = Decimal(20)
_DUST_QTY = Decimal("0.01")
_EXPLICIT_TAGS = {"gift", "lost", "donation", "cost"}
_MARGIN_TYPES = (
    TrType.MARGIN_GAIN,
    TrType.MARGIN_LOSS,
    TrType.MARGIN_FEE,
    TrType.MARGIN_FEE_REBATE,
)
_IGNORED = "_Ignored"


def classify_before_pair(
    rows: Sequence[DataRow], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Apply typed rules that must win before a withdrawal is paired as a transfer."""
    for row in rows:
        if row.t_record is None or row.failure is not None:
            continue
        if _classify_margin(row, registry, result):
            continue
        if _classify_fiat(row, result):
            continue
        if _classify_spam(row, registry, result):
            continue
        if _classify_contract(row, registry, result):
            continue
        _classify_utility(row, registry, result)


class LegIndex:
    """Incoming legs keyed by hash, by asset, and by time.

    Each outgoing leg then looks at the rows that can still match it, instead of
    scanning every incoming leg.
    """

    def __init__(self, incoming: Sequence[TransferLeg], settings: MatchSettings) -> None:
        self._settings = settings
        self._by_hash: Dict[str, List[TransferLeg]] = {}
        asset_stamps: Dict[str, List[datetime]] = {}
        asset_legs: Dict[str, List[TransferLeg]] = {}
        all_stamps: List[datetime] = []
        all_legs: List[TransferLeg] = []
        for leg in incoming:
            digest = normalise_tx_hash(_tx_hash(leg.row))
            if digest:
                self._by_hash.setdefault(digest, []).append(leg)
            stamp = _stamp(leg.row)
            symbol = _asset(leg.row, "in").strip().upper()
            asset_stamps.setdefault(symbol, []).append(stamp)
            asset_legs.setdefault(symbol, []).append(leg)
            all_stamps.append(stamp)
            all_legs.append(leg)
        self._asset_stamps = {}
        self._asset_legs = {}
        for symbol, stamps in asset_stamps.items():
            ordered_stamps, ordered_legs = _sort_legs(stamps, asset_legs[symbol])
            self._asset_stamps[symbol] = ordered_stamps
            self._asset_legs[symbol] = ordered_legs
        self._all_stamps, self._all_legs = _sort_legs(all_stamps, all_legs)

    def by_hash(self, out: TransferLeg) -> List[TransferLeg]:
        digest = normalise_tx_hash(_tx_hash(out.row))
        if not digest:
            return []
        return self._by_hash.get(digest, [])

    def asset_window(self, out: TransferLeg, window: timedelta) -> List[TransferLeg]:
        symbols = _related_symbols(_asset(out.row, "out"), self._settings)
        return self._collect(symbols, out, window)

    def time_window(self, out: TransferLeg, window: timedelta) -> List[TransferLeg]:
        stamp = _stamp(out.row)
        return _slice(self._all_stamps, self._all_legs, stamp - window, stamp + window)

    def _collect(
        self, symbols: Sequence[str], out: TransferLeg, window: timedelta
    ) -> List[TransferLeg]:
        stamp = _stamp(out.row)
        start = stamp - window
        end = stamp + window
        found: List[TransferLeg] = []
        seen = set()
        for symbol in symbols:
            stamps = self._asset_stamps.get(symbol, [])
            legs = self._asset_legs.get(symbol, [])
            for leg in _slice(stamps, legs, start, end):
                ident = id(leg)
                if ident in seen:
                    continue
                seen.add(ident)
                found.append(leg)
        return found


def _sort_legs(
    stamps: Sequence[datetime], legs: Sequence[TransferLeg]
) -> Tuple[List[datetime], List[TransferLeg]]:
    paired = sorted(zip(stamps, legs), key=lambda item: item[0])
    if not paired:
        return [], []
    ordered_stamps, ordered_legs = zip(*paired)
    return list(ordered_stamps), list(ordered_legs)


def _slice(
    stamps: Sequence[datetime],
    legs: Sequence[TransferLeg],
    start: datetime,
    end: datetime,
) -> List[TransferLeg]:
    left = bisect_left(stamps, start)
    right = bisect_right(stamps, end)
    return list(legs[left:right])


def _related_symbols(asset: str, settings: MatchSettings) -> List[str]:
    symbol = asset.strip().upper()
    found = [symbol]
    for group in settings.token_groups:
        names = [item.upper() for item in group.all_symbols()]
        if symbol not in names:
            continue
        for name in names:
            if name not in found:
                found.append(name)
    return found


def _union_legs(*groups: Sequence[TransferLeg]) -> List[TransferLeg]:
    found: List[TransferLeg] = []
    seen = set()
    for group in groups:
        for leg in group:
            ident = id(leg)
            if ident in seen:
                continue
            seen.add(ident)
            found.append(leg)
    return found


def pair_by_hash(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Pair legs that share a transaction hash after 0x and case normalisation."""
    incoming = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    index = LegIndex(incoming, registry.match)
    for out in legs:
        if out.direction != "out" or not out.eligible or out.consumed:
            continue
        raw = _tx_hash(out.row)
        if not raw:
            continue
        chosen = _closest(out, index.by_hash(out), registry, _hash_candidate)
        if chosen is None:
            continue
        how = "tx hash normalised"
        action = "matched"
        if _chains_differ(out.row, chosen.row):
            how = "tx hash normalised; same token, different chain"
            action = "bridge-transfer"
        record_transfer_pair(out, chosen, registry.match, result, how, action)


def pair_cex_chain(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Pair an exchange id with a chain hash when destination, amount and time agree."""
    incoming = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    index = LegIndex(incoming, registry.match)
    for out in legs:
        if out.direction != "out" or not out.eligible or out.consumed:
            continue
        chosen = _closest(
            out, index.asset_window(out, registry.match.cex_window), registry, _cex_candidate
        )
        if chosen is None:
            continue
        record_transfer_pair(
            out,
            chosen,
            registry.match,
            result,
            "exchange id paired with chain hash by destination, amount and time",
            "matched-cex",
        )


def pair_bridges(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    incoming = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    index = LegIndex(incoming, registry.match)
    for out in legs:
        if out.direction != "out" or not out.eligible or out.consumed:
            continue
        if not _bridge_evidence(out.row, registry):
            continue
        chosen = _closest_bridge_in(
            out, index.time_window(out, registry.match.bridge_window), registry
        )
        if chosen is None:
            continue
        if assets_compatible(_asset(out.row, "out"), _asset(chosen.row, "in"), registry.match):
            record_transfer_pair(
                out,
                chosen,
                registry.match,
                result,
                "bridge; same token, different chain",
                "bridge-transfer",
            )
        else:
            _merge_exchange(
                out,
                chosen,
                result,
                "bridge-exchange",
                "bridge; different token treated as an exchange",
            )


def pair_same_wallet_swaps(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """One principal out and a different principal in. Off unless the setting is on.

    Fee and dust legs are not principals. A hash that names two of your own
    wallets is left for transfer handling. An out with no principal in stays
    unmatched. The fee's local value is added to the acquired cost.
    """
    if not registry.match.same_wallet_swaps:
        return
    grouped: Dict[str, List[TransferLeg]] = {}
    for leg in legs:
        if not leg.eligible or not _leg_readable(leg):
            continue
        digest = normalise_tx_hash(_tx_hash(leg.row))
        if not digest:
            continue
        grouped.setdefault(digest, []).append(leg)
    for group in grouped.values():
        if _several_own_wallets(group, registry):
            continue
        _pair_wallet_group(
            [leg for leg in group if _swap_in_scope(leg.row, registry)], legs, registry, result
        )


def pair_receipt_tokens(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """A listed venue deposit with a same-hash receipt in-leg is one exchange.

    The list is empty unless you set it. A deposit with no receipt leg stays
    on review.
    """
    rules = registry.receipt_tokens
    if not rules:
        return
    grouped: Dict[str, List[TransferLeg]] = {}
    for leg in legs:
        if not leg.eligible or not _leg_readable(leg):
            continue
        digest = normalise_tx_hash(_tx_hash(leg.row))
        if digest:
            grouped.setdefault(digest, []).append(leg)
    for group in grouped.values():
        _apply_receipt_rules(group, rules, result)


def pair_swaps(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    incoming = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    index = LegIndex(incoming, registry.match)
    for out in legs:
        if out.direction != "out" or not out.eligible or out.consumed:
            continue
        rule = _rule_on(out.row, registry, "swap", "out")
        if rule is None:
            continue
        pool = _union_legs(index.time_window(out, registry.match.window), index.by_hash(out))
        chosen = _closest(out, pool, registry, _swap_candidate)
        if chosen is None:
            continue
        _merge_exchange(out, chosen, result, "swap", f"swap via {rule.label}")


def pair_fuzzy(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Own-wallet match. At least two of hash, address, and amount-plus-time."""
    incoming = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    index = LegIndex(incoming, registry.match)
    for out in legs:
        if out.direction != "out" or not out.eligible or out.consumed:
            continue
        # The window depends on the incoming leg, so this uses the widest window.
        chosen = _closest(
            out, index.asset_window(out, registry.match.bridge_window), registry, _fuzzy_candidate
        )
        if chosen is None:
            continue
        record_transfer_pair(
            out,
            chosen,
            registry.match,
            result,
            "fuzzy own-wallet match; two of hash, address, amount and time",
            "matched-fuzzy",
        )


def flag_unresolved(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    for leg in legs:
        if leg.consumed or not leg.eligible:
            continue
        if leg.direction == "out" and _bridge_evidence(leg.row, registry):
            _flag(leg, result, "missing-destination", "missing destination-chain deposit")
            continue
        if _rule_on(leg.row, registry, "swap", leg.direction) is not None:
            _flag(leg, result, "swap", "swap with no counter-leg; left on the review queue")


def _classify_margin(row: DataRow, registry: WalletRegistry, result: ReconcileResult) -> bool:
    record = _record(row)
    if record.t_type in _MARGIN_TYPES:
        _log(result, "margin-internal", row, _side(record), "already margin; not a chain transfer")
        return True
    direction = _side(record)
    if direction is None:
        return False
    asset = _asset(row, direction)
    text = _text(row)
    suffix = any(asset.upper().endswith(item.upper()) for item in registry.margin.suffixes if item)
    pattern = any(item and item in text for item in registry.margin.patterns)
    if not suffix and not pattern:
        return False
    record.t_type = TrType.MARGIN_GAIN if direction == "in" else TrType.MARGIN_LOSS
    _note(record, "margin/internal; not a chain transfer")
    _log(result, "margin-internal", row, direction, "classed as margin; not a chain transfer")
    return True


def _classify_fiat(row: DataRow, result: ReconcileResult) -> bool:
    record = _record(row)
    direction = _side(record)
    if direction is None:
        return False
    if record.t_type not in (
        TrType.GIFT_SENT,
        TrType.GIFT_RECEIVED,
        TrType.DEPOSIT,
        TrType.WITHDRAWAL,
    ):
        return False
    if _asset(row, direction) not in config.fiat_list:
        return False
    _redact_fiat(row)
    if record.t_type is TrType.GIFT_SENT:
        record.t_type = TrType.WITHDRAWAL
        record.sell_value = None
    elif record.t_type is TrType.GIFT_RECEIVED:
        record.t_type = TrType.DEPOSIT
        record.buy_value = None
    _note(record, "fiat movement")
    _log(result, "fiat", row, direction, "fiat movement; bank account numbers are not stored")
    return True


def _classify_spam(row: DataRow, registry: WalletRegistry, result: ReconcileResult) -> bool:
    record = _record(row)
    reason = _poison_reason(row, registry)
    assets = [asset for asset in (record.sell_asset, record.buy_asset) if asset]
    if reason is None:
        for asset in assets:
            reason = _contract_spam_reason(row, asset, registry)
            if reason:
                break
    if reason:
        _mark_spam(row, result, reason)
        return True
    for asset in assets:
        if _spam_asset(asset, registry) or _zero_lookalike(record, asset, registry):
            _mark_spam(row, result, "lookalike or spam; not merged")
            return True
        if is_confusable_official(asset, registry.match.official_assets):
            record.t_type = UnmappedType(NEEDS_REVIEW_TYPE)
            _note(record, "lookalike of an official asset; not merged into that asset")
            _log(
                result,
                "lookalike",
                row,
                _side(record),
                "confusable ticker kept apart from the official asset",
            )
            item = _review(row, _side(record), "lookalike")
            result.review.append(item)
            result.queue.append(item)
            return True
    return False


def _mark_spam(row: DataRow, result: ReconcileResult, reason: str) -> None:
    """Drop the row. It is not a disposal, a transfer, or a wallet movement."""
    record = _record(row)
    record.t_type = UnmappedType(POSSIBLE_SPAM_TYPE)
    _note(record, reason)
    _log(result, "possible-spam", row, _side(record), reason)
    result.spam.append(_review(row, _side(record), "possible-spam", reason))


def _classify_contract(row: DataRow, registry: WalletRegistry, result: ReconcileResult) -> bool:
    record = _record(row)
    direction = _side(record)
    if direction is None:
        return False
    rule = _rule_on(row, registry, "", direction)
    if rule is None or rule.category in ("swap", "bridge"):
        return False
    if rule.category == "transfer-to-venue":
        if direction != "out":
            return False
        _as_transfer(record, direction)
        _note(record, f"transfer-to-venue: {rule.label}")
        _log(result, "transfer-to-venue", row, direction, f"destination {rule.label}")
        return True
    kind = _position_kind(rule.category, direction)
    treatment = _treatment_for(rule.category, registry)
    _apply_treatment(row, direction, kind, treatment, rule.label, result)
    return True


def _classify_utility(row: DataRow, registry: WalletRegistry, result: ReconcileResult) -> bool:
    record = _record(row)
    direction = _side(record)
    if direction is None or _tag(row).casefold() in _EXPLICIT_TAGS:
        return False
    if _owned_or_payee(row, registry, direction):
        return False
    text = _text(row)
    rules = sorted(registry.utilities, key=lambda rule: len(rule.match), reverse=True)
    for rule in rules:
        if rule.match and rule.match in text:
            _apply_utility(row, direction, rule.match, rule.treatment, result)
            return True
    return False


def _apply_treatment(
    row: DataRow,
    direction: str,
    kind: str,
    treatment: str,
    label: str,
    result: ReconcileResult,
) -> None:
    record = _record(row)
    detail = (
        f"{kind} at {label}: tax treatment '{treatment}' is a configured option, "
        "not a confirmed rule"
    )
    _note(record, detail)
    if treatment == "transfer":
        _as_transfer(record, direction)
        _log(result, kind, row, direction, detail)
        return
    if treatment == "disposal":
        if direction == "out":
            record.t_type = TrType.SPEND
        else:
            record.t_type = TrType.GIFT_RECEIVED
            row.row_dict["Tag"] = "configured"
        _log(result, kind, row, direction, detail)
        return
    record.t_type = UnmappedType(NEEDS_REVIEW_TYPE)
    _remember_hash(record, row)
    _log(result, kind, row, direction, detail)
    item = _review(row, direction, kind)
    result.review.append(item)
    result.queue.append(item)


def _apply_utility(
    row: DataRow, direction: str, pattern: str, treatment: str, result: ReconcileResult
) -> None:
    record = _record(row)
    if treatment == "ignore":
        record.t_type = UnmappedType(_IGNORED)
        _note(record, f"ignored by utility rule '{pattern}'")
        _log(result, "utility-ignore", row, direction, f"utility rule '{pattern}' set to ignore")
        return
    if treatment == "income" and direction == "in":
        record.t_type = TrType.INCOME
        _note(record, f"utility income '{pattern}'")
        _log(result, "utility-income", row, direction, f"utility rule '{pattern}' set to income")
        return
    if treatment == "income":
        record.t_type = UnmappedType(NEEDS_REVIEW_TYPE)
        _note(record, f"utility income rule '{pattern}' does not apply to a withdrawal")
        _log(result, "utility-income", row, direction, "withdrawal left on the review queue")
        item = _review(row, direction, "utility-income")
        result.review.append(item)
        result.queue.append(item)
        return
    if direction == "out":
        record.t_type = TrType.SPEND
    else:
        record.t_type = TrType.REVIEW_ACQUISITION
    _note(record, f"dust conversion '{pattern}'")
    _log(result, "utility-dust", row, direction, f"utility rule '{pattern}' set to dust conversion")


def _hash_candidate(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    if not hashes_equal(_tx_hash(out.row), _tx_hash(incoming.row)):
        return False
    if not _wallets_differ(out, incoming):
        return False
    return assets_compatible(_asset(out.row, "out"), _asset(incoming.row, "in"), registry.match)


def _cex_candidate(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    kinds = {hash_kind(_tx_hash(out.row)), hash_kind(_tx_hash(incoming.row))}
    if kinds != {"chain", "exchange"}:
        return False
    if not _wallets_differ(out, incoming):
        return False
    if not assets_compatible(_asset(out.row, "out"), _asset(incoming.row, "in"), registry.match):
        return False
    if not amounts_match(out.row, incoming.row, registry.match):
        return False
    if _gap(out, incoming) > registry.match.cex_window:
        return False
    return _dest_links(out, incoming, registry)


def _bridge_candidate(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    if not _wallets_differ(out, incoming):
        return False
    if _gap(out, incoming) > registry.match.bridge_window:
        return False
    same = assets_compatible(_asset(out.row, "out"), _asset(incoming.row, "in"), registry.match)
    if same and not amounts_match(out.row, incoming.row, registry.match):
        return False
    if same:
        return _chains_differ(out.row, incoming.row) or _bridge_evidence(incoming.row, registry)
    return (
        _bridge_evidence(incoming.row, registry)
        or hashes_equal(_tx_hash(out.row), _tx_hash(incoming.row))
        or _chains_differ(out.row, incoming.row)
    )


def _closest_bridge_in(
    out: TransferLeg, incoming: Sequence[TransferLeg], registry: WalletRegistry
) -> Optional[TransferLeg]:
    """Closest unused in-leg whose amount fits. The same asset is preferred.

    A later exact amount does not beat a nearer compatible one. A different
    asset does not win when a same-asset leg also fits.
    """
    same: List[TransferLeg] = []
    changed: List[TransferLeg] = []
    for leg in incoming:
        if leg.consumed or leg.row is out.row or not _bridge_candidate(out, leg, registry):
            continue
        if not amounts_match(out.row, leg.row, registry.match):
            continue
        if assets_compatible(_asset(out.row, "out"), _asset(leg.row, "in"), registry.match):
            same.append(leg)
        else:
            changed.append(leg)
    pool = same or changed
    if not pool:
        return None
    return min(pool, key=_gap_from(out))


def _gap_from(out: TransferLeg) -> Callable[[TransferLeg], timedelta]:
    def _measure(incoming: TransferLeg) -> timedelta:
        return _gap(out, incoming)

    return _measure


def _pair_wallet_group(
    group: Sequence[TransferLeg],
    legs: Sequence[TransferLeg],
    registry: WalletRegistry,
    result: ReconcileResult,
) -> None:
    by_wallet: Dict[str, List[TransferLeg]] = {}
    for leg in group:
        name = _record(leg.row).wallet.strip().casefold()
        if name:
            by_wallet.setdefault(name, []).append(leg)
    for wallet_legs in by_wallet.values():
        _pair_one_wallet(wallet_legs, legs, registry, result)


def _pair_one_wallet(
    wallet_legs: Sequence[TransferLeg],
    legs: Sequence[TransferLeg],
    registry: WalletRegistry,
    result: ReconcileResult,
) -> None:
    # Judge fee and dust before the merge. The merge clears the received record.
    principals = [leg for leg in wallet_legs if not _dust_or_fee(leg, legs)]
    outs = [leg for leg in principals if leg.direction == "out"]
    incoming = [leg for leg in principals if leg.direction == "in"]
    if len(outs) != 1 or len(incoming) != 1:
        return
    out, received = outs[0], incoming[0]
    if assets_compatible(_asset(out.row, "out"), _asset(received.row, "in"), registry.match):
        return
    if _lp_looking(out, received, registry):
        detail = "LP or receipt token left on review"
        _flag(out, result, "lp-review", detail)
        _flag(received, result, "lp-review", detail)
        return
    _merge_exchange(out, received, result, "same-wallet-swap", _swap_detail(out))
    fees = [leg for leg in wallet_legs if leg not in principals]
    _fold_fee_into_cost(out, fees)


def _swap_detail(out: TransferLeg) -> str:
    if _blank_destination(out.row):
        return "same-hash swap on one wallet; destination blank"
    return "same-hash swap on one wallet"


def _fold_fee_into_cost(out: TransferLeg, fees: Sequence[TransferLeg]) -> None:
    """Acquired cost is the outgoing local value plus each fee leg's local value."""
    record = _record(out.row)
    extra = Decimal(0)
    for leg in fees:
        if not _leg_readable(leg):
            continue
        value = _leg_value(leg)
        if value is not None:
            extra += value
        leg.row.t_record = None
        leg.consumed = True
    if record.sell_value is not None:
        record.buy_value = record.sell_value + extra
    elif extra and record.buy_value is not None:
        record.buy_value = record.buy_value + extra
    elif extra:
        record.buy_value = extra
    if extra:
        _note(record, "fee included in the acquired cost")


def _several_own_wallets(group: Sequence[TransferLeg], registry: WalletRegistry) -> bool:
    names = set()
    for leg in group:
        wallet = _record(leg.row).wallet.strip()
        if wallet and _named_own_wallet(wallet, registry):
            names.add(wallet.casefold())
    return len(names) >= 2


def _named_own_wallet(wallet: str, registry: WalletRegistry) -> bool:
    folded = wallet.strip().casefold()
    for entry in registry.own:
        for name in (entry.label, entry.account, entry.wallet):
            if name and name.strip().casefold() == folded:
                return True
    return False


def _dust_or_fee(leg: TransferLeg, legs: Sequence[TransferLeg]) -> bool:
    """A fee word, a tiny same-side leg, or a much smaller local value."""
    if _FEE_WORD.search(_text(leg.row)):
        return True
    quantity = _leg_quantity(leg)
    if quantity <= 0:
        return True
    asset = _asset(leg.row, leg.direction).strip().upper()
    value = _leg_value(leg)
    for other in _hash_siblings(leg, legs):
        if other is leg or not _leg_readable(other):
            continue
        other_qty = _leg_quantity(other)
        other_asset = _asset(other.row, other.direction).strip().upper()
        tiny_side = (
            other.direction == leg.direction
            and quantity < _DUST_QTY
            and other_qty >= quantity * _DUST_RATIO
        )
        same_asset = other_asset == asset and other_qty >= quantity * _DUST_RATIO
        if tiny_side or same_asset:
            return True
        other_value = _leg_value(other)
        if value is not None and value > 0 and other_value is not None:
            if other_value >= value * _DUST_RATIO:
                return True
    return False


def _hash_siblings(leg: TransferLeg, legs: Sequence[TransferLeg]) -> List[TransferLeg]:
    digest = normalise_tx_hash(_tx_hash(leg.row))
    if not digest:
        return [leg] if _leg_readable(leg) else []
    return [
        item
        for item in legs
        if _leg_readable(item) and normalise_tx_hash(_tx_hash(item.row)) == digest
    ]


def _leg_readable(leg: TransferLeg) -> bool:
    """A merged receive has no record, and a consumed leg is not judged again."""
    return not leg.consumed and leg.row.t_record is not None


def _swap_in_scope(row: DataRow, registry: WalletRegistry) -> bool:
    """Wallet and chain lists both apply. An empty list does not restrict that side."""
    wallets = registry.match.same_wallet_swap_wallets
    chains = registry.match.same_wallet_swap_chains
    if wallets and _record(row).wallet.strip().casefold() not in wallets:
        return False
    if chains and _chain(row).strip().casefold() not in chains:
        return False
    return True


def _leg_quantity(leg: TransferLeg) -> Decimal:
    record = _record(leg.row)
    if leg.direction == "in":
        return record.buy_quantity or Decimal(0)
    return record.sell_quantity or Decimal(0)


def _leg_value(leg: TransferLeg) -> Optional[Decimal]:
    record = _record(leg.row)
    if leg.direction == "in":
        return record.buy_value
    return record.sell_value


def _apply_receipt_rules(
    group: Sequence[TransferLeg], rules: Sequence[ReceiptTokenRule], result: ReconcileResult
) -> None:
    for out in group:
        if out.direction != "out" or not _leg_readable(out):
            continue
        rule = _receipt_rule(out, rules)
        if rule is None:
            continue
        incoming = _receipt_in(out, group, rule)
        if incoming is None:
            _flag(out, result, "receipt-missing", "deposit has no receipt-token in-leg")
            continue
        _merge_exchange(
            out,
            incoming,
            result,
            "receipt-exchange",
            "deposit and receipt token on one hash",
        )


def _receipt_rule(
    out: TransferLeg, rules: Sequence[ReceiptTokenRule]
) -> Optional[ReceiptTokenRule]:
    asset = _asset(out.row, "out").strip().casefold()
    for rule in rules:
        if rule.deposit.strip().casefold() != asset:
            continue
        if _venue_matches(out.row, rule.venue):
            return rule
    return None


def _venue_matches(row: DataRow, venue: str) -> bool:
    folded = venue.strip().casefold()
    if not folded:
        return False
    if _record(row).wallet.strip().casefold() == folded:
        return True
    return any(item.strip().casefold() == folded for item in _dest_candidates(row))


def _receipt_in(
    out: TransferLeg, group: Sequence[TransferLeg], rule: ReceiptTokenRule
) -> Optional[TransferLeg]:
    wanted = rule.receipt.strip().casefold()
    digest = normalise_tx_hash(_tx_hash(out.row))
    for leg in group:
        if leg.direction != "in" or not _leg_readable(leg) or leg.row is out.row:
            continue
        if normalise_tx_hash(_tx_hash(leg.row)) != digest:
            continue
        if _asset(leg.row, "in").strip().casefold() == wanted:
            return leg
    return None


def _swap_candidate(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    rule = _rule_on(out.row, registry, "swap", "out")
    if rule is None or not _wallets_differ(out, incoming):
        return False
    if assets_compatible(_asset(out.row, "out"), _asset(incoming.row, "in"), registry.match):
        return False
    if hashes_equal(_tx_hash(out.row), _tx_hash(incoming.row)):
        return True
    return _gap(out, incoming) <= registry.match.window and _touches(incoming.row, rule)


def _fuzzy_candidate(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    if not _wallets_differ(out, incoming):
        return False
    if not assets_compatible(_asset(out.row, "out"), _asset(incoming.row, "in"), registry.match):
        return False
    window = _fuzzy_window(out, incoming, registry)
    amount_time = (
        amounts_match(out.row, incoming.row, registry.match) and _gap(out, incoming) <= window
    )
    if not amount_time:
        return False
    signals = 0
    if hashes_equal(_tx_hash(out.row), _tx_hash(incoming.row)):
        signals += 1
    if _address_signal(out, incoming, registry):
        signals += 1
    if amount_time:
        signals += 1
    return signals >= 2 and _address_signal(out, incoming, registry)


def _fuzzy_window(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> timedelta:
    if _bridge_evidence(out.row, registry) or _bridge_evidence(incoming.row, registry):
        return registry.match.bridge_window
    if out.cex or incoming.cex:
        return registry.match.cex_window
    return registry.match.fuzzy_window


def _address_signal(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    out_owned = out.own_entry is not None or _wallet_owned(_record(out.row).wallet, registry)
    in_owned = _wallet_owned(_record(incoming.row).wallet, registry) or _source_owned(
        incoming.row, registry
    )
    return out_owned and in_owned


def _closest(
    out: TransferLeg,
    incoming: Sequence[TransferLeg],
    registry: WalletRegistry,
    allowed: Callable[[TransferLeg, TransferLeg, WalletRegistry], bool],
) -> Optional[TransferLeg]:
    chosen: Optional[TransferLeg] = None
    chosen_key: Optional[Tuple[Decimal, timedelta]] = None
    for leg in incoming:
        if leg.consumed or leg.row is out.row or not allowed(out, leg, registry):
            continue
        key = (amount_gap_of(out.row, leg.row, registry.match), _gap(out, leg))
        if chosen_key is None or key < chosen_key:
            chosen = leg
            chosen_key = key
    return chosen


def _merge_exchange(
    out: TransferLeg, incoming: TransferLeg, result: ReconcileResult, action: str, detail: str
) -> None:
    sent = _record(out.row)
    received = _record(incoming.row)
    in_view = _capture(incoming.row, "in")
    sent.t_type = TrType.TRADE
    sent.buy_quantity = received.buy_quantity
    sent.buy_asset = received.buy_asset
    sent.buy_value = received.buy_value
    _note(sent, detail)
    incoming.row.t_record = None
    out.consumed = True
    incoming.consumed = True
    _log(result, action, out.row, "out", detail)
    result.summary.append(
        SummaryRow(
            action=action,
            timestamp=in_view[0],
            asset=in_view[1],
            quantity=in_view[2],
            wallet=in_view[3],
            destination=in_view[4],
            tx_hash=in_view[5],
            detail=detail,
        )
    )


def _flag(leg: TransferLeg, result: ReconcileResult, action: str, detail: str) -> None:
    record = _record(leg.row)
    record.t_type = UnmappedType(NEEDS_REVIEW_TYPE)
    _note(record, detail)
    _remember_hash(record, leg.row)
    leg.consumed = True
    _log(result, action, leg.row, leg.direction, detail)
    item = _review(leg.row, leg.direction, action)
    result.review.append(item)
    result.queue.append(item)


def _bridge_evidence(row: DataRow, registry: WalletRegistry) -> bool:
    text = _text(row)
    if any(pattern and pattern in text for pattern in registry.bridges.patterns):
        return True
    return (
        _rule_on(row, registry, "bridge", "out") is not None
        or _rule_on(row, registry, "bridge", "in") is not None
    )


def _rule_on(
    row: DataRow, registry: WalletRegistry, category: str, direction: str
) -> Optional[AddressRule]:
    rules = _address_rules(registry)
    chain = _chain(row)
    candidates = _dest_candidates(row) if direction == "out" else _src_candidates(row)
    for candidate in candidates:
        if not candidate.strip():
            continue
        for rule in rules:
            if category and rule.category != category:
                continue
            if rule.chain and chain and rule.chain.casefold() != chain.casefold():
                continue
            if _norm(rule.address) == _norm(candidate):
                return rule
    return None


def _address_rules(registry: WalletRegistry) -> List[AddressRule]:
    return list(registry.venues) + list(registry.contracts) + list(registry.bridges.contracts)


def _dest_links(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    dests = _dest_candidates(out.row)
    wallet = _record(incoming.row).wallet
    others = _dest_candidates(incoming.row) + _src_candidates(incoming.row) + (wallet,)
    for dest in dests:
        for other in others:
            if other and _norm(dest) == _norm(other):
                return True
        entry = find_entry(registry.own, dest)
        if entry is not None and _names(entry.label, entry.account, entry.address, wallet):
            return True
    return False


def _touches(row: DataRow, rule: AddressRule) -> bool:
    for candidate in _dest_candidates(row) + _src_candidates(row):
        if candidate and _norm(candidate) == _norm(rule.address):
            return True
    return False


def _owned_or_payee(row: DataRow, registry: WalletRegistry, direction: str) -> bool:
    if direction != "out":
        return False
    chain = _chain(row)
    candidates = _dest_candidates(row)
    return (
        find_entry(registry.own, *candidates, chain=chain) is not None
        or find_entry(registry.payees, *candidates, chain=chain) is not None
    )


def _wallet_owned(wallet: str, registry: WalletRegistry) -> bool:
    folded = wallet.strip().casefold()
    if not folded:
        return False
    for entry in registry.own:
        if _names(entry.label, entry.account, entry.address, wallet):
            return True
    return False


def _source_owned(row: DataRow, registry: WalletRegistry) -> bool:
    return find_entry(registry.own, *_src_candidates(row), chain=_chain(row)) is not None


def _names(label: str, account: str, address: str, wallet: str) -> bool:
    folded = wallet.strip().casefold()
    values = [item.strip().casefold() for item in (label, account, address) if item.strip()]
    return folded in values


def _poison_reason(row: DataRow, registry: WalletRegistry) -> Optional[str]:
    if not registry.poison_addresses:
        return None
    chain = _chain(row)
    for rule in registry.poison_addresses:
        if rule.chain and chain and rule.chain.casefold() != chain.casefold():
            continue
        target = _norm(rule.address)
        if any(_norm(candidate) == target for candidate in _dest_candidates(row)):
            return f"address-poisoning: destination {rule.address} is a configured poison address"
        if any(_norm(candidate) == target for candidate in _src_candidates(row)):
            return f"address-poisoning: source {rule.address} is a configured poison address"
    return None


def _contract_spam_reason(row: DataRow, asset: str, registry: WalletRegistry) -> Optional[str]:
    if not registry.official_contracts or not asset.strip():
        return None
    contract = _row_contract(row, asset)
    if not contract:
        return None
    applicable = [rule for rule in registry.official_contracts if _name_matches(asset, rule)]
    if not applicable:
        return None
    chain = _chain(row)
    scoped = [
        rule
        for rule in applicable
        if not rule.chain or not chain or rule.chain.casefold() == chain.casefold()
    ]
    if any(_norm_contract(rule.address) == _norm_contract(contract) for rule in scoped):
        return None
    chain_name = chain or "any chain"
    symbol = applicable[0].asset
    return (
        f"address-poisoning: contract {contract} is not an official {chain_name} "
        f"contract for {symbol}"
    )


def _name_matches(asset: str, rule: OfficialContract) -> bool:
    names = {rule.asset.casefold()}
    names.update(name.casefold() for name in rule.names)
    folded = asset.strip().casefold()
    prefix = folded.split("(", 1)[0].strip()
    return bool(prefix) and (folded in names or prefix in names)


def _row_contract(row: DataRow, asset: str) -> str:
    for key in _CONTRACT_KEYS:
        if key in row.row_dict and row.row_dict[key].strip():
            return row.row_dict[key].strip()
    found = _CONTRACT_IN_NAME.search(asset)
    if found is None:
        return ""
    return found.group(1)


def _norm_contract(value: str) -> str:
    text = "".join(value.split()).casefold()
    if text.startswith("0x"):
        return text[2:]
    return text


def _blank_destination(row: DataRow) -> bool:
    """True when TxDest is blank. The wallet column is not a destination."""
    if row.tx_raw is not None:
        return not _at(row, row.tx_raw.tx_dest_pos).strip()
    if "TxDest" in row.row_dict:
        return not row.row_dict["TxDest"].strip()
    return not _dest_candidates(row)


def _lp_looking(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    return _lp_asset(_asset(out.row, "out"), registry) or _lp_asset(
        _asset(incoming.row, "in"), registry
    )


def _lp_asset(asset: str, registry: WalletRegistry) -> bool:
    symbol = asset.strip().upper()
    if not symbol:
        return False
    if symbol in _BUILTIN_LP_SYMBOLS or symbol in {item.upper() for item in registry.lp_symbols}:
        return True
    folded = symbol.casefold()
    patterns = _BUILTIN_LP_PATTERNS + tuple(registry.lp_patterns)
    return any(pattern and pattern in folded for pattern in patterns)


def _spam_asset(asset: str, registry: WalletRegistry) -> bool:
    symbol = asset.strip().upper()
    official = {item.upper() for item in registry.match.official_assets}
    if symbol in official:
        return False
    if symbol in {item.upper() for item in registry.spam_symbols}:
        return True
    folded = asset.casefold()
    return any(pattern and pattern in folded for pattern in registry.spam_patterns)


def _zero_lookalike(record: TransactionOutRecord, asset: str, registry: WalletRegistry) -> bool:
    if related_official(asset, registry.match.official_assets) is None:
        return False
    value = record.sell_value if record.sell_asset == asset else record.buy_value
    return value is None or value == 0


def _position_kind(category: str, direction: str) -> str:
    if category == "lp":
        return "lp-add" if direction == "out" else "lp-remove"
    if category == "stake":
        return "stake-lock" if direction == "out" else "stake-return"
    return "collateral"


def _treatment_for(category: str, registry: WalletRegistry) -> str:
    if category == "lp":
        return registry.tax_treatment.lp
    if category == "stake":
        return registry.tax_treatment.stake
    return registry.tax_treatment.collateral


def _as_transfer(record: TransactionOutRecord, direction: str) -> None:
    if direction == "out":
        record.t_type = TrType.WITHDRAWAL
        record.sell_value = None
    else:
        record.t_type = TrType.DEPOSIT
        record.buy_value = None


def _redact_fiat(row: DataRow) -> None:
    for key in ("TxDest", "TxSrc", "Description", "Sending Wallet", "Receiving Wallet", "Note"):
        if key in row.row_dict:
            row.row_dict[key] = redact_account_numbers(row.row_dict[key])
    record = _record(row)
    record.wallet = redact_account_numbers(record.wallet)
    record.note = redact_account_numbers(record.note)
    if row.tx_raw is None:
        return
    for pos in (row.tx_raw.tx_dest_pos, row.tx_raw.tx_src_pos):
        if pos is None or pos < 0 or pos >= len(row.row):
            continue
        row.row[pos] = redact_account_numbers(row.row[pos])


def _side(record: TransactionOutRecord) -> Optional[str]:
    sold = record.sell_quantity is not None and record.sell_quantity > 0
    bought = record.buy_quantity is not None and record.buy_quantity > 0
    if sold and not bought:
        return "out"
    if bought and not sold:
        return "in"
    return None


def _wallets_differ(out: TransferLeg, incoming: TransferLeg) -> bool:
    left = _record(out.row).wallet.strip().casefold()
    right = _record(incoming.row).wallet.strip().casefold()
    if not left or not right:
        return True
    return left != right


def _gap(out: TransferLeg, incoming: TransferLeg) -> timedelta:
    return abs(_stamp(out.row) - _stamp(incoming.row))


def _chains_differ(left: DataRow, right: DataRow) -> bool:
    first = _chain(left)
    second = _chain(right)
    return bool(first and second and first.casefold() != second.casefold())


def _log(
    result: ReconcileResult, action: str, row: DataRow, direction: Optional[str], detail: str
) -> None:
    captured = _capture(row, direction or "out")
    result.summary.append(
        SummaryRow(
            action=action,
            timestamp=captured[0],
            asset=captured[1],
            quantity=captured[2],
            wallet=captured[3],
            destination=captured[4],
            tx_hash=captured[5],
            detail=detail,
        )
    )


def _review(
    row: DataRow, direction: Optional[str], disposition: str, description: Optional[str] = None
) -> ReviewRow:
    captured = _capture(row, direction or "out")
    return ReviewRow(
        timestamp=captured[0],
        asset=captured[1],
        quantity=captured[2],
        wallet=captured[3],
        destination=captured[4],
        tx_hash=captured[5],
        description=_description(row) if description is None else description,
        disposition=disposition,
    )


def _capture(row: DataRow, direction: str) -> Tuple[datetime, str, Decimal, str, str, str]:
    record = _record(row)
    if direction == "in":
        quantity = record.buy_quantity or Decimal(0)
        asset = record.buy_asset
    else:
        quantity = record.sell_quantity or Decimal(0)
        asset = record.sell_asset
    return (_stamp(row), asset, quantity, record.wallet, _destination(row), _tx_hash(row))


def _remember_hash(record: TransactionOutRecord, row: DataRow) -> None:
    tx_hash = _tx_hash(row)
    if tx_hash and f"tx {tx_hash}" not in record.note:
        _note(record, f"tx {tx_hash}")


def _note(record: TransactionOutRecord, text: str) -> None:
    record.note = f"{record.note} | {text}" if record.note else text


def _record(row: DataRow) -> TransactionOutRecord:
    if row.t_record is None:
        raise RuntimeError("Missing t_record")
    return row.t_record


def _asset(row: DataRow, direction: str) -> str:
    record = _record(row)
    if direction == "out":
        return record.sell_asset
    return record.buy_asset


def _stamp(row: DataRow) -> datetime:
    timestamp = _record(row).timestamp
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=TZ_UTC)
    return timestamp.astimezone(TZ_UTC)


def _text(row: DataRow) -> str:
    parts = []
    for key in ("Description", "description", "Tag", "Label", "Note"):
        if key in row.row_dict and row.row_dict[key].strip():
            parts.append(row.row_dict[key])
    if row.t_record is not None and row.t_record.note:
        parts.append(row.t_record.note)
    return " ".join(parts).casefold()


def _tag(row: DataRow) -> str:
    for key in ("Tag", "Label"):
        if key in row.row_dict:
            return row.row_dict[key].strip()
    return ""


def _description(row: DataRow) -> str:
    for key in ("Description", "description", "Note"):
        if key in row.row_dict and row.row_dict[key].strip():
            return row.row_dict[key].strip()
    if row.t_record is not None and row.t_record.note:
        return row.t_record.note
    return ""


def _chain(row: DataRow) -> str:
    for key in _CHAIN_KEYS:
        if key in row.row_dict and row.row_dict[key].strip():
            return row.row_dict[key].strip()
    return ""


def _tx_hash(row: DataRow) -> str:
    if row.tx_raw is not None:
        raw = _at(row, row.tx_raw.tx_hash_pos)
        if raw:
            return raw
    if "TxHash" in row.row_dict:
        return row.row_dict["TxHash"].strip()
    return ""


def _dest_candidates(row: DataRow) -> Tuple[str, ...]:
    return _candidates(row, _DEST_KEYS, "dest")


def _src_candidates(row: DataRow) -> Tuple[str, ...]:
    return _candidates(row, _SRC_KEYS, "src")


def _candidates(row: DataRow, keys: Sequence[str], end: str) -> Tuple[str, ...]:
    values: List[str] = []
    if row.tx_raw is not None:
        pos = row.tx_raw.tx_dest_pos if end == "dest" else row.tx_raw.tx_src_pos
        _add(values, _at(row, pos))
    for key in keys:
        if key in row.row_dict:
            _add(values, row.row_dict[key])
    return tuple(values)


def _destination(row: DataRow) -> str:
    candidates = _dest_candidates(row)
    if candidates:
        return candidates[0]
    return ""


def _at(row: DataRow, pos: Optional[int]) -> str:
    if pos is None or pos < 0 or pos >= len(row.row):
        return ""
    return row.row[pos].strip()


def _add(values: List[str], text: str) -> None:
    cleaned = text.strip()
    if cleaned and cleaned not in values:
        values.append(cleaned)


def _norm(value: str) -> str:
    return "".join(value.split()).casefold()
